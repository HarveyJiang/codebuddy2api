"""TraeBackend — 字节 TRAE Work SOLO 免费通道（私有协议）→ OpenAI Chat SSE。

移植自 Go 项目 traework2api（internal/upstream/*）。上游是私有协议而非
OpenAI 形状，因此本模块把：
  1. OpenAI 请求体改写为 SOLO 形状（prepare_solo_body，对应 Go PrepareBody）；
  2. SOLO 自定义 SSE（metadata/timing_cost/output/extra_info/token_usage/done/error）
     归一化为标准 OpenAI Chat SSE chunk（normalize_solo_stream，对应 Go Stream）；
  3. 上游错误分类为 HTTP 语义码（classify_*）。

有意偏离 Go（记录在案）：
- chunk/completion 的 model 盖章为请求 config_name（Go 为 ""）；
- map_model 大小写不敏感兜底（修 Go normalizeModelName → DeepSeek-V4-Pro 不匹配的 bug）；
- 流内错误发 OpenAI 形状 error chunk（Go 发 `event: error` + 字符串），让
  PoolMiddleware 的 SSE 扫描命中 → 冷却账号；
- 不在请求内换号重试（Go MaxRotate=3）；
- 1005→402 走既有指数退避（Go 平铺 12h）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .base import BackendHTTPError, BackendSpec

# ---------------------------------------------------------------------------
# 常量（SPEC §1，来自实测，禁止随意改动）
# ---------------------------------------------------------------------------

AGENT_HOST = "https://trae-api-cn.mchost.guru"
UG_HOST = "https://api.trae.cn"
OAUTH_HOST = "https://api.trae.com.cn"
CONSOLE_HOST = "https://www.trae.cn"

CLIENT_ID = "en1oxy7wnw8j9n"  # SOLO stable
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.43"
IDE_VERSION_CODE = "20260716"
DEVICE_BRAND = "83DG"
OS_VERSION = "Windows 11 Pro"
FUNCTION = "solo_work_lite"
# SOLO chat 的 function 参数决定「哪些模型可用」，两套互补（实测同一模型只在其中一套可调）：
#   solo_work_lite  ：qwen3.8-max 可用，Doubao-Seed-Code 报 4001
#   solo_agent_remote：Doubao-Seed-Code 可用，qwen3.8-max 报 4001
# 因此请求时按模型自适应（先 work_lite，4001 再换 agent_remote，结果缓存）。
FUNCTION_FALLBACK = "solo_agent_remote"
CLIENT_UA = f"Trae/{IDE_VERSION}"

EP_CHAT = "/api/agent/v3/llm_utils_chat"
EP_MODELS = "/api/ide/v1/get_detail_param"
# 官方客户端模型菜单：带倍率（consumption_rate / 会员折扣）与上下文，比 get_detail_param 更权威
REMOTE_MODELS_HOST = "https://work.trae.cn"
EP_REMOTE_MODELS = "/api/remote/v1/models"
REMOTE_MODELS_FUNCTIONS = "solo_agent_remote,solo_work_remote,solo_design_remote"
# 官网界面展示的是 solo_agent_remote 组（craft 模式）——目录以此组为准。
# 同名模型在不同组里的上下文/倍率可能不同（实测 design=232768 vs work/agent=256000），
# 且上游分组顺序不固定，故按此优先级排序去重，保证结果确定。
REMOTE_MODELS_FUNCTION_ORDER = ("solo_agent_remote", "solo_work_remote", "solo_design_remote")
CATALOG_FUNCTION = "solo_agent_remote"
EP_EXCHANGE = "/cloudide/api/v3/trae/oauth/ExchangeToken"
EP_USERINFO = "/cloudide/api/v3/trae/GetUserInfo"
EP_CHECKIN_STATUS = "/trae/api/v2/ug/checkin_credits/status"
EP_CHECKIN_CLAIM = "/trae/api/v2/ug/checkin_credits/claim"
EP_ENT_USAGE = "/trae/api/v2/pay/ide_user_ent_usage"

DEFAULT_CONFIG_NAME = "glm-5.2"
REFRESH_SKEW_SECONDS = 24 * 3600  # token 预刷新窗口（Go RefreshSkew=24h）

# ---------------------------------------------------------------------------
# 静态模型表（SPEC P3：32 个 config_name，来自逆向报告；动态拉取失败时回退）
# ---------------------------------------------------------------------------

_OWNED_BY = "trae-solo"
_CREATED = 1753600000
_CTX = 131072

# 静态兜底表（冷启动 / 上游不可用时）：= 官方菜单 solo_agent_remote 组（官网界面所见 20 个）。
# 隐藏/别名的 config_name（custom_model_*、*subagent、summary、seed-code-pro-0430 等）不入表。
_TRAE_STATIC_SPECS = (
    # (config_name, 官方展示名, 上下文 dev, max 模式上限；0 表示无 max 模式)
    ("Doubao-Seed-Evolving", "Seed-Evolving", 256000, 1000000),
    ("Doubao-Seed-2.1-Pro", "Seed-2.1-Pro-0915", 256000, 1000000),
    ("Doubao-Seed-2.1-Turbo", "Seed-2.1-Turbo", 256000, 0),
    ("Doubao-Seed-Code", "Seed-Code", 256000, 0),
    ("mimo-v2.6-flash", "MiMo-V2.6-Flash", 200000, 981504),
    ("mimo-v2.6-pro", "MiMo-V2.6-Pro", 200000, 981504),
    ("step-5-preview", "Step-5-Preview", 200000, 1000000),
    ("glm-5.3-flashx", "GLM-5.3-FlashX", 200000, 1000000),
    ("glm-5.3-flash", "GLM-5.3-Flash", 200000, 1000000),
    ("glm-5.3", "GLM-5.3", 200000, 1000000),
    ("glm-5.2", "GLM-5.2", 200000, 1000000),
    ("deepseek-v4.1-flash", "DeepSeek-V4.1-Flash", 200000, 1000000),
    ("DeepSeek-V4-Flash-Official", "DeepSeek-V4-Flash", 200000, 1000000),
    ("DeepSeek-V4-Pro-Official", "DeepSeek-V4-Pro", 200000, 1000000),
    ("kimi-k3", "Kimi-K3", 200000, 1000000),
    ("kimi-k2.7-code", "Kimi-K2.7-Code", 200000, 0),
    ("kimi-k2.6", "Kimi-K2.6", 200000, 0),
    ("minimax-m3", "MiniMax-M3", 200000, 1000000),
    ("qwen3.8-max", "Qwen3.8-Max", 200000, 1000000),
    ("qwen-3.7-plus", "Qwen3.7-Plus", 200000, 1000000),
)

TRAE_STATIC_MODELS = [
    {"id": mid, "name": name, "object": "model", "created": _CREATED, "owned_by": _OWNED_BY,
     "context_length": ctx, "max_context_length": mx or None}
    for mid, name, ctx, mx in _TRAE_STATIC_SPECS
]

# ---------------------------------------------------------------------------
# 模型目录：动态表（get_detail_param）优先，静态兜底
#
# 目录状态属于 **TraeBackend 实例**（见该类的 catalog_entries / set_catalog_provider）：
# 实例级而非模块级，避免同一进程建两个 app 时互相串。下面这个模块级函数保留给
# 「拿不到实例」的调用点，副作用是它会构造/复用注册表里的那个单例。
# ---------------------------------------------------------------------------

def trae_model_catalog() -> list[dict]:
    """当前生效的 Trae 模型目录（动态优先，静态兜底）。

    实例存在就用实例的（admin 注入过提供者）；否则回落到静态表 —— 这样在
    没起 admin 的进程里（如纯协议单测）行为不变。
    """
    try:
        from .registry import _BACKENDS

        inst = _BACKENDS.get("trae")
        if inst is not None:
            return inst.catalog_entries()
    except Exception:
        pass
    return TRAE_STATIC_MODELS


def set_catalog_provider(provider):
    """兼容旧入口：把提供者注入注册表里的 Trae 单例。"""
    from .registry import trae_backend

    trae_backend().set_catalog_provider(provider)


class UnknownTraeModel(Exception):
    """客户端请求了静态模型表里没有的 Trae 模型。"""


class TraeAPIError(RuntimeError):
    """Trae ug/api 调用失败（模型表 / 签到 / 积分）。status 为 HTTP 状态码（可选）。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def normalize_model_name(s: str) -> str:
    """下划线内部名 → config_name 风格（每段首字母大写、横线连接）。

    deepseek_v4_pro → Deepseek-V4-Pro（与表内 DeepSeek-V4-Pro 大小写不同，
    故 map_model 还需大小写不敏感兜底）。
    """
    parts = []
    for p in s.split("_"):
        if not p:
            continue
        parts.append(p[:1].upper() + p[1:].lower())
    return "-".join(parts)


def map_model(model: str) -> str:
    """客户端 model → 上游 config_name。

    "glm-5.2"（config_name） → 直用
    "glm-5.2__dev"（内部名） → 去后缀
    "auto" / "" / "default"  → 默认模型 glm-5.2
    "deepseek_v4_pro"        → 大小写兜底命中 DeepSeek-V4-Pro
    其他                      → UnknownTraeModel
    """
    name = (model or "").strip()
    if name.startswith("trae/"):
        name = name[len("trae/"):].strip()
    if name in ("", "auto", "default"):
        return DEFAULT_CONFIG_NAME
    if "__" in name:
        name = name.split("__", 1)[0]
    catalog = trae_model_catalog()
    ids = {m["id"] for m in catalog}
    if name in ids:
        return name
    norm = normalize_model_name(name)
    if norm in ids:
        return norm
    # 大小写不敏感兜底（只影响原本会失败的请求）
    lower = {m["id"].lower(): m["id"] for m in catalog}
    return lower.get(name.lower()) or lower.get(norm.lower()) or _unknown(name, model)


def _unknown(name, original):
    raise UnknownTraeModel(original)


# ---------------------------------------------------------------------------
# OpenAI 请求体 → SOLO 形状（对应 Go PrepareBody）
# ---------------------------------------------------------------------------


def _normalize_tool_choice(obj: dict) -> None:
    def suppress():
        obj.pop("tools", None)
        obj.pop("functions", None)

    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            obj.pop("tool_choice", None)
            suppress()
        return
    if isinstance(tc, dict):
        typ = str(tc.get("type") or "").strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            suppress()
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = fn.get("name") or tc.get("name") or ""
            name = str(name).strip()
            obj["tool_choice"] = name if name else "auto"
        else:
            obj.pop("tool_choice", None)
        return
    obj.pop("tool_choice", None)


def _normalize_tools(obj: dict) -> None:
    raw = obj.get("tools")
    if not isinstance(raw, list) or not raw:
        return
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if isinstance(params, dict):
            # 上游 Go struct 里 parameters 是 string（OpenAI 标准是 object）
            fn["parameters"] = json.dumps(params, ensure_ascii=False)
        out.append(item)
    if not out:
        obj.pop("tools", None)
    else:
        obj["tools"] = out


def prepare_solo_body(body: dict, function: str | None = None) -> dict:
    """OpenAI → SOLO llm_utils_chat 请求体（单 pass 改写）。非 dict 原样返回。

    function 缺省用 FUNCTION；按模型自适应时由调用方传入。
    """
    if not isinstance(body, dict):
        return body
    obj = dict(body)
    obj["stream"] = True
    obj["function"] = function or FUNCTION

    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "assistant" and isinstance(m.get("tool_calls"), list):
                kept = []
                for tc in m["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    if isinstance(tc.get("function"), dict):
                        tc = dict(tc)
                        tc["function_call"] = tc.pop("function")
                    fc = tc.get("function_call")
                    if isinstance(fc, dict) and not str(fc.get("name") or "").strip():
                        continue  # 上游要求 FunctionCall.Name 必填，缺则剔除
                    kept.append(tc)
                if kept:
                    m["tool_calls"] = kept
                else:
                    m.pop("tool_calls", None)
            if "content" not in m or m.get("content") is None:
                continue
            content = m["content"]
            if isinstance(content, str):
                m["content"] = [{"type": "text", "text": content}]
            # 已是数组 → 透传（多模态，保守透传，未实测）
    model = str(obj.get("model") or "").strip() or DEFAULT_CONFIG_NAME
    obj["config_name"] = model
    obj["model"] = model

    _normalize_tool_choice(obj)
    _normalize_tools(obj)
    return obj


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


def classify_http_status(status: int, body_text: str = "") -> int:
    """HTTP 状态 + body → HTTP 语义码（SPEC §4.3）。

    1005 权益不足 → 402（额度耗尽）；401 会话失效 → 401；429 → 429；
    404 → 404；5xx → 500；其他 4xx → 400。
    """
    text = body_text or ""
    lower = text.lower()
    if '"code":1005' in text or ("1005" in text and "plan" in lower):
        return 402
    if status == 401:
        return 401
    if status == 429:
        return 429
    if status == 404:
        return 404
    if status >= 500:
        return 500
    if status >= 400:
        return 400
    return status


def classify_stream_code(code) -> int:
    """SOLO 流内 event:error 业务码 → HTTP 语义码。1005（额度耗尽）→ 402。"""
    if code == 1005:
        return 402
    if code == 401:
        return 401
    if code == 429:
        return 429
    if code == 404:
        return 404
    if isinstance(code, int) and 500 <= code < 600:
        return 500
    return 400


# ---------------------------------------------------------------------------
# HTTP 辅助（刷新 / 用户信息）
# ---------------------------------------------------------------------------


def normalize_expires_at(v) -> int:
    """ExchangeToken 的 TokenExpireAt 毫秒 → Unix 秒（1.7e12 vs 1.7e9，用 1e12 区分）。"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 0
    return v // 1000 if v > 1_000_000_000_000 else v


def _oauth_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CLIENT_UA,
    }


def exchange_token(client: httpx.Client, api_host: str, refresh_token: str) -> dict:
    """调 ExchangeToken（refreshToken 轮换）。返回 Result dict；失败抛 RuntimeError。"""
    host = api_host or OAUTH_HOST
    body = {"ClientID": CLIENT_ID, "RefreshToken": refresh_token, "ClientSecret": "-", "UserID": ""}
    r = client.post(host + EP_EXCHANGE, headers=_oauth_headers(), json=body)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"ExchangeToken 返回非 JSON（HTTP {r.status_code}）")
    result = data.get("Result") or {}
    if not result.get("Token"):
        raise RuntimeError(f"refresh_failed: no token in response（HTTP {r.status_code}）— 需重新登录")
    return result


def get_user_info(client: httpx.Client, api_host: str, token: str) -> tuple[str, str, str]:
    """调 GetUserInfo。返回 (uid, nickname, enterprise_id)。"""
    host = api_host or OAUTH_HOST
    headers = _oauth_headers()
    headers["X-Cloudide-Token"] = token
    r = client.post(
        host + EP_USERINFO,
        headers=headers,
        json={"ReqSource": "IDE", "IDEVersion": IDE_VERSION},
    )
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"GetUserInfo 返回非 JSON（HTTP {r.status_code}）")
    result = data.get("Result") or data
    return (
        str(result.get("UserID") or ""),
        str(result.get("ScreenName") or ""),
        str(result.get("EnterpriseID") or ""),
    )


# ---------------------------------------------------------------------------
# ug API：动态模型表 / 签到 / 积分（对应 Go FetchModels / Checkin* / UserEntUsage）
# 头集合对应 Go UgHeaders：Authorization + X-User-Region + X-Device-Id。
# ---------------------------------------------------------------------------

FETCH_MODELS_BODY = {
    "function": FUNCTION,
    "config_names": None,
    "need_prompt": False,
    "current_config_info": None,
    "poly_prompt": True,
    "mode_type": None,
    "agent_type": None,
}


def _pick(data, key):
    """ug/SOLO 响应取值：依次尝试顶层、Result、data 包装（实测形态不固定，全部兼容）。"""
    if not isinstance(data, dict):
        return None
    for scope in (data, data.get("Result"), data.get("data")):
        if isinstance(scope, dict) and key in scope:
            return scope[key]
    return None


def fetch_models(client: httpx.Client, headers: dict, agent_host: str | None = None) -> list[dict]:
    """拉 SOLO 动态模型表（get_detail_param）。返回 [{id,name,context_length}]；空表抛 TraeAPIError。"""
    host = agent_host or AGENT_HOST
    r = client.post(host + EP_MODELS, headers=headers, json=FETCH_MODELS_BODY)
    if r.status_code != 200:
        raise TraeAPIError(f"模型表接口返回 HTTP {r.status_code}", status=r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise TraeAPIError("模型表接口返回非 JSON")
    out = []
    for cfg in _pick(data, "config_info_list") or []:
        if not isinstance(cfg, dict):
            continue
        cid = str(cfg.get("config_name") or "").strip()
        if not cid:
            continue
        display = cfg.get("display_config") if isinstance(cfg.get("display_config"), dict) else {}
        out.append({"id": cid, "name": str(display.get("display_name") or cid), "context_length": _CTX})
    if not out:
        raise TraeAPIError("模型表接口返回空列表")
    return out


# 内部槽位（自定义模型占位 / 子代理 / 工具）不是可调用的对话模型，不入目录。
_INTERNAL_SLOT_MARKERS = ("custom_model", "subagent", "sub_agent")
_INTERNAL_SLOT_NAMES = {"summary", "file_search_agent"}


def _is_internal_slot(mid: str) -> bool:
    low = mid.lower()
    return low in _INTERNAL_SLOT_NAMES or any(m in low for m in _INTERNAL_SLOT_MARKERS)


def _feature_rate(features) -> float | None:
    """features(JSON 字符串) → 实际消耗倍率。

    会员/补贴折扣生效时取 `activity_discount.data.current.consumption_rate`（用户实付倍率），
    否则取基础 `consumption_rate.data.rate`。缺失/未启用返回 None（按标准计费展示）。
    """
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except ValueError:
            return None
    if not isinstance(features, dict):
        return None
    disc = features.get("activity_discount")
    if isinstance(disc, dict) and disc.get("enable"):
        current = (disc.get("data") or {}).get("current") or {}
        rate = current.get("consumption_rate")
        if isinstance(rate, (int, float)):
            return float(rate)
    base = features.get("consumption_rate")
    if isinstance(base, dict) and base.get("enable"):
        rate = (base.get("data") or {}).get("rate")
        if isinstance(rate, (int, float)):
            return float(rate)
    return None


def fetch_remote_models(client: httpx.Client, headers: dict,
                        host: str | None = None) -> list[dict]:
    """拉官方客户端模型菜单（/api/remote/v1/models）。

    返回 [{id,name,context_length,credits}]（credits＝倍率）；空表抛 TraeAPIError。
    多个 function 分组按 name 去重合并。
    """
    base = host or REMOTE_MODELS_HOST
    url = f"{base}{EP_REMOTE_MODELS}?functions={quote(REMOTE_MODELS_FUNCTIONS, safe='')}&show_custom_model=true"
    r = client.get(url, headers=headers)
    if r.status_code != 200:
        raise TraeAPIError(f"模型菜单接口返回 HTTP {r.status_code}", status=r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise TraeAPIError("模型菜单接口返回非 JSON")
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        raise TraeAPIError(f"模型菜单接口返回 code={data.get('code')}")
    out, seen = [], set()
    groups = [g for g in ((data or {}).get("data") or {}).get("list") or [] if isinstance(g, dict)]
    order = {fn: i for i, fn in enumerate(REMOTE_MODELS_FUNCTION_ORDER)}
    groups.sort(key=lambda g: order.get(str(g.get("function") or ""), len(order)))
    for group in groups:
        for item in group.get("models") or []:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("name") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            window = item.get("context_window_tokens") or {}
            dev, mx = window.get("dev"), window.get("max")
            out.append({"id": mid, "name": str(item.get("display_name") or mid),
                        "function": str(group.get("function") or ""),
                        "context_length": int(dev) if isinstance(dev, (int, float)) and dev else None,
                        # max 模式下的上下文上限（0/缺失表示没有 max 模式）
                        "max_context_length": int(mx) if isinstance(mx, (int, float)) and mx else None,
                        "credits": _feature_rate(item.get("features"))})
    if not out:
        raise TraeAPIError("模型菜单接口返回空列表")
    return out


# ---------------------------------------------------------------------------
# 请求侧 function 自适应：同一模型在不同 function 下可用性不同（4001 param invalid）
# ---------------------------------------------------------------------------

_FUNCTION_CACHE: dict[str, str] = {}
_FUNCTION_LOCK = threading.Lock()

#: 上游「参数校验失败」业务码：function 与模型不匹配（换 function 可解）
PARAM_INVALID_CODE = 4001
#: 传输链末位：会话式远程 agent（非 llm_utils_chat）
AGENT_STEP = "agent"
#: 宽松匹配 `"code": 4001`（上游换行/空格不定），用于字节级窥探
_CODE_RE = re.compile(rb'"code"\s*:\s*%d\b' % PARAM_INVALID_CODE)


async def _peek_first_event(chunks, limit: int = 8192):
    """读取首个完整 SSE 事件（或至 limit），返回 (bytes, 剩余迭代器)。

    用于在**向客户端发出任何内容之前**判定流内 4001（此时才允许换 function 重试）。
    """
    buf = b""
    iterator = chunks.__aiter__()
    async for chunk in iterator:
        buf += chunk
        if b"\n\n" in buf or len(buf) >= limit:
            break
    return buf, iterator


async def _replay(head: bytes, rest):
    if head:
        yield head
    async for chunk in rest:
        yield chunk


def function_chain(model: str) -> list[str]:
    """该模型要依次尝试的调用方式。

    `solo_work_lite` / `solo_agent_remote` 是 llm_utils_chat 的两套 function；
    都不可用时退到 `agent`：work.trae.cn 的会话式远程 agent（如 deepseek-v4.1-flash）。
    """
    with _FUNCTION_LOCK:
        first = _FUNCTION_CACHE.get(str(model or ""), FUNCTION)
    return [first] + [s for s in (FUNCTION, FUNCTION_FALLBACK, AGENT_STEP) if s != first]


def remember_function(model: str, fn: str) -> None:
    with _FUNCTION_LOCK:
        _FUNCTION_CACHE[str(model or "")] = fn


def _is_param_invalid(err: BackendHTTPError) -> bool:
    """上游 4001 ＝ function 与模型不匹配（param invalid），可换 function 重试。

    两种到达形态：HTTP 400 带 JSON body，或 HTTP 200 + 流内 event:error（body 只有文本，
    此时靠 upstream_code 判定）。
    """
    if getattr(err, "upstream_code", None) == PARAM_INVALID_CODE:
        return True
    try:
        return _CODE_RE.search(err.body or b"") is not None
    except Exception:
        return False


def _param_invalid_body(model: str) -> bytes:
    """两个 function 都试过仍 4001：该模型当前账号不可用（多为尚未开放/需更新客户端）。"""
    return json.dumps({
        "error": {
            "message": f"模型 {model} 当前不可用：上游拒绝了该模型的参数校验（4001）。"
                       f"可能该模型尚未对当前账号开放。",
            "type": "invalid_request_error",
            "code": 4001,
        }
    }, ensure_ascii=False).encode("utf-8")


def build_trae_catalog(menu: list[dict] | None) -> list[dict]:
    """构建 Trae 模型目录 = **官方菜单的 solo_agent_remote 组**（= 官网界面展示的模型集合）。
    该组即客户端 craft 模式的模型菜单，带倍率（`consumption_rate`，会员/补贴折扣生效取实付倍率）
    与上下文。同名模型若也出现在其它组，已按 `REMOTE_MODELS_FUNCTION_ORDER` 取该组的值。
    该组缺失时退回全部组；内部槽位（custom_model_*/subagent/工具）始终剔除。
    """
    groups = [g for g in menu or [] if isinstance(g, dict)]
    picked = [e for e in groups if str(e.get("function") or "") == CATALOG_FUNCTION] or groups
    out, seen = [], set()
    for entry in picked:
        mid = str(entry.get("id") or "").strip()
        if not mid or mid in seen or _is_internal_slot(mid):
            continue
        seen.add(mid)
        out.append({"id": mid, "name": entry.get("name") or mid,
                    "context_length": entry.get("context_length"),
                    "max_context_length": entry.get("max_context_length"),
                    "credits": entry.get("credits")})
    return out


def checkin_status(client: httpx.Client, headers: dict, ug_host: str | None = None) -> dict:
    """查询签到状态。返回 {checked, credits, enable}。"""
    host = ug_host or UG_HOST
    r = client.post(host + EP_CHECKIN_STATUS, headers=headers, json=CHECKIN_BODY)
    if r.status_code != 200:
        raise TraeAPIError(f"签到状态接口返回 HTTP {r.status_code}", status=r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise TraeAPIError("签到状态接口返回非 JSON")
    try:
        credits = int(_pick(data, "credits") or 0)
    except (TypeError, ValueError):
        credits = 0
    return {"checked": bool(_pick(data, "checked_in")), "credits": credits, "enable": bool(_pick(data, "enable"))}


def checkin_claim(client: httpx.Client, headers: dict, ug_host: str | None = None) -> dict:
    """执行签到，返回上游原始 JSON（含 code/message）。HTTP 非 200 或非 JSON 抛 TraeAPIError。

    注意上游对"失败"也返回 HTTP 200，真实结果看 body 的 code（见 interpret_checkin_claim）。
    """
    host = ug_host or UG_HOST
    r = client.post(host + EP_CHECKIN_CLAIM, headers=headers, json=CHECKIN_BODY)
    if r.status_code != 200:
        raise TraeAPIError(f"签到接口返回 HTTP {r.status_code}", status=r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise TraeAPIError("签到接口返回非 JSON")
    return data if isinstance(data, dict) else {}


# 签到接口请求体：req_source 1 = Trae CN IDE（SOLO 为 2）。
# 生产实测：body 缺 req_source 时上游按"订单参数不正确"拒绝（9004/9074）。
CHECKIN_REQ_SOURCE = 1
CHECKIN_BODY = {"req_source": CHECKIN_REQ_SOURCE}

# 签到 claim 可重试的业务码：上游拒绝（高峰限流或设备/请求形态不被识别），稍后重试即可。
CHECKIN_RETRYABLE_CODES = {9074, 9004}


def interpret_checkin_claim(data: dict) -> tuple[bool, str, bool]:
    """签到 claim 响应 → (成功?, 消息, 可重试?)。

    上游 HTTP 恒 200，用业务码判定：code 0/缺失＝成功；9074/9004 等＝上游暂时拒绝可重试；
    其余非 0＝明确失败，透出上游 message（避免把上游拒绝误报成"尚未确认"）。
    """
    code = data.get("code")
    message = str(data.get("message") or "").strip()
    if code in (0, None):
        return True, message, False
    retryable = code in CHECKIN_RETRYABLE_CODES or any(k in message for k in ("稍后", "太多", "重试", "频繁", "请稍"))
    return False, message or f"签到失败（code={code}）", retryable


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def user_ent_usage(client: httpx.Client, headers: dict, ug_host: str | None = None,
                   now: float | None = None) -> float:
    """聚合**剩余**积分。

    口径：每个权益包可用额度 = `quota.credits_limit − usage.credits_amount`。
    注意上游 `usage_summary.total_amount` 是 Σ`credits_limit`（**总额度**而非余额），
    直接对其求和会把已消耗部分算进去、导致余额虚高（实测 9100 vs 真实 5428.39）。
    逐包口径与其它字段（过期时间、包名）同源，便于跳过已过期包；
    仅当没有任何权益包时回落到 `usage_summary.total_amount − consumed_amount`。
    """
    host = ug_host or UG_HOST
    r = client.post(host + EP_ENT_USAGE, headers=headers, json={})
    if r.status_code != 200:
        raise TraeAPIError(f"积分接口返回 HTTP {r.status_code}", status=r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise TraeAPIError("积分接口返回非 JSON")
    if not isinstance(data, dict):
        raise TraeAPIError("积分接口返回结构异常")
    ts = time.time() if now is None else now
    remain = 0.0
    counted = False
    for pack in _pick(data, "user_entitlement_pack_list") or []:
        if not isinstance(pack, dict):
            continue
        base = pack.get("entitlement_base_info") if isinstance(pack.get("entitlement_base_info"), dict) else {}
        quota = base.get("quota") if isinstance(base.get("quota"), dict) else {}
        usage = pack.get("usage") if isinstance(pack.get("usage"), dict) else {}
        expire = _num(pack.get("expire_time") or base.get("end_time") or 0)
        if expire and expire < ts:  # 已过期额度不可用
            continue
        remain += max(_num(quota.get("credits_limit")) - _num(usage.get("credits_amount")), 0.0)
        counted = True
    if not counted:
        summary = data.get("usage_summary") if isinstance(data.get("usage_summary"), dict) else {}
        remain = max(_num(summary.get("total_amount")) - _num(summary.get("consumed_amount")), 0.0)
    return round(remain, 2)


# ---------------------------------------------------------------------------
# 凭据管理（对应 Go internal/auth.Auth + client.RefreshToken）
# ---------------------------------------------------------------------------


class TraeCredentialManager:
    """Trae auth 文件（嵌套形 {"auth":{...},"account":{...}}）读取 + token 自动刷新。

    并发模型与 CredentialManager 一致：单写者（服务进程），锁内整段刷新 + 原子写回。
    expiresAt 为 **Unix 秒**（Trae 惯例）；预刷新窗口 24h。
    """

    backend_name = "trae"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    # -- 读取 --

    def _read_raw(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 Trae auth 文件：{self.path}")
        return self._cached

    def _auth(self) -> dict:
        return self._session().get("auth") or {}

    def _account(self) -> dict:
        return self._session().get("account") or {}

    def access_token(self) -> str:
        return str(self._auth().get("accessToken") or "")

    def expires_at(self) -> int:
        try:
            return int(self._auth().get("expiresAt") or 0)
        except (TypeError, ValueError):
            return 0

    def _is_expired(self, within: int = REFRESH_SKEW_SECONDS) -> bool:
        exp = self.expires_at()
        if exp <= 0:
            return True
        return time.time() + within >= exp

    # -- 刷新 --

    def _refresh(self):
        auth = self._auth()
        refresh_token = str(auth.get("refreshToken") or "")
        if not refresh_token.strip():
            raise RuntimeError("Trae 凭据缺少 refreshToken，请重新登录")
        api_host = str(auth.get("apiHost") or OAUTH_HOST)
        with httpx.Client(timeout=20) as client:
            result = exchange_token(client, api_host, refresh_token)
        new_auth = dict(auth)
        new_auth["accessToken"] = result["Token"]
        if result.get("RefreshToken"):
            new_auth["refreshToken"] = result["RefreshToken"]
        exp = normalize_expires_at(result.get("TokenExpireAt") or 0)
        if exp <= 0 and result.get("TokenExpireDuration"):
            exp = int(time.time()) + int(result["TokenExpireDuration"])
        if exp > 0:
            new_auth["expiresAt"] = exp
        self._session()["auth"] = new_auth
        self._save_atomic()

    def _save_atomic(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cached, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # -- 对外 --

    def _solo_headers(self, stream: bool) -> dict:
        auth = self._auth()
        account = self._account()
        token = str(auth.get("accessToken") or "")
        h = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": CLIENT_UA,
            "Authorization": f"Cloud-IDE-JWT {token}",
            "X-Cloudide-Token": token,
            "X-Ide-Token": token,
            "X-App-Id": APP_ID,
            "X-App-Version": "default",
            "X-Ide-Version": IDE_VERSION,
            "X-Ide-Version-Code": IDE_VERSION_CODE,
            "X-App-Version-Code": IDE_VERSION_CODE,
            "X-Ide-Version-Type": "stable",
            "X-Device-Type": "windows",
            "X-OS-Version": OS_VERSION,
            "X-Device-Brand": DEVICE_BRAND,
            "Request-Traffic-Type": "prod",
        }
        uid = str(account.get("uid") or "")
        if uid:
            h["X-Uid"] = uid
        machine_id = str(auth.get("machineId") or "")
        if machine_id:
            h["X-Machine-Id"] = machine_id
        device_id = str(auth.get("deviceId") or "")
        if device_id:
            h["X-Device-Id"] = device_id
        return h

    def get_headers(self, stream: bool = True) -> dict:
        with self._lock:
            if self._is_expired():
                self._refresh()
            return self._solo_headers(stream=stream)

    # -- ug API（动态模型表 / 签到 / 积分） --

    def ug_headers(self) -> dict:
        """签到/积分（api.trae.cn）头：确保 token 新鲜后构造（对应 Go UgHeaders）。

        生产实测（对照官方客户端与开源签到实现）：ug 接口除 Cloud-IDE-JWT 外还校验
        设备三头（x-device-id/x-device-brand/x-device-type）与 x-app-version；
        缺 x-device-id 上游返回 9004（设备形态不被识别），签到必失败。
        """
        with self._lock:
            if self._is_expired():
                self._refresh()
            auth = self._auth()
            token = str(auth.get("accessToken") or "")
            h = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": CLIENT_UA,
                "Authorization": f"Cloud-IDE-JWT {token}",
                "X-User-Region": "CN",
                "x-app-version": "1.0.0",
            }
            device_id = str(auth.get("deviceId") or "")
            if device_id:
                h["X-Device-Id"] = device_id
                h["x-device-brand"] = "PC"
                h["x-device-type"] = "Linux"
            return h

    def fetch_models(self) -> list[dict]:
        """Trae 模型目录 = 官方菜单 solo_agent_remote 组（与官网界面一致，含倍率/上下文）。

        不再依赖 get_detail_param：它返回的是「本 function 可调用」超集，含官网不显示的
        隐藏/别名 config_name（custom_model_*、*subagent、summary、seed-code-pro-0430 等），
        而那部分有效性已由请求时的 function 自适应解决（见 function_chain）。
        菜单拉取失败即抛错，由调用方保留上次快照。
        """
        with httpx.Client(timeout=20) as client:
            menu = fetch_remote_models(client, self.ug_headers())
        return build_trae_catalog(menu)

    def checkin_status(self, client) -> dict:
        return checkin_status(client, self.ug_headers())

    def checkin_claim(self, client) -> dict:
        return checkin_claim(client, self.ug_headers())

    def user_ent_usage(self, client) -> float:
        return user_ent_usage(client, self.ug_headers())

    def summary(self) -> dict:
        acct = self._account()
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": self.expires_at(),
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# SOLO SSE → OpenAI SSE 归一化
# ---------------------------------------------------------------------------


class _SseState:
    __slots__ = ("event", "data")

    def __init__(self):
        self.event = ""
        self.data = []

    def reset(self):
        self.event = ""
        self.data = []


def _parse_solo_event(event: str, data: str) -> dict | None:
    """解析一条 SOLO 事件（event 行为事件名，data 行为 JSON）。"""
    ev = {"event": (event or "").strip()}
    if not data:
        return ev
    try:
        raw = json.loads(data)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    name = ev["event"]
    if name == "output":
        ev["response"] = raw.get("response") if isinstance(raw.get("response"), str) else ""
        ev["reasoning"] = raw.get("reasoning_content") if isinstance(raw.get("reasoning_content"), str) else ""
        ev["tool_calls"] = raw.get("tool_calls")
    elif name == "token_usage":
        ev["usage"] = raw
    elif name == "done":
        fr = raw.get("finish_reason")
        ev["finish_reason"] = fr if isinstance(fr, str) else ""
    elif name == "error":
        code = raw.get("code")
        ev["error_code"] = int(code) if isinstance(code, (int, float)) else 0
        msg = raw.get("message")
        ev["error_message"] = msg if isinstance(msg, str) else ""
    return ev


def _scan_line(state: _SseState, line: str) -> dict | None:
    """处理一行；事件边界（空行）时解析并返回事件。"""
    if line == "":
        if state.event == "":
            state.reset()
            return None
        ev = _parse_solo_event(state.event, "".join(state.data))
        state.reset()
        return ev
    if line.startswith("event:"):
        state.event = line[len("event:"):].strip()
    elif line.startswith("data:"):
        state.data.append(line[len("data:"):])
    # ":" 注释行忽略
    return None


def _clean_tool_calls(raw) -> list | None:
    """SOLO tool_calls（function_call 字段）→ OpenAI function 形状。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        arr = [raw]
    elif isinstance(raw, list):
        arr = raw
    else:
        return None
    out = []
    for call in arr:
        if not isinstance(call, dict):
            continue
        call = dict(call)
        fc = call.pop("function_call", None)
        if isinstance(fc, dict):
            call["function"] = {k: v for k, v in fc.items() if k not in ("namespace", "partial_arguments")}
        fn = call.get("function")
        if isinstance(fn, dict):
            fn.pop("namespace", None)
            fn.pop("partial_arguments", None)
        out.append(call)
    return out or None


def _sse_chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _error_chunk(code: int, message: str) -> bytes:
    return _sse_chunk({"error": {"message": (message or "")[:500], "type": "upstream_error", "code": code}})


async def normalize_solo_stream(chunks, *, model: str = "", raise_on_error: bool = False):
    """SOLO 自定义 SSE 字节流 → OpenAI Chat SSE 字节流。

    产出 chunk 的 delta 含 content / reasoning_content / tool_calls（标准 function 形状）。
    usage 挂到 done chunk 上（token_usage 事件先缓存，随下一个 chunk 发出）。
    上游 event:error：
      - raise_on_error=False（流式端点）：先 yield error chunk + [DONE]，再
        raise BackendHTTPError(emitted=True)；
      - raise_on_error=True（非流式 / count_tokens）：直接 raise（不 emit）。
    上游无 done 仍补 [DONE]（幂等）。
    """
    state = _SseState()
    pending_usage = None
    saw_done = False

    def _chunk(delta: dict, finish: str | None = None) -> bytes:
        nonlocal pending_usage
        choice = {"index": 0, "delta": delta}
        if finish:
            choice["finish_reason"] = finish
        payload = {
            "id": "chatcmpl-trae",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model or "",
            "choices": [choice],
        }
        if pending_usage is not None:
            payload["usage"] = pending_usage
            pending_usage = None
        return _sse_chunk(payload)

    async for line in _aiter_lines(chunks):
        ev = _scan_line(state, line)
        if ev is None:
            continue
        name = ev.get("event")
        if name == "output":
            delta: dict = {}
            if ev.get("response"):
                delta["content"] = ev["response"]
            if ev.get("reasoning"):
                delta["reasoning_content"] = ev["reasoning"]
            tcs = _clean_tool_calls(ev.get("tool_calls"))
            if tcs:
                delta["tool_calls"] = tcs
            if delta:
                yield _chunk(delta)
        elif name == "token_usage":
            pending_usage = ev.get("usage")
        elif name == "done":
            yield _chunk({}, ev.get("finish_reason") or "stop")
            yield b"data: [DONE]\n\n"
            saw_done = True
        elif name == "error":
            code = classify_stream_code(ev.get("error_code") or 0)
            message = ev.get("error_message") or ""
            if raise_on_error:
                raise BackendHTTPError(code, message, emitted=False, upstream_code=int(ev.get("error_code") or 0))
            yield _error_chunk(code, message)
            yield b"data: [DONE]\n\n"
            raise BackendHTTPError(code, message, emitted=True, upstream_code=int(ev.get("error_code") or 0))

    if not saw_done:
        # 幂等兜底：上游中断（无 done）仍补 [DONE]；若还有未挂载的 usage（上游只发了
        # token_usage 没有 done），先发一个携带 usage 的空 delta chunk，供 count_tokens 读取。
        if pending_usage is not None:
            yield _chunk({})
        yield b"data: [DONE]\n\n"


async def _aiter_lines(chunks):
    """字节流 → 行（按 \\n 切，去尾部 \\r）。"""
    buf = b""
    async for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", "replace").rstrip("\r")
    if buf:
        yield buf.decode("utf-8", "replace").rstrip("\r")


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------


class TraeBackend(BackendSpec):
    name = "trae"
    owned_by = "trae-solo"
    tencent_policies = False

    # 路由前缀：`trae/<名>` → 本后端，剥前缀后 map_model 归一为上游 config_name。
    model_prefix = "trae/"

    # 管理页元数据（需求 2/5/7）
    label = "Trae"
    short = "Trae"
    icon = "⚡"
    color = "#7c4dff"
    order = 1
    login_kind = "trae"

    # 凭据：Unix 秒级 expiresAt（归一为毫秒要 ×1000）。
    credential_backend = "trae"
    expires_at_to_ms = 1000

    # 积分/签到走 ug 接口，由 pool 分派到本后端的实现（_operate_nondefault）。
    account_ops_backend = "trae"

    # 404＝该 config_name 不存在/不可用：软冷却 10 分钟、不计连续失败
    # （账号本身通常是好的，不升级到 2h 硬冷却）。
    soft_cooldown = {404: 600}

    # 上游是私有协议：非流式聚合要保留 reasoning_content；count_tokens 走本地估算
    # （SOLO 上游大概率不遵守 max_tokens=1，走真实请求会白烧生成额度）。
    keep_reasoning_delta = True
    count_tokens_upstream = False

    # 模型目录：动态表（get_detail_param）优先，静态兜底；拉动态表必须用
    # **Trae 自己的**账号（拿默认后端账号的 headers 打过去必然 4xx）。
    refreshable_catalog = True
    needs_own_credential_for_catalog = True
    catalog_method = "fetch_models"
    static_catalog = TRAE_STATIC_MODELS

    def __init__(self):
        # 动态目录提供者由 admin 在启动时注入（它知道动态表存在哪、
        # 什么时候刷过）。实例级而非模块级：模块级全局会在「同一进程里建了
        # 两个 app」时互相串（测试就是这么用的）。
        self._catalog_provider = None

    def catalog_entries(self):
        """当前生效的目录：动态表优先，静态兜底（见 set_catalog_provider）。"""
        if self._catalog_provider is not None:
            try:
                dynamic = self._catalog_provider()
            except Exception:
                dynamic = None
            if dynamic:
                return list(dynamic)
        return [{"id": m["id"], "name": m.get("name") or m["id"],
                 "context_length": m.get("context_length")} for m in TRAE_STATIC_MODELS]

    def set_catalog_provider(self, provider):
        self._catalog_provider = provider

    def model_catalog(self) -> list[dict]:
        """本实例当前生效的模型目录（兼容 trae_model_catalog 的模块级调用）。"""
        return self.catalog_entries()

    def chat_url(self) -> str:
        return AGENT_HOST + EP_CHAT

    def make_credential(self, path):
        return TraeCredentialManager(path)

    def function_for(self, model: str) -> str:
        """该模型首选 function（自适应结果缓存优先，默认 FUNCTION）。"""
        with _FUNCTION_LOCK:
            return _FUNCTION_CACHE.get(str(model or ""), FUNCTION)

    async def astream(self, headers, body, *, timeout=None, raise_on_error=False):
        model = str((body or {}).get("model") or "")
        chain = function_chain(model)
        for idx, step in enumerate(chain):
            emitted = False
            try:
                if step == AGENT_STEP:
                    stream = self._stream_agent(body, headers, timeout=timeout, raise_on_error=raise_on_error)
                else:
                    stream = self._stream(prepare_solo_body(body, function=step), headers,
                                          timeout=timeout, raise_on_error=raise_on_error)
                async for chunk in stream:
                    emitted = True
                    yield chunk
                return
            except BackendHTTPError as e:
                # 4001 ＝ 该 function 与模型不匹配（param invalid）→ 换下一种调用方式重试，
                # 并把可用方式记住。仅在「尚未向客户端吐出 chunk」时重试，
                # 避免流中途切换导致内容重复。
                if (idx + 1 < len(chain) and not e.emitted and not emitted and _is_param_invalid(e)):
                    with _FUNCTION_LOCK:
                        _FUNCTION_CACHE[model] = chain[idx + 1]
                    continue
                if _is_param_invalid(e) and not e.emitted and not emitted:
                    # 所有方式都试过仍失败 → 给出可读原因（避免直接抛上游原文）
                    raise BackendHTTPError(400, _param_invalid_body(model)) from e
                raise

    async def _stream_agent(self, body, headers, *, timeout=None, raise_on_error=False):
        """会话式远程 agent：建会话 → 投递消息 → 读事件流 → 收尾删会话。"""
        import asyncio

        from . import trae_agent as agent

        model = str((body or {}).get("model") or "")
        query = agent.build_agent_query((body or {}).get("messages"))
        if not query:
            raise BackendHTTPError(400, json.dumps(
                {"error": {"message": "agent 请求缺少消息内容", "type": "invalid_request_error"}},
                ensure_ascii=False).encode("utf-8"))
        uid = str(headers.get("X-Uid") or "")
        display = next((m.get("name") for m in trae_model_catalog() if m.get("id") == model), None)

        def _prepare():
            with httpx.Client(timeout=60) as c:
                sid = agent.create_session(c, headers)
                mid = agent.send_message(c, headers, sid, model, query, uid, display)
            return sid, mid

        def _cleanup(sid):
            try:
                with httpx.Client(timeout=30) as c:
                    c.delete(f"{agent.AGENT_HOST}{agent.EP_CHAT_SESSIONS}/{sid}", headers=headers)
            except Exception:
                pass

        session_id, _message_id = await asyncio.to_thread(_prepare)
        try:
            async with httpx.AsyncClient(timeout=timeout or 900) as c:
                url = f"{agent.AGENT_HOST}{agent.EP_CHAT_SESSIONS}/{session_id}/events"
                async with c.stream("GET", url, headers={**headers, "Accept": "text/event-stream"}) as r:
                    if r.status_code != 200:
                        raw = await r.aread()
                        raise BackendHTTPError(classify_http_status(r.status_code, raw.decode("utf-8", "replace")), raw)
                    async for chunk in agent.normalize_agent_stream(r.aiter_lines(), model=model):
                        yield chunk
        finally:
            await asyncio.to_thread(_cleanup, session_id)

    async def _stream(self, prepared, headers, *, timeout=None, raise_on_error=False):
        url = self.chat_url()
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("POST", url, headers=headers, json=prepared) as r:
                    if r.status_code != 200:
                        raw = await r.aread()
                        status = classify_http_status(r.status_code, raw.decode("utf-8", "replace"))
                        raise BackendHTTPError(status, raw,
                                               upstream_code=PARAM_INVALID_CODE if _CODE_RE.search(raw) else None)
                    # 先窥探首个事件：若上游以「HTTP 200 + 流内 4001」拒绝该 function，
                    # 此刻尚未向客户端输出，抛出（未 emitted）让上层换 function 重试。
                    head, rest = await _peek_first_event(r.aiter_bytes())
                    if _CODE_RE.search(head or b""):
                        raise BackendHTTPError(400, head, upstream_code=PARAM_INVALID_CODE)
                    async for chunk in normalize_solo_stream(
                        _replay(head, rest),
                        model=str(prepared.get("model") or ""),
                        raise_on_error=raise_on_error,
                    ):
                        yield chunk
        except httpx.HTTPError as e:
            raise BackendHTTPError(502, str(e), transport=True) from e

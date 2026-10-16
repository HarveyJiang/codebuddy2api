#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import uvicorn

from . import runtime_config as config, runtime_logging as logging_setup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from .desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏

    def desensitize_body(
        body,
        roles=("system",),
        desensitize_harness_user=False,
        desensitize_tools=False,
        compact_harness=False,
        strip_tool_metadata=False,
    ):
        return body


from .anthropic_adapter import (
    AnthropicStreamConverter,
    anthropic_request_to_chat,
)
from .backends.base import BackendHTTPError  # noqa: F401（re-export 供测试/中间件/适配器使用）
from .backends.registry import (  # noqa: F401（re-export 便利）
    backend_name_for,
    make_credential,
    pinned_model_owner,
    resolve_backend,
    spec_for,
)
from .workbuddy_atrest_crypto import (  # issue #23：WorkBuddy 5.6.0 $wbEncrypted 信封
    decrypt_auth_field,
    encrypt_auth_field,
    is_encrypted_field,
)
from .responses_adapter import (
    ResponsesStreamConverter,
    responses_request_to_chat,
)
from .responses_projection import project_responses_chat_body
from .replay_policy import explain as explain_transport
from .system_identity import filter_system_identity

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
# 与官方 CLI 一致（admin/models.py 同值）：上游按 UA 识别客户端，
# 自定 UA 会导致用量接口 client 为空。
# ── 调用方身份（决定上游用量接口的 client 字段）────────────────────────────
# 上游用量接口的 client 字段直接取自请求头 X-IDE-Type / X-IDE-Name 的值：
#   值写 workbuddy → 上游记录 client="workbuddy"
#   值写 WorkBuddy → 上游记录 client="WorkBuddy"（目标）
# 其余头保持「已验证能出值」的组合（见 git 历史 FULL 实验）。
# 桌面端真实 UA 形如 WorkBuddy/5.8.2 WorkBuddy/5.8.2 CLI/2.161.0。
# 本网关支持 WORKBUDDY_PRODUCT_* 环境变量覆盖（旧名 CLIENT_INFO_* 仍兼容）。
_PRODUCT_NAME = config.raw("WORKBUDDY_PRODUCT_NAME", "WorkBuddy")
_PRODUCT_VERSION = config.raw("WORKBUDDY_PRODUCT_VERSION", "5.8.2")
_UA_EXTENSION = config.raw("WORKBUDDY_UA_EXTENSION", "CLI/2.161.0")
# 这两个开关是模块级常量（导入时求值），与其它配置一致的语义：改配置即重启。
# 必须走 config.get() 的布尔解析（认 1/true/yes/on），不要自己写 `== "1"`——
# 两套解析器会让 WORKBUDDY_DESENSITIZE=true 只影响 chat 路径、Responses 路径静默失效。
_RESPONSES_DESENSITIZE = config.get().desensitize
_LOSSY_PROJECTION = config.get().lossy_projection
# client 字段的取值来源（大写 WorkBuddy 为目标值）。
IDE_TYPE = _PRODUCT_NAME
USER_AGENT = f"{_PRODUCT_NAME}/{_PRODUCT_VERSION} {_UA_EXTENSION}"


def _uuid7_fallback_hex() -> str:
    """UUIDv7 的 32 位 hex 形态（无连字符）——**手工实现，仅供 < 3.14 兜底**。

    布局（RFC 9562 §5.7，高位 → 低位）：

        unix_ts_ms 48 位 │ ver=7 4 位 │ rand_a 12 位 │ var=0b10 2 位 │ rand_b 62 位

    只取时间位、不做同毫秒内的单调递增：上游要的是时刻而不是严格全序，随机位足够。
    生产镜像已是 3.14，走 :func:`_uuid7_hex` 的内置分支；这条仅为让本地
    低版本解释器（以及某些 CI 镜像）仍能跑得起来。
    """
    ms = time.time_ns() // 1_000_000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return f"{value:032x}"


#: 生产跑 3.14 → 直接用标准库。本地/CI 若低于 3.14 走手工实现兜底。
_builtin_uuid7 = getattr(uuid, "uuid7", None)


def _uuid7_hex() -> str:
    """生成 X-Request-ID 用的 UUIDv7（32 位 hex，无连字符）。

    ## 为什么是 v7 而不是 v4

    上游按 **UUIDv7 的时间位**还原请求时刻。v4 的那 48 位是随机值、解析不出任何
    有意义的毫秒数，于是上游要么渲染出错乱的时刻，要么判为不可解析、改用自己
    那套 ``crb-`` 前缀 ID。v7 把 unix 毫秒时间戳放在最高 48 位，ID 自带时间序，
    上游不需要额外的时间头就能还原时刻。
    """
    if _builtin_uuid7 is not None:
        return _builtin_uuid7().hex
    return _uuid7_fallback_hex()

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------


def auth_dirs() -> list[Path]:
    env_dir = config.raw("WORKBUDDY_AUTH_DIR")
    if env_dir:
        return [Path(env_dir)]
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [
            home
            / "Library"
            / "Application Support"
            / "CodeBuddyExtension"
            / "Data"
            / "Public"
            / "auth"
        ]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_file() -> Path | None:
    for d in auth_dirs():
        if d.is_dir():
            files = list(d.glob("*.info"))
            if not files:
                continue
            # 优先用无时间戳后缀的 "当前" 文件；否则按 mtime 取最新
            current = d / "workbuddy-desktop.info"
            if current.is_file():
                return current
            return max(files, key=lambda p: p.stat().st_mtime)
    return None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------


class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    #: 所属后端（账号 doc 无 backend 字段时的默认归属）；池选号与端点闸门据此分派
    backend_name = "workbuddy"

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
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
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = decrypt_auth_field(auth.get("refreshToken", ""))
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        if not isinstance(new_auth, dict) or not new_auth.get("accessToken"):
            raise RuntimeError("刷新响应缺少访问令牌")
        # 继承部分字段（refreshToken 兜底取解密后的旧值，避免加密信封被二次加密）
        new_auth["refreshToken"] = new_auth.get("refreshToken") or decrypt_auth_field(
            auth.get("refreshToken", "")
        )
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = (
                int(time.time() * 1000) + new_auth["expiresIn"] * 1000
            )
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = (
                int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
            )
        s["auth"] = new_auth
        # issue #23：若原文件为 $wbEncrypted 加密格式，回写前必须重新加密，
        # 否则 WorkBuddy 客户端读到明文会报 integrity 错误、可能重置登录态。
        if is_encrypted_field(auth.get("accessToken")):
            new_auth["accessToken"] = encrypt_auth_field(decrypt_auth_field(new_auth["accessToken"]))
            new_auth["refreshToken"] = encrypt_auth_field(decrypt_auth_field(new_auth["refreshToken"]))
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {decrypt_auth_field(auth.get('accessToken', ''))}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "X-IDE-Type": IDE_TYPE,
            "X-IDE-Name": IDE_TYPE,
            "X-IDE-Version": _PRODUCT_VERSION,
            "X-Product": "SaaS",
            "X-Product-Version": _PRODUCT_VERSION,
            # UUIDv7：上游从这里还原请求时刻（见 _uuid7_hex 的说明）。
            # 只作用于 WorkBuddy —— Trae 有自己的 _solo_headers，不走这条路径。
            "X-Request-ID": _uuid7_hex(),
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "hy3",
    "hy4-preview",
    "kimi-k3",
    "kimi-k2.8-preview",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "kimi-k2.7",
    "kimi-k2.6",
    "kimi-k2.5",
    "deepseek-v4-pro",
    "deepseek-v4.1-flash",
    "deepseek-v4-flash",
    "minimax-m3-pay",
    "hy3-preview-agent",
    "auto",
]

# 标识非聊天模型的 tag（需要过滤掉）
NON_CHAT_MODEL_TAGS = {
    "text-to-image",
    "image-to-image",
    "text-to-video",
}


def _find_workbuddy_product_json() -> Path | None:
    """
    查找本机 WorkBuddy 应用的 product.json 配置文件。

    WorkBuddy 在安装时会自动解压 asar 到 app.asar.unpacked 目录，
    因此无需用户手动提取。

    macOS: /Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/product.json
    Windows: %LOCALAPPDATA%\\Programs\\WorkBuddy\\resources\\app.asar.unpacked\\cli\\product.json
    Linux: /opt/WorkBuddy/resources/app.asar.unpacked/cli/product.json

    Returns:
        Path 对象如果找到配置文件，否则 None
    """
    possible_paths = []

    if sys.platform == "darwin":  # macOS
        possible_paths.extend(
            [
                # 标准安装路径（WorkBuddy 自动解压）
                Path(
                    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/product.json"
                ),
                # 开发/调试：本地提取的目录
                Path.home()
                / "Desktop/workspace/opensource/codebuddy2api/workbuddy_extracted/cli/product.json",
            ]
        )
    elif sys.platform == "win32":  # Windows
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        possible_paths.extend(
            [
                local_app_data
                / "Programs/WorkBuddy/resources/app.asar.unpacked/cli/product.json",
                Path(
                    "C:/Program Files/WorkBuddy/resources/app.asar.unpacked/cli/product.json"
                ),
            ]
        )
    else:  # Linux
        possible_paths.extend(
            [
                Path("/opt/WorkBuddy/resources/app.asar.unpacked/cli/product.json"),
                Path.home() / ".local/share/WorkBuddy/cli/product.json",
            ]
        )

    for path in possible_paths:
        if path.exists() and path.is_file():
            return path

    return None


def _load_models_from_workbuddy() -> list[str]:  # arch-guard: ok —— 读的是 WorkBuddy 自己的 product.json（该后端独有），不是分派入口
    """
    从本机 WorkBuddy product.json 读取模型列表。

    名字里带后端名是**准确**的、也是必要的：它读的就是 WorkBuddy 桌面客户端
    自己落盘的那份 product.json（路径与格式都是该后端独有的），不是「按后端名
    分派」的入口。另一个后端不会、也不该用这个文件。加后端**不需要**改这里。

    过滤规则：
    1. 只保留聊天模型（排除 text-to-image, text-to-video 等）
    2. 排除 vendor 为 "tencent" 的内部模型（通常是补全/内部专用）
    3. 返回模型 ID 列表

    Returns:
        模型 ID 列表，如果加载失败返回空列表
    """
    product_json_path = _find_workbuddy_product_json()

    if product_json_path is None:
        return []

    try:
        with open(product_json_path, encoding="utf-8") as f:
            data = json.load(f)

        models = data.get("models", [])
        chat_models = []

        for model in models:
            model_id = model.get("id")
            if not model_id:
                continue

            # 过滤掉非聊天模型
            tags = model.get("tags", [])
            if any(tag in NON_CHAT_MODEL_TAGS for tag in tags):
                continue

            # 过滤掉内部模型（vendor 为 tencent 的通常是补全/跳转等内部功能）
            vendor = model.get("vendor", "")
            if vendor == "tencent":
                continue

            # 过滤掉名称中明显是补全/内部功能的模型
            name_lower = model_id.lower()
            if any(
                keyword in name_lower
                for keyword in ["completion", "rewrite", "jump", "codewise"]
            ):
                continue

            chat_models.append(model_id)

        return chat_models

    except Exception as e:
        # 解析失败时静默降级，不影响服务启动
        print(
            f"Warning: Failed to load models from WorkBuddy product.json: {e}",
            file=sys.stderr,
        )
        return []


def get_available_models() -> list[str]:
    """
    获取可用的模型列表。

    优先从 WorkBuddy product.json 读取，如果失败则使用 DEFAULT_MODELS。

    Returns:
        模型 ID 列表
    """
    workbuddy_models = _load_models_from_workbuddy()  # arch-guard: ok —— 见该函数定义处的说明

    if workbuddy_models:
        # 成功从 WorkBuddy 加载，使用动态列表
        return workbuddy_models
    else:
        # 降级到硬编码列表
        return DEFAULT_MODELS


# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "max_tokens",
    "max_completion_tokens",
    "top_p",
    "stream",
    "stream_options",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "n",
    "response_format",
    "seed",
    "user",
    "reasoning_effort",
    "verbosity",
    "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")
CONFIG: dict = {
    "api_key": "",
    "cred": None,
    "log_path": None,
    "desensitize": False,
    "no_compact": False,
}  # cred: CredentialManager | None


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

_logger = logging.getLogger("core.converter")


def _log(msg: str):
    """记一条运行日志。

    以前这里只在 ``CONFIG['log_path']`` 设了才写文件、否则整个丢弃——容器里不传
    ``--log`` 就等于没有任何输出，排障时 ``docker logs`` 是空的。现在统一走 logging，
    恒有 stderr 输出（由容器收集），``log_path`` 只决定是否**额外**落文件。

    带完整请求/响应报文的调用用 ``_log_debug``，避免默认级别下把敏感内容刷到标准流。
    """
    _logger.info("%s", msg)


def _log_debug(msg: str):
    """DEBUG 级日志：完整 prompt/响应/上游原始 SSE。

    默认级别（INFO）下不输出，需要时用 ``WORKBUDDY_LOG_LEVEL=DEBUG`` 打开。
    """
    _logger.debug("%s", msg)


def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: str | None, x_api_key: str | None):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "invalid api key", "type": "auth_error"}},
        )


def _cred() -> CredentialManager:
    # The management deployment binds one credential to each ASGI request.
    # Standalone converter usage keeps the original single-account behavior.
    try:
        from admin.pool import REQUEST_CREDENTIAL
        selected = REQUEST_CREDENTIAL.get()
        if selected is not None:
            return selected
    except ImportError:
        pass
    if CONFIG["cred"] is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy",
                    "type": "auth_error",
                }
            },
        )
    return CONFIG["cred"]


@app.get("/health")
def health():
    cred = CONFIG["cred"]
    info: dict = {
        "status": "ok",
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "auth_file": str(find_auth_file() or "(未找到)"),
        "mode": "direct-proxy (native function calling)",
    }
    if cred is not None:
        try:
            info["credential"] = cred.summary()
        except Exception as e:
            info["credential_error"] = str(e)
    return info


@app.get("/v1/models")
def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    _check_auth(authorization, x_api_key)
    # 每行 = (id, 归属后端)。去前缀后同名模型在两个后端各有一行，`owned_by`
    # 按**这一行自己的后端**报（用户 2026-10-16 拍板）——`glm-5.3` 因此列出两条，
    # 一条 `codebuddy`、一条 `trae-solo`。查不到行清单（未注入/独立跑 converter）
    # 时回落到扁平名字列表，归属仍按 §9.6 的策略判定。
    #
    # 用 `is None` 判「查不到」而不是 `or`：空目录（[]）是**合法结果**
    # （上游还没拉过、全被禁用），不该被当成「没注入」而回落 —— 那会让两条
    # 不同来源的语义混在一起，且回落出来的名字列表本就是空的，行为看起来一样，
    # 等到有数据时才暴露分叉。
    rows = _listed_rows()
    if rows is None:
        rows = [(m, None) for m in get_available_models()]
    # 每行的元数据（上下文长度/倍率/描述）：来自注册表目录（admin 注入）。
    # OpenAI 协议允许 model 对象带额外字段，客户端忽略不认识的不受影响；
    # 而上下文长度正是「外部工具添加模型时必须手动填」的那一项 —— 直接给出
    # 就不用人工查（用户 2026-10-16）。查不到行元数据时字段省略，不报错。
    meta = _listed_meta()
    data = [
        {
            "id": mid,
            "object": "model",
            "created": 1700000000,
            # owned_by 取自**后端自己的声明**（BackendSpec.owned_by），不用
            # `m.startswith("trae/")` 猜——那是把「谁是我」的判断硬编码在调用方。
            # 行自带 backend 时直接用它（每行一个后端）；否则按 §9.6 判策略第一名
            # （客户端拿这个判断「这个 id 实际会路由到谁」）。
            "owned_by": spec_for(backend or _owner_backend_for(mid)).owned_by,
            **({k: meta[mid] for k in ("context_length", "credits", "description")
                if meta.get(mid, {}).get(k) is not None} if meta else {}),
        }
        for mid, backend in rows
    ]
    return {"object": "list", "data": data}


def _listed_rows():
    """`/v1/models` 的行清单 (id, backend)；admin 未加载时返回 None。"""
    try:
        from admin.bare_routing_provider import listed_rows_for
        return listed_rows_for()
    except Exception:
        return None


def _listed_meta():
    """`/v1/models` 每行的元数据（裸名 -> 上下文/倍率/描述）；不可用返回 None。"""
    try:
        from admin.bare_routing_provider import listed_meta_for
        return listed_meta_for()
    except Exception:
        return None


def _owner_backend_for(model) -> str:
    """`/v1/models` 里某个 id 的归属后端（**回落路径**）。

    **主路径**是 `ModelRegistry.listed_models()`：它逐行给出 `(id, backend)`，
    于是同名模型在两个后端各一行、各自报自己的 `owned_by`（用户 2026-10-16 拍板）。
    本函数只在**拿不到行清单**时兜底（admin 未加载 / 独立跑 converter / 单测）。

    兜底规则：
    - `pinned_models` → 声明的归属（三档智能路由永远只可能归它）；
    - 其余裸名 → **当前策略第一名**（与请求实际落点一致；客户端据此显示「这条会走谁」）。

    策略不可用时**回落** `backend_name_for`（默认后端），不让展示路径因
    「没有可用候选」而报错 —— `/v1/models` 是**只读展示**，它列的模型可能此刻
    确实没有可用账号（全都禁用/冷却），那不是 500 的理由。
    """
    if pinned_model_owner(model) is not None:
        return backend_name_for(model)
    try:
        from admin.bare_routing_provider import strategy_winner_for
        return strategy_winner_for(model) or backend_name_for(model)
    except Exception:
        # 展示路径永不因路由失败而中断（含 admin 未加载、registry 缺失等）。
        return backend_name_for(model)


def _routed_backend(request) -> str | None:
    """本请求由池中间件选定的后端（§5 裸名路由）；没有中间件时为 None。

    scope 是 Starlette 请求对象上挂着的那份（与中间件共享同一个 dict）。
    局部 import 防循环：`admin.pool` import core.converter。
    """
    try:
        from admin.pool import ROUTED_BACKEND
    except ImportError:
        return None
    scope = getattr(request, "scope", None) or {}
    return scope.get(ROUTED_BACKEND)


def _resolve_backend_or_http(model_raw, routed=None) -> tuple:
    """四端点共用前置：模型名 → (backend, upstream_model, cred)。

    trae/<名> → TraeBackend + map_model 归一（未知模型 → 400）；
    bare 名 + `routed`（中间件已选定）→ 按选定后端解析；
    其余 → WorkBuddyBackend + 原模型名。
    凭据-后端一致性闸门：池选到的账号与请求后端不匹配 → 503
    （防独立部署模式下 Trae 账号收到 WorkBuddy 请求或反之）。
    """
    backend, upstream_model = resolve_backend(model_raw, routed=routed)
    cred = _cred()
    # 凭据-后端一致性闸门：不比后端**名字**，比双方声明的凭据归属
    # （见 BackendSpec.credential_name / CredentialManager.backend_name）。
    cred_backend = getattr(cred, "backend_name", None)
    if cred_backend is not None and cred_backend != backend.credential_name():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": f"当前账号不适用于 {backend.name} 后端，请检查账号类型或启用对应账号",
                    "type": "auth_error",
                }
            },
        )
    return backend, upstream_model, cred


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "messages is required",
                    "type": "invalid_request_error",
                }
            },
        )

    # 路由：模型名 → 后端（trae/ 前缀进 Trae，其余走 WorkBuddy）；并做凭据一致性闸门
    backend, upstream_model, cred = _resolve_backend_or_http(payload.get("model") or "auto", routed=_routed_backend(request))

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    # 剥掉 trae/ 前缀、归一化后的上游模型名（空则保留原值，行为不变）
    body["model"] = upstream_model or body.get("model") or "auto"
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if backend.tencent_policies and "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 腾讯后端不吃 developer role（OpenAI 新规范里 system 的替身），统一映射为 system。
    #
    # 注意：**这不是 11128 的原因**。11128（"Illegal API invocation from an
    # unapproved channel"）是上游 WAF 对请求内容的指纹识别，换账号无效——线上实测
    # 连续换遍全池 8 个账号全部 11128，能救的只有改请求内容（见 system_identity /
    # desensitize）。developer role 只是被上游当未知 role 拒掉，属于另一回事。
    if "messages" in body and isinstance(body["messages"], list):
        body["messages"] = [
            dict(m, role="system")
            if isinstance(m, dict) and m.get("role") == "developer"
            else m
            for m in body["messages"]
        ]

    if backend.tencent_policies:
        body = filter_system_identity(body)

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if backend.tencent_policies and CONFIG.get("desensitize"):
        body = desensitize_body(
            body,
            roles=("system", "developer"),
            desensitize_harness_user=False,
            desensitize_tools=True,
            compact_harness=not CONFIG.get("no_compact"),
            strip_tool_metadata=True,
        )

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [
        t.get("function", {}).get("name")
        for t in (payload.get("tools") or [])
        if isinstance(t, dict)
    ]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(
        f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
        + (f" | tools={tool_names}" if tool_names else "")
        + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else "")
    )
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log_debug(
        f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}"
    )

    headers = cred.get_headers()
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(backend, headers, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        _, raw = await _post_backend_once(backend, headers, body, timeout=300)
        collected = await _collect_stream(
            _aiter_str_lines(raw), keep_reasoning=backend.keep_reasoning_delta
        )
    except BackendHTTPError as e:
        _log(
            f"[{rid}] ✗ HTTP {e.status} | {model_name} | {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
        )
        _log_debug(f"[{rid}] ── ERROR BODY ──\n{e.body.decode('utf-8', 'replace')}")
        raise HTTPException(
            status_code=e.status,
            detail=_safe_err_raw(e.body, e.status),
        )
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)

def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(
        f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
        + (f" | tool_calls={tc_names}" if tc_names else "")
        + f" | tokens={usage.get('total_tokens', '?')}"
    )
    # 完整响应体（含模型回答全文，只在 DEBUG 级输出）
    _log_debug(
        f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}"
    )


async def _aiter_str_lines(raw: bytes):
    """已收齐的字节 → 逐行 str（供聚合消费，等价旧 response.aiter_lines()）。"""
    for line in raw.splitlines():
        yield line.decode("utf-8", "replace")


async def _aiter_sse_lines(chunks):
    """字节流 → 逐行 str（近似 httpx aiter_lines：按 \\n 切，去尾部 \\r）。"""
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


def _estimate_tokens(chat_body: dict) -> int:
    """本地粗估 input tokens（chars/4）。Trae 的 count_tokens 默认走这里，不打上游。

    SOLO 上游大概率不遵守 max_tokens=1，走真实请求会白烧生成额度；Claude Code
    在每条消息前都调 count_tokens，因此默认本地估算，CODEBUDDY_TRAE_COUNT_TOKENS_UPSTREAM=1 可逃生。
    """
    total = 0
    for m in chat_body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    total += len(blk["text"])
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                total += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
    for tool in chat_body.get("tools") or []:
        if isinstance(tool, dict):
            total += len(json.dumps(tool, ensure_ascii=False))
    return max(1, total // 4)


async def _collect_stream(lines, *, keep_reasoning: bool = False) -> dict:
    """消费上游 OpenAI SSE 行流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    keep_reasoning=True（Trae）时额外累积 delta.reasoning_content → message.reasoning_content。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in lines:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if keep_reasoning and delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx, {"id": None, "name": None, "arguments": ""}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {
                "id": v["id"],
                "type": "function",
                "function": {"name": v["name"], "arguments": v["arguments"]},
            }
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason or "stop"}
        ],
        "usage": usage
        or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {
            "error": {
                "message": raw.decode("utf-8", "replace")[:500],
                "type": "upstream_error",
                "code": status,
            }
        }


async def _stream_upstream(
    backend,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    saw_done = False  # 是否观察到 [DONE]：判「正常结束」还是「被截断」的依据
    buf = b""
    raw_parts: list[bytes] = []  # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes, flush: bool = False):
        """解析累计数据里的完整 data: 行。

        flush=True 表示这是流的最后一帧：把残留的不完整行也当作一行处理。
        上游的最后一帧（finish_reason / [DONE]）不一定带尾随换行——连接正好在
        帧尾关闭时很常见——不 flush 就会漏掉终止信号，把正常结束误判成截断。
        """
        nonlocal finish_reason, saw_filter, saw_done, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        if flush:
            buf += b"\n"
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                saw_done = True
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if (
                "content-filter" in text_repr
                or "敏感" in text_repr
                or "审核" in text_repr
            ):
                saw_filter = True

    try:
        # 空闲超时包在 backend.astream 外层：约束的是分片间隔而非总时长，
        # 长推理/长输出的正常流不受影响；上游卡死时 wait_for 取消本次读取，
        # 由下面的 except 补发截断事件。
        async for chunk in _iter_with_idle_timeout(
            backend.astream(headers, body, timeout=None),
            config.get().stream_idle_timeout, model_name
        ):
            if chunk:
                raw_parts.append(chunk)
                _feed(chunk)
                yield chunk
    except BackendHTTPError as e:
        if e.emitted:
            # Trae 流内错误：error chunk + [DONE] 已在 astream 内直通给客户端，
            # PoolMiddleware 的 SSE 扫描已命中 → 冷却账号；此处仅记日志。
            _log(
                f"{prefix}✗ 上游流内错误 | {model_name} | {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
            )
            return
        _log(
            f"{prefix}✗ HTTP {e.status} | {model_name} | {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
        )
        _log_debug(f"{prefix}── ERROR BODY ──\n{e.body.decode('utf-8', 'replace')}")
        yield _err_event(e.body, e.status)
        return
    except (TimeoutError, asyncio.TimeoutError):
        # 空闲超时：客户端拿到明确错误，而不是一条永久挂起的连接。
        _log(f"{prefix}✗ 流空闲超时 | {model_name} | 上游 {config.get().stream_idle_timeout}s 无数据")
        yield _truncated_event(
            f"上游 {config.get().stream_idle_timeout} 秒无数据，连接已中止"
        )
        return


    # 收尾：把缓冲区里残留的不完整行（无尾随换行）也解析掉，否则会漏掉
    # 最后一帧里的 finish_reason / [DONE]，把正常结束误判成截断。
    _feed(b"", flush=True)

    # 上游在没有终止信号的情况下关闭：补一个明确事件，避免客户端把截断当正常结束。
    # finish_reason 与 [DONE] 任一出现都算正常收尾——不同上游的终止信号不完全一致。
    if finish_reason is None and not saw_done:
        _log(f"{prefix}✗ 流被截断 | {model_name} | 未观察到终止事件")
        yield _truncated_event("上游连接在完成前关闭，响应可能不完整")

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(
        f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
        + (f" | tool_calls={tool_names}" if tool_names else "")
        + f" | tokens={usage.get('total_tokens', '?')}"
    )
    # 完整原始 SSE（后端返回的全部内容）
    _log_debug(
        f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8', 'replace')}"
    )


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {
            "error": {
                "message": r.text[:500],
                "type": "upstream_error",
                "code": r.status_code,
            }
        }


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json

    chunk = {
        "error": {
            "message": msg.decode("utf-8", "replace")[:500],
            "type": "upstream_error",
            "code": status,
        },
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


def _truncated_event(reason: str) -> bytes:
    """流被截断时补发的 OpenAI SSE 错误 chunk。

    与 _err_event 分开：类型是 stream_truncated，客户端可据此区分「上游报错」
    与「流中途断了」，而不必把截断当成正常结束。
    """
    chunk = {"error": {"message": reason[:500], "type": "stream_truncated"}}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


async def _iter_with_idle_timeout(aiter, idle_seconds, label=""):
    """逐个 yield 上游分片；相邻两个分片间隔超过 idle_seconds 就抛 TimeoutError。

    约束的是**间隔**而不是总时长：用 asyncio.wait_for 包每一次 __anext__，
    计时随每个分片重置，因此长推理、长输出的正常流不会被掐断。
    上游卡死不关连接（TCP 还在但不发数据）时才会触发——那种情况下若用
    timeout=None，请求会永远挂着，一直占着账号的 in-flight 名额。
    """
    iterator = aiter.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_seconds)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise TimeoutError(f"上游 {idle_seconds} 秒无数据{('（' + label + '）') if label else ''}") from None
        yield chunk


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=False,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(
    backend, headers: dict, body: dict, *, timeout: float = 120
) -> tuple[int, bytes]:
    """经 backend.astream 收齐全部字节。非 2xx/传输错误 → BackendHTTPError。

    成功时长恒返回 (200, raw)——astream 契约保证非 2xx 一定 raise。
    """
    chunks: list[bytes] = []
    async for chunk in backend.astream(
        headers, body, timeout=timeout, raise_on_error=True
    ):
        if chunk:
            chunks.append(chunk)
    return 200, b"".join(chunks)


async def _post_backend_with_filter_retry(
    backend, headers: dict, body: dict, rid: str = "", model_name: str = "?"
) -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(backend, headers, body)
    text = raw.decode("utf-8", "replace")
    if (
        status == 200
        and backend.tencent_policies
        and _looks_like_content_filter_text(text)
        and CONFIG.get("desensitize")
        and CONFIG.get("no_compact")
        and _RESPONSES_DESENSITIZE
    ):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(
            f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness"
        )
        _log_debug(
            f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}"
        )
        retry_status, retry_raw = await _post_backend_once(backend, headers, retry_body)
        retry_text = retry_raw.decode("utf-8", "replace")
        if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
            return retry_status, retry_raw, retry_body
    return status, raw, body


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------


@app.post("/v1/responses")
async def create_response(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"request conversion error: {e}",
                    "type": "invalid_request_error",
                }
            },
        )

    # 路由：模型名 → 后端（trae/ 前缀进 Trae）；并做凭据一致性闸门
    backend, upstream_model, cred = _resolve_backend_or_http(chat_body.get("model") or "auto", routed=_routed_backend(request))

    if backend.tencent_policies:
        chat_body = filter_system_identity(chat_body)
        chat_body, projection_stats = project_responses_chat_body(
            chat_body, preserve=not _LOSSY_PROJECTION
        )
    else:
        # Trae：跳过腾讯投影（上游是私有协议），日志用 passthrough 占位
        projection_stats = {"mode": "passthrough"}
    chat_body.setdefault("model", "auto")
    chat_body["model"] = upstream_model or chat_body.get("model") or "auto"
    chat_body["stream"] = True
    if backend.tencent_policies and "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if backend.tencent_policies and _RESPONSES_DESENSITIZE:
        chat_body = _chat_body_desensitize(chat_body)

    client_wants_stream = bool(payload.get("stream", False))
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    _log(
        f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}"
    )
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log_debug(
        f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}"
    )

    headers = cred.get_headers()
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(backend, headers, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, final_body = await _post_backend_with_filter_retry(
            backend, headers, chat_body, rid, model_name
        )
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except BackendHTTPError as e:
        _log(
            f"[{rid}] ✗ HTTP {e.status} | {model_name} | {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
        )
        raise HTTPException(
            status_code=e.status, detail=_safe_err_raw(e.body, e.status)
        )

    result = converter.get_nonstream_response()
    response_status = 200
    if result.get("error"):
        code = result["error"].get("code", "")
        response_status = int(code) if str(code).isdigit() and 400 <= int(code) <= 599 else 502
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log_debug(
        f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}"
    )
    return JSONResponse(content=result, status_code=response_status)


async def _stream_responses(
    backend,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流输出。"""
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    raw_sse_lines = []
    try:
        async for line in _aiter_sse_lines(backend.astream(headers, body, timeout=300)):
            if line.strip():
                raw_sse_lines.append(line)
                raw_sse_lines = raw_sse_lines[-30:]
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except BackendHTTPError as e:
        if e.emitted:
            # 流内错误 chunk 已被 feed_line 转成 response.failed；此处仅记日志
            _log(
                f"{prefix}✗ 上游流内错误 | {model_name} | {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
            )
            elapsed = time.time() - t0 if t0 else 0
            _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream failed")
            return
        _log(f"{prefix}✗ HTTP {e.status} | {model_name}")
        yield converter.error(e.body.decode("utf-8", "replace")[:500], e.status).encode("utf-8")
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log_debug(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------


@app.post("/v1/messages")
async def create_message(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "messages is required",
                    "type": "invalid_request_error",
                }
            },
        )

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"request conversion error: {e}",
                    "type": "invalid_request_error",
                }
            },
        )

    # 路由：模型名 → 后端（trae/ 前缀进 Trae）；并做凭据一致性闸门
    backend, upstream_model, cred = _resolve_backend_or_http(chat_body.get("model") or "auto", routed=_routed_backend(request))

    chat_body.setdefault("model", "auto")
    chat_body["model"] = upstream_model or chat_body.get("model") or "auto"
    # 读取用户的 stream 参数，如果未提供则默认为 True
    user_stream = payload.get("stream", True)
    # 无论用户如何设置，都向后端请求流式响应（后端只支持流式）
    chat_body["stream"] = True
    if backend.tencent_policies and "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if backend.tencent_policies:
        chat_body = filter_system_identity(chat_body)
    if backend.tencent_policies and CONFIG.get("desensitize"):
        chat_body = desensitize_body(
            chat_body,
            roles=("system", "developer"),
            desensitize_harness_user=False,
            desensitize_tools=True,
            compact_harness=not CONFIG.get("no_compact"),
            strip_tool_metadata=True,
        )

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(
        f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)} | user_stream={user_stream}"
    )
    _log_debug(
        f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}"
    )

    headers = cred.get_headers()
    t0 = time.time()

    # 如果用户请求流式响应，直接返回流式
    if user_stream:
        return StreamingResponse(
            _stream_anthropic(backend, headers, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 否则，收集完整响应并返回 JSON
    from fastapi.responses import JSONResponse

    response_data = await _collect_anthropic_nonstream(
        backend, headers, chat_body, model_name, t0, rid
    )
    return JSONResponse(content=response_data)


async def _collect_anthropic_nonstream(
    backend,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
) -> dict:
    """收集完整的流式响应并返回非流式 Anthropic Message 对象。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        async for line in _aiter_sse_lines(
            backend.astream(headers, body, timeout=120.0, raise_on_error=True)
        ):
            converter.feed_line(line)
    except BackendHTTPError as e:
        _log(
            f"{prefix}✗ HTTP {e.status} | {model_name} | {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
        )
        raise HTTPException(
            status_code=e.status,
            detail={
                "error": {
                    "message": e.body.decode("utf-8", "replace")[:500],
                    "type": "api_error",
                    "code": e.status,
                }
            },
        ) from None

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | nonstream done")
    return converter.get_nonstream_response()


async def _stream_anthropic(
    backend,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        async for line in _iter_with_idle_timeout(
            _aiter_sse_lines(backend.astream(headers, body, timeout=None)),
            config.get().stream_idle_timeout, model_name
        ):
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except BackendHTTPError as e:
        # Anthropic 适配器不认识 OpenAI 形状的 error chunk（会被忽略），
        # 因此无论 emitted 与否，都在此处补发一条 event:error。
        _log(
            f"{prefix}✗ HTTP {e.status} | {model_name} | {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
        )
        error_evt = {
            "type": "error",
            "error": {
                "message": e.body.decode("utf-8", "replace")[:500],
                "type": "api_error",
                "code": e.status,
            },
        }
        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
        return
    except (TimeoutError, asyncio.TimeoutError):
        _log(f"{prefix}✗ 流空闲超时 | {model_name} | 上游 {config.get().stream_idle_timeout}s 无数据")
        error_evt = {
            "type": "error",
            "error": {
                "message": f"上游 {config.get().stream_idle_timeout} 秒无数据，连接已中止",
                "type": "api_error",
            },
        }
        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
        return


    # 上游未发出终止信号就结束：补一个 error 事件，客户端才不会把截断当正常结束。
    if not converter.completed:
        _log(f"{prefix}✗ 流被截断 | {model_name} | 未观察到终止事件")
        error_evt = {
            "type": "error",
            "error": {"message": "上游连接在完成前关闭，响应可能不完整",
                      "type": "api_error"},
        }
        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


@app.post("/v1/messages/count_tokens")
async def count_tokens(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Anthropic token 计数端点。

    Claude Code 在发送消息前调用此端点获取 token 计数。
    后端只支持流式请求，所以我们发送流式请求并从中提取 usage。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "messages is required",
                    "type": "invalid_request_error",
                }
            },
        )

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"request conversion error: {e}",
                    "type": "invalid_request_error",
                }
            },
        )

    # 路由：模型名 → 后端（trae/ 前缀进 Trae）；并做凭据一致性闸门
    backend, upstream_model, cred = _resolve_backend_or_http(chat_body.get("model") or "auto", routed=_routed_backend(request))

    # 最小化实际生成：只需要 usage 统计
    chat_body.setdefault("model", "auto")
    chat_body["model"] = upstream_model or chat_body.get("model") or "auto"
    chat_body["max_tokens"] = 1
    chat_body["stream"] = True  # 后端只支持流式
    if backend.tencent_policies:
        chat_body["stream_options"] = {"include_usage": True}

    if backend.tencent_policies:
        chat_body = filter_system_identity(chat_body)
    if backend.tencent_policies and CONFIG.get("desensitize"):
        chat_body = desensitize_body(
            chat_body,
            roles=("system", "developer"),
            desensitize_harness_user=False,
            desensitize_tools=True,
            compact_harness=not CONFIG.get("no_compact"),
            strip_tool_metadata=True,
        )

    # Trae：SOLO 上游大概率不遵守 max_tokens=1，走真实请求会白烧生成额度（Claude Code
    # 每条消息前都调 count_tokens），默认本地 chars/4 估算；env 可逃生走上游。
    if not backend.count_tokens_upstream and os.environ.get("CODEBUDDY_TRAE_COUNT_TOKENS_UPSTREAM", "0") != "1":
        return {"input_tokens": _estimate_tokens(chat_body)}

    headers = cred.get_headers()

    try:
        input_tokens = 0
        async for line in _aiter_sse_lines(
            backend.astream(headers, chat_body, timeout=30.0, raise_on_error=True)
        ):
            if not line or line.startswith(":"):
                continue
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    # message_delta 事件直接包含 usage
                    if "usage" in chunk:
                        usage = chunk.get("usage") or {}
                        tokens = usage.get("prompt_tokens", 0) or usage.get(
                            "input_tokens", 0
                        )
                        if tokens > 0:
                            input_tokens = tokens
                    # message_start 事件在 message 对象中包含 usage
                    elif "message" in chunk and "usage" in chunk["message"]:
                        usage = chunk["message"].get("usage") or {}
                        tokens = usage.get("prompt_tokens", 0) or usage.get(
                            "input_tokens", 0
                        )
                        if tokens > 0:
                            input_tokens = tokens
                except json.JSONDecodeError:
                    continue

        return {"input_tokens": input_tokens}

    except BackendHTTPError as e:
        _log(
            f"✗ count_tokens HTTP {e.status}: {_truncate(e.body.decode('utf-8', 'replace'), 200)}"
        )
        raise HTTPException(
            status_code=e.status,
            detail={
                "error": {
                    "message": e.body.decode("utf-8", "replace")[:500],
                    "type": "api_error",
                    "code": e.status,
                }
            },
        ) from None


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def preflight() -> bool:
    af = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if af is None:
        sys.stderr.write(
            "\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n"
        )
        ok = False
    else:
        try:
            cm = make_credential(af)
            info = cm.summary()
            sys.stderr.write(
                f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}\n"
            )
            sys.stderr.write(
                f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n"
            )
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败：{e}\n")
            ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(
        description="CodeBuddy -> OpenAI 兼容转换器（直连后端）"
    )
    cfg = config.get()
    ap.add_argument("--host", default=cfg.host or "127.0.0.1")
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument(
        "--api-key",
        default=cfg.api_key,
        help="可选：要求客户端携带的 API key（默认不校验）",
    )
    ap.add_argument(
        "--log",
        default=cfg.log_path,
        metavar="PATH",
        help="额外把日志写到该文件（如 --log converter.log）。不传则只输出到 stderr。",
    )
    ap.add_argument(
        "--desensitize",
        action="store_true",
        help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
        "插入零宽空格，缓解被后端内容审核误拦。默认关闭。",
    )
    ap.add_argument(
        "--no-compact",
        action="store_true",
        help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
        "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
        "但审核误拦风险略高于默认压缩模式。",
    )
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    # 命令行开关是「或」的关系：配置开了就开，命令行也开就开，两者都没有才关。
    CONFIG["desensitize"] = args.desensitize or cfg.desensitize
    CONFIG["no_compact"] = args.no_compact or cfg.no_compact
    CONFIG["log_path"] = args.log
    # 日志最早初始化：之后的启动信息与请求日志才有出口。
    logging_setup.setup_logging(cfg.log_level, args.log)
    _legacy = config.legacy_used()
    if _legacy:
        logging.getLogger("core.converter").info(
            "以下配置使用旧名，仍生效，建议改为规范名：%s",
            "、".join(f"{old}→{new}" for old, new in _legacy),
        )
    af = find_auth_file()
    CONFIG["cred"] = make_credential(af) if af else None

    if not args.skip_check:
        preflight()

    sys.stderr.write(
        f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n"
    )
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write(
        "   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n"
    )
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write(
        "   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n"
    )
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log("==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port,
                log_level=logging_setup.uvicorn_level(), access_log=False)


if __name__ == "__main__":
    main()

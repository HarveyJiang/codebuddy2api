"""Dynamic model registry: pull the live model catalog from the upstream console API,
merge with built-in official metadata, and expose enable/disable management.

Discovery endpoint (discovered from the official desktop client's traffic, matches
Sliverkiss/cpa-plugin's clean-room implementation):
    GET {BACKEND}/console/enterprises/personal/models
The response carries data.models[] (id/name/credits/maxInputTokens/disabled...) and
data.agents[] where the "cli" agent lists the model ids shown in the official UI.

Refresh policy: fetch on startup, then refresh on an interval. Failures keep the last
good snapshot; if nothing was ever fetched, fall back to BUILTIN_MODELS.

Display rules (admin model catalog): descriptions are Chinese-only — the official
descriptionZh, with the built-in Chinese text as fallback when upstream omits it;
rows are ordered by unit price (credits) ascending with disabled models last.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

BACKEND = "https://copilot.tencent.com"
ORIGIN = "https://www.codebuddy.cn"
USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"
CN = timezone(timedelta(hours=8))
# 官方产品配置接口（免鉴权，但校验 CLI User-Agent）。客户端将其缓存为
# .workbuddy/cache/acc-product-config-v3.json，模型列表/倍率/折扣以它为准。
CONFIG_URL = "https://www.workbuddy.cn/v3/config"
FETCH_TIMEOUT = 15
REFRESH_INTERVAL = 6 * 3600


def _credits_multiplier(text):
    """'x0.06 credits' -> 0.06; '' or missing -> None (billed as standard)."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return float(text.strip().lower().replace("x", "").replace("credits", "").strip())
    except ValueError:
        return None


def _label(model):
    """展示用描述：只取官方中文描述（descriptionZh），不掺英文。"""
    value = model.get("descriptionZh")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class ModelRegistry:
    def __init__(self, get_credential=None, clock=time.time):
        """get_credential: callable returning a manager whose get_headers() yields auth headers."""
        self.get_credential = get_credential
        self.clock = clock
        # RLock: snapshot() 持锁调用 active_promo()，后者经模块级引用回到
        # promotions 属性再次取锁，非重入锁会自死锁，导致 /admin/api/overview
        # 永久挂起（登录成功但账号/模型全部加载不出）。
        self._lock = threading.RLock()
        self._catalog = {}      # id -> {name, description, credits, max_input_tokens, source}
        self._available = []    # ordered ids from the upstream cli agent
        self._disabled = set()  # user-disabled ids
        self._promotions = []   # dynamic discounts parsed from the product config
        self._updated = 0
        self._error = None
        _registry_ref[0] = self

    # -- persistence -------------------------------------------------------
    def export_state(self):
        with self._lock:
            return {"catalog": dict(self._catalog), "available": list(self._available),
                    "disabled": sorted(self._disabled), "promotions": list(self._promotions),
                    "updated": self._updated}

    def import_state(self, state):
        if not isinstance(state, dict):
            return
        with self._lock:
            catalog = state.get("catalog")
            if isinstance(catalog, dict):
                self._catalog = {k: v for k, v in catalog.items() if isinstance(v, dict)}
            available = state.get("available")
            if isinstance(available, list):
                self._available = [str(i) for i in available]
            disabled = state.get("disabled")
            if isinstance(disabled, list):
                self._disabled = {str(i) for i in disabled}
            promotions = state.get("promotions")
            if isinstance(promotions, list):
                self._promotions = [p for p in promotions if isinstance(p, dict)]
            self._updated = int(state.get("updated") or 0)

    # -- upstream fetch ----------------------------------------------------
    def fetch(self):
        """Pull the live product config. Returns True on success. Never raises.

        Endpoint: GET https://www.workbuddy.cn/v3/config — the gateway checks the
        CLI User-Agent (a fake bearer alone is not enough): without valid account
        credentials it answers with an empty shell (agents/models all null), so
        an enabled account's headers are sent when available. The desktop client
        caches this response to .workbuddy/cache/acc-product-config-v3.json. It
        carries data.agents (the cli agent's models array is the official UI
        menu), data.models (credits/maxInputTokens/description...) and
        data.modelPromotions.
        """
        try:
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            # 未登录时上游只返回空壳（agents/models 全为 null），必须带账号凭据。
            manager = None
            if self.get_credential is not None:
                try:
                    manager = self.get_credential()
                except Exception:
                    manager = None
            if manager is not None:
                try:
                    account_headers = dict(manager.get_headers())
                    # 上游校验 CLI User-Agent（非 CLI UA 返回 400 check ua），
                    # 账号凭据里的 codebuddy2openai UA 必须让位给 CLI UA。
                    account_headers.pop("User-Agent", None)
                    account_headers.pop("user-agent", None)
                    headers.update(account_headers)
                except Exception:
                    pass
            with httpx.Client(timeout=FETCH_TIMEOUT) as client:
                response = client.get(CONFIG_URL, headers=headers)
            if response.status_code != 200:
                self._record(f"配置接口返回 {response.status_code}")
                return False
            data = response.json()
            if not isinstance(data, dict):
                self._record("配置接口返回异常结构")
                return False
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            models = payload.get("models") or []
            agents = payload.get("agents") or []
            if not isinstance(models, list) or not isinstance(agents, list):
                self._record("配置缺少 models/agents 字段")
                return False
            if not agents:
                self._record("上游返回空配置（凭据可能已失效，请重新授权账号）")
                return False
            by_id = {m.get("id"): m for m in models if isinstance(m, dict) and m.get("id")}
            cli_ids = next((a.get("models") for a in agents
                            if isinstance(a, dict) and a.get("name") == "cli"), None)
            if not isinstance(cli_ids, list) or not cli_ids:
                self._record("配置未包含 cli agent 列表")
                return False
            # 智能路由三档（快速/均衡/极致）不在 cli 列表里，官方固定提供，始终附加。
            cli_ids = list(cli_ids)
            for tier in ("fast-model", "balanced-model", "deep-model"):
                if tier not in cli_ids:
                    cli_ids.append(tier)
            catalog, available = {}, []
            for mid in cli_ids:
                if not isinstance(mid, str):
                    continue
                raw = by_id.get(mid)
                if raw is not None and raw.get("disabled"):
                    continue
                # 上游未返回该模型（如三档路由）时，从内置官方元数据兜底。
                if raw is None:
                    builtin = BUILTIN_MODELS.get(mid)
                    if builtin is None:
                        continue
                    catalog[mid] = {"name": builtin["name"], "description": builtin["description"],
                                   "credits": builtin["credits"],
                                   "max_input_tokens": builtin["max_input_tokens"], "source": "builtin"}
                    available.append(mid)
                    continue
                max_in = raw.get("maxInputTokens")
                # 上游缺中文描述时用内置官方描述兜底（保证只有中文描述）。
                description = _label(raw)
                if not description:
                    builtin = BUILTIN_MODELS.get(mid)
                    if builtin is not None and builtin.get("description"):
                        description = builtin["description"]
                catalog[mid] = {
                    "name": raw.get("name") if isinstance(raw.get("name"), str) else mid,
                    "description": description,
                    "credits": _credits_multiplier(raw.get("credits")),
                    "max_input_tokens": max_in if isinstance(max_in, int) else None,
                    "source": "upstream",
                }
                available.append(mid)
            if not available:
                self._record("cli agent 模型全部被禁用")
                return False
            promotions = _parse_promotions(payload.get("modelPromotions"))
            with self._lock:
                # Keep management choices for models that still exist; add new ones enabled.
                self._disabled &= set(available)
                self._catalog, self._available = catalog, available
                self._promotions = promotions
                self._updated = int(self.clock())
                self._error = None
            return True
        except Exception as e:
            self._record(f"拉取失败：{type(e).__name__}")
            return False

    def _record(self, message):
        with self._lock:
            self._error = message

    # -- builtin fallback --------------------------------------------------
    @property
    def promotions(self):
        with self._lock:
            return list(self._promotions)

    def ensure_builtin(self):
        """Seed the catalog with BUILTIN_MODELS when nothing upstream is available yet."""
        with self._lock:
            if self._available:
                return
            self._catalog = {mid: dict(info, source="builtin") for mid, info in BUILTIN_MODELS.items()}
            self._available = [mid for mid in BUILTIN_ORDER if mid in self._catalog]
            self._updated = 0

    # -- queries -----------------------------------------------------------
    def snapshot(self):
        """Full catalog with management flags and current promo, for the admin UI.

        排序规则：启用的在前、禁用的在后；同组内按单价（credits）升序，
        None（按标准计费）视为 0，与免费模型并列最前。
        """
        now = datetime.now(CN)
        with self._lock:
            models = []
            for mid in self._available:
                if mid not in self._catalog:
                    continue
                entry = dict(self._catalog[mid], id=mid, enabled=mid not in self._disabled)
                promo = active_promo(mid, now)
                if promo and isinstance(entry.get("credits"), (int, float)):
                    entry["promo"] = {"label": promo[0], "factor": promo[1],
                                      "credits": round(entry["credits"] * promo[1], 4)}
                models.append(entry)
            models.sort(key=lambda m: (
                not m["enabled"],
                m["credits"] if isinstance(m.get("credits"), (int, float)) else 0,
                self._available.index(m["id"]),
            ))
            return {"updated": self._updated, "error": self._error, "models": models}

    def enabled_models(self):
        """Ids available to API clients: upstream list minus user-disabled."""
        with self._lock:
            return [mid for mid in self._available if mid not in self._disabled]

    def snapshot_ids(self):
        """All known ids (enabled + disabled), for distinguishing 'disabled' from 'unknown'."""
        with self._lock:
            return set(self._available)

    def set_enabled(self, mid, enabled):
        with self._lock:
            if mid not in self._catalog and mid not in self._available:
                raise KeyError(mid)
            self._disabled.discard(mid) if enabled else self._disabled.add(mid)
        return self.export_state()

    def info(self, mid):
        with self._lock:
            return dict(self._catalog.get(mid) or {})


# 官方元数据快照（/v3/config live 数据 + 官方 product.json）。cli 菜单模型在前，
# 三档智能路由（快速/均衡/极致）为官方固定提供，附加在末尾。
BUILTIN_ORDER = [
    "hy4-preview", "hy3", "hy3-x", "step-5-preview", "space-bunny",
    "deepseek-v4.1-flash", "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1",
    "glm-5v-turbo", "minimax-m3", "minimax-m2.7", "kimi-k3-1", "kimi-k2.8-preview",
    "kimi-k2.7", "kimi-k2.6", "deepseek-v4-pro",
    "fast-model", "balanced-model", "deep-model",
]

BUILTIN_MODELS = {
    "hy4-preview": {"name": "Hy4 preview", "description": "混元思考模型，具有增强的推理能力", "credits": 0.29, "max_input_tokens": 960000},
    "hy3": {"name": "Hy3", "description": "混元思考模型，具有增强的推理能力", "credits": 0.0, "max_input_tokens": 192000},
    "hy3-x": {"name": "Hy3", "description": "混元思考模型，具有增强的推理能力", "credits": 0.05, "max_input_tokens": 192000},
    "step-5-preview": {"name": "Step-5-Preview", "description": "阶跃 StepFun 面向真实任务的新一代旗舰基模", "credits": 0.0, "max_input_tokens": 1000000},
    "space-bunny": {"name": "Space-Bunny", "description": "推理速度极快，编码能力强劲，并支持原生多模态输入的匿名大模型", "credits": 0.08, "max_input_tokens": 1000000},
    "deepseek-v4.1-flash": {"name": "Deepseek-V4.1-Flash", "description": "DeepSeek 旗舰模型，支持 1M 上下文窗口，原生多模态", "credits": 0.11, "max_input_tokens": 1000000},
    "glm-5.3": {"name": "GLM-5.3", "description": "旗舰模型，擅长复杂软件工程与长程 Agent 任务", "credits": 0.79, "max_input_tokens": 1000000},
    "glm-5.3-flash": {"name": "GLM-5.3-Flash", "description": "原生多模态模型，擅长视觉理解与专业任务", "credits": 0.06, "max_input_tokens": 1000000},
    "glm-5.2": {"name": "GLM-5.2", "description": "1M 上下文，擅长长程任务", "credits": 0.79, "max_input_tokens": 1000000},
    "glm-5.1": {"name": "GLM-5.1", "description": "能力均衡，适合日常使用", "credits": 0.79, "max_input_tokens": 200000},
    "glm-5v-turbo": {"name": "GLM-5v-Turbo", "description": "原生多模态模型", "credits": 0.71, "max_input_tokens": 200000},
    "minimax-m3": {"name": "MiniMax-M3", "description": "原生多模态，擅长代码、智能体任务", "credits": 0.25, "max_input_tokens": 512000},
    "minimax-m2.7": {"name": "MiniMax-M2.7", "description": "能力均衡，适合日常使用", "credits": 0.19, "max_input_tokens": 200000},
    "kimi-k3-1": {"name": "Kimi-K3", "description": "擅长处理复杂的长程自主任务，前端开发能力突出，同时在知识工作与科研推理上表现出色", "credits": 1.62, "max_input_tokens": 1000000},
    "kimi-k2.8-preview": {"name": "Kimi-K2.8-Preview", "description": "擅长处理复杂的长程自主任务，前端开发能力突出，同时在知识工作与科研推理上表现出色", "credits": 0.77, "max_input_tokens": 1000000},
    "kimi-k2.7": {"name": "Kimi-K2.7-Code", "description": "多模态模型，适合日常任务", "credits": 0.57, "max_input_tokens": 256000},
    "kimi-k2.6": {"name": "Kimi-K2.6", "description": "多模态模型，适合日常任务", "credits": 0.52, "max_input_tokens": 256000},
    "deepseek-v4-pro": {"name": "Deepseek-V4-Pro", "description": "DeepSeek 旗舰模型，支持 1M 上下文窗口", "credits": 0.51, "max_input_tokens": 1000000},
    "fast-model": {"name": "快速", "description": "智能路由 · 优先响应速度，适合简单任务与快速问答", "credits": 0.21, "max_input_tokens": 300000},
    "balanced-model": {"name": "均衡", "description": "智能路由 · 兼顾速度与质量，适合大多数日常工作", "credits": 0.65, "max_input_tokens": 300000},
    "deep-model": {"name": "极致", "description": "智能路由 · 优先深度与准确性，适合复杂分析和高要求任务", "credits": 1.2, "max_input_tokens": 300000},
}

# 官方折扣活动（modelPromotions）：validFrom/validUntil 与 daily 时段均按 Asia/Shanghai 判断，
# factor 与官方 credits 基准相乘得到活动期实际倍率。
PROMOTIONS = [
    {"label": "夜间折扣", "model_ids": ["glm-5.2"], "factor": 0.5,
     "daily": [("23:00", "24:00"), ("00:00", "07:50")]},
    {"label": "夜间折扣", "model_ids": ["deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash"], "factor": 0.5,
     "daily": [("00:00", "24:00")]},
    {"label": "限时免费", "model_ids": ["hy3", "hy3-x"], "factor": 0.0,
     "valid_from": "2026-07-06", "valid_until": "2026-11-01", "daily": [("00:00", "24:00")]},
    {"label": "夜间免费", "model_ids": ["hy4-preview"], "factor": 0.0,
     "valid_from": "2026-09-11", "valid_until": "2026-11-01", "daily": [("00:00", "08:00")]},
    {"label": "独家限免", "model_ids": ["step-5-preview"], "factor": 0.0,
     "valid_from": "2026-10-12", "valid_until": "2026-10-26", "daily": [("00:00", "24:00")]},
]


def _parse_hhmm(text):
    parts = str(text).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _parse_promotions(raw):
    """官方 modelPromotions → 内部结构（兼容内置 PROMOTIONS 的键）。"""
    result = []
    if not isinstance(raw, list):
        return result
    for promo in raw:
        if not isinstance(promo, dict) or promo.get("enabled") is not True:
            continue
        if promo.get("kind") != "discount":
            continue
        factor = (promo.get("discount") or {}).get("factor")
        if not isinstance(factor, (int, float)):
            continue
        model_ids = [m for m in promo.get("modelIds") or [] if isinstance(m, str)]
        if not model_ids:
            continue
        entry = {"label": (promo.get("badge") or {}).get("label") or "折扣",
                 "model_ids": model_ids, "factor": factor}
        schedule = promo.get("schedule") or {}
        if schedule.get("validFrom"):
            entry["valid_from"] = str(schedule["validFrom"])[:10]
        if schedule.get("validUntil"):
            entry["valid_until"] = str(schedule["validUntil"])[:10]
        daily = []
        for window in schedule.get("daily") or []:
            if isinstance(window, dict) and window.get("start") and window.get("end"):
                daily.append((str(window["start"]), str(window["end"])))
        entry["daily"] = daily or [("00:00", "24:00")]
        result.append(entry)
    return result


def active_promo(mid, now=None):
    """返回模型当前生效的折扣（label, factor）；无活动返回 None。

    优先查 registry 拉到的动态活动表，其次内置 PROMOTIONS 兜底。
    """
    moment = now or datetime.now(CN)
    date_str = moment.strftime("%Y-%m-%d")
    minutes = moment.hour * 60 + moment.minute

    def _hit(promo_list):
        best = None
        for promo in promo_list:
            if mid not in promo["model_ids"]:
                continue
            if promo.get("valid_from") and date_str < promo["valid_from"]:
                continue
            if promo.get("valid_until") and date_str >= promo["valid_until"]:
                continue
            if not any(_parse_hhmm(s) <= minutes < _parse_hhmm(e) for s, e in promo["daily"]):
                continue
            if best is None or promo["factor"] < best[1]:
                best = (promo["label"], promo["factor"])
        return best

    registry = _registry_ref[0] if _registry_ref[0] is not None else None
    if registry is None:
        return _hit(PROMOTIONS)
    with registry._lock:
        promos = list(registry._promotions)
    best = _hit(promos)
    if best is None:
        best = _hit(PROMOTIONS)
    return best


# 模块级注册表引用：fetch 成功后 active_promo 优先用动态活动表。
_registry_ref = [None]

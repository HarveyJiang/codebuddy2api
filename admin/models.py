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

from core.backends import trae as trae_backend
from core.backends.registry import trae_enabled

BACKEND = "https://copilot.tencent.com"
ORIGIN = "https://www.codebuddy.cn"
USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"
CN = timezone(timedelta(hours=8))
# 官方产品配置接口（免鉴权，但校验 CLI User-Agent）。客户端将其缓存为
# .workbuddy/cache/acc-product-config-v3.json，模型列表/倍率/折扣以它为准。
CONFIG_URL = "https://www.workbuddy.cn/v3/config"
FETCH_TIMEOUT = 15
REFRESH_INTERVAL = 6 * 3600


# 模型来源（models.source 列），管理页按它显示徽标：
#   upstream —— 上游 /v3/config 本次返回了它，元数据以上游为准
#   builtin  —— 上游没返回，用内置官方元数据兜底（如三档智能路由）
#   custom   —— 用户手动添加的，上游没返回但实际可调用
SOURCE_UPSTREAM = "upstream"
SOURCE_BUILTIN = "builtin"
SOURCE_CUSTOM = "custom"
# Trae 第二后端模型在 models 表与对外的 id 前缀（内存里存 bare id）。
TRAE_PREFIX = "trae/"


def normalize_source(value):
    """收敛到三种来源之一；不认识的历史值当作上游。"""
    return value if value in (SOURCE_UPSTREAM, SOURCE_BUILTIN, SOURCE_CUSTOM) else SOURCE_UPSTREAM


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
    def __init__(self, get_credential=None, clock=time.time, trae_credential_getter=None):
        """get_credential: callable returning a manager whose get_headers() yields auth headers.
        trae_credential_getter: callable returning a TraeCredentialManager (dynamic Trae catalog)."""
        self.get_credential = get_credential
        self.trae_credential_getter = trae_credential_getter
        self.clock = clock
        # RLock: snapshot() 持锁调用 active_promo()，后者经模块级引用回到
        # promotions 属性再次取锁，非重入锁会自死锁，导致 /admin/api/overview
        # 永久挂起（登录成功但账号/模型全部加载不出）。
        self._lock = threading.RLock()
        self._catalog = {}      # id -> {name, description, credits, max_input_tokens, source}
        self._available = []    # ordered ids from the upstream cli agent
        self._disabled = set()  # user-disabled ids
        # 用户手工添加的模型 id。上游菜单不一定列全（灰度、区域差异、菜单滞后），
        # 这些模型上游没返回但仍可调用，必须跨 fetch() 存活。
        self._custom = set()
        self._promotions = []   # dynamic discounts parsed from the product config
        self._updated = 0
        self._error = None
        # Trae 第二后端模型目录：bare config_name 存储，对外以 `trae/` 前缀暴露。
        self._trae_catalog = {}      # id -> {name, description, credits, max_input_tokens, source}
        self._trae_available = []    # ordered bare ids
        self._trae_disabled = set()  # user-disabled bare ids
        self._trae_updated = 0
        self._trae_error = None
        _registry_ref[0] = self

    # -- persistence -------------------------------------------------------
    def load(self, db):
        """从 models 表恢复内存目录。只加载 available=1 的行。

        上游已消失的模型（available=0）只留在表里供 request_logs join，不进 registry：
        否则 snapshot_ids() 会把它们算成「已知」，PoolMiddleware 就会把「上游已下线」
        误报成「管理页已禁用」而拒掉请求。

        Trae 第二后端的模型与 WorkBuddy 同表存放，model_id 带 `trae/` 前缀（与
        对外暴露的 id 一致），加载时剥前缀进 _trae_* 结构。
        """
        with self._lock:
            # include_custom：手工添加的行可能 available=0（上游没返回它），
            # 只按 available=1 加载会让它们在重启后消失。
            rows = db.load_models(only_available=True, include_custom=True)
            wb_rows = {mid: row for mid, row in rows.items()
                       if not mid.startswith(TRAE_PREFIX)}
            trae_rows = {mid: row for mid, row in rows.items()
                         if mid.startswith(TRAE_PREFIX)}
            self._catalog, self._available, self._custom = {}, [], set()
            order_rows = sorted(wb_rows.items(),
                                key=lambda kv: kv[1].get("sort_order", 0))
            for mid, row in order_rows:
                source = normalize_source(row.get("source"))
                self._catalog[mid] = {"name": row.get("name") or mid,
                                      "description": row.get("description"),
                                      "credits": row.get("credits"),
                                      "max_input_tokens": row.get("max_input_tokens"),
                                      "source": source,
                                      "_promo": row.get("promo")}
                self._available.append(mid)
                if source == SOURCE_CUSTOM:
                    self._custom.add(mid)
                if not row.get("enabled", 1):
                    self._disabled.add(mid)
            settings = db.get_settings() or {}
            self._updated = settings.get("models_updated", 0)
            # Trae 目录：同表前缀行剥前缀恢复；禁用状态取 enabled=0。
            self._trae_catalog, self._trae_available, self._trae_disabled = {}, [], set()
            trae_order = sorted(trae_rows.items(),
                                key=lambda kv: kv[1].get("sort_order", 0))
            for mid, row in trae_order:
                bare = mid[len(TRAE_PREFIX):]
                self._trae_catalog[bare] = {"name": row.get("name") or bare,
                                            "description": row.get("description"),
                                            "credits": row.get("credits"),
                                            "max_input_tokens": row.get("max_input_tokens"),
                                            "source": normalize_source(row.get("source"))}
                self._trae_available.append(bare)
                if not row.get("enabled", 1):
                    self._trae_disabled.add(bare)

    def persist(self, db):
        """把当前目录写回 models 表（UPSERT 不删行）。

        fetch() 拉到的 available 列表之外、上次存在但这次没出现的模型标 available=0 保留，
        保证 request_logs.model_id 长期可 join。用户禁用状态 enabled 只由用户操作改写。

        Trae 目录以 `trae/` 前缀 id 写入同一张表，available/enabled 语义与
        WorkBuddy 行一致（禁用 = enabled=0）。
        """
        with self._lock:
            rows = {}
            tail = len(self._available)   # 不在 _available 的行从末尾开始编号，保持稳定
            for mid, info in self._catalog.items():
                # source 必须忠实写回：自定义项若被写成 upstream，重启后 _custom
                # 就空了，下一次 fetch() 会把它当「上游已消失」而冲掉。
                source = normalize_source(info.get("source"))
                if mid in self._custom:
                    source = SOURCE_CUSTOM
                if mid in self._available:
                    sort_order = self._available.index(mid)
                else:
                    # 同一序位会让 reload 时的相对顺序随机（sorted 稳定但输入无序）。
                    tail += 1
                    sort_order = tail
                rows[mid] = {"name": info.get("name") or mid, "description": info.get("description"),
                             "credits": info.get("credits"), "max_input_tokens": info.get("max_input_tokens"),
                             "source": source,
                             "available": 1 if mid in self._available else 0,
                             "enabled": 0 if mid in self._disabled else 1,
                             "sort_order": sort_order,
                             "promo": info.get("_promo")}
            # 上次标记 available 的、这次 catalog 里已不在的：写 available=0 保留行。
            trae_tail = len(self._trae_available)
            for bare, info in self._trae_catalog.items():
                mid = TRAE_PREFIX + bare
                if bare in self._trae_available:
                    sort_order = self._trae_available.index(bare)
                else:
                    trae_tail += 1
                    sort_order = trae_tail
                rows[mid] = {"name": info.get("name") or bare,
                             "description": info.get("description"),
                             "credits": info.get("credits"),
                             "max_input_tokens": info.get("max_input_tokens"),
                             "source": normalize_source(info.get("source")),
                             "available": 1 if bare in self._trae_available else 0,
                             "enabled": 0 if bare in self._trae_disabled else 1,
                             "sort_order": sort_order,
                             "promo": None}
            db.save_models(rows)
            db.update_settings({"models_updated": self._updated})

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
                    # 账号凭据里的 UA 必须让位给 CLI UA。
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
                                   "max_input_tokens": builtin["max_input_tokens"], "source": SOURCE_BUILTIN}
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
                    "source": SOURCE_UPSTREAM,
                }
                available.append(mid)
            if not available:
                self._record("cli agent 模型全部被禁用")
                return False
            promotions = _parse_promotions(payload.get("modelPromotions"))
            with self._lock:
                # 合并而不是替换：用户手工添加的模型上游不会返回，整体替换会让它们
                # 每次自动拉取（启动 + 每 6 小时）后全部消失。
                merged_catalog, merged_available = dict(catalog), list(available)
                for mid in self._custom:
                    if mid in merged_catalog:
                        # 上游这次也有了它：元数据以上游为准，但 source 保持 custom，
                        # 否则下一次拉取时 _custom 就断了，用户保存的项会「变成上游的」。
                        entry = merged_catalog[mid]
                        if isinstance(entry, dict):
                            entry["source"] = SOURCE_CUSTOM
                        continue
                    entry = self._catalog.get(mid) or {}
                    merged_catalog[mid] = dict(entry, source=SOURCE_CUSTOM)
                    # 追加在末尾，不打乱上游按单价排好的顺序。
                    merged_available.append(mid)
                # Keep management choices for models that still exist; add new ones enabled.
                self._disabled &= set(merged_available)
                self._catalog, self._available = merged_catalog, merged_available
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
            self._catalog = {mid: dict(info, source=SOURCE_BUILTIN) for mid, info in BUILTIN_MODELS.items()}
            self._available = [mid for mid in BUILTIN_ORDER if mid in self._catalog]
            self._updated = 0

    # -- Trae 第二后端模型目录 ----------------------------------------------
    def ensure_trae_builtin(self):
        """无动态表时用 Trae 静态 32 条兜底（ Trae 后端恒在 /v1/models 可见）。"""
        with self._lock:
            if self._trae_available:
                return
            self._trae_catalog = {m["id"]: {"name": m["id"], "description": None, "credits": None,
                                            "max_input_tokens": m.get("context_length"),
                                            "max_context_length": m.get("max_context_length"),
                                            "source": "builtin"}
                                  for m in trae_backend.TRAE_STATIC_MODELS}
            self._trae_available = [m["id"] for m in trae_backend.TRAE_STATIC_MODELS]
            self._trae_updated = 0

    def refresh_trae_models(self):
        """拉取 Trae 动态模型表（get_detail_param，需一个启用的 Trae 账号）。

        失败保留上次快照；从未拉到过则维持 builtin。返回是否拉到新表。
        """
        manager = None
        if self.trae_credential_getter is not None:
            try:
                manager = self.trae_credential_getter()
            except Exception:
                manager = None
        entries = None
        if manager is not None:
            try:
                entries = manager.fetch_models()
            except Exception as e:
                with self._lock:
                    self._trae_error = f"拉取失败：{type(e).__name__}"
                return False
        if not entries:
            return False
        with self._lock:
            catalog = {}
            for e in entries:
                mid = str(e.get("id") or "").strip()
                if not mid:
                    continue
                catalog[mid] = {"name": str(e.get("name") or mid),
                                "description": e.get("description") or None,
                                "credits": e.get("credits"),
                                "max_input_tokens": e.get("context_length"),
                                "max_context_length": e.get("max_context_length"),
                                "source": "upstream"}
            if not catalog:
                return False
            self._trae_catalog = catalog
            self._trae_available = [str(e["id"]) for e in entries if str(e.get("id") or "") in catalog]
            self._trae_disabled &= set(self._trae_available)
            self._trae_updated = int(self.clock())
            self._trae_error = None
        return True

    def trae_entries(self):
        """供 core.backends.trae 目录提供者：bare id 列表（含被禁用的——禁用只挡客户端请求）。"""
        with self._lock:
            return [{"id": mid, "name": (self._trae_catalog.get(mid) or {}).get("name") or mid,
                     "context_length": (self._trae_catalog.get(mid) or {}).get("max_input_tokens") or 131072}
                    for mid in self._trae_available if mid in self._trae_catalog]

    # -- queries -----------------------------------------------------------
    def snapshot(self):
        """Full catalog with management flags and current promo, for the admin UI.

        排序规则：启用的在前、禁用的在后；同组内按单价（credits）升序，
        None（按标准计费）视为 0，与免费模型并列最前。
        Trae 模型固定排在 WorkBuddy 之后（backend="trae"），组内启用在前。
        """
        now = datetime.now(CN)
        with self._lock:
            models = []
            for mid in self._available:
                if mid not in self._catalog:
                    continue
                entry = dict(self._catalog[mid], id=mid, enabled=mid not in self._disabled,
                             custom=mid in self._custom)
                entry["source"] = normalize_source(entry.get("source"))
                stored = entry.pop("_promo", None)
                # promo 列存的是上游活动文案（只做展示）；折后价仍由内置 PROMOTIONS
                # 在读取时算，两者不一致时以内置表为准。
                promo = active_promo(mid, now)
                if promo and isinstance(entry.get("credits"), (int, float)):
                    entry["promo"] = {"label": promo[0] or stored or "折扣", "factor": promo[1],
                                      "credits": round(entry["credits"] * promo[1], 4)}
                elif stored and isinstance(entry.get("credits"), (int, float)):
                    entry["promo"] = {"label": stored, "factor": 1.0, "credits": entry["credits"]}
                models.append(entry)
            models.sort(key=lambda m: (
                not m["enabled"],
                m["credits"] if isinstance(m.get("credits"), (int, float)) else 0,
                self._available.index(m["id"]),
            ))
            if trae_enabled():
                trae_models = []
                for mid in self._trae_available:
                    entry = self._trae_catalog.get(mid)
                    if not entry:
                        continue
                    trae_models.append(dict(entry, id="trae/" + mid, backend="trae",
                                            enabled=mid not in self._trae_disabled))
                trae_models.sort(key=lambda m: not m["enabled"])
                models.extend(trae_models)
            return {"updated": self._updated, "error": self._error, "models": models}

    def enabled_models(self):
        """Ids available to API clients: upstream list minus user-disabled (+ Trae 前缀表)。"""
        with self._lock:
            result = [mid for mid in self._available if mid not in self._disabled]
            if trae_enabled():
                result += ["trae/" + mid for mid in self._trae_available if mid not in self._trae_disabled]
            return result

    def snapshot_ids(self):
        """All known ids (enabled + disabled), for distinguishing 'disabled' from 'unknown'."""
        with self._lock:
            return set(self._available) | {"trae/" + mid for mid in self._trae_available}

    def set_enabled(self, mid, enabled):
        with self._lock:
            if mid.startswith(TRAE_PREFIX):
                bare = mid[len(TRAE_PREFIX):]
                if bare not in self._trae_catalog and bare not in self._trae_available:
                    raise KeyError(mid)
                self._trae_disabled.discard(bare) if enabled else self._trae_disabled.add(bare)
            else:
                if mid not in self._catalog and mid not in self._available:
                    raise KeyError(mid)
                self._disabled.discard(mid) if enabled else self._disabled.add(mid)

    def add(self, mid, enabled=True, **fields):
        """添加或更新一个模型。

        两种语义：

        - **上游已有的 id**：只覆盖用户可改的字段，`source` 保持原值，这样它继续
          跟着上游刷新（用户只是改了名字/单价或顺手关掉它）。
        - **上游没有的 id**：新建为 `source="custom"` 并记进 `_custom`，从此
          `fetch()` 会把它合并回来，不会冲掉。`available=1` 表示「用户认为它能用」。

        返回最终的 source。
        """
        with self._lock:
            known = mid in self._catalog
            source = normalize_source((self._catalog.get(mid) or {}).get("source"))
            if not known:
                source = SOURCE_CUSTOM
                self._custom.add(mid)
                self._available.append(mid)
                self._catalog[mid] = {"source": SOURCE_CUSTOM}
            entry = self._catalog[mid]
            for key, value in fields.items():
                if value is not None:
                    entry[key] = value
            if not entry.get("name"):
                entry["name"] = mid
            entry["source"] = source
            if enabled:
                self._disabled.discard(mid)
            else:
                self._disabled.add(mid)
            return source

    def remove(self, mid):
        """删除模型。只允许删自定义项。

        上游模型删了没用：下一次 fetch() 会把它原地带回来，而且会让
        request_logs.model_id 的 join 断掉（表里保留 available=0 的行正是为此）。
        自定义项必须同时从 _custom 移除，否则下次合并又把它加回来。
        """
        with self._lock:
            if mid not in self._catalog:
                raise KeyError(mid)
            if mid not in self._custom:
                raise ValueError("upstream")
            self._custom.discard(mid)
            self._catalog.pop(mid, None)
            self._available = [x for x in self._available if x != mid]
            self._disabled.discard(mid)

    def is_custom(self, mid):
        with self._lock:
            return mid in self._custom

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

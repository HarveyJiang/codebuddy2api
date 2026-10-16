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

from core.backends.registry import (
    backend_name_for,
    default_name as default_backend,
    is_enabled as is_backend_enabled,
    known_names as known_backends,
    spec_for,
    strip_prefix,
)

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

#: 排序第一层用的来源序号（specs/model-backend-attribution.md §6）：
#: 自定义最前、内置其次、上游最后。自定义排在前面是因为它们最需要被看见
#: —— 上游没列出、随时可能被误当成「已下线」；动态拉来的几百条垫底。
_SOURCE_RANK = {SOURCE_CUSTOM: 0, SOURCE_BUILTIN: 1, SOURCE_UPSTREAM: 2}


def backend_for_model_id(mid):
    """对外模型 id → 所属后端名（按各后端声明的前缀）。

    取代原先散落各处的 ``mid.startswith("trae/")`` 判断：前缀是后端自己的声明，
    加后端时这里不用改。
    """
    name = backend_name_for(mid)
    return name


def strip_model_prefix(mid):
    """对外模型 id → bare id（内存里各后端目录用的键）。"""
    return strip_prefix(mid)


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
    """模型目录：**每个后端各持一份**。

    内存结构是 ``_catalogs[backend] = {bare_id: info}`` + 每份的 available/disabled/
    updated/error。默认后端（workbuddy）与 Trae 对称存放，加第三个后端时这里一行不用改
    —— 结构从 ``BackendSpec`` 的后端列表上长出来。

    对外的 id 带后端前缀（``trae/glm-5.2``）以便客户端路由；内存里存 bare id。
    """

    def __init__(self, get_credential=None, clock=time.time, backend_credential_getter=None):
        """get_credential: callable returning a manager whose get_headers() yields auth headers
        （默认后端的目录凭据）。
        backend_credential_getter: ``callable(backend_name) -> manager``，非默认后端的
        目录凭据。取代了原先写死的 ``trae_credential_getter`` —— 加第三个后端时
        server 不用再传一个专用函数进来。
        """
        self.get_credential = get_credential
        self.backend_credential_getter = backend_credential_getter
        self.clock = clock
        # RLock: snapshot() 持锁调用 active_promo()，后者经模块级引用回到
        # promotions 属性再次取锁，非重入锁会自死锁，导致 /admin/api/overview
        # 永久挂起（登录成功但账号/模型全部加载不出）。
        self._lock = threading.RLock()
        self._promotions = []   # dynamic discounts parsed from the product config
        # backend -> 目录状态。默认后端也用同一个结构（不再有「主目录 + trae 附录」）。
        self._state: dict[str, dict] = {}
        self._default = default_backend()
        for name in known_backends():
            self._ensure_state(name)
        _registry_ref[0] = self

    # -- per-backend catalog state -----------------------------------------
    def _ensure_state(self, backend):
        st = self._state.get(backend)
        if st is None:
            st = self._state[backend] = {
                "catalog": {},      # bare id -> {name, description, credits, max_input_tokens, source}
                "available": [],    # ordered bare ids
                "disabled": set(),  # user-disabled bare ids
                "custom": set(),    # 用户手工添加的（只对默认后端有意义）
                "updated": 0,
                "error": None,
            }
        return st

    def _state_for(self, backend):
        return self._state.get(backend) or self._ensure_state(backend)

    def credential_getter_for(self, backend):
        """该后端拉目录用的凭据获取函数。

        只有声明了 ``needs_own_credential_for_catalog`` 的后端才拿自己的账号；
        其余（含默认后端）用 ``get_credential``。这样「拉目录要用哪个后端的账号」
        完全由后端声明决定，admin 不写死任何后端名。
        """
        spec = spec_for(backend)
        if spec.needs_own_credential_for_catalog and backend != self._default \
                and self.backend_credential_getter is not None:
            return lambda: self.backend_credential_getter(backend)
        return self.get_credential

    # -- 兼容层：默认后端的旧属性名 ---------------------------------------
    # 既有代码/测试直接读写这些名字（默认后端的目录就是主目录）。语义不变；
    # **新代码请用 _state_for(backend)**（多后端版本）。
    def _default_state(self):
        return self._state_for(self._default)

    @property
    def _catalog(self):
        return self._default_state()["catalog"]

    @_catalog.setter
    def _catalog(self, value):
        self._default_state()["catalog"] = value

    @property
    def _available(self):
        return self._default_state()["available"]

    @_available.setter
    def _available(self, value):
        self._default_state()["available"] = value

    @property
    def _disabled(self):
        return self._default_state()["disabled"]

    @_disabled.setter
    def _disabled(self, value):
        self._default_state()["disabled"] = value

    @property
    def _custom(self):
        return self._default_state()["custom"]

    @_custom.setter
    def _custom(self, value):
        self._default_state()["custom"] = value

    @property
    def _updated(self):
        return self._default_state()["updated"]

    @_updated.setter
    def _updated(self, value):
        self._default_state()["updated"] = value

    @property
    def _error(self):
        return self._default_state()["error"]

    @_error.setter
    def _error(self, value):
        self._default_state()["error"] = value

    # Trae 侧的旧属性名，同样保留（历史代码/测试用）。
    @property
    def _trae_state(self):
        return self._state_for("trae")

    @property
    def _trae_catalog(self):
        return self._trae_state["catalog"]

    @_trae_catalog.setter
    def _trae_catalog(self, value):
        self._trae_state["catalog"] = value

    @property
    def _trae_available(self):
        return self._trae_state["available"]

    @_trae_available.setter
    def _trae_available(self, value):
        self._trae_state["available"] = value

    @property
    def _trae_disabled(self):
        return self._trae_state["disabled"]

    @_trae_disabled.setter
    def _trae_disabled(self, value):
        self._trae_state["disabled"] = value

    @property
    def _trae_updated(self):
        return self._trae_state["updated"]

    @_trae_updated.setter
    def _trae_updated(self, value):
        self._trae_state["updated"] = value

    @property
    def _trae_error(self):
        return self._trae_state["error"]

    @_trae_error.setter
    def _trae_error(self, value):
        self._trae_state["error"] = value

    # -- persistence -------------------------------------------------------
    def load(self, db):
        """从 models 表恢复内存目录。只加载 available=1 的行。

        上游已消失的模型（available=0）只留在表里供 request_logs join，不进 registry：
        否则 snapshot_ids() 会把它们算成「已知」，PoolMiddleware 就会把「上游已下线」
        误报成「管理页已禁用」而拒掉请求。

        各后端的模型同表存放，model_id 带各自前缀（与对外暴露的 id 一致），
        加载时按前缀分派到对应后端的目录。
        """
        with self._lock:
            # include_custom：手工添加的行可能 available=0（上游没返回它），
            # 只按 available=1 加载会让它们在重启后消失。
            rows = db.load_models(only_available=True, include_custom=True)
            settings = db.get_settings() or {}
            for backend in list(self._state):
                prefix = spec_for(backend).model_prefix
                state = self._ensure_state(backend)
                state["catalog"], state["available"], state["disabled"] = {}, [], set()
                if backend == self._default:
                    state["custom"] = set()
                # 「上次拉取时刻」记在目录行自己身上（models.synced_at），
                # 而不是 settings 的固定列 —— 后端数量是可变的，settings 留不出列。
                state["updated"] = 0
                # 带前缀的后端只吃自己的行；默认后端吃所有**没有别的前缀**的行。
                # 归属读的是持久化的 `backend` 列（§3.1：列管存下来的行），
                # 不再逐行解析 model_id —— 前缀推导只留给请求路径。
                own = {mid: row for mid, row in rows.items()
                       if (row.get("backend") or backend_for_model_id(mid)) == backend}
                order_rows = sorted(own.items(), key=lambda kv: kv[1].get("sort_order", 0))
                for mid, row in order_rows:
                    bare = mid[len(prefix):] if prefix else mid
                    source = normalize_source(row.get("source"))
                    # available=0 的行不进 registry —— 否则 snapshot_ids() 会把它
                    # 算成「已知」，PoolMiddleware 会把「上游已下线」误报成「管理页
                    # 已禁用」而拒掉请求（见本方法 docstring）。
                    # 例外：**自定义模型**即使 available=0 也要加载 —— 上游没返回它
                    # 正是它的定义（用户手动加的），丢掉就等于删除。
                    # 真正「已删除」的行由 `deleted=1` 表达，load_models 已过滤掉，
                    # 不会走到这里。
                    if not row.get("available", 1) and source != SOURCE_CUSTOM:
                        state["updated"] = max(state["updated"], int(row.get("synced_at") or 0))
                        continue
                    state["catalog"][bare] = {"name": row.get("name") or bare,
                                              "description": row.get("description"),
                                              "credits": row.get("credits"),
                                              "max_input_tokens": row.get("max_input_tokens"),
                                              "source": source,
                                              "_promo": row.get("promo") if backend == self._default else None}
                    state["available"].append(bare)
                    # 自定义只对默认后端有意义（见 add()）：别的后端目录完全由上游
                    # 决定。非默认后端的自定义行不进 catalog，下次 persist 时会被标
                    # 成 available=0 保留行（供 request_logs join），不再对外可见。
                    if source == SOURCE_CUSTOM and backend == self._default:
                        state["custom"].add(bare)
                    if not row.get("enabled", 1):
                        state["disabled"].add(bare)
                    state["updated"] = max(state["updated"], int(row.get("synced_at") or 0))
                # 默认后端的同步时刻另有 settings.models_updated 一条既有契约。
                # 目录里一行都没有（拉过一次但上游返回空表）时，行上带不了这个值，
                # 回落到 settings —— 否则重启后会被当成「从没拉过」而反复重拉。
                if backend == self._default and not state["updated"]:
                    state["updated"] = settings.get("models_updated", 0) or 0

    def persist(self, db):
        """把当前目录写回 models 表（UPSERT 不删行）。

        fetch() 拉到的 available 列表之外、上次存在但这次没出现的模型标 available=0 保留，
        保证 request_logs.model_id 长期可 join。用户禁用状态 enabled 只由用户操作改写。

        Trae 目录以 `trae/` 前缀 id 写入同一张表，available/enabled 语义与
        WorkBuddy 行一致（禁用 = enabled=0）。
        """
        with self._lock:
            rows = {}
            for backend in list(self._state):
                state = self._ensure_state(backend)
                prefix = spec_for(backend).model_prefix
                catalog, available = state["catalog"], state["available"]
                disabled, custom = state["disabled"], state["custom"]
                # 不在 available 的行从末尾开始编号，保持稳定（同一序位会让 reload
                # 时的相对顺序随机 —— sorted 稳定但输入无序）。
                tail = len(available)
                for bare, info in catalog.items():
                    # source 必须忠实写回：自定义项若被写成 upstream，重启后 custom
                    # 就空了，下一次 fetch() 会把它当「上游已消失」而冲掉。
                    source = normalize_source(info.get("source"))
                    if bare in custom:
                        source = SOURCE_CUSTOM
                    if bare in available:
                        sort_order = available.index(bare)
                    else:
                        tail += 1
                        sort_order = tail
                    rows[prefix + bare] = {
                        "name": info.get("name") or bare,
                        "description": info.get("description"),
                        "credits": info.get("credits"),
                        "max_input_tokens": info.get("max_input_tokens"),
                        "source": source,
                        "available": 1 if bare in available else 0,
                        "enabled": 0 if bare in disabled else 1,
                        "sort_order": sort_order,
                        # 归属：循环变量在这里**已知**，直接存列（§3.1）。
                        # 与 model_id 前缀同源同值，drift 防护测试钉住这一点。
                        "backend": backend,
                        "promo": info.get("_promo") if backend == self._default else None,
                        # 每份目录的「上次拉取时刻」写在行上，重启后各后端各归各的。
                        "synced_at": state["updated"],
                    }
            db.save_models(rows)
            # settings.models_updated 继续写默认后端的值：它是既有对外契约
            # （迁移回填与老版本读它），不能因为换列就断掉。
            db.update_settings({"models_updated": self._state_for(self._default)["updated"]})

    # -- upstream fetch ----------------------------------------------------
    def fetch(self):
        """Pull the live product config. Returns True on success. Never raises.

        守卫：默认后端被总闸关掉时不拉（specs/backend-toggle.md §2 第 4 条）。
        放在方法内部而不是每个调用方，是为了让「不碰上游」这条规则只有一处。

        Endpoint: GET https://www.workbuddy.cn/v3/config — the gateway checks the
        CLI User-Agent (a fake bearer alone is not enough): without valid account
        credentials it answers with an empty shell (agents/models all null), so
        an enabled account's headers are sent when available. The desktop client
        caches this response to .workbuddy/cache/acc-product-config-v3.json. It
        carries data.agents (the cli agent's models array is the official UI
        menu), data.models (credits/maxInputTokens/description...) and
        data.modelPromotions.
        """
        if not is_backend_enabled(self._default):
            return False
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

    # -- 各后端的静态兜底与动态刷新 ------------------------------------------
    def ensure_static_catalog(self, backend):
        """无动态目录时用该后端声明的 ``static_catalog`` 兜底。

        静态表由后端自己声明（``BackendSpec.static_catalog``），admin 不再知道
        「Trae 有 20 条静态模型」这件事 —— 加后端时这里不用改。
        默认后端的静态兜底是内置官方列表，走 ensure_builtin()。
        """
        if backend == self._default:
            self.ensure_builtin()
            return
        spec = spec_for(backend)
        with self._lock:
            state = self._ensure_state(backend)
            if state["available"]:
                return
            state["catalog"] = {m["id"]: {"name": m.get("name") or m["id"],
                                          "description": m.get("description"),
                                          "credits": m.get("credits"),
                                          "max_input_tokens": m.get("context_length"),
                                          "max_context_length": m.get("max_context_length"),
                                          "source": SOURCE_BUILTIN}
                                for m in spec.static_catalog}
            state["available"] = [m["id"] for m in spec.static_catalog]
            state["updated"] = 0

    def refresh_catalog(self, backend):
        """拉取该后端的动态目录（需一个该后端的启用账号）。

        失败保留上次快照；从未拉到过则维持静态兜底。返回是否拉到新表。
        传输细节（打哪个接口、怎么鉴权）在后端自己那里，admin 只负责挑凭据、
        存结果、算 updated/error —— 所以加后端时这个方法不用改。
        """
        spec = spec_for(backend)
        # 总闸关掉的后端不拉目录（specs/backend-toggle.md §2 第 4 条）：
        # 连上游都不碰 —— 拉回来也是白存一份没人用的数据，还白花一次请求。
        if not is_backend_enabled(backend):
            return False
        if not spec.refreshable_catalog or not spec.catalog_method:
            return False
        getter = self.credential_getter_for(backend)
        manager = None
        if getter is not None:
            try:
                manager = getter()
            except Exception:
                manager = None
        if manager is None:
            return False
        try:
            entries = spec.fetch_catalog(manager)
        except Exception as e:
            with self._lock:
                self._ensure_state(backend)["error"] = f"拉取失败：{type(e).__name__}"
            return False
        if not entries:
            return False
        with self._lock:
            state = self._ensure_state(backend)
            catalog = {}
            order = []
            for e in entries:
                mid = str(e.get("id") or "").strip()
                if not mid:
                    continue
                catalog[mid] = {"name": str(e.get("name") or mid),
                                "description": e.get("description") or None,
                                "credits": e.get("credits"),
                                "max_input_tokens": e.get("context_length"),
                                "max_context_length": e.get("max_context_length"),
                                "source": SOURCE_UPSTREAM}
                order.append(mid)
            if not catalog:
                return False
            state["catalog"] = catalog
            state["available"] = order
            # 上游已下线的模型不再保留禁用状态（重新上线时默认启用）。
            state["disabled"] &= set(order)
            state["updated"] = int(self.clock())
            state["error"] = None
        return True

    def catalog_entries(self, backend):
        """供后端自己的目录提供者用：bare id 列表（含被禁用的 —— 禁用只挡客户端请求）。"""
        with self._lock:
            state = self._state_for(backend)
            return [{"id": mid,
                     "name": (state["catalog"].get(mid) or {}).get("name") or mid,
                     "context_length": (state["catalog"].get(mid) or {}).get("max_input_tokens") or 131072}
                    for mid in state["available"] if mid in state["catalog"]]

    def catalog_updated(self, backend):
        """该后端目录的「上次拉取时刻」；0 表示**从没拉到过**（仍是静态兜底）。

        启动路径据此判断要不要拉一次动态目录（见 ``server._refresh_missing_catalogs``）：
        0 才拉，已有快照就不打扰上游。对外提供这个读数是为了让 server 侧不必
        伸手进 ``_state`` 私有结构。
        """
        with self._lock:
            return int(self._state_for(backend)["updated"])

    # -- queries -----------------------------------------------------------
    def snapshot(self):
        """Full catalog with management flags and current promo, for the admin UI.

        排序规则（四层，specs/model-backend-attribution.md §6，用户已拍板）：

        1. **来源**：custom(0) → builtin(1) → upstream(2)。自定义永远在最前，
           内置其次，动态拉取的最后；
        2. **归属**：注册表顺序（``BackendSpec.order``，默认后端在最前）；
        3. **启用**：启用的在前，禁用的沉底；
        4. **倍率**：``credits`` 升序；``None``（按标准计费）视为 0，与免费并列最前。
           同分用目录原顺序（``available`` 下标）稳定决胜。

        **第 4 层的倍率取「同名组内的最小值」**，不是单条自己的值（用户 2026-10-16
        拍板：「如果有倍率，选择较小的一个进行排」）。理由：管理页把同名模型跨后端
        合并成**一行**（`groupModelsByName`），一行的排序键必须唯一 —— 用组内最小值
        才与「这一行最便宜多少钱能买到」的直觉一致。组名 = 去掉后端前缀的 bare id。

        来源之所以是**第一层**：用户手动加的、上游没列出的模型最需要被看见
        （它们随时可能被误当成「上游没了」而找不到）；动态拉来的几百条垫底。

        后端顺序来自注册表声明，不再是「默认后端 + 追加 Trae」的写死两段式。
        """
        now = datetime.now(CN)
        models = []
        updated, error = 0, None
        with self._lock:
            for backend in known_backends():
                if not is_backend_enabled(backend):
                    continue
                state = self._state_for(backend)
                prefix = spec_for(backend).model_prefix
                available, catalog = state["available"], state["catalog"]
                for bare in available:
                    if bare not in catalog:
                        continue
                    entry = dict(catalog[bare], id=prefix + bare,
                                 enabled=bare not in state["disabled"],
                                 backend=backend)
                    if backend == self._default:
                        entry["custom"] = bare in state["custom"]
                    entry["source"] = normalize_source(entry.get("source"))
                    stored = entry.pop("_promo", None)
                    # promo 列存的是上游活动文案（只做展示）；折后价仍由内置 PROMOTIONS
                    # 在读取时算，两者不一致时以内置表为准。
                    promo = active_promo(prefix + bare, now)
                    if promo and isinstance(entry.get("credits"), (int, float)):
                        entry["promo"] = {"label": promo[0] or stored or "折扣", "factor": promo[1],
                                          "credits": round(entry["credits"] * promo[1], 4)}
                    elif stored and isinstance(entry.get("credits"), (int, float)):
                        entry["promo"] = {"label": stored, "factor": 1.0, "credits": entry["credits"]}
                    # 目录原顺序（稳定决胜用）：取该后端 available 里的下标。
                    entry["_seq"] = available.index(bare)
                    # 同名组键：**去前缀的 bare 名**（`trae/glm-5.3` 与 `glm-5.3`
                    # 归同一组）。第 4 层倍率取组内最小值，故必须先有组键。
                    entry["_group"] = bare
                    models.append(entry)
                if backend == self._default:
                    updated, error = state["updated"], state["error"]

            # 第 4 层用「同名组内的最小倍率」（见本方法 docstring 末尾）。先按
            # 去前缀的 bare 名分组算一遍 —— 不分后端：同一个模型在 WorkBuddy 与
            # Trae 上的价格要一起比，取更便宜的那个作为这一行的代表价。
            group_min: dict[str, float] = {}
            for m in models:
                group = m["_group"]
                cr = m["credits"] if isinstance(m.get("credits"), (int, float)) else 0
                cur = group_min.get(group)
                if cur is None or cr < cur:
                    group_min[group] = cr

            models.sort(key=lambda m: (
                _SOURCE_RANK[m["source"]],          # ① 来源
                spec_for(m["backend"]).order,       # ② 归属（注册表顺序）
                not m["enabled"],                   # ③ 启用优先
                group_min[m["_group"]],             # ④ 倍率（同名组内最小值）
                m["_seq"],                          # 稳定决胜：同分保持目录原顺序
            ))
            for m in models:
                m.pop("_seq", None)
                m.pop("_group", None)
            # updated/error 报告**默认后端**的刷新状态：管理页的「上次拉取」提示
            # 说的是主目录；各后端的细节在各自的目录提供者里。
            return {"updated": updated, "error": error, "models": models}

    def enabled_models(self):
        """Ids available to API clients: 各后端 enabled 的模型（带各自前缀）。"""
        with self._lock:
            out = []
            for backend in known_backends():
                if not is_backend_enabled(backend):
                    continue
                state = self._state_for(backend)
                prefix = spec_for(backend).model_prefix
                out += [prefix + mid for mid in state["available"] if mid not in state["disabled"]]
            return out

    def snapshot_ids(self):
        """All known ids (enabled + disabled), for distinguishing 'disabled' from 'unknown'."""
        with self._lock:
            out = set()
            for backend in known_backends():
                state = self._state_for(backend)
                prefix = spec_for(backend).model_prefix
                out |= {prefix + mid for mid in state["available"]}
            return out

    def set_enabled(self, mid, enabled):
        backend = backend_for_model_id(mid)
        prefix = spec_for(backend).model_prefix
        bare = mid[len(prefix):] if prefix else mid
        with self._lock:
            state = self._state_for(backend)
            if bare not in state["catalog"] and bare not in state["available"]:
                raise KeyError(mid)
            if enabled:
                state["disabled"].discard(bare)
            else:
                state["disabled"].add(bare)

    def add(self, mid, enabled=True, **fields):
        """添加或更新一个模型。

        两种语义：

        - **上游已有的 id**：只覆盖用户可改的字段，`source` 保持原值，这样它继续
          跟着上游刷新（用户只是改了名字/单价或顺手关掉它）。
        - **上游没有的 id**：新建为 `source="custom"` 并记进 `_custom`，从此
          `fetch()` 会把它合并回来，不会冲掉。`available=1` 表示「用户认为它能用」。

        返回最终的 source。

        自定义模型只对**默认后端**有意义：其它后端的目录完全由上游决定
        （用户不该往 Trae 目录里塞一个上游没有的模型 —— 那个模型调不通）。
        """
        backend = backend_for_model_id(mid)
        prefix = spec_for(backend).model_prefix
        bare = mid[len(prefix):] if prefix else mid
        with self._lock:
            state = self._state_for(backend)
            catalog, available = state["catalog"], state["available"]
            known = bare in catalog
            source = normalize_source((catalog.get(bare) or {}).get("source"))
            if not known:
                source = SOURCE_CUSTOM
                state["custom"].add(bare)
                available.append(bare)
                catalog[bare] = {"source": SOURCE_CUSTOM}
            entry = catalog[bare]
            for key, value in fields.items():
                if value is not None:
                    entry[key] = value
            if not entry.get("name"):
                entry["name"] = bare
            entry["source"] = source
            if enabled:
                state["disabled"].discard(bare)
            else:
                state["disabled"].add(bare)
            return source

    def remove(self, mid, db=None):
        """删除模型。只允许删自定义项。

        上游模型删了没用：下一次 fetch() 会把它原地带回来，而且会让
        request_logs.model_id 的 join 断掉（表里保留 available=0 的行正是为此）。
        自定义项必须同时从 custom 移除，否则下次合并又把它加回来。

        ``db`` 必须传：删除后这个 id 已不在内存 catalog 里，`persist()` 再也
        写不到它 —— 库里会留着上一次写的 available=1，重启时 `load()`（只读
        available=1）把它**原样装回来**，删除形同虚设。所以要在这里就地把它
        落成 available=0（保留行供 request_logs join）。
        """
        backend = backend_for_model_id(mid)
        prefix = spec_for(backend).model_prefix
        bare = mid[len(prefix):] if prefix else mid
        with self._lock:
            state = self._state_for(backend)
            if bare not in state["catalog"]:
                raise KeyError(mid)
            if bare not in state["custom"]:
                raise ValueError("upstream")
            entry = dict(state["catalog"][bare])
            state["custom"].discard(bare)
            state["catalog"].pop(bare, None)
            state["available"] = [x for x in state["available"] if x != bare]
            state["disabled"].discard(bare)
        if db is not None:
            # 行保留（供 request_logs join），但标 deleted=1：load_models 不再返回它，
            # 重启也不会复活。**不能**只标 available=0 —— 那个语义归「上游没返回
            # 的自定义模型要保留」所有，两者混用会让删除与保留互相打架。
            db.save_models({mid: {
                "name": entry.get("name") or mid,
                "description": entry.get("description"),
                "credits": entry.get("credits"),
                "max_input_tokens": entry.get("max_input_tokens"),
                "source": SOURCE_CUSTOM,
                "available": 0,
                "enabled": 0,
                "backend": backend,
                "deleted": 1,
            }})

    def is_custom(self, mid):
        backend = backend_for_model_id(mid)
        prefix = spec_for(backend).model_prefix
        bare = mid[len(prefix):] if prefix else mid
        with self._lock:
            return bare in self._state_for(backend)["custom"]

    def info(self, mid):
        backend = backend_for_model_id(mid)
        prefix = spec_for(backend).model_prefix
        bare = mid[len(prefix):] if prefix else mid
        with self._lock:
            return dict(self._state_for(backend)["catalog"].get(bare) or {})


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

"""Account-scoped billing, refresh, daily check-in and request routing."""
import asyncio
import hashlib
import json
import logging
import random
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from admin.cost_ledger import CostLedger
from admin.guards import (
    DailyUsage,
    DailyUsageCache,
    any_enabled,
    guard_block_reason,
    parse_guard_limits,
)
from admin.reset_at import earliest_deadline, parse_reset_at, parse_retry_after
from admin.rotation import WAF_IP_WINDOW, WafIpGate, backoff_after, is_waf_error
from core.bare_routing import (
    BackendOrderSpec,
    Candidate,
    Rejection,
    build_order,
    decide,
    NoCandidate,
)
from core.backends.registry import (
    backend_name_for,
    default_name as default_backend,
    is_enabled as is_backend_enabled,
    known_names as known_backends,
    pinned_model_owner,
    spec_for,
)

REQUEST_CREDENTIAL = ContextVar("pool_credential", default=None)
# 池中间件观察到的路由信息，交由外层 MetricsMiddleware 合并成一行 request_logs。
# 用 scope 传递而不是 ContextVar：middleware 是嵌套调用，内层 finally 的 reset
# 会先于外层读取执行，ContextVar 到外层就变回默认值了（实测确认）。
# scope 是同一条链上共享的可变对象，不受 finally 时序影响。
REQUEST_LOG_CONTEXT = "codebuddy.request_log"
#: 裸名路由的选定后端（§5），由池中间件写、converter 读。
#:
#: 必须共享：中间件在选号**之前**定后端（选号要按 backend 过滤），converter
#: 的 `resolve_backend` 也要给出同一个后端——否则「凭据-后端一致性闸门」
#: 会发现「选定后端 ≠ 账号后端」而回 503，裸名请求全挂。两处各算一遍会
#: 分叉（中间隔着选号与可能的排队换号），所以只算一次、放在 scope 上。
ROUTED_BACKEND = "codebuddy.routed_backend"
_logger = logging.getLogger("admin.pool")
CN = timezone(timedelta(hours=8))
BILLING = "https://www.codebuddy.cn/v2/billing/meter/"
# 上游用量明细接口在另一个域、且没有 /v2 前缀，不能复用 BILLING 常量。
# 注意参数名是驼峰 pageNum/pageSize（与同域 get-user-resource 的 PageNumber/PageSize 不同）。
USAGE_BILLING = "https://www.workbuddy.cn/billing/meter/"
USAGE_PAGE_SIZE = 100
USAGE_SYNC_DAYS = 30   # 拉取窗口必须 ≥ 同步周期，否则连续断档会留永久空洞
EXPIRY_SOON_DAYS = 7
# 三档轮转策略。round_robin 按权重+最早过期排序后用游标轮转；
# random 随机；expiry_first 只看最早过期、不看权重。
ROUTING_POLICIES = frozenset(("round_robin", "random", "expiry_first"))


def number(value):
    if isinstance(value, bool):
        raise ValueError("invalid number")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid number")
    if not n.is_finite():
        raise ValueError("invalid number")
    return max(Decimal(0), n)


def _same_day(timestamp, now):
    """timestamp 是否是 now 所在的北京时间那一天（按天判断，避免同一天重复拉取）。"""
    if not timestamp:
        return False
    try:
        day = datetime.fromtimestamp(timestamp, CN).date()
        today = datetime.fromtimestamp(now, CN).date()
    except (TypeError, ValueError, OSError):
        return False
    return day == today


def package_expiry(p):
    """Parse CycleEndTime (Beijing time) into a unix timestamp; None if absent/invalid."""
    value = p.get("CycleEndTime")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp()
    except ValueError:
        return None


def summarize_packages(packages, now=None):
    """Aggregate credits. `expiring_soon` sums the spendable balance of every package whose
    CycleEndTime falls within the next `EXPIRY_SOON_DAYS` days."""
    now = time.time() if now is None else now
    cutoff = now + EXPIRY_SOON_DAYS * 86400
    remain = Decimal(0)
    size = Decimal(0)
    earliest = None
    earliest_remaining = None
    expiring_soon = Decimal(0)
    for p in packages:
        # Precise values carry fractional credit balances; cycle values are spendable now.
        prefix = "Cycle" if any(k in p for k in ["CycleCapacityRemainPrecise", "CycleCapacityRemain"]) else ""
        def field(name, default=None):
            return p.get(prefix + name + "Precise", p.get(prefix + name, default))
        r = number(field("CapacityRemain"))
        capacity = number(field("CapacitySize", r))
        spendable = min(r, capacity) if capacity else r
        remain += spendable
        size += capacity
        if spendable > 0:
            expiry = package_expiry(p)
            if expiry is not None:
                if earliest is None or expiry < earliest:
                    earliest, earliest_remaining = expiry, float(spendable)
                if now < expiry <= cutoff:
                    expiring_soon += spendable
    return {"remaining": float(remain), "packages": len(packages),
            "earliest_expiry": earliest, "earliest_expiry_remaining": earliest_remaining,
            "expiring_soon": float(expiring_soon), "expiring_soon_updated": int(now)}


class BillingError(Exception):
    def __init__(self, message, status=502, code=None):
        super().__init__(message)
        self.status, self.code = status, code


# 免费模型降级时的「同价位」上界（倍率）。0 倍率模型（如 Step-5-Preview）
# 排队的概率最高，此时把免费与低价档合并成一档，才换得到旁边那些便宜模型。
FREE_TIER_CEILING = 0.10


CREDIT_FIELDS = ("credit", "credits", "total_credit", "credit_used", "cost")


def _credits_of(usage):
    """从 usage 里取实测 credit；取不到返回 0.0（护栏按「没花钱」计）。

    字段名逐个试、试不到就当 0：护栏的 credit 计数是**辅助**维度，
    拿到 0 不会让护栏放行本该拦下的请求（token 计数仍然可靠）。
    """
    if not isinstance(usage, dict):
        return 0.0
    for field in CREDIT_FIELDS:
        if field in usage:
            try:
                return float(usage[field])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


class AccountPool:
    # 池设置默认值。Store._load 用它们补齐旧数据缺失的字段，
    # 这里保留 setdefault 兼容直接构造 Store 的老用法。
    # routing 不在这里：全局默认改由 settings.default_routing 提供，每 key 的策略在 keys.routing。
    POOL_DEFAULTS = {"auto_checkin": True, "checkin_time": "09:00",
                     "affinity_expiry_days": 3, "credit_min_interval": 60,
                     "sync_start": "10:00", "sync_end": "20:00", "batch_spread": 90,
                     "default_routing": "round_robin",
                     # 容量队列自动切换（specs/queue-failover.md）。默认关闭：这是
                     # 唯一会「重放整个请求」的功能，出问题的影响面比其它设置大，
                     # 让用户在管理页显式打开。
                     "queue_failover": False,
                     "queue_retry": 3,
                     "queue_cooldown": 120,
                     "queue_hold_ms": 3000,
                     "queue_model_fallback": True,
                     "queue_fallback_model": "fast-model",
                     # 数据库备份（admin/backup.py）。默认关闭：定时备份持续占磁盘，
                     # 开不开由用户决定，不因升级替人做主。
                     "backup_enabled": False,
                     "backup_interval_hours": 24,
                     "backup_keep": 10,
                     "backup_last_at": 0,
                     "backup_last_name": "",
                     # 账号级限额护栏（admin/guards.py）。**默认全 0 = 关闭**：
                     # 护栏会主动拒绝请求，是「替用户做决定」的功能，默认必须无为。
                     "guard_reserve_credits": 0,
                     "guard_daily_token_limit": 0,
                     "guard_daily_credit_limit": 0,
                     "guard_model_daily_token_limit": 0}

    # 设置项分类：管理页据此把「通用/全局」的设置搬到独立设置页，账号池页只留
    # 与账号列表直接相关的。放在这里而不是前端，是因为校验与默认值都在后端，
    # 分类口径跟着走才不会两头对不上。
    SETTING_GROUPS = {
        "checkin": ("auto_checkin", "checkin_time", "batch_spread",
                    "credit_min_interval", "sync_start", "sync_end"),
        "queue": ("queue_failover", "queue_model_fallback", "queue_retry",
                  "queue_cooldown", "queue_hold_ms", "queue_fallback_model"),
        "routing": ("default_routing",),
        "guards": ("guard_reserve_credits", "guard_daily_token_limit",
                   "guard_daily_credit_limit", "guard_model_daily_token_limit"),
        "backup": ("backup_enabled", "backup_interval_hours", "backup_keep",
                   "backup_last_at", "backup_last_name"),
    }

    def __init__(self, store, clock=time.time, client_factory=None, model_registry=None):
        self.store, self.clock = store, clock
        # 模型注册表由外部注入（server.py 里先建池后建注册表，建好后回填）。
        # 降级模型要用它读倍率；核心账号池不该反向依赖 admin.models。
        self.model_registry = model_registry
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=20, follow_redirects=False))
        self.locks = {}
        self.cursor = 0
        self.jobs = asyncio.Lock()
        self.affinity_hits = 0
        self.affinity_rebinds = 0
        self.dirty = False
        self.last_flush = 0
        # 实测成本账本（admin/cost_ledger.py）。观测不到上游价就完全不生效。
        self.cost_ledger = CostLedger(clock=clock)
        # 账号级限额护栏的用量缓存（admin/guards.py）。护栏全关时不查库。
        self.guard_usage = DailyUsageCache(clock=clock)
        # WAF IP 级拦截状态机（admin/rotation.py）。进程内状态，窗口 60 秒。
        self.waf_gate = WafIpGate(clock=clock)
        self._usage_manual_at = {}   # aid -> 上次手动拉取时间戳，进程内节流
        with store.lock:
            store.data.setdefault("pool", dict(self.POOL_DEFAULTS))
            store.data["pool"] = {**self.POOL_DEFAULTS, **store.data["pool"]}
            store.data.setdefault("account_status", {})
            store.data.setdefault("session_bindings", {})
            # 只在真的补了缺失字段时才落库：否则每次启动都全量重写
            # accounts/keys 两张表，纯属白写。
            filled = False
            for item in store.data.get("keys", {}).values():
                for field, default in (("daily_limit", None), ("usage", {}), ("enabled", 1),
                                       ("revoked_at", None), ("expires_at", None), ("routing", None)):
                    if field not in item:
                        item[field] = default
                        filled = True
            for item in store.data["accounts"].values():
                if "backend" not in item:
                    item["backend"] = default_backend()
                    filled = True
            if filled:
                store.keys_dirty = True
                store.save()

    def operation_lock(self, aid):
        with self.store.lock:
            return self.locks.setdefault(aid, threading.Lock())

    def snapshot(self, aid):
        with self.store.lock:
            item = self.store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            return dict(item), dict(self.store.data["account_status"].get(aid, {}))

    def update(self, aid, **values):
        with self.store.lock:
            if aid in self.store.data["accounts"]:
                status = self.store.data["account_status"].setdefault(aid, {})
                status.update(values)
                self.dirty = True

    def flush(self, force=False):
        """Debounced persistence: per-request updates only mark dirty; tick flushes every 5s.

        触发条件除 pool.dirty 外，还要覆盖 keys 用量（keys_dirty）与 request_logs 缓冲——
        record_key_usage 现在只改内存，不落库，漏了它们计数就永远不会写进库。
        """
        with self.store.lock:
            pending = (self.dirty or self.store.keys_dirty or self.store.log_buffer)
            if pending and (force or self.clock() - self.last_flush >= 5):
                self.store.save()
                self.dirty = False
                self.last_flush = self.clock()

    def state(self, row):
        status = self.store.data["account_status"].get(row["id"], {})
        if not row["enabled"]:
            return "paused"
        if row["status"] == "invalid" or status.get("auth_invalid"):
            return "invalid"
        if (status.get("cooldown_until") or 0) > self.clock():
            return "cooling"
        if status.get("credits_updated", 0) > self.clock() - 600 and status.get("remaining") == 0 and status.get("packages", 0) > 0:
            return "exhausted"
        return "available"

    def rows(self, rows):
        today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
        for row in rows:
            status = self.store.data["account_status"].get(row["id"], {})
            row.update({"pool_state": self.state(row), "remaining": status.get("remaining"),
                        "credits_updated": status.get("credits_updated"),
                        "credits_stale": status.get("credits_updated", 0) < self.clock() - 600,
                        "earliest_expiry": status.get("earliest_expiry"),
                        "earliest_expiry_remaining": status.get("earliest_expiry_remaining"),
                        "expiring_soon": status.get("expiring_soon"),
                        "expiring_soon_updated": status.get("expiring_soon_updated"),
                        "weight": int(status.get("weight", 0)),
                        "checkin_slot": status.get("checkin_slot"), "checkin_when": status.get("checkin_when"),
                        "today_checked_in": status.get("checkin_date") == today,
                        "cooldown_until": status.get("cooldown_until", 0),
                        "queued_until": status.get("queued_until", 0),
                        "last_error": status.get("last_error"), "token_refreshed": status.get("token_refreshed"),
                        "request_count": status.get("request_count", 0),
                        # 上游用量同步留痕：不吐给 overview 的话「留痕」没人看得到，
                        # 出了同步故障只能翻库，等于没有这个功能。
                        "usage_synced_at": status.get("usage_synced_at"),
                        "usage_sync_error": status.get("usage_sync_error")})
        return rows

    # -- 容量队列（specs/queue-failover.md） --------------------------------

    # 明确**不**算排队的码：11101 是「非流式请求不支持」的本地/上游语义错误，
    # USER_LIMIT 是个人额度到顶——两者换号换模型都救不了，必须让用户看到。
    QUEUE_EXCLUDED_CODES = {"11101", "USER_LIMIT"}
    # 视为排队的错误码：前缀匹配上游繁忙码，后缀匹配 HTTP 限流码。
    QUEUE_CODES = {"WAITING", "FULL", "QUEUE", "QUEUED", "BUSY", "OVERLOAD",
                   "11102", "11103", "11001", "50001", "429", "503"}
    # 文案兜底。**只作兜底**：主判据是结构化 code，上游改文案不该让功能失效，
    # 但反过来，只靠文案会在上游换措辞时静默失灵，所以两者都要有。
    QUEUE_PATTERNS = ("当前模型资源紧张", "资源紧张", "前方约", "位用户排队", "用户排队",
                      "优先通道", "排队", "繁忙", "拥堵", "稍后重试", "限流",
                      "busy", "overload", "too many", "queue", "rate limit")

    def _pool_cfg(self):
        return self.store.data.get("pool", {})

    def is_queue_error(self, status=None, text="", code=None):
        """判断上游响应是否属于容量队列（可换号/降级重试）。

        形态全兼容：上游可能以非 200 返回，也可能 200 + SSE 帧里带 code，
        见 spec 4.3。返回 True 只代表「可以安全地重放」，不代表重放一定成功。
        """
        code_text = "" if code is None else str(code).strip()
        if code_text and code_text.upper() in self.QUEUE_EXCLUDED_CODES:
            return False
        if status in (429, 503):
            return True
        if code_text and code_text.upper() in self.QUEUE_CODES:
            return True
        if text:
            lowered = str(text).lower()
            for pattern in self.QUEUE_PATTERNS:
                if pattern.lower() in lowered:
                    return True
        return False

    def mark_queued(self, aid, seconds=None):
        """账号命中容量队列：让位 N 秒（不是硬冷却）。

        刻意不写 cooldown_until：那是认证/限流失败的硬冷却，会让账号从候选集里
        整个消失；让位只是「优先跳过」，队列消化完就该继续用。
        """
        cfg = self._pool_cfg()
        seconds = int(seconds or cfg.get("queue_cooldown", 120))
        with self.store.lock:
            if aid not in self.store.data["accounts"]:
                return
            data = self.store.data["account_status"].setdefault(aid, {})
            data["queued_until"] = self.clock() + max(1, seconds)
            data["last_error"] = f"上游排队，已让位 {seconds} 秒"
            self.dirty = True

    def queued(self, aid):
        return self.store.data["account_status"].get(aid, {}).get("queued_until", 0) > self.clock()

    def fallback_models(self, current_model, tried=(), backend=None):
        """按「同价位免费模型 → 快速路由兜底」给出降级候选（spec 4.5）。

        返回 id 列表，已试过的排除。管理后台未加载注册表时返回空列表——
        此时降级模型无从判断，退回纯换号行为比瞎猜模型名安全。

        只能在同一 backend 内换模型：快照里 workbuddy 与 trae 的模型混在一起
        （trae 带 ``trae/`` 前缀），而换模型后仍然用**原 backend** 的账号去请求
        （见 PoolMiddleware 的 try_select(..., backend=backend)）。若不按 backend
        过滤，workbuddy 请求会降级成 ``trae/xxx``，然后拿腾讯账号去要字节的模型，
        上游必然 4xx——这正是后端隔离要防的那类串扰。
        """
        registry = self.model_registry
        if registry is None or not self._pool_cfg().get("queue_model_fallback", True):
            return []
        tried = {m for m in (tried or ()) if m}
        if current_model:
            tried.add(current_model)
        snapshot = {entry["id"]: entry for entry in registry.snapshot()["models"]}
        base = snapshot.get(current_model) or {}
        base_credits = base.get("credits") if isinstance(base.get("credits"), (int, float)) else None

        def billable(entry):
            """免费模型优先；倍率缺失时按标准计费，与 snapshot 的排序口径一致。"""
            credits = entry.get("credits")
            return credits if isinstance(credits, (int, float)) else 0.0

        def same_backend(entry):
            # 未指定 backend（老调用方/测试）时不过滤，保持原有行为。
            # 快照条目一律带 backend（models.snapshot 显式写入），缺失时按 workbuddy。
            return backend is None or entry.get("backend", default_backend()) == backend

        candidates = [entry for entry in snapshot.values()
                      if entry.get("enabled") and entry["id"] not in tried
                      and same_backend(entry)]
        if base_credits is not None and base_credits > 0:
            # ±50% 视为同档：上游倍率是离散小数，精确相等会几乎选不中。
            low, high = base_credits * 0.5, base_credits * 1.5
            same_tier = [entry for entry in candidates if low <= billable(entry) <= high]
        elif base_credits == 0:
            # 免费模型（倍率 0）的同档 = 免费 + 低价档。0 的 ±50% 还是 0，
            # 只认 0 会退化成「只有自己」，而用户排队时恰恰需要换到便宜的邻居模型。
            same_tier = [entry for entry in candidates if billable(entry) <= FREE_TIER_CEILING]
        else:
            # 当前模型不在注册表里（上游没返回、也没手工加）：无法判档，
            # 给全部候选按价格升序，让最省的先试。
            same_tier = candidates
        same_tier.sort(key=lambda entry: (billable(entry), entry["id"]))

        ordered = [entry["id"] for entry in same_tier]
        # 兜底模型不参与上面的排序：它只在同档都排过队后才用一次。
        # 同样要过 backend 检查：用户在设置里写的兜底模型可能属于另一个后端，
        # 用它就会拿腾讯账号去要字节模型。
        fallback = self._pool_cfg().get("queue_fallback_model") or "fast-model"
        if (fallback not in tried and fallback in snapshot and fallback not in ordered
                and same_backend(snapshot[fallback])):
            ordered.append(fallback)
        return ordered

    def order_candidates(self, candidates, routing="round_robin"):
        """round_robin：权重降序 + 最早过期升序；expiry_first：只看最早过期。

        未知到期时间在同权重内排最后。unknown expiry sorts last。
        """
        def weight_key(row):
            return -int(self.store.data["account_status"].get(row["id"], {}).get("weight", 0))

        def expiry_key(row):
            expiry = self.store.data["account_status"].get(row["id"], {}).get("earliest_expiry")
            return (expiry is None, expiry or 0)

        if routing == "expiry_first":
            return sorted(candidates, key=expiry_key)
        return sorted(candidates, key=lambda row: (weight_key(row), *expiry_key(row)))

    def _routing_for(self, kid):
        """该 key 的轮转策略；NULL 动态跟随 settings.default_routing。"""
        item = self.store.data["keys"].get(kid) or {}
        policy = item.get("routing") or self.store.data["pool"].get("default_routing", "round_robin")
        return policy if policy in ROUTING_POLICIES else "round_robin"

    def select(self, affinity_key=None, kid=None, exclude=None, model=None, backend=None):
        """按 key 选账号：候选集 = key 的 accounts 白名单 ∩ 可用账号。

        kid=None 表示无 key 路径（管理页测试请求），此时不施加白名单。凭据失败后
        重试也必须传同一个 kid，否则会选到白名单外的账号。

        exclude：本次请求已经试过的账号（排队切号时传入），从候选里剔除。

        backend：按后端隔离——trae 请求只分给 Trae 账号，其余分给 WorkBuddy
        账号；None 时不过滤（旧调用/测试）。
        """
        exclude = set(exclude or ())
        with self.store.lock:
            candidates = [row for row in self.store.account_rows()
                          if self.state(row) == "available" and row["id"] not in exclude]
            if kid is not None:
                # 空白名单 = 不限制；只有显式列了账号才收窄候选集。
                whitelist = self.store.key_bindings(kid)[1]
                if whitelist:
                    candidates = [row for row in candidates if row["id"] in whitelist]
            if backend is not None:
                candidates = [row for row in candidates if row.get("backend", default_backend()) == backend]
            if not candidates:
                raise HTTPException(503, "暂无可用账号：请检查暂停、积分、冷却或登录状态")
            routing = self._routing_for(kid)
            now = self.clock()
            # 优先跳过刚排过队的账号。但**全部**都在让位期内时必须忽略该标记——
            # 否则排队会把整个池子自我封锁成 503，比排队本身更糟。
            fresh = [row for row in candidates if not self.queued(row["id"])]
            if fresh:
                candidates = fresh
            # 实测免费的账号优先。账本为空（或该模型没有任何免费观测）时原样返回，
            # 行为与加这个功能之前一致——见 admin/cost_ledger.py 的「观测不到就什么都不做」。
            if model:
                candidates = self.cost_ledger.prefer_free(
                    candidates, model, lambda row: row["id"])
            # 账号级限额护栏（admin/guards.py）。默认全关时这里是恒等变换。
            # 全部候选都被挡下时**不**改写成 503——护栏挡下的号仍然是可用账号，
            # 上报「护栏」比上报「暂无可用账号」有用得多（后者会让人去查登录态）。
            if candidates:
                candidates, blocked = self._apply_guards(candidates, model)
                if not candidates and blocked:
                    raise HTTPException(
                        503, f"账号已达本地限额（{blocked[0]}），请调整设置或等待次日重置")
            if not candidates:
                raise HTTPException(503, "暂无可用账号：请检查暂停、积分、冷却或登录状态")
            bindings = self.store.data["session_bindings"]
            for key in list(bindings):
                if bindings[key].get("expires", 0) <= now:
                    del bindings[key]
            bound = bindings.get(affinity_key, {}).get("account_id")
            if bound in {row["id"] for row in candidates}:
                # 亲和默认保持以命中上游缓存；但若绑定账号的最早过期积分离过期还远，
                # 而其他账号的积分将在阈值内过期，则改绑过去，避免积分白白清零。
                self.affinity_hits += 1
                threshold = now + int(self.store.data["pool"].get("affinity_expiry_days", 3)) * 86400
                status_of = self.store.data["account_status"]
                bound_expiry = status_of.get(bound, {}).get("earliest_expiry")
                urgent = [row for row in candidates if row["id"] != bound
                          and (expiry := status_of.get(row["id"], {}).get("earliest_expiry")) is not None
                          and expiry <= threshold]
                if urgent and (bound_expiry is None or bound_expiry > threshold):
                    bound = min(urgent, key=lambda row: status_of[row["id"]]["earliest_expiry"])["id"]
                    self.affinity_rebinds += 1
                    bindings[affinity_key] = {"account_id": bound, "expires": now + 86400}
            if bound in {row["id"] for row in candidates}:
                aid = bound
            elif routing == "random":
                aid = random.choice(candidates)["id"]
            else:
                ordered = self.order_candidates(candidates, routing)
                aid = ordered[self.cursor % len(ordered)]["id"]
                self.cursor += 1
            if affinity_key:
                if affinity_key not in bindings and len(bindings) >= 4096:
                    del bindings[min(bindings, key=lambda key: bindings[key]["expires"])]
                bindings[affinity_key] = {"account_id": aid, "expires": now + 86400}
            item = self.store.data["accounts"][aid]
            return aid, self.store.manager_for(aid, item)

    def _apply_guards(self, candidates, model):
        """按账号级护栏筛掉候选，返回 (留下的, 被挡下的原因列表)。

        护栏全部关闭时直接返回原列表，**一次库都不查**——这是热路径，
        默认配置下不该有任何额外开销。
        """
        limits = parse_guard_limits(self.store.data.get("pool"))
        if not any_enabled(limits):
            return candidates, []

        usage = self.guard_usage.get(self._load_daily_usage)
        status_of = self.store.data["account_status"]
        kept, blocked = [], []
        for row in candidates:
            aid = row["id"]
            status = status_of.get(aid, {})
            daily_tokens = daily_credits = model_tokens = None
            if usage is not None:
                daily_tokens = usage.tokens_for(aid)
                daily_credits = usage.credits_for(aid)
                model_tokens = usage.models.get(str(aid)) or {}
            # 免费判据来自**实测账本**：没观测过的模型按付费处理——
            # 「不确定是否免费」不该被当成免费放行，否则 daily_credit_limit 形同虚设。
            is_free = model is not None and self.cost_ledger.is_free(aid, model) is True
            reason = guard_block_reason(
                limits,
                credits=status.get("remaining"),
                daily_tokens=daily_tokens,
                daily_credits=daily_credits,
                model=model,
                model_daily_tokens=model_tokens,
                is_free_model=is_free,
            )
            if reason:
                blocked.append(reason)
                _logger.debug("账号 %s 被限额护栏挡下：%s", aid, reason)
            else:
                kept.append(row)
        return kept, blocked

    def _load_daily_usage(self):
        """从库里读当天账号用量；读不到返回 None（护栏据此放行）。"""
        rows = self.store.db.account_usage_today()
        usage = DailyUsage()
        for aid, bucket in rows.items():
            usage.tokens[aid] = bucket["tokens"]
            usage.credits[aid] = bucket["credits"]
            usage.models[aid] = dict(bucket.get("models") or {})
        return usage

    def note_usage(self, aid, model, tokens, credits=0.0):
        """记一次完成的请求用量，供护栏统计。

        护栏全关时直接返回，不写库——默认配置下不该产生任何额外写入。
        """
        if not aid:
            return
        limits = parse_guard_limits(self.store.data.get("pool"))
        if not any_enabled(limits):
            return
        try:
            self.store.db.add_account_usage(aid, model, tokens, credits)
        except Exception:  # 记不上账不该影响请求本身
            _logger.exception("账号 %s 用量落库失败，护栏计数将偏低", aid)
        # 立刻失效缓存：否则刚用完额度的号在 TTL 内仍会被判为可用。
        self.guard_usage.invalidate()

    def try_select(self, affinity_key, kid, exclude, model=None, backend=None):
        """尝试选一个没试过的账号；没有候选时返回 None，不抛异常。

        排队切号专用：候选耗尽时我们要把上游的排队响应**原样**返回给客户端，
        而不是抛 503——那样客户端会看到一个与真实原因（排队）无关的错误。
        把「有没有候选」和「选到谁」合成一次调用，也顺带消掉了原先
        「先探测候选集、再选号」两步之间的竞态窗口。

        backend 必须原样传给 select：漏掉它就等于在重试时丢掉后端隔离，
        腾讯请求会拿到字节账号（反之亦然）——凭据格式、上游域名、模型目录
        全都不兼容，等于把一次排队变成一个莫名其妙的 4xx。
        """
        try:
            return self.select(affinity_key, kid, exclude=exclude, model=model, backend=backend)
        except HTTPException:
            return None

    def _observe_cost(self, aid, model, usage):
        """从 usage 里提取实测 credit 并记入账本。

        字段名有几种可能（上游未公开契约），**逐个试、试不到就放弃**：
        宁可账本为空，也不能猜一个值出来——猜错会让选号把流量全压到
        一个其实收费的号上。
        """
        if not isinstance(usage, dict):
            return
        credits = None
        for field in CREDIT_FIELDS:
            if field in usage:
                credits = usage[field]
                break
        if credits is None:
            return
        tokens = usage.get("total_tokens")
        if tokens is None:
            # 没有 token 数就算不出单价；试一下拆分字段，仍不行就放弃。
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
                tokens = prompt + completion
        self.cost_ledger.observe(aid, model, credits, tokens)

    def record(self, aid, status=200, *, hint_text=None, hint_headers=None, usage=None, model=None):
        """记录一次请求结果，并在失败时施加冷却。

        ``hint_text`` / ``hint_headers`` 是上游响应里**可能**带着的恢复时刻
        （见 admin/reset_at.py）。给了就用它，给不出才回落本地有界退避——
        本地猜的时长必然不准，见该模块开头的说明。

        ``usage`` / ``model`` 用于实测成本账本（admin/cost_ledger.py）：只有响应里
        真的带回了可解析的 credit 才会记录，否则账本保持为空、选号行为不变。
        """
        if usage and model and aid:
            self._observe_cost(aid, model, usage)
        cooldown = 0
        recovered = False
        with self.store.lock:
            if aid not in self.store.data["accounts"]:
                return
            data = self.store.data["account_status"].setdefault(aid, {})
            data["request_count"] = data.get("request_count", 0) + 1
            data["last_request"] = int(self.clock())
            backend = self.store.data["accounts"][aid].get("backend", default_backend())
            soft = spec_for(backend).soft_cooldown_for(status)
            if soft is not None:
                # 该状态码对这个后端是「软」的：模型/资源不可用，账号本身通常是好的。
                # 只做有界冷却，不计入连续失败（否则会把好账号升级到 2h 硬冷却）。
                # 具体冷却时长由后端自己声明（见 BackendSpec.soft_cooldown）。
                minutes = max(1, int(soft) // 60)
                data.update(cooldown_until=self.clock() + soft,
                            last_error=f"模型不可用（{status}），已软冷却 {minutes} 分钟")
            elif status in (401, 403, 402, 429):
                # 指数退避：连续失败翻倍冷却时长，上限 2 小时；成功请求重置计数。
                fails = data.get("consecutive_fails", 0) + 1
                data["consecutive_fails"] = fails
                base = 300 if status in (401, 403) else 1800
                cooldown = min(base * (2 ** (fails - 1)), 7200)
                # 上游给了明确恢复时刻就采信它——本地猜的必然偏。
                # 与本地退避取**更晚**者：提前放出来只会在窗口内反复撞墙，
                # 每次都是一次风控痕迹。
                explicit = earliest_deadline(
                    parse_reset_at(hint_text, now=self.clock()),
                    parse_retry_after(hint_headers, now=self.clock()),
                )
                until = self.clock() + cooldown
                source = "本地退避"
                if explicit:
                    ceiling = self.clock() + 86400  # 上游报到一天以后必然是脏数据
                    until = min(max(explicit, until), ceiling)
                    if until == explicit:
                        source = "上游明示"
                label = "上游认证或访问被拒绝" if status in (401, 403) else "上游额度或频率限制"
                data.update(cooldown_until=until,
                            last_error=f"{label}，已冷却 {max(1, (until - self.clock()) // 60)} 分钟（{source}）")
                cooldown = until - self.clock()
                if status == 402:
                    data["credits_updated"] = 0  # 触发下个周期立即重新查询积分
            else:
                recovered = bool(data.get("consecutive_fails"))
                data["consecutive_fails"] = 0
            self.dirty = True
        # 日志放在锁外：record() 是每请求热路径，stderr/文件 I/O 可能被管道背压拖慢，
        # 持锁写会把所有账号选择排在同一把锁后面。
        if status in (401, 403):
            # 认证类失败是要人介入的状态（重新授权），必须留痕——否则只能靠主动
            # 打开管理页才看得到「需重新授权」的红标。
            _logger.warning("账号 %s 上游认证被拒（HTTP %s），冷却 %d 分钟，可能需要重新授权",
                            aid, status, cooldown // 60)
        elif status in (402, 429):
            _logger.info("账号 %s 触发上游限流（HTTP %s），冷却 %d 分钟",
                         aid, status, cooldown // 60)
        elif recovered:
            _logger.info("账号 %s 请求恢复正常", aid)

    def billing(self, client, headers, path, body=None):
        response = client.post(BILLING + path, headers=headers, json=body or {})
        if response.status_code != 200:
            raise BillingError("积分服务请求失败", response.status_code)
        try:
            d = response.json()
        except ValueError:
            raise BillingError("积分服务返回格式异常")
        if not isinstance(d, dict) or d.get("code") != 0:
            raise BillingError("上游未接受该操作，请检查账号或稍后重试", code=d.get("code") if isinstance(d, dict) else None)
        return d.get("data")

    def credits(self, client, headers):
        packages = []
        now = datetime.fromtimestamp(self.clock(), CN)
        for page in range(1, 101):
            d = self.billing(client, headers, "get-user-resource", {"PageNumber": page, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3], "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"), "PackageEndTimeRangeEnd": "2126-01-01 00:00:00"})
            try:
                data = d["Response"]["Data"]
                current = data["Accounts"]
                total = int(data["TotalCount"])
                if not isinstance(current, list) or total < 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise BillingError("积分数据格式异常，上次余额已保留")
            packages.extend(current)
            if len(packages) >= total:
                return summarize_packages(packages, now=self.clock())
            if not current:
                break
        raise BillingError("积分包分页不完整，上次余额已保留")

    def checkin_status(self, client, headers):
        try:
            d = self.billing(client, headers, "checkin-activity-status")
        except BillingError as e:
            if e.status not in (404, 405):
                raise
            d = self.billing(client, headers, "checkin-status")
        if not isinstance(d, dict):
            raise BillingError("签到状态格式异常")
        def flag(snake, camel):
            v = d.get(snake, d.get(camel))
            if v not in (True, False, 0, 1, "true", "false", "1", "0"):
                raise BillingError("签到状态缺少必要字段")
            return v in (True, 1, "true", "1")
        return {"active": flag("active", "active"), "checked": flag("today_checked_in", "todayCheckedIn")}

    def operate(self, aid, action):
        with self.operation_lock(aid):
            item, previous = self.snapshot(aid)
            # 暂停只是不参与请求分配，签到与积分查询照常执行。
            if not item["enabled"] and action not in ("checkin", "status"):
                return {"id": aid, "ok": False, "message": "账号已暂停，未执行操作"}
            manager = self.store.manager_for(aid, item)
            spec = spec_for(item.get("backend", default_backend()))
            try:
                if action == "refresh":
                    with manager._lock:
                        manager._refresh()
                    self.update(aid, token_refreshed=int(self.clock()), auth_invalid=False, cooldown_until=0, last_error=None)
                    return {"id": aid, "ok": True, "message": "登录凭据已刷新"}
                # 非默认后端（如 Trae）：刷新（上方）走各自凭据的刷新；积分/签到
                # 走各自的上游接口。分派依据是后端**声明**的支持情况，不是后端名。
                only = spec.account_ops_backend
                if only is not None:
                    return self._operate_nondefault(aid, manager, action, only)
                headers = manager.get_headers()
                if "workbuddy.ai" in str(headers.get("X-Domain", "")).lower():
                    return {"id": aid, "ok": False, "message": "当前仅支持国内账号积分与签到"}
                today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
                with self.client_factory() as client:
                    message = "积分状态已更新"
                    checkin_error = None
                    try:
                        ci = self.checkin_status(client, headers)
                        if ci["checked"]:
                            self.update(aid, checkin_date=today)
                        if action == "checkin":
                            if ci["checked"]:
                                message = "今日已签到，无需重复签到"
                            elif not ci["active"]:
                                message = "当前没有可参与的签到活动"
                            else:
                                self.billing(client, headers, "daily-checkin")
                                ci = self.checkin_status(client, headers)
                                if not ci["checked"]:
                                    raise BillingError("签到已提交，但上游尚未确认；请刷新状态")
                                self.update(aid, checkin_date=today)
                                message = "签到成功"
                    except (BillingError, httpx.HTTPError) as e:
                        if action == "checkin":
                            raise
                        checkin_error = "签到状态暂时无法查询"
                    data = self.credits(client, headers)
                    # A successful balance lookup is proof of current authentication.
                    self.update(aid, **data, credits_updated=int(self.clock()), auth_invalid=False, last_error=checkin_error)
                    if data["remaining"] > 0 and action == "checkin":
                        self.update(aid, cooldown_until=0)
                    return {"id": aid, "ok": True, "message": message, "remaining": data["remaining"]}
            except BillingError as e:
                self.update(aid, last_error=str(e))
                if e.status in (401, 403):
                    self.update(aid, cooldown_until=self.clock()+300)
                return {"id": aid, "ok": False, "message": str(e)}
            except spec.upstream_error_class() as e:
                # 本后端自己的上游调用失败（如 Trae 的 ug 接口）：401/403＝凭据失效
                # （冷却 + 标记待重新授权），其余原样暴露。
                # 异常类从**本账号的后端声明**取，不硬写 trae —— 见 base.error_class_name。
                label = spec.label
                if getattr(e, "status", None) in (401, 403):
                    self.update(aid, auth_invalid=True, cooldown_until=self.clock() + 300,
                                last_error=f"{label} 登录凭据已失效，请重新授权")
                    return {"id": aid, "ok": False, "message": f"{label} 登录凭据已失效，请重新授权"}
                self.update(aid, last_error=str(e))
                return {"id": aid, "ok": False, "message": str(e)}
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError):
                # Do not expose raw refresh responses or bearer credentials.
                message = "操作失败：请检查登录凭据或网络，必要时重新授权"
                self.update(aid, last_error=message)
                return {"id": aid, "ok": False, "message": message}

    def _operate_nondefault(self, aid, manager, action, backend):
        """非默认后端的积分/签到（目前即 Trae：ug 接口 checkin_credits + ide_user_ent_usage）。

        与 WorkBuddy 路径同构：先查签到状态 → 按需执行签到 → 拉积分余额。
        签到查询失败不阻断积分拉取（action=checkin 除外，必须显式报错）。

        ``backend`` 由 BackendSpec.account_ops_backend 分派传入，本方法目前只有
        Trae 一种实现（凭据管理器的 checkin_status / checkin_claim /
        user_ent_summary 是 Trae 侧的能力）；第三个后端接入时按同样的接口实现，
        pool 无需再改。
        """
        today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
        message = "积分状态已更新"
        with self.client_factory() as client:
            checkin_error = None
            try:
                ci = manager.checkin_status(client)
                if ci["checked"]:
                    self.update(aid, checkin_date=today)
                if action == "checkin":
                    if ci["checked"]:
                        message = "今日已签到，无需重复签到"
                    elif not ci["enable"]:
                        message = "当前没有可参与的签到活动"
                    else:
                        # 上游对签到失败也返回 HTTP 200，真实结果看 claim 的 code。
                        # 9074/9004 等＝上游暂时拒绝（高峰限流或设备/请求形态问题），
                        # 对 claim 本身做有界重试；其余非 0＝明确失败，透出上游
                        # message（而非误报"尚未确认"）。
                        confirmed = False
                        last_msg = "签到失败，请稍后再试"
                        for attempt in range(4):
                            ok, msg, retryable = spec_for(backend).interpret_checkin_claim(manager.checkin_claim(client))
                            if ok:
                                confirmed = True
                                break
                            last_msg = msg or last_msg
                            if retryable and attempt < 3:
                                time.sleep(2.5)
                                continue
                            # 明确失败：可能是"今日已签到"之类，查一次 status 兜底
                            if manager.checkin_status(client)["checked"]:
                                confirmed = True
                            break
                        if not confirmed:
                            raise BillingError(last_msg)
                        self.update(aid, checkin_date=today)
                        message = "签到成功"
            except (BillingError, httpx.HTTPError):
                if action == "checkin":
                    raise
                checkin_error = "签到状态暂时无法查询"
            remain = manager.user_ent_summary(client)
            # remain>0 说明至少有一个权益包；为 0 时不置 packages（避免把「无权益包」
            # 误判成 exhausted，真实耗尽由上游 1005→402 冷却兜底）。
            # earliest_* / expiring_* 与 WorkBuddy 路径（summarize_packages）同字段名，
            # 前端「最近过期 / 7 天内过期」两列对两个后端同一套渲染。
            self.update(aid, **{k: v for k, v in remain.items() if k != "remaining"},
                        remaining=remain["remaining"], packages=1 if remain["remaining"] > 0 else 0,
                        credits_updated=int(self.clock()), auth_invalid=False, last_error=checkin_error)
            if remain["remaining"] > 0 and action == "checkin":
                self.update(aid, cooldown_until=0)
            return {"id": aid, "ok": True, "message": message, "remaining": remain["remaining"]}

    async def batch(self, action):
        if self.jobs.locked():
            raise HTTPException(409, "已有批量任务进行中，请稍后刷新")
        async with self.jobs:
            with self.store.lock:
                # 总闸关掉的后端不参与（specs/backend-toggle.md §2 第 7 条）：
                # 概览账号表已隐藏它们，批量动作却去签到，会让用户看到
                # 「表格里没有的行在报签到成功」，正好破坏刻意对齐的计数口径。
                ids = [aid for aid, item in self.store.data["accounts"].items()
                       if is_backend_enabled(item.get("backend", default_backend()))]
            semaphore = asyncio.Semaphore(3)
            async def run(aid):
                # 批量签到错峰：每个账号在 0~spread 秒内随机延迟后再发起，避免同时打满上游。
                if action == "checkin":
                    spread = max(0, int(self.store.data["pool"].get("batch_spread", 90)))
                    if spread:
                        await asyncio.sleep(random.uniform(0, spread))
                async with semaphore:
                    try:
                        return await asyncio.to_thread(self.operate, aid, action)
                    except HTTPException:
                        return {"id": aid, "ok": False, "message": "账号已移除"}
            return {"results": await asyncio.gather(*(run(aid) for aid in ids))}

    CHECKIN_SLOTS = [("上午", "08:00", "11:00"), ("中午", "11:00", "14:00"), ("下午", "14:00", "18:00"), ("晚间", "18:00", "22:00")]
    # 单账号单日签到失败上限：触顶后当天跳过，跨天自动解除（见 _checkin_fails_today）。
    CHECKIN_FAIL_LIMIT = 5

    def checkin_offset(self, aid, today, window_minutes):
        """每账号每天在上午/中午/下午/晚间中随机选一个时段，再在时段内随机取分钟，
        全天分散签到，避免所有账号集中在同一时刻触发风控。返回 (时段名, HH:MM)。"""
        with self.store.lock:
            status = self.store.data["account_status"].setdefault(aid, {})
            slot = status.get("checkin_slot")
            when = status.get("checkin_when")
            if when and status.get("checkin_offset_date") == today:
                return slot, when
            name, start, end = random.choice(self.CHECKIN_SLOTS)
            lo = int(start[:2]) * 60 + int(start[3:])
            hi = int(end[:2]) * 60 + int(end[3:])
            # 每个时段内再取一个不早于配置签到时间的随机点。
            floor = int(self.store.data["pool"].get("checkin_time", "09:00")[:2]) * 60 + int(self.store.data["pool"].get("checkin_time", "09:00")[3:])
            lo = max(lo, floor) if floor < hi else lo
            minute = random.randint(lo, max(lo, hi))
            when = f"{minute // 60:02d}:{minute % 60:02d}"
            status["checkin_slot"] = name
            status["checkin_when"] = when
            status["checkin_offset_date"] = today
            self.dirty = True
            return name, when

    def _checkin_fails_today(self, aid, today):
        """当日累计签到失败次数（跨天归零）。

        ``checkin_fails`` 只按天计数：失败 5 次当天不再尝试，第二天自动重新开始。
        早期版本不做按天隔离，触顶即永久跳过（只能手工改库），已修复——别回归。
        """
        status = self.store.data["account_status"].get(aid, {})
        if status.get("checkin_fails_date") != today:
            return 0
        return status.get("checkin_fails", 0) or 0

    def _bump_checkin_fails(self, aid, today):
        """签到失败 +1；跨天先归零再计。"""
        fails = self._checkin_fails_today(aid, today) + 1
        self.update(aid, checkin_fails=fails, checkin_fails_date=today)
        return fails

    async def tick(self):
        now = datetime.fromtimestamp(self.clock(), CN)
        with self.store.lock:
            cfg = dict(self.store.data["pool"])
            # 自动签到覆盖两后端（WorkBuddy 走 billing，Trae 走 ug checkin_credits）。
            # is_backend_enabled：总闸关掉的后端不签到（specs/backend-toggle.md
            # §2 第 7 条）——后台定时任务不该去打一个用户已下线后端的上游。
            # checkin_fails 熔断按天重置：连续失败 5 次当天不再尝试，跨天自动解除。
            # 早期版本是「失败 5 次永久跳过」，账号一旦触顶就再也不会被自动签到
            # （只能手工改库），事实上把自动签到变成了薛定谔的猫——不要回归。
            today = now.strftime("%Y-%m-%d")
            due = [aid for aid, item in self.store.data["accounts"].items()
                   if is_backend_enabled(item.get("backend", default_backend()))
                   and spec_for(item.get("backend", default_backend())).supports_auto_checkin
                   and self._checkin_fails_today(aid, today) < self.CHECKIN_FAIL_LIMIT
                   and self.store.data["account_status"].get(aid, {}).get("checkin_date") != today
                   and self.store.data["account_status"].get(aid, {}).get("auto_attempt", 0) < self.clock() - 1800]
        if cfg["auto_checkin"]:
            for aid in due:
                _, due_time = self.checkin_offset(aid, now.strftime("%Y-%m-%d"), 0)
                if now.strftime("%H:%M") >= due_time:
                    self.update(aid, auto_attempt=int(self.clock()))
                    try:
                        result = await asyncio.to_thread(self.operate, aid, "checkin")
                        fails = 0 if result.get("ok") else self._bump_checkin_fails(aid, today)
                        self.update(aid, checkin_fails=fails)
                    except HTTPException:
                        pass
                    except Exception:
                        # 关键隔离点：operate 对非默认后端账号会抛各自的上游异常 / JSON /
                        # 网络错误等任意异常。若只 catch HTTPException，一个坏掉的
                        # 字节账号会把**整轮 tick 打断**，排在其后的腾讯账号这轮既不
                        # 签到也不刷新——「加了字节不该影响腾讯」在这里被打破。
                        # 逐账号兜住并留痕，让其余账号照常推进。
                        _logger.exception("账号 %s 自动签到异常，已跳过（不影响其它账号）", aid)
                        self.update(aid, checkin_fails=self._bump_checkin_fails(aid, today))
        # 低峰凭据预刷新：expiresAt 2 小时内到期的账号提前刷新，避免请求撞上 401 再冷却。
        # 凭据预刷新：分散到白天时段，每个账号每天随机一个时刻，模拟正常使用时刷新，
        # 避免半夜集中刷新。仅在 access token 两小时内到期时才动作。
        window = [cfg.get("sync_start") or "10:00", cfg.get("sync_end") or "20:00"]
        in_window = window[0] <= now.strftime("%H:%M") <= window[1]
        if in_window:
            today = now.strftime("%Y-%m-%d")
            for aid, item in list(self.store.data["accounts"].items()):
                status = self.store.data["account_status"].get(aid, {})
                # 总闸关掉的后端不做凭据预刷新（同上：别去碰下线后端的上游）。
                if not item["enabled"] or not is_backend_enabled(item.get("backend", default_backend())) or status.get("refresh_window_date") == today:
                    continue
                # 每个账号每天抽一个时刻（默认统计时段内），到点后当天不再考虑。
                slot = status.get("refresh_slot")
                if slot is None or status.get("refresh_slot_date") != today:
                    lo = int(window[0][:2]) * 60 + int(window[0][3:])
                    hi = max(lo, int(window[1][:2]) * 60 + int(window[1][3:]))
                    minute = random.randint(lo, hi)
                    slot = f"{minute // 60:02d}:{minute % 60:02d}"
                    self.update(aid, refresh_slot=slot, refresh_slot_date=today)
                if now.strftime("%H:%M") < slot:
                    continue
                row = next((r for r in self.store.account_rows() if r["id"] == aid), None)
                self.update(aid, refresh_window_date=today)
                if (row and row.get("refresh_available") and not row.get("expired")
                        and status.get("token_refreshed", 0) < self.clock() - 86400
                        and 0 < row["expires_at"] / 1000 - self.clock() < 7200):
                    try:
                        await asyncio.to_thread(self.operate, aid, "refresh")
                    except HTTPException:
                        pass
                    except Exception:
                        # 同上：逐账号兜住，否则一个坏账号会让本轮后续账号都刷不到。
                        _logger.exception("账号 %s 凭据预刷新异常，已跳过（不影响其它账号）", aid)
        self.flush()
        # 上游用量明细：每账号每天一次，失败只留痕不影响其它账号。
        await self.sync_upstream_usage_batch()

    # 页面刷新触发的积分拉取：每账号限流，避免频繁请求被上游风控。
    REFRESH_MIN_INTERVAL = 60

    async def refresh_credits(self):
        """Pull fresh balances because the admin page was loaded. No background/incremental
        polling happens elsewhere: credits change only when the page asks for them.
        Each account is throttled by pool.credit_min_interval seconds to avoid hammering upstream."""
        limit = max(0, int(self.store.data["pool"].get("credit_min_interval", self.REFRESH_MIN_INTERVAL)))
        with self.store.lock:
            ids = [aid for aid, item in self.store.data["accounts"].items()
                   if is_backend_enabled(item.get("backend", default_backend()))
                   and self.store.data["account_status"].get(aid, {}).get("credits_updated", 0)
                   < self.clock() - limit]
        if not ids:
            return {"refreshed": 0, "skipped": True}
        if self.jobs.locked():
            return {"refreshed": 0, "skipped": True, "message": "已有任务进行中"}
        async with self.jobs:
            semaphore = asyncio.Semaphore(3)
            async def run(aid):
                async with semaphore:
                    try:
                        return await asyncio.to_thread(self.operate, aid, "status")
                    except HTTPException:
                        return {"id": aid, "ok": False, "message": "账号已移除"}
            await asyncio.gather(*(run(aid) for aid in ids))
        return {"refreshed": len(ids)}

    # -- 上游用量明细（对账）-----------------------------------------------
    @staticmethod
    def _usage_time(value):
        """上游 requestTime 是北京时间且无时区标记，必须按 CN 解析，否则跨天统计差 8 小时。"""
        try:
            return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
        except (TypeError, ValueError):
            return None

    def fetch_upstream_usage(self, client, headers, start, end):
        """按 data.total 翻页拉取近端用量明细，返回原始行列表。"""
        rows, page, total = [], 1, None
        while page <= 200:
            response = client.post(USAGE_BILLING + "get-user-request-usage", headers=headers,
                                   json={"startTime": start, "endTime": end,
                                         "pageNum": page, "pageSize": USAGE_PAGE_SIZE})
            if response.status_code != 200:
                raise BillingError("上游用量接口请求失败", response.status_code)
            try:
                payload = response.json()
            except ValueError:
                raise BillingError("上游用量接口返回格式异常")
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise BillingError("上游未返回用量明细")
            data = payload.get("data") or {}
            batch = data.get("data") or []
            if not isinstance(batch, list):
                raise BillingError("上游用量明细格式异常")
            total = int(data.get("total") or 0)
            rows.extend(item for item in batch if isinstance(item, dict))
            if not batch or len(rows) >= total:
                break
            page += 1
        return rows

    # 手动拉取的每账号节流（秒）：连点「拉取上游」不该反复打上游。
    USAGE_MANUAL_THROTTLE = 60

    def _pull_upstream_usage(self, aid, item, start, end):
        """拉一个账号在 [start, end) 区间的用量并落库，返回记录数。"""
        fmt = "%Y-%m-%d %H:%M:%S"
        with self.operation_lock(aid):
            manager = self.store.manager_for(aid, item)
            headers = manager.get_headers()
            with self.client_factory() as client:
                rows = self.fetch_upstream_usage(
                    client, headers, start.strftime(fmt), end.strftime(fmt))
        name = item.get("name")
        records = []
        for row in rows:
            request_id = row.get("requestId")
            when = self._usage_time(row.get("requestTime"))
            if not request_id or when is None:
                continue
            records.append({
                "request_id": request_id, "account_id": aid, "account_name": name,
                # credit 可能不是终值，db 层用 UPSERT 允许后续修正，不能 OR IGNORE。
                "credit": number(row.get("credit")), "model": row.get("model"),
                "client": row.get("client"), "request_time": when,
                "agent_purpose": row.get("agentPurpose"),
            })
        self.store.db.save_upstream_usage(records)
        return len(records)

    def sync_upstream_usage(self, aid):
        """后台每日同步：固定 30 天窗口，成功写 usage_synced_at。"""
        item = self.store.data["accounts"].get(aid)
        if not item:
            raise HTTPException(404, "账号不存在")
        end = datetime.fromtimestamp(self.clock(), CN)
        start = end - timedelta(days=USAGE_SYNC_DAYS)
        count = self._pull_upstream_usage(aid, item, start, end)
        self.update(aid, usage_synced_at=int(self.clock()), usage_sync_error=None)
        return count

    def sync_upstream_usage_range(self, aid, start, end):
        """手动拉取：任意区间，只写库 + 留痕，**不写 usage_synced_at**。

        写了 usage_synced_at 会让后台每日同步认为「今天已同步」而跳过当天，
        其余时段永远拉不到，留下永久空洞。所以手动与自动的留痕字段必须分开。
        """
        item = self.store.data["accounts"].get(aid)
        if not item:
            raise HTTPException(404, "账号不存在")
        if start >= end:
            raise HTTPException(400, "开始时间必须早于结束时间")
        if (end - start).days > 366:
            raise HTTPException(400, "单次拉取区间不能超过 366 天")
        now = self.clock()
        last = self._usage_manual_at.get(aid, 0)
        if now - last < self.USAGE_MANUAL_THROTTLE:
            wait = self.USAGE_MANUAL_THROTTLE - int(now - last)
            raise HTTPException(429, f"该账号刚拉取过，请 {wait} 秒后重试")
        # 时间戳必须在**拉取成功之后**才写：上游 5xx / 凭据失效是最需要重试的
        # 场景，失败时把 60 秒窗口消耗掉，用户只会看到「刚拉取过请稍后重试」，
        # 而实际上什么都没拉到。防连点由 jobs 锁 + 前端冷却承担。
        count = self._pull_upstream_usage(aid, item, start, end)
        self._usage_manual_at[aid] = now
        self.update(aid, usage_manual_synced_at=int(now))
        return count

    async def sync_upstream_usage_batch(self):
        """每天每账号一次；失败写 usage_sync_error 并当天不再重试（避免每 60 秒打上游）。"""
        now = self.clock()
        today = datetime.fromtimestamp(now, CN).strftime("%Y-%m-%d")
        with self.store.lock:
            due = []
            for aid, item in self.store.data["accounts"].items():
                # 总闸关掉的后端不打上游（与 tick 同一口径）。
                if not item["enabled"] or not is_backend_enabled(item.get("backend", default_backend())):
                    continue
                # 用量对账接口只认默认后端凭据（见 BackendSpec.supports_upstream_usage）：
                # 不支持的账号进队列就是每天一次必然的 401 + 一条永远清不掉的留痕。
                if not spec_for(item.get("backend", default_backend())).supports_upstream_usage:
                    continue
                status = self.store.data["account_status"].get(aid, {})
                # usage_synced_at 语义是「最后同步成功时间」，失败时不改它；
                # 靠 usage_sync_error 里的日期前缀兼作「当天已尝试」标记，不新增列。
                if _same_day(status.get("usage_synced_at"), now):
                    continue
                if (status.get("usage_sync_error") or "").startswith(today):
                    continue
                due.append(aid)
        if not due:
            return {"synced": 0, "skipped": True}
        if self.jobs.locked():
            return {"synced": 0, "skipped": True, "message": "已有任务进行中"}
        results = {"synced": 0, "failed": 0}
        async with self.jobs:
            semaphore = asyncio.Semaphore(3)

            async def run(aid):
                async with semaphore:
                    try:
                        return await asyncio.to_thread(self.sync_upstream_usage, aid)
                    except HTTPException:
                        return None
                    except Exception as e:
                        # 留痕：同步挂了、过时几天都没人知道。前缀日期兼作「当天已尝试」，
                        # 避免失败账号被每 60 秒的 tick 反复重试打上游。
                        self.update(aid, usage_sync_error=f"{today} {type(e).__name__}: {e}"[:200])
                        return None
            for outcome in await asyncio.gather(*(run(aid) for aid in due)):
                if outcome is None:
                    results["failed"] += 1
                else:
                    results["synced"] += 1
        return results

    async def sync_upstream_usage_manual(self, start, end, account_id=None, backend=None):
        """手动批量拉取：只打启用账号，走 jobs 并发锁防重入。

        backend 限定只拉某后端的账号（用量页 tab 用：停在 Trae tab 上点
        「拉取上游」就只拉 Trae 账号，不把另一个后端也打一遍）。
        被节流/失败的账号单独记在 results["skipped"] 里返回，不影响其它账号——
        连点按钮时应当看到「大部分已跳过」而不是整批报错。
        """
        with self.store.lock:
            targets = [aid for aid, item in self.store.data["accounts"].items()
                       if item["enabled"] and is_backend_enabled(item.get("backend", default_backend()))
                       and spec_for(item.get("backend", default_backend())).supports_upstream_usage
                       and (not account_id or aid == account_id)
                       and (not backend or item.get("backend", default_backend()) == backend)]
        if not targets:
            if account_id:
                # 指定了账号却拉不到：说清是「这个账号拉不了」，别让用户以为选错了人。
                item = self.store.data["accounts"].get(account_id)
                if item and not spec_for(item.get("backend", default_backend())).supports_upstream_usage:
                    return {"synced": 0, "failed": 0, "skipped": [],
                            "message": "该后端没有上游用量对账接口"}
            return {"synced": 0, "failed": 0, "skipped": [], "message": "没有可拉取的账号"}
        if self.jobs.locked():
            return {"synced": 0, "failed": 0, "skipped": targets,
                    "message": "已有任务进行中"}
        results = {"synced": 0, "failed": 0, "skipped": []}
        async with self.jobs:
            semaphore = asyncio.Semaphore(3)

            async def run(aid):
                async with semaphore:
                    try:
                        return await asyncio.to_thread(
                            self.sync_upstream_usage_range, aid, start, end)
                    except HTTPException as e:
                        # 429 节流不算失败：这是防抖生效的正常结果。
                        return ("skipped", e.detail)
                    except Exception as e:
                        return ("error", f"{type(e).__name__}: {e}")
            outcomes = await asyncio.gather(*(run(aid) for aid in targets))
        for aid, outcome in zip(targets, outcomes):
            if isinstance(outcome, tuple):
                results["skipped"].append({"id": aid, "reason": outcome[1]})
                if outcome[0] == "error":
                    results["failed"] += 1
            else:
                results["synced"] += 1
        return results


def request_affinity(headers, body):
    """Persist only a hash of caller/model/session identity, never raw prompts or keys."""
    if not isinstance(body, dict):
        return None
    identity = None
    for name in (b"x-session-id", b"session_id", b"x-conversation-id"):
        if headers.get(name):
            identity = [name.decode(), headers[name].decode(errors="replace")]
            break
    if identity is None:
        for name in ("prompt_cache_key", "conversation_id", "session_id"):
            if body.get(name):
                identity = [name, body[name]]
                break
    if identity is None:
        messages = body.get("messages", body.get("input", []))
        if isinstance(messages, str) and messages:
            identity = ["first_user", messages]
        elif isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    identity = ["first_user", message.get("content", "")]
                    break
    if identity is None:
        return None
    caller = headers.get(b"authorization") or headers.get(b"x-api-key", b"")
    value = [caller.decode(errors="replace"), body.get("model", "auto"), identity]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PoolMiddleware:
    def __init__(self, app, pool, model_registry=None):
        self.app, self.pool = app, pool
        self.model_registry = model_registry

    def _resolve_backend_for(self, model, kid):
        """模型名 → 后端名。裸名走策略选择，带前缀/私有模型走声明。

        §5 的落点。**唯一一处**决定后端的地方（converter 侧的 `resolve_backend`
        读同一个结果 —— 见 `scope["codebuddy.routed_backend"]` 的传递）。

        三道过滤（§5.1）在候选集里做，出局原因逐条留给 400 报错：
        1. 总闸 + 环境逃生门（`is_backend_enabled`）；
        2. 该后端有启用账号（`store.has_enabled_account`）；
        3. 该后端这条模型行启用（模型管理页的开关）。

        裸名路由开关（`settings.bare_name_routing`）关掉时，直接返回
        `backend_name_for` 的结果 = 今天的行为（裸名走默认后端）。这是
        §8 阶段三要求的 kill switch。
        """
        # 私有模型（pinned）：行为与今天完全一致，不走策略。
        # pinned 是「本来就只可能归我」（三档路由），不需要在候选集里挑。
        # 判定复用 registry 的声明（不是在这里重写一遍 `in spec.pinned_models`）。
        #
        # （v16 去前缀后不再有「带前缀 = 用户点名后端」这一类：id 一律是上游裸名，
        #  所以这里只剩 pinned 一个例外。）
        if pinned_model_owner(model) is not None:
            return backend_name_for(model)

        # `auto` / 空 = 「不指定」→ 默认后端（§4 表格末行：「不变：走默认后端」）。
        # 它们**不是目录里的模型**，拿去查候选集必然零候选 —— 那会把它变成
        # 400，破坏既有客户端（`converter` 各处都 `payload.get("model") or "auto"`）。
        if not model or model == "auto":
            return backend_name_for(model)

        if not self.pool.store.data["pool"].get("bare_name_routing", True):
            return backend_name_for(model)   # kill switch：裸名走默认后端

        registry = self.model_registry
        if registry is None or not isinstance(model, str) or not model:
            # 无注册表（旧部署/单测）或非字符串：退回今天的行为。
            return backend_name_for(model)

        rows = registry.bare_name_candidates(model)
        known = tuple(known_backends())
        candidates, rejections = [], []
        for backend in known:
            row = rows.get(backend)
            if row is None:
                rejections.append(Rejection(backend, "目录里没有这个模型"))
                continue
            if not is_backend_enabled(backend):
                rejections.append(Rejection(backend, "后端已被管理员禁用"))
                continue
            if not self.pool.store.has_enabled_account(backend):
                rejections.append(Rejection(backend, "没有启用账号"))
                continue
            if not row["enabled"]:
                rejections.append(Rejection(backend, "该模型在此后端已被禁用"))
                continue
            candidates.append(Candidate(backend=backend,
                                        credits_micro=row["credits_micro"],
                                        model_id=row["model_id"]))
        if not candidates:
            # §5.1：候选为空 → 400（今天也是 400，只是文案更有用）。
            # 未知模型（两边都没有）走到这里，rejections 会逐条说「目录里没有」。
            raise HTTPException(400, str(NoCandidate(model, rejections)))

        order, explicit = build_order(
            known,
            configured=self.pool.store.data["pool"].get("model_backend_order"),
            key_configured=(self.pool.store.data["keys"].get(kid) or {}).get("model_backend_order"))
        return decide(model, candidates,
                      BackendOrderSpec(order=order, explicit_order=explicit)).backend

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            kid = self.pool.store.check_api(headers.get(b"authorization", b"").decode(), headers.get(b"x-api-key", b"").decode())
            raw = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                raw.extend(message.get("body", b""))
                if len(raw) > 32 * 1024 * 1024:
                    raise HTTPException(413, "请求体超过 32 MiB")
                if not message.get("more_body", False):
                    break
            raw = bytes(raw)
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            # 模型名 → 后端（§5 裸名路由）**必须在模型门之前**：
            #   - 后端已被管理员禁用 / 没有启用账号 / 目录里没这个模型 —— 这些原因
            #     只有策略算得出来（它逐后端看候选），`_resolve_backend_for` 会以
            #     400 抛出**逐条原因**（`NoCandidate`，如「trae：后端已被管理员禁用」）。
            #   - 反过来先跑模型门会误报：`enabled_models()` 已排除被禁用后端的模型，
            #     于是「Trae 下线了」被说成「模型 X 已在管理页禁用」——排障会跑去
            #     模型页找一个从没被动过的开关（这正是这条注释顶上那段历史）。
            #
            # v16 去前缀后尤其不能再用 `backend_name_for(裸名)` 判后端：它对所有裸名
            # 都返回默认后端（同名模型两边都有，静态判不出），Trae 会被当成 WB。
            # 想读某一行**存下来**的归属是 `models.backend` 列；想判「此刻该走谁」
            # 就是下面这次策略解析 —— 唯一真相源。
            #
            # 三档路由 / `auto` 由 `_resolve_backend_for` 内部处理（pinned → 声明归属，
            # auto/空 → 默认后端），这里不再需要单独一分支。
            backend = self._resolve_backend_for(
                body.get("model") if isinstance(body, dict) else None, kid)
            # 管理页显式**禁用某一条模型**时拒绝，不打上游。
            #
            # ⚠️ 这条门现在是**兜底**，主判定已在其上（`_resolve_backend_for` 逐后端看
            # 候选，禁用行会记成 rejection；全部候选不可用时直接抛 `NoCandidate`）。
            # 保留它的唯一理由：`_resolve_backend_for` 在**裸名路由被总闸关掉**或
            # **没有注册表**（旧部署/单测）时会直接回落 `backend_name_for(model)`，
            # 绕过候选过滤 —— 那种形态下这条门仍要挡住「已在管理页禁用」的请求
            # （否则禁用会静默失效：`enabled_models()` 里没有它，但没人拦）。
            #
            # 判据用 `enabled_models()`/`snapshot_ids()` 的差集：前者排除被禁用后端
            # 与被禁用行，后者含全部已知名 —— 差集 = 「已知但不可用」。
            if self.model_registry is not None and isinstance(body, dict):
                model = body.get("model")
                if isinstance(model, str) and model \
                        and model not in self.model_registry.enabled_models() \
                        and model in self.model_registry.snapshot_ids():
                    raise HTTPException(400, f"模型 {model} 已在管理页禁用")
            # 密钥的模型白名单：空白名单 = 不限制；显式列了模型则只放行列表内的。
            if kid is not None and isinstance(body, dict):
                model = body.get("model")
                allowed_models = self.pool.store.key_bindings(kid)[0]
                if allowed_models and isinstance(model, str) and model and model not in allowed_models:
                    raise HTTPException(403, f"该密钥无权使用模型 {model}")
            # 把选定后端交给 converter（同一请求内的下一次决策必须给出同一个答案，
            # 否则凭据-后端一致性闸门会 503）。见 ROUTED_BACKEND 的说明。
            #
            # `backend` 在上面（模型门之前）就算好了 —— 顺序反过来的话，
            # 「后端禁用」会被模型门误报成「模型已禁用」，见那段注释。
            scope[ROUTED_BACKEND] = backend
            affinity_key = request_affinity(headers, body)
            model_name = None
            if isinstance(body, dict) and isinstance(body.get("model"), str):
                model_name = body["model"]
            # Refresh before entering a stream; retry another eligible account only
            # if credential preparation fails, never replay a partially emitted response.
            # 重试分支也要传同一个 kid、同一个 backend 与同一个 model，否则凭据失败后
            # 可能选到该 key 白名单之外、或另一个后端的账号。
            #
            # 这里重试是**安全**的：失败发生在 get_headers()（本地凭据准备），
            # 一个字节都没发给上游，不存在重复计费。上游侧的传输异常绝不在此重试，
            # 判别依据见 core/replay_policy.py。
            aid, manager = self.pool.select(affinity_key, kid, model=model_name, backend=backend)
            try:
                await asyncio.to_thread(manager.get_headers)
            except Exception:
                self.pool.update(aid, cooldown_until=self.pool.clock()+300, last_error="凭据暂不可用，已冷却 5 分钟")
                aid, manager = self.pool.select(affinity_key, kid, model=model_name, backend=backend)
                await asyncio.to_thread(manager.get_headers)
        except HTTPException as e:
            return await JSONResponse({"detail": e.detail}, e.status_code)(scope, receive, send)
        except Exception:
            return await JSONResponse({"detail": "账号凭据暂不可用，请刷新凭据或重新授权"}, 503)(scope, receive, send)
        log_context = {"key_id": kid, "account_id": aid,
                       "account_name": self.pool.store.data["accounts"].get(aid, {}).get("name"),
                       # 这条请求走的是哪条链路（trae / workbuddy）。取**实际选中的账号**
                       # 的 backend，而不是由模型名推导的值——轮转/降级之后两者可能不同，
                       # 记实际发生的事实才能用于事后排查。
                       "backend": self.pool.store.data["accounts"].get(aid, {}).get("backend", default_backend()),
                       "model_id": model_name, "model_name": model_name,
                       "error_code": None}
        # 挂到 scope 上交给外层 MetricsMiddleware（同一条链共享，见 REQUEST_LOG_CONTEXT 注释）。
        scope[REQUEST_LOG_CONTEXT] = log_context
        pool_cfg = self.pool.store.data.get("pool", {})
        failover_on = bool(pool_cfg.get("queue_failover"))
        max_tries = max(1, int(pool_cfg.get("queue_retry", 3))) if failover_on else 1
        if not failover_on:
            # 开关关闭时走原路径：必须仍然用重放闭包包一层 receive，
            # 否则下游应用第二次 await receive() 会阻塞在 ASGI 上。
            return await self._serve_once(scope, _replay_receive(raw, receive), send,
                                          aid, manager, kid, log_context, model=model_name)

        # -- 容量队列自动切换（specs/queue-failover.md） ------------------------
        # 只有在「尚未向客户端发出任何字节」时才能重放；一旦开始输出内容就必须
        # 放弃切换，否则客户端会收到重复输出。
        tried_accounts = []
        computed_models = set()   # 已经**算过候选**的模型（不等于试过该模型）
        # attempt_body 必须始终是 **bytes**。它会被 _replay_receive 塞进 ASGI 的
        # http.request.body 交给下游应用，下游（Starlette）用 b"".join(...) 收集请求体，
        # 一旦这里传 dict 进去就会抛
        #   TypeError: sequence item 0: expected a bytes-like object, dict found
        # 客户端侧表现为一个莫名其妙的 400「bad json」。首次尝试直接复用原始字节
        # （不重新序列化，避免无谓地改动客户端发出的字节）。
        attempt_body = raw
        attempt_model = model_name
        last_outcome = None
        for attempt in range(max_tries):
            # 第一次尝试用调用方已选好的账号；之后每次重新选号。
            if attempt:
                picked = self.pool.try_select(affinity_key, kid, tried_accounts, model=attempt_model, backend=backend)
                if picked is None:
                    # 候选账号已试尽。先看能不能换个模型再继续，否则收工，
                    # 把上游最后一次的排队响应原样返回。
                    next_model = self._next_model(pool_cfg, attempt_model, computed_models, backend)
                    if next_model is None:
                        break
                    computed_models.add(attempt_model)
                    attempt_model = next_model
                    # 换模型必须重新序列化（这里才第一次产生「改过的请求体」）。
                    # 仍要是 bytes：见下面 attempt_body 的说明。
                    attempt_body = (json.dumps(dict(body, model=next_model)).encode("utf-8")
                                    if isinstance(body, dict) else raw)
                    # 换模型后账号可以重新从头试：新模型面对的是另一个上游队列。
                    tried_accounts = []
                    _logger.info("可用账号已试尽，降级到 %s 重试", next_model)
                    picked = self.pool.try_select(affinity_key, kid, tried_accounts, model=attempt_model, backend=backend)
                    if picked is None:
                        break
                aid, manager = picked
                try:
                    await asyncio.to_thread(manager.get_headers)
                except Exception:
                    self.pool.update(aid, cooldown_until=self.pool.clock() + 300,
                                     last_error="凭据暂不可用，已冷却 5 分钟")
                    tried_accounts.append(aid)
                    continue
                log_context["account_id"] = aid
                log_context["account_name"] = self.pool.store.data["accounts"].get(aid, {}).get("name")
                # 与 account_id/account_name 同步刷新：切号后这条记录才是「实际用的那个号」。
                # 排队切号已按后端隔离（try_select 传 backend），所以正常不会变；但一旦
                # 哪天隔离被改坏，这里如实记录反而能让事后排查看到「打到了另一个后端」。
                log_context["backend"] = self.pool.store.data["accounts"].get(aid, {}).get("backend", default_backend())
                log_context["model_id"] = attempt_model
                log_context["model_name"] = attempt_model
            # 每次尝试都要用新的 receive 闭包与新的 attempt_body（换模型时 body 变了）。
            attempt_receive = _replay_receive(attempt_body, receive)
            outcome = await self._attempt(scope, attempt_receive, send, manager)
            if outcome["kind"] == "done":
                # 已放行并完整结束：记录本次生效的账号与用量，收工。
                self.pool.record(aid, outcome["status"],
                                 hint_text=outcome.get("error_text"),
                                 hint_headers=outcome.get("error_headers"),
                                 usage=outcome.get("usage"), model=attempt_model)
                self.pool.note_usage(aid, attempt_model, outcome.get("tokens") or 0,
                                     _credits_of(outcome.get("usage")))
                if kid is not None:
                    self.pool.store.record_key_usage(kid, tokens=outcome["tokens"] or None)
                # WAF 判定必须在收工**之前**：403/11128 也走 kind="done"（响应已由
                # _attempt 转交给客户端，判为 done 是对的），但它是出口 IP 级信号，
                # 不记账就等于把这条线索整个丢掉。见 admin/rotation.py。
                self._note_waf_if_any(aid, outcome, backend=backend)
                self.pool.flush()
                return
            # 命中排队：标记让位。决策顺序见 spec 4.4——先换号，候选耗尽再降级模型。
            last_outcome = outcome
            self.pool.mark_queued(aid)
            tried_accounts.append(aid)
            _logger.info("账号 %s 命中容量队列（%s），让位 %s 秒", aid,
                         outcome.get("code") or "WAITING",
                         pool_cfg.get("queue_cooldown", 120))
            # WAF/通道拦截是**出口 IP 级**的，换号无用（实测换遍全池仍被拦）。
            # 识别出来就立刻停手：继续轮转只会把一次客户端请求放大成 N 次上游
            # 请求，把一个已经触发的风控推得更重。见 admin/rotation.py。
            if self._note_waf_if_any(aid, outcome, backend=backend):
                _logger.warning(
                    "上游 WAF 拦截疑似出口 IP 级（%d 秒内多个账号被拦），"
                    "停止本次轮转以免放大：%s", WAF_IP_WINDOW,
                    self._text_of(outcome))
                break
            self.pool.flush()
            # 轮转退避：换号零间隔本身就是可被统计的节奏特征，抖动用于打散
            # 多请求的相位（固定周期会让它们再次聚团）。最后一次尝试后不必等。
            if attempt + 1 < max_tries:
                delay = backoff_after(attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        # 尝试用尽：把最后一次的原始上游响应**原样**返回给客户端，让它按
        # data.retry_after 自行轮询（spec 4.4 第 4 条）。**不是** 503——
        # 503 会掩盖真实原因。last_outcome 为 None 说明连一次上游都没打到。
        if last_outcome is not None:
            await send({"type": "http.response.start", "status": last_outcome["status"],
                        "headers": last_outcome["headers"]})
            await send({"type": "http.response.body", "body": last_outcome["body"]})
            self.pool.record(aid, last_outcome["status"],
                             hint_text=self._text_of(last_outcome),
                             hint_headers=last_outcome.get("headers"))
            if kid is not None:
                self.pool.store.record_key_usage(kid, tokens=None)
        else:
            return await JSONResponse(
                {"detail": "暂无可用账号：请检查暂停、积分、冷却或登录状态"}, 503)(scope, receive, send)
        self.pool.flush()

    def _note_waf_if_any(self, aid, outcome, backend=None):
        """这次结果是否算 WAF/通道拦截？是则记账，返回该后端是否已激活。

        判据见 admin/rotation.py：结构化 code（11128）为主，403 需配合通道文案。
        记账口径是「短窗内**不同账号**」，所以单个坏号反复被拦永远不会停掉轮转；
        按 backend 分桶，所以字节被拦不会停掉腾讯的轮转。
        """
        text = self._text_of(outcome) or outcome.get("error_text")
        if not is_waf_error(outcome.get("status"), text, outcome.get("code")):
            return False
        return self.pool.waf_gate.note_waf(aid, backend=backend)

    def _next_model(self, pool_cfg, current_model, computed_models, backend=None):
        """挑下一个该试的模型；没有则返回 None（表示该收工了）。

        ``computed_models`` 是「已经取过候选的当前模型」集合——每换一次模型就
        把上一个记进来，保证同一个模型不会被反复取到同一个候选。
        ``backend`` 必须透传：降级只能在同后端内换模型，否则会拿一个后端的账号
        去要另一个后端的模型（见 fallback_models 的说明）。
        """
        if not pool_cfg.get("queue_model_fallback", True):
            return None
        tried = set(computed_models) | {current_model}
        for candidate in self.pool.fallback_models(current_model, tried=tried, backend=backend):
            return candidate
        return None

    async def _attempt(self, scope, receive, send, manager):
        """跑一次下游应用，扣留首包以判定是否排队。

        返回 dict：
          kind="queue" → 命中容量队列。此时**未向客户端发出任何字节**，可安全重放；
                         同时带回被丢弃响应的 ``status`` / ``headers`` / ``body``，
                         重试耗尽时用它把上游原始响应原样还给客户端。
          kind="done"  → 已决定放行，响应已经（经 observe）转发给客户端，不可重放。

        扣留**必须有硬上界**（``queue_hold_ms``）：历史上这功能被回滚就是因为
        无上界扣留把 /health 一起拖死。窗口内没有首包 = 上游慢，不是排队，
        立刻原样放行，绝不等待。
        """
        hold_ms = max(0, int(self.pool.store.data.get("pool", {}).get("queue_hold_ms", 3000)))
        state = {"status": 200, "headers": [], "held": False, "first_body": None,
                 "queue": False, "started": False, "tokens": 0}
        # 上游错误文案：判定排队之外，冷却时长也要用它（可能写了恢复时刻）。
        err_texts: list[str] = []
        body_ready = asyncio.Event()

        def sniff(payload):
            """从首包嗅出排队特征。返回 (is_queue, code, has_content)。"""
            code, texts, has_content = None, [], False
            for line in payload.split(b"\n"):
                stripped = line.strip()
                for prefix in (b"data:", b"event:"):
                    if stripped.startswith(prefix):
                        stripped = stripped[len(prefix):].strip()
                        break
                if not stripped or stripped in (b"[DONE]",):
                    continue
                try:
                    obj = json.loads(stripped)
                except (ValueError, AttributeError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                # 真实内容：choices / delta / content / output 任一出现就说明
                # 这不是排队响应，必须放行（重放会重复输出）。
                if obj.get("choices") or obj.get("delta") or obj.get("content") \
                        or obj.get("output") or obj.get("output_text"):
                    has_content = True
                resp = obj.get("response") if isinstance(obj.get("response"), dict) else {}
                err = obj.get("error") or resp.get("error") or {}
                c = obj.get("code")
                if c is None and isinstance(err, dict):
                    c = err.get("code")
                if c is not None:
                    code = c
                for holder in (err, resp, obj):
                    if isinstance(holder, dict) and holder.get("message"):
                        texts.append(str(holder["message"]))
            joined = " ".join(texts)
            if joined:
                # 冷却也要读这段文案：上游可能在这里写了恢复时刻。
                err_texts.append(joined)
            return self.pool.is_queue_error(None, joined, code), code, has_content

        async def observe(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = message.get("headers", [])
                if not state["started"]:
                    # 扣留响应头，等首包判定是否排队。绝不能无上界地等下去。
                    state["held"] = True
                    return
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            payload = message.get("body", b"")
            more = message.get("more_body", False)
            if state["held"] and state["first_body"] is None:
                state["first_body"] = payload
                is_queue, code, has_content = sniff(payload)
                if code is not None:
                    state["error_code"] = code
                if is_queue and not has_content:
                    state["queue"] = True
                    state["code"] = code
                    # 记下被丢弃的原始分片，并继续收集后续分片：全部重试都用尽时
                    # 要把这份响应**原样**还给客户端（让它按 retry_after 轮询），
                    # 所以丢弃的内容必须留得住，不能只留个 code。
                    state["raw"] = [payload]
                    body_ready.set()
                    return
                # 放行：补发 start + 首个分片，之后走普通透传。
                state["held"] = False
                state["started"] = True
                body_ready.set()
                await send({"type": "http.response.start", "status": state["status"],
                            "headers": state["headers"]})
                await send({"type": "http.response.body", "body": payload, "more_body": more})
                return
            if state["queue"]:
                # 已判定排队、正在收集被丢弃的原始分片：继续累积，最终原样回放。
                state["raw"].append(payload)
                return
            if not state["started"]:
                # 兜底：没见过 start 就来了 body（不应发生），补发 start。
                state["started"] = True
                await send({"type": "http.response.start", "status": state["status"],
                            "headers": state["headers"]})
            self._track_usage(payload, state)
            await send(message)

        token = REQUEST_CREDENTIAL.set(manager)
        app_task = asyncio.ensure_future(self.app(scope, receive, observe))
        try:
            # 要么首包到达、要么硬上界到点——两者取先到者，绝不无限等待。
            done, _ = await asyncio.wait({app_task, asyncio.ensure_future(body_ready.wait())},
                                         timeout=hold_ms / 1000.0,
                                         return_when=asyncio.FIRST_COMPLETED)
            if state["queue"]:
                # 命中排队：下游应用已经结束（排队是完整小响应），回收它。
                if app_task.done():
                    app_task.exception()  # 吞掉异常：这次响应本来就要丢弃
                return {"kind": "queue", "code": state.get("code"),
                        "status": state["status"], "headers": state["headers"],
                        "body": b"".join(state.get("raw") or [])}
            if state["held"] and not state["started"]:
                # 窗口内没有首包：不是排队，是上游慢。原样放行，本次不切号。
                state["held"] = False
                state["started"] = True
                await send({"type": "http.response.start", "status": state["status"],
                            "headers": state["headers"]})
            await self._await_app(app_task)
        except asyncio.CancelledError:
            app_task.cancel()
            raise
        finally:
            REQUEST_CREDENTIAL.reset(token)
        # error_text / error_headers 交给调用方的 record()：失败冷却可能需要
        # 采信上游明示的恢复时刻（见 admin/reset_at.py）。error_code 供 WAF
        # 判定（见 admin/rotation.py）。
        return {"kind": "done", "status": state["status"], "tokens": state["tokens"],
                "usage": state.get("usage"),
                "error_text": " ".join(err_texts) or None,
                "error_code": state.get("error_code"),
                "error_headers": state["headers"]}

    @staticmethod
    def _text_of(outcome):
        """从被丢弃的原始响应体里取出可读文案，供冷却解析恢复时刻。"""
        body = outcome.get("body") or b""
        try:
            return body.decode("utf-8", "replace")[:2000] or None
        except Exception:
            return None

    @staticmethod
    async def _await_app(task):
        """等下游应用结束；它抛错时按「已放行」处理，不再尝试切号。"""
        try:
            await task
        except Exception:
            _logger.exception("下游应用在扣留期间异常结束")

    @staticmethod
    def _track_usage(payload, state):
        """从流里提取 usage（token 计数 + 可能的实测 credit）。

        旧的 observe 只留 total_tokens；实测成本账本还需要 credit，所以这里
        把整个 usage 对象留下最后一份副本（最后一次出现的 usage 才是最终用量）。
        """
        text = payload.decode("utf-8", "replace")
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            usage = obj.get("usage") or (obj.get("response") or {}).get("usage")
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if isinstance(total, (int, float)):
                state["tokens"] = max(state["tokens"], int(total))
            # 留最后一份完整 usage：账本要从这里读实测 credit。
            if isinstance(usage, dict):
                state["usage"] = usage

    async def _serve_once(self, scope, receive, send, aid, manager, kid, log_context, model=None):
        """开关关闭时的原路径：完全保持改动前的行为。"""
        token = REQUEST_CREDENTIAL.set(manager)
        status = 200
        buffer = b""
        usage_tokens = 0
        # 失败冷却可能要采信上游明示的恢复时刻（见 admin/reset_at.py）。
        resp_headers = []
        err_texts: list[str] = []
        # 最后一次出现的完整 usage：实测成本账本要读 credit。
        nonlocal_last_usage = [None]

        def extract_usage(obj):
            nonlocal usage_tokens
            usage = obj.get("usage") if isinstance(obj, dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if isinstance(total, (int, float)):
                usage_tokens = max(usage_tokens, int(total))
            if isinstance(usage, dict):
                # 账本要从这里读实测 credit。
                nonlocal_last_usage[0] = usage

        async def observe(message):
            nonlocal status, buffer
            if message["type"] == "http.response.start":
                status = message["status"]
                resp_headers = message.get("headers", [])
            if message["type"] == "http.response.body":
                buffer += message.get("body", b"")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            obj = json.loads(line[5:])
                            extract_usage(obj.get("response") or obj)
                            err = obj.get("error") or (obj.get("response") or {}).get("error") or {}
                            if isinstance(err, dict) and err.get("message"):
                                err_texts.append(str(err["message"]))
                            code = err.get("code") if isinstance(err, dict) else None
                            if isinstance(code, str) and code.isdigit():
                                code = int(code)
                            if code in (401, 402, 403, 404, 429):
                                status = code
                                log_context["error_code"] = code
                        except (ValueError, AttributeError):
                            pass
                if len(buffer) > 65536:
                    buffer = b""
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            REQUEST_CREDENTIAL.reset(token)
            self.pool.record(aid, status,
                             hint_text=" ".join(err_texts) or None,
                             hint_headers=resp_headers,
                             usage=nonlocal_last_usage[0], model=model)
            self.pool.note_usage(aid, model, usage_tokens or 0,
                                 _credits_of(nonlocal_last_usage[0]))
            if kid is not None:
                self.pool.store.record_key_usage(kid, tokens=usage_tokens or None)
            self.pool.flush()


def _replay_receive(raw, original_receive):
    """把已读进内存的请求体重放给下游应用（下游可能读多次）。

    ``raw`` 必须是 **bytes**。它会被原样放进 ASGI 的 ``http.request.body``，
    而下游用 Starlette 的 ``Request.stream()`` 收集请求体，内部是
    ``b"".join(chunks)``。传 dict 进去会抛
    ``TypeError: sequence item 0: expected a bytes-like object, dict found``，
    客户端侧表现为莫名其妙的 400「bad json」——所以这里显式挡住，别让它
    再以「客户端请求体损坏」的形式冒出来（历史上就是这么漏到线上的）。
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError(
            f"_replay_receive 需要 bytes，收到 {type(raw).__name__}；"
            "下游应用会用 b''.join() 拼接请求体")

    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": bytes(raw), "more_body": False}
        return await original_receive()
    return receive


"""Account-scoped billing, refresh, daily check-in and request routing."""
import asyncio
import hashlib
import json
import random
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

REQUEST_CREDENTIAL = ContextVar("pool_credential", default=None)
# 池中间件观察到的路由信息，交由外层 MetricsMiddleware 合并成一行 request_logs。
# 用 scope 传递而不是 ContextVar：middleware 是嵌套调用，内层 finally 的 reset
# 会先于外层读取执行，ContextVar 到外层就变回默认值了（实测确认）。
# scope 是同一条链上共享的可变对象，不受 finally 时序影响。
REQUEST_LOG_CONTEXT = "codebuddy.request_log"
CN = timezone(timedelta(hours=8))
BILLING = "https://www.codebuddy.cn/v2/billing/meter/"
# 上游用量明细接口在另一个域、且没有 /v2 前缀，不能复用 BILLING 常量。
# 注意参数名是驼峰 pageNum/pageSize（与同域 get-user-resource 的 PageNumber/PageSize 不同）。
USAGE_BILLING = "https://www.workbuddy.cn/billing/meter/"
USAGE_PAGE_SIZE = 100
USAGE_SYNC_DAYS = 30   # 拉取窗口必须 ≥ 同步周期，否则连续断档会留永久空洞
EXPIRY_SOON_DAYS = 3
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


class AccountPool:
    # 池设置默认值。Store._load 用它们补齐旧数据缺失的字段，
    # 这里保留 setdefault 兼容直接构造 Store 的老用法。
    # routing 不在这里：全局默认改由 settings.default_routing 提供，每 key 的策略在 keys.routing。
    POOL_DEFAULTS = {"auto_checkin": True, "checkin_time": "09:00",
                     "affinity_expiry_days": 3, "credit_min_interval": 60,
                     "sync_start": "10:00", "sync_end": "20:00", "batch_spread": 90,
                     "default_routing": "round_robin"}

    def __init__(self, store, clock=time.time, client_factory=None):
        self.store, self.clock = store, clock
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=20, follow_redirects=False))
        self.locks = {}
        self.cursor = 0
        self.jobs = asyncio.Lock()
        self.affinity_hits = 0
        self.affinity_rebinds = 0
        self.dirty = False
        self.last_flush = 0
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
                        "last_error": status.get("last_error"), "token_refreshed": status.get("token_refreshed"),
                        "request_count": status.get("request_count", 0),
                        # 上游用量同步留痕：不吐给 overview 的话「留痕」没人看得到，
                        # 出了同步故障只能翻库，等于没有这个功能。
                        "usage_synced_at": status.get("usage_synced_at"),
                        "usage_sync_error": status.get("usage_sync_error")})
        return rows

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

    def select(self, affinity_key=None, kid=None):
        """按 key 选账号：候选集 = key 的 accounts 白名单 ∩ 可用账号。

        kid=None 表示无 key 路径（管理页测试请求），此时不施加白名单。凭据失败后
        重试也必须传同一个 kid，否则会选到白名单外的账号。
        """
        with self.store.lock:
            candidates = [row for row in self.store.account_rows() if self.state(row) == "available"]
            if kid is not None:
                # 空白名单 = 不限制；只有显式列了账号才收窄候选集。
                whitelist = self.store.key_bindings(kid)[1]
                if whitelist:
                    candidates = [row for row in candidates if row["id"] in whitelist]
            if not candidates:
                raise HTTPException(503, "暂无可用账号：请检查暂停、积分、冷却或登录状态")
            routing = self._routing_for(kid)
            now = self.clock()
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

    def record(self, aid, status=200):
        with self.store.lock:
            if aid not in self.store.data["accounts"]:
                return
            data = self.store.data["account_status"].setdefault(aid, {})
            data["request_count"] = data.get("request_count", 0) + 1
            data["last_request"] = int(self.clock())
            if status in (401, 403, 402, 429):
                # 指数退避：连续失败翻倍冷却时长，上限 2 小时；成功请求重置计数。
                fails = data.get("consecutive_fails", 0) + 1
                data["consecutive_fails"] = fails
                base = 300 if status in (401, 403) else 1800
                cooldown = min(base * (2 ** (fails - 1)), 7200)
                label = "上游认证或访问被拒绝" if status in (401, 403) else "上游额度或频率限制"
                data.update(cooldown_until=self.clock() + cooldown, last_error=f"{label}，已冷却 {cooldown // 60} 分钟")
                if status == 402:
                    data["credits_updated"] = 0  # 触发下个周期立即重新查询积分
            else:
                data["consecutive_fails"] = 0
            self.dirty = True

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
            try:
                if action == "refresh":
                    with manager._lock:
                        manager._refresh()
                    self.update(aid, token_refreshed=int(self.clock()), auth_invalid=False, cooldown_until=0, last_error=None)
                    return {"id": aid, "ok": True, "message": "登录凭据已刷新"}
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
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError):
                # Do not expose raw refresh responses or bearer credentials.
                message = "操作失败：请检查登录凭据或网络，必要时重新授权"
                self.update(aid, last_error=message)
                return {"id": aid, "ok": False, "message": message}

    async def batch(self, action):
        if self.jobs.locked():
            raise HTTPException(409, "已有批量任务进行中，请稍后刷新")
        async with self.jobs:
            with self.store.lock:
                ids = list(self.store.data["accounts"])
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

    async def tick(self):
        now = datetime.fromtimestamp(self.clock(), CN)
        with self.store.lock:
            cfg = dict(self.store.data["pool"])
            due = [aid for aid, item in self.store.data["accounts"].items() if self.store.data["account_status"].get(aid, {}).get("checkin_date") != now.strftime("%Y-%m-%d") and self.store.data["account_status"].get(aid, {}).get("auto_attempt", 0) < self.clock()-1800 and self.store.data["account_status"].get(aid, {}).get("checkin_fails", 0) < 5]
        if cfg["auto_checkin"]:
            for aid in due:
                _, due_time = self.checkin_offset(aid, now.strftime("%Y-%m-%d"), 0)
                if now.strftime("%H:%M") >= due_time:
                    self.update(aid, auto_attempt=int(self.clock()))
                    try:
                        result = await asyncio.to_thread(self.operate, aid, "checkin")
                        fails = 0 if result.get("ok") else self.store.data["account_status"].get(aid, {}).get("checkin_fails", 0) + 1
                        self.update(aid, checkin_fails=fails)
                    except HTTPException:
                        pass
        # 低峰凭据预刷新：expiresAt 2 小时内到期的账号提前刷新，避免请求撞上 401 再冷却。
        # 凭据预刷新：分散到白天时段，每个账号每天随机一个时刻，模拟正常使用时刷新，
        # 避免半夜集中刷新。仅在 access token 两小时内到期时才动作。
        window = [cfg.get("sync_start") or "10:00", cfg.get("sync_end") or "20:00"]
        in_window = window[0] <= now.strftime("%H:%M") <= window[1]
        if in_window:
            today = now.strftime("%Y-%m-%d")
            for aid, item in list(self.store.data["accounts"].items()):
                status = self.store.data["account_status"].get(aid, {})
                if not item["enabled"] or status.get("refresh_window_date") == today:
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
                   if self.store.data["account_status"].get(aid, {}).get("credits_updated", 0)
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
                if not item["enabled"]:
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

    async def sync_upstream_usage_manual(self, start, end, account_id=None):
        """手动批量拉取：只打启用账号，走 jobs 并发锁防重入。

        被节流/失败的账号单独记在 results["skipped"] 里返回，不影响其它账号——
        连点按钮时应当看到「大部分已跳过」而不是整批报错。
        """
        with self.store.lock:
            targets = [aid for aid, item in self.store.data["accounts"].items()
                       if item["enabled"] and (not account_id or aid == account_id)]
        if not targets:
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
            # 管理页禁用的模型直接拒绝，不打上游。
            if self.model_registry is not None and isinstance(body, dict):
                model = body.get("model")
                if isinstance(model, str) and model and model not in self.model_registry.enabled_models() and model in self.model_registry.snapshot_ids():
                    raise HTTPException(400, f"模型 {model} 已在管理页禁用")
            # 密钥的模型白名单：空白名单 = 不限制；显式列了模型则只放行列表内的。
            if kid is not None and isinstance(body, dict):
                model = body.get("model")
                allowed_models = self.pool.store.key_bindings(kid)[0]
                if allowed_models and isinstance(model, str) and model and model not in allowed_models:
                    raise HTTPException(403, f"该密钥无权使用模型 {model}")
            affinity_key = request_affinity(headers, body)
            original_receive = receive
            delivered = False
            async def replay_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await original_receive()
            receive = replay_receive
            model_name = None
            if isinstance(body, dict) and isinstance(body.get("model"), str):
                model_name = body["model"]
            # Refresh before entering a stream; retry another eligible account only
            # if credential preparation fails, never replay a partially emitted response.
            # 重试分支也要传同一个 kid，否则凭据失败后可能选到该 key 白名单之外的账号。
            aid, manager = self.pool.select(affinity_key, kid)
            try:
                await asyncio.to_thread(manager.get_headers)
            except Exception:
                self.pool.update(aid, cooldown_until=self.pool.clock()+300, last_error="凭据暂不可用，已冷却 5 分钟")
                aid, manager = self.pool.select(affinity_key, kid)
                await asyncio.to_thread(manager.get_headers)
        except HTTPException as e:
            return await JSONResponse({"detail": e.detail}, e.status_code)(scope, receive, send)
        except Exception:
            return await JSONResponse({"detail": "账号凭据暂不可用，请刷新凭据或重新授权"}, 503)(scope, receive, send)
        log_context = {"key_id": kid, "account_id": aid,
                       "account_name": self.pool.store.data["accounts"].get(aid, {}).get("name"),
                       "model_id": model_name, "model_name": model_name,
                       "error_code": None}
        # 挂到 scope 上交给外层 MetricsMiddleware（同一条链共享，见 REQUEST_LOG_CONTEXT 注释）。
        scope[REQUEST_LOG_CONTEXT] = log_context
        token = REQUEST_CREDENTIAL.set(manager)
        status = 200
        buffer = b""
        usage_tokens = 0
        def extract_usage(obj):
            nonlocal usage_tokens
            usage = obj.get("usage") if isinstance(obj, dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if isinstance(total, (int, float)):
                usage_tokens = max(usage_tokens, int(total))  # 流式里 usage 块通常在最后，取最大值即总量
        async def observe(message):
            nonlocal status, buffer
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                buffer += message.get("body", b"")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            obj = json.loads(line[5:])
                            extract_usage(obj.get("response") or obj)
                            err = obj.get("error") or (obj.get("response") or {}).get("error") or {}
                            code = err.get("code") if isinstance(err, dict) else None
                            if isinstance(code, str) and code.isdigit():
                                code = int(code)
                            if code in (401, 402, 403, 429):
                                status = code
                                # 交由外层 MetricsMiddleware 统一决定生效状态码，这里只留痕。
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
            self.pool.record(aid, status)
            if kid is not None:
                self.pool.store.record_key_usage(kid, tokens=usage_tokens or None)
            self.pool.flush()

"""Account-scoped billing, refresh, daily check-in and request routing."""
import asyncio
import hashlib
import json
import math
import random
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core.backends.registry import backend_name_for
from core.backends.trae import TraeAPIError, interpret_checkin_claim

REQUEST_CREDENTIAL = ContextVar("pool_credential", default=None)
CN = timezone(timedelta(hours=8))
BILLING = "https://www.codebuddy.cn/v2/billing/meter/"
EXPIRY_SOON_DAYS = 3


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
    return {"remaining": float(remain), "capacity": float(size), "used": float(max(Decimal(0), size - remain)), "packages": len(packages),
            "earliest_expiry": earliest, "earliest_expiry_remaining": earliest_remaining,
            "expiring_soon": float(expiring_soon), "expiring_soon_updated": int(now)}


class BillingError(Exception):
    def __init__(self, message, status=502, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class AccountPool:
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
        with store.lock:
            store.data.setdefault("pool", {"routing": "round_robin", "auto_checkin": True, "checkin_time": "09:00", "affinity_expiry_days": 3, "credit_min_interval": 60, "sync_window": ["10:00", "20:00"], "batch_spread": 90})
            store.data.setdefault("account_status", {})
            store.data.setdefault("session_bindings", {})
            for item in store.data.get("keys", {}).values():
                item.setdefault("daily_limit", None)
                item.setdefault("usage", {})
            for item in store.data["accounts"].values():
                item.setdefault("backend", "workbuddy")
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
                old = status.get("remaining")
                new = values.get("remaining")
                # 记录积分余额变化：按天滚动存 (时间戳, 变化量)，用于消耗速率与预测。
                if isinstance(new, (int, float)) and isinstance(old, (int, float)) and old != new:
                    today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
                    history = status.setdefault("usage_history", [])
                    if not history or history[-1].get("date") != today:
                        history.append({"date": today, "samples": []})
                    history[-1]["samples"].append([int(self.clock()), round(old - new, 2)])
                    # 只保留最近 30 天，每天最多 288 个采样点（足够覆盖 5 分钟~30 分钟同步频率）。
                    del history[:-30]
                    if len(history[-1]["samples"]) > 288:
                        del history[-1]["samples"][:len(history[-1]["samples"]) - 288]
                    self.dirty = True
                status.update(values)
                self.dirty = True

    def flush(self, force=False):
        """Debounced persistence: per-request updates only mark dirty; tick flushes every 5s."""
        with self.store.lock:
            if self.dirty and (force or self.clock() - self.last_flush >= 5):
                self.store.save()
                self.dirty = False
                self.last_flush = self.clock()

    def state(self, row):
        status = self.store.data["account_status"].get(row["id"], {})
        if not row["enabled"]:
            return "paused"
        if row["status"] == "invalid" or status.get("auth_invalid"):
            return "invalid"
        if status.get("cooldown_until", 0) > self.clock():
            return "cooling"
        if status.get("credits_updated", 0) > self.clock() - 600 and status.get("remaining") == 0 and status.get("packages", 0) > 0:
            return "exhausted"
        return "available"

    def consumption(self, status):
        """从 usage_history 估算日消耗速率（近 7 天，剔除余额上涨样本如签到/充值）。"""
        history = status.get("usage_history", [])
        if not history:
            return None
        now = self.clock()
        cutoff = now - 7 * 86400
        total_spent, span_start, span_end = 0.0, None, None
        for day in history:
            for ts, delta in day.get("samples", []):
                if ts < cutoff or delta <= 0:
                    continue
                total_spent += delta
                span_start = ts if span_start is None else min(span_start, ts)
                span_end = ts if span_end is None else max(span_end, ts)
        if not total_spent or span_start is None or not span_end or span_end <= span_start:
            return None
        days = max((span_end - span_start) / 86400, 0.5)
        return {"daily_rate": round(total_spent / days, 2), "spent_7d": round(total_spent, 2)}

    def rows(self, rows):
        today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
        for row in rows:
            status = self.store.data["account_status"].get(row["id"], {})
            consumption = self.consumption(status)
            expiry = status.get("earliest_expiry")
            remaining = status.get("remaining")
            # 过期前用不完的积分：整号剩余 - 到期前按当前速率能消耗的量（粗估）。
            expire_unused = None
            if expiry and consumption and isinstance(remaining, (int, float)) and remaining > 0:
                burnable = max(0.0, consumption["daily_rate"] * max((expiry - self.clock()) / 86400, 0))
                expire_unused = round(max(0.0, remaining - burnable), 2)
            row.update({"pool_state": self.state(row), "remaining": remaining, "credits_updated": status.get("credits_updated"),
                        "credits_stale": status.get("credits_updated", 0) < self.clock() - 600,
                        "earliest_expiry": expiry, "earliest_expiry_remaining": status.get("earliest_expiry_remaining"),
                        "expiring_soon": status.get("expiring_soon"),
                        "expiring_soon_updated": status.get("expiring_soon_updated"),
                        "weight": int(status.get("weight", 0)),
                        "daily_rate": consumption["daily_rate"] if consumption else None,
                        "days_to_empty": round(remaining / consumption["daily_rate"], 1) if consumption and isinstance(remaining, (int, float)) and consumption["daily_rate"] > 0 else None,
                        "expire_unused": expire_unused,
                        "checkin_slot": status.get("checkin_slot"), "checkin_when": status.get("checkin_when"),
                        "today_checked_in": status.get("checkin_date") == today, "cooldown_until": status.get("cooldown_until", 0),
                        "last_error": status.get("last_error"), "token_refreshed": status.get("token_refreshed"),
                        "request_count": status.get("request_count", 0)})
        return rows

    def order_candidates(self, candidates):
        """Sort by weight desc, then earliest credit expiry asc so the soonest-to-expire
        account is consumed first; unknown expiry sorts last within the same weight."""
        def key(row):
            status = self.store.data["account_status"].get(row["id"], {})
            expiry = status.get("earliest_expiry")
            return (-int(status.get("weight", 0)), expiry is None, expiry or 0)
        return sorted(candidates, key=key)

    def select(self, affinity_key=None, backend=None):
        with self.store.lock:
            candidates = [row for row in self.store.account_rows() if self.state(row) == "available"]
            # 按后端隔离：trae 请求只分给 Trae 账号，非 trae 请求只分给 WorkBuddy 账号。
            # backend=None 时不过滤（旧调用/测试）。
            if backend is not None:
                candidates = [row for row in candidates if row.get("backend", "workbuddy") == backend]
            if self.store.data["pool"]["routing"] == "manual":
                candidates = [row for row in candidates if row["id"] == self.store.data["active"]]
            if not candidates:
                raise HTTPException(503, "暂无可用账号：请检查暂停、积分、冷却或登录状态")
            now = self.clock()
            bindings = self.store.data["session_bindings"]
            for key in list(bindings):
                if bindings[key].get("expires", 0) <= now:
                    del bindings[key]
            bound = bindings.get(affinity_key, {}).get("account_id")
            if bound in {row["id"] for row in candidates} and self.store.data["pool"]["routing"] != "manual":
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
            else:
                ordered = self.order_candidates(candidates)
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
            backend = self.store.data["accounts"][aid].get("backend", "workbuddy")
            if status == 404 and backend == "trae":
                # Trae 404＝该 config_name 不存在/不可用：软冷却 10 分钟，不计入连续失败
                # （账号本身通常是好的，不升级到 2h 硬冷却）。
                data.update(cooldown_until=self.clock() + 600,
                            last_error="模型不可用（404），已软冷却 10 分钟")
            elif status in (401, 403, 402, 429):
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
                # Trae 账号：刷新（上方）走 ExchangeToken；积分/签到走 ug 接口。
                if item.get("backend", "workbuddy") == "trae":
                    return self._operate_trae(aid, manager, action)
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
            except TraeAPIError as e:
                # Trae ug 接口失败：401/403＝凭据失效（冷却 + 标记待重新授权），其余原样暴露。
                if e.status in (401, 403):
                    self.update(aid, auth_invalid=True, cooldown_until=self.clock() + 300,
                                last_error="Trae 登录凭据已失效，请重新授权")
                    return {"id": aid, "ok": False, "message": "Trae 登录凭据已失效，请重新授权"}
                self.update(aid, last_error=str(e))
                return {"id": aid, "ok": False, "message": str(e)}
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError):
                # Do not expose raw refresh responses or bearer credentials.
                message = "操作失败：请检查登录凭据或网络，必要时重新授权"
                self.update(aid, last_error=message)
                return {"id": aid, "ok": False, "message": message}

    def _operate_trae(self, aid, manager, action):
        """Trae 账号的积分/签到：ug 接口（checkin_credits status/claim + ide_user_ent_usage）。

        与 WorkBuddy 路径同构：先查签到状态 → 按需执行签到 → 拉积分余额。
        签到查询失败不阻断积分拉取（action=checkin 除外，必须显式报错）。
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
                        # 9074 等＝"人多稍后再试"（容量限制），对 claim 本身做有界重试；
                        # 其余非 0＝明确失败，透出上游 message（而非误报"尚未确认"）。
                        confirmed = False
                        last_msg = "签到失败，请稍后再试"
                        for attempt in range(4):
                            ok, msg, retryable = interpret_checkin_claim(manager.checkin_claim(client))
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
            remain = manager.user_ent_usage(client)
            # remain>0 说明至少有一个权益包；为 0 时不置 packages（避免把「无权益包」
            # 误判成 exhausted，真实耗尽由上游 1005→402 冷却兜底）。
            self.update(aid, remaining=float(remain), packages=1 if remain > 0 else 0,
                        credits_updated=int(self.clock()), auth_invalid=False, last_error=checkin_error)
            if remain > 0 and action == "checkin":
                self.update(aid, cooldown_until=0)
            return {"id": aid, "ok": True, "message": message, "remaining": float(remain)}

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
            # 自动签到覆盖两后端（WorkBuddy 走 billing，Trae 走 ug checkin_credits）。
            due = [aid for aid, item in self.store.data["accounts"].items() if item.get("backend", "workbuddy") in ("workbuddy", "trae") and self.store.data["account_status"].get(aid, {}).get("checkin_date") != now.strftime("%Y-%m-%d") and self.store.data["account_status"].get(aid, {}).get("auto_attempt", 0) < self.clock()-1800 and self.store.data["account_status"].get(aid, {}).get("checkin_fails", 0) < 5]
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
        window = cfg.get("sync_window") or ["10:00", "20:00"]
        in_window = len(window) == 2 and window[0] <= now.strftime("%H:%M") <= window[1]
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
            # 模型名 → 后端：trae/ 前缀分给 Trae 账号，其余分给 WorkBuddy 账号。
            backend = backend_name_for(body.get("model") if isinstance(body, dict) else None)
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
            aid, manager = self.pool.select(affinity_key, backend)
            # Refresh before entering a stream; retry another eligible account only
            # if credential preparation fails, never replay a partially emitted response.
            try:
                await asyncio.to_thread(manager.get_headers)
            except Exception:
                self.pool.update(aid, cooldown_until=self.pool.clock()+300, last_error="凭据暂不可用，已冷却 5 分钟")
                aid, manager = self.pool.select(affinity_key, backend)
                await asyncio.to_thread(manager.get_headers)
        except HTTPException as e:
            return await JSONResponse({"detail": e.detail}, e.status_code)(scope, receive, send)
        except Exception:
            return await JSONResponse({"detail": "账号凭据暂不可用，请刷新凭据或重新授权"}, 503)(scope, receive, send)
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
                            if code in (401, 402, 403, 404, 429):
                                status = code
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

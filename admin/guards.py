"""账号级限额护栏：在**本地**拦住会把账号烧穿的请求。

上游只告诉你「余额还剩多少」，不会阻止你把余额用尽。四条护栏补上这一层，
各自的作用域与后果都不同：

| 护栏 | 触发后行为 | 防的是什么 |
|---|---|---|
| ``reserve_credits`` | 账号整体不再接单 | 余额被用尽后触发上游的提醒 / 风控 |
| ``daily_token_limit`` | 账号整体不再接单 | 单账号一天吃掉过多 token |
| ``daily_credit_limit`` | 账号**只服务免费模型** | 付费模型把当天积分额度烧光 |
| ``model_daily_token_limit`` | 该账号**仅该模型**不再接单 | 单账号单模型撞上游 24h 窗口配额 |

## 三条不可动摇的语义

1. **``0`` = 关闭**。默认全关：装上护栏之前的老部署行为逐字不变。
   护栏是「替用户做决定」的功能，默认值必须是无为。
2. **计数未知时绝不停用**。用量读不出来（库异常、还没聚合过）时计数是
   ``None``，护栏**放行**而不是停用。宁可放过一次请求，也不能因为读不到
   数据就把整个账号池判死——那比被烧穿严重得多。
3. **按本地 0 点解封**，与上游的日窗口一致。护栏只在**请求路径**生效，
   定时任务（签到 / 保活 / 领取）不受影响——任务本身不消耗对话额度，
   被护栏挡住只会白丢功能。

## 免费 / 付费怎么判

判据是**实测成本账本**（``admin/cost_ledger.py``）：账本认为该账号跑该模型
免费，才算免费。账本没观测过的模型一律按**付费**处理——「不确定是否免费」
不该被当作免费放行，否则 ``daily_credit_limit`` 形同虚设。
"""

from __future__ import annotations

import threading

# 护栏键名，与 settings 的 guard_* 列一一对应。
GUARD_KEYS = (
    "reserve_credits",
    "daily_token_limit",
    "daily_credit_limit",
    "model_daily_token_limit",
)

# 设置项名 -> 护栏键名。
SETTING_PREFIX = "guard_"

# 用量快照缓存时长（秒）。请求路径上每次选号都查库太重；护栏本身是分钟级
# 语义，几秒的陈旧度完全可以接受。
USAGE_TTL_SECONDS = 5


class DailyUsage:
    """本地当天（0 点起算）的账号用量快照。"""

    def __init__(self):
        self.tokens: dict[str, int] = {}
        self.credits: dict[str, float] = {}
        self.models: dict[str, dict[str, int]] = {}

    def tokens_for(self, aid) -> int:
        return int(self.tokens.get(str(aid), 0))

    def credits_for(self, aid) -> float:
        return float(self.credits.get(str(aid), 0.0))

    def model_tokens_for(self, aid, model) -> int:
        return int((self.models.get(str(aid)) or {}).get(str(model), 0))

    def note(self, aid, model, tokens, credits):
        aid = str(aid)
        self.tokens[aid] = self.tokens.get(aid, 0) + int(tokens or 0)
        self.credits[aid] = self.credits.get(aid, 0.0) + float(credits or 0.0)
        if model:
            bucket = self.models.setdefault(aid, {})
            bucket[str(model)] = bucket.get(str(model), 0) + int(tokens or 0)


def parse_guard_limits(pool_cfg) -> dict:
    """从池设置里解析四条护栏阈值。``0`` = 关闭，非法值一律当 0（关闭）。"""
    limits = {}
    for key in GUARD_KEYS:
        raw = (pool_cfg or {}).get(SETTING_PREFIX + key, 0)
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            value = 0
        limits[key] = max(0, value)
    return limits


def any_enabled(limits) -> bool:
    return any(int(v or 0) > 0 for v in limits.values())


def guard_block_reason(limits, *, credits, daily_tokens, daily_credits,
                       model, model_daily_tokens, is_free_model):
    """这个账号此刻能不能接单？返回原因（空串 = 可以）。

    计数为 ``None`` 一律放行（见模块文档第 2 条）。
    """
    if not any_enabled(limits):
        return ""

    reserve = int(limits.get("reserve_credits", 0) or 0)
    if reserve > 0 and credits is not None and int(credits) <= reserve:
        return f"保留余额 {reserve}（当前 {int(credits)}）"

    limit = int(limits.get("daily_token_limit", 0) or 0)
    if limit > 0 and daily_tokens is not None and int(daily_tokens) >= limit:
        return f"今日 token 已达上限 {limit}"

    limit = int(limits.get("daily_credit_limit", 0) or 0)
    if limit > 0 and daily_credits is not None and float(daily_credits) >= limit \
            and not is_free_model:
        return f"今日积分已达上限 {limit}，仅免费模型可用"

    limit = int(limits.get("model_daily_token_limit", 0) or 0)
    if limit > 0 and model and isinstance(model_daily_tokens, dict):
        used = int(model_daily_tokens.get(str(model), 0) or 0)
        if used >= limit:
            return f"模型 {model} 今日 token 已达上限 {limit}"
    return ""


class DailyUsageCache:
    """带 TTL 的当天用量缓存。

    请求路径上每个候选账号都要判一次护栏，每次查库不现实（选号是热路径）。
    护栏本身是分钟级语义，5 秒的陈旧度无影响。
    """

    def __init__(self, ttl_seconds: int = USAGE_TTL_SECONDS, clock=None):
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._clock = clock or (lambda: __import__("time").time())
        self._lock = threading.Lock()
        self._at = 0.0
        self._usage: DailyUsage | None = None
        self._loaded = False

    def get(self, loader, *, refresh: bool = False) -> DailyUsage | None:
        """取当天用量。``loader`` 返回 ``DailyUsage`` 或 ``None``（读不到）。

        返回 ``None`` 是**有意义**的信号：调用方必须把它当「计数未知」放行。
        """
        now = self._clock()
        with self._lock:
            if not refresh and self._loaded and (now - self._at) < self.ttl_seconds:
                return self._usage
        try:
            usage = loader()
        except Exception:  # 读不到 = 未知，由调用方放行；绝不让护栏因此停用账号
            usage = None
        with self._lock:
            self._at = now
            self._usage = usage
            self._loaded = True
            return self._usage

    def invalidate(self):
        with self._lock:
            self._loaded = False
            self._usage = None

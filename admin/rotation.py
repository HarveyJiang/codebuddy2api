"""轮转退避抖动，以及 WAF 的 IP 级拦截 fail-fast。

## 为什么轮转换号要加抖动

换号之间零间隔本身就是可被统计的节奏特征：上游看到的是「一个出口 IP 在
几十毫秒内用不同身份连续发请求」。但固定周期退避同样不行——多个客户端请求
会在同一时刻失败、同一时刻重试，**再次聚团**，而 WAF 的频控恰恰按密度判罚。
所以退避必须带抖动打散相位。

## 为什么需要 IP 级 fail-fast

`403` 有时拦的是**网关的出口 IP**，而不是某个账号。这种情况账号级软冷却不够：
轮转会继续拿别的号打同一个 IP，把一次客户端请求**放大成 N 次上游请求**，
越转越糟（本项目已知的 11128 WAF 拦截就是这类，实测换遍全池 8 个账号全被拦）。

判定口径刻意选「**短窗内数不同账号**」而不是「同一账号连续失败次数」：
单号反复 403 属于账号自己的偶发问题，归软冷却管；而两个**不同**账号在 60 秒内
接连被拦，就只能是出口 IP 层面的问题了。这样既不会误伤单个坏号，又能在
放大发生之前就止损。

## 与账号级冷却的关系

协同、不叠加。账号级状态照常记账（该冷却的号照常冷却），IP 级状态只回答
一个问题：**这一轮还要不要继续换号**。
"""

from __future__ import annotations

import random
import threading

# 轮转退避基数。500ms 是官方 CLI 的形态，够短不至于让客户端等出超时，
# 又足以让频控窗口滑过去。
BACKOFF_BASE = 0.5

# 退避封顶。轮转次数本身有上限（queue_retry，默认 3），实际等待序列只有
# 500ms / 1s 两级；封顶只是防御性约束，防止有人把重试次数配得很大。
BACKOFF_CAP = 8.0

# 抖动比例（±25%）。取这个量级是因为它足以打散相位，又不会让短退避
# 变得偏离预期太多（500ms 最坏也只是 625ms）。
JITTER_FRACTION = 0.25

# IP 级判定的滑动窗口，也是激活后的持续时长（到期自然解除）。
WAF_IP_WINDOW = 60.0

# 判定阈值：窗内**不同账号**命中 WAF 达到该值即激活。
#
# 取 2 是「多号」的最小定义。取 1 会把任何单号的偶发 403 误判成 IP 被封，
# 从而停掉整个池子的轮转——那比问题本身更糟。
WAF_IP_THRESHOLD = 2

# 视为 WAF/通道拦截的上游错误码。
#
# 11128 是证据最充分的一个："Illegal API invocation from an unapproved
# channel"，实测换遍全池所有账号都被拦，与账号无关。
WAF_CODES = frozenset({"11128"})

# 已知后端名。snapshot() 恒定列出这几个桶，好让管理页拿到**稳定**的结构——
# 只列「碰巧出现过」的后端，前端就得处理桶随时出现/消失的情况。
KNOWN_BACKENDS = ("workbuddy", "trae")

# WAF 拦截的文案兜底。**只作兜底**：主判据是结构化 code，上游改文案时
# 结构化码还在；反过来只靠文案会在编解码差异下漏判。
WAF_PATTERNS = (
    "unapproved channel",
    "illegal api invocation",
    "unapproved",
)


def is_waf_error(status, text=None, code=None) -> bool:
    """这次失败是不是 WAF/通道拦截（因而可能属于出口 IP 而非账号）。"""
    code_text = str(code or "").strip()
    if code_text in WAF_CODES:
        return True
    if isinstance(status, int) and status == 403:
        # 403 未必是 WAF（也可能是账号被禁），但加上文案就能区分：
        # 账号被禁的文案与「通道未授权」完全不同。
        blob = (text or "").lower()
        return any(pattern in blob for pattern in WAF_PATTERNS)
    blob = (text or "").lower()
    return any(pattern in blob for pattern in WAF_PATTERNS)


def jitter_duration(seconds: float, *, rng=None) -> float:
    """给时长施加 ±JITTER_FRACTION 的均匀抖动。

    ``<= 0`` 原样返回（零等待没什么可抖的）。
    """
    if seconds <= 0:
        return seconds
    draw = (rng or random.random)()
    factor = 1 + (draw * 2 - 1) * JITTER_FRACTION
    return max(0.0, seconds * factor)


def backoff_after(n: int, base: float = BACKOFF_BASE, cap: float = BACKOFF_CAP) -> float:
    """第 n 次轮转（0 基：首次失败、换下一个号之前 n=0）应等待的秒数。

    ``base · 2^n`` 封顶 ``cap``，再施加 ±25% 抖动。``base <= 0`` 恒定 0
    （关闭退避，便于测试与排障时对齐行为）。
    """
    if base <= 0:
        return 0.0
    value = base
    for _ in range(max(0, n)):
        if value >= cap:
            break
        value *= 2
    return jitter_duration(min(value, cap))


class WafIpGate:
    """WAF IP 级拦截状态机（按出口 IP 分桶）。

    进程内状态，重启清零——窗口只有 60 秒，重建成本极低，落库反而会带来
    「重启后继承一个可能早已过期的封锁」的风险。

    **按 backend 分桶**：判定依据是「同一个出口 IP 被不同账号接连拦」，而
    两个后端用的是完全不同的凭据体系与上游域名。把它们混在一个桶里，字节
    的 403 会把腾讯的轮转也一起停掉——这正是「加了字节就不该影响腾讯」的
    反面。分桶后互不干扰。
    """

    def __init__(self, *, window: float = WAF_IP_WINDOW,
                 threshold: int = WAF_IP_THRESHOLD, clock=None):
        self.window = float(window)
        self.threshold = max(1, int(threshold))
        self._clock = clock or (lambda: __import__("time").time())
        # backend -> {account_id: 命中时刻}
        self._hits: dict[str, dict[str, float]] = {}
        # backend -> 解除时刻
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def note_waf(self, account_id, now=None, backend="workbuddy") -> bool:
        """记一次某账号的 WAF 拦截；返回记账后该后端的 IP 级拦截是否激活。

        调用方据此 fail-fast 终止轮转（优先于退避等待）。

        - 已激活：不续期、不记账。窗口期内不重置是刻意的——自然解除即可，
          不必因为持续有拦截就把封锁无限延长（那会让一次误判变成长期停摆）。
        - 未激活：记下这个账号，剪掉窗外的过期命中，然后数**不同账号数**。
          同一个账号重复命中只覆盖时间、不增加计数，因为判定口径是
          「有几个不同的号被打」而不是「被打了几次」。
        - 达到阈值才激活，并清空该桶的窗口：解除后需要全新的一批命中才能
          再次判定，不会拿旧账立刻重新触发。
        """
        bucket = str(backend or "workbuddy")
        current = float(now if now is not None else self._clock())
        with self._lock:
            if current < self._until.get(bucket, 0.0):
                return True
            hits = self._hits.setdefault(bucket, {})
            hits[str(account_id)] = current
            for other, at in list(hits.items()):
                if current - at > self.window:
                    del hits[other]
            if len(hits) >= self.threshold:
                self._until[bucket] = current + self.window
                self._hits[bucket] = {}
                return True
            return False

    def active(self, now=None, backend="workbuddy") -> bool:
        """该后端的 IP 级拦截是否处于激活期。"""
        current = float(now if now is not None else self._clock())
        bucket = str(backend or "workbuddy")
        with self._lock:
            return current < self._until.get(bucket, 0.0)

    def reset(self, backend=None) -> None:
        """清空状态。``backend=None`` 清空全部（管理页手动解除或测试用）。"""
        with self._lock:
            if backend is None:
                self._hits.clear()
                self._until.clear()
                return
            bucket = str(backend)
            self._hits.pop(bucket, None)
            self._until.pop(bucket, None)

    def snapshot(self, now=None, backend=None) -> dict:
        """供后台透出：还剩多久、窗口里有几个号。

        ``backend=None`` 时返回**按后端分列**的汇总——两个后端的状态要能
        各自独立判断，不能只看到一个总数。
        """
        current = float(now if now is not None else self._clock())
        with self._lock:
            def one(bucket):
                return {"active": current < self._until.get(bucket, 0.0),
                        "remaining_seconds": int(max(0.0, self._until.get(bucket, 0.0) - current)),
                        "accounts_in_window": len(self._hits.get(bucket, {})),
                        "threshold": self.threshold,
                        "window_seconds": int(self.window)}
            if backend is not None:
                return one(str(backend))
            return {"by_backend": {b: one(b) for b in KNOWN_BACKENDS},
                    "threshold": self.threshold,
                    "window_seconds": int(self.window)}

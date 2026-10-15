"""上游「重置时刻」的统一解析。

## 为什么需要

限流类错误的响应里，上游常常**明说**了什么时候恢复。而本地只能猜一个冷却时长，
两个口径的后果完全不同：

- **猜短了**：冷却窗口内账号被放回候选集，于是一次又一次撞同一堵墙。每次撞墙
  都是一次失败请求，也都是一次风控痕迹——越撞越糟。
- **猜长了**：健康的账号被白白扣住几小时，额度用不掉。

因此只要上游给了时刻，就以它为准；拿不到才回落到本地有界退避。

## 三级来源（按权威性排序）

1. **body 文案** —— 上游直接写给人看的恢复时刻（如 `将在 2026-10-15 10:00:00 重置`）。
   最权威，且最便宜（一次正则）。
2. **body 结构化字段** —— `data.resets_at` / `retry_after` 之类。上游不保证发，
   但发了比文案精确。排在文案之后是刻意的：文案是**已知存在**的形态，字段是
   **可能存在**的形态，扫全 body 的代价更高。
3. **响应头** —— `Retry-After` / `Retry-After-Ms` / `X-RateLimit-Reset`。
   语义是「多久后可再试」，精度最低，属兜底。

三级都拿不到 → 返回 `None`，调用方回落本地有界退避。**绝不臆造时间**：臆造一个
「看起来正常」的时刻，会让排障时分不清「上游说的」与「我们猜的」。

## 时区（最容易错的地方）

上游文案里的墙钟是**上游本地时区（UTC+8）**。把无后缀墙钟当 UTC 解析会整体偏
8 小时——偏早 8 小时等于冷却几乎立刻到期（窗口内反复撞墙），偏晚 8 小时等于白扣
半天。两种错都比「没有时间信息」更糟，所以显式固定 UTC+8，不交给运行环境时区。

    形态                                处理
    ----------------------------------  -------------------------------------
    2026-10-15 10:00:00（无后缀）        按 UTC+8 解释
    2026-10-15T10:00:00+08:00           RFC3339：按自带偏移解析（最精确）
    2026-10-15T10:00:00Z                UTC 时刻
    1791972000 / "1791972000"           epoch 秒
    1791972000000                       epoch 毫秒（>= 1e12）
    3600（< 1e8）                        相对秒数 → now + 3600s

## 边界情况

- **缺失**：返回 None，调用方回落本地退避，不延长也不缩短。
- **已过期**：解析到了但不在未来 → 同样按「没有时间信息」处理，而**不是**立刻恢复。
  立刻恢复会让并发重试同秒再吃一次同样的错误（热循环）；本地退避最短也是秒级，
  足以让频控窗口滑过去。
- **并发**：由调用方取更晚的截止时刻，绝不因响应乱序而缩短已有冷却。
"""

from __future__ import annotations

import datetime as _dt
import re

# 上游墙钟的时区偏移（分钟）。腾讯后端文案用北京时间。
_UPSTREAM_TZ = _dt.timezone(_dt.timedelta(hours=8))

# 正则形式的时间戳：2026-10-15 10:00:00 / 2026-10-15T10:00:00（可选小数秒与时区后缀）
_WALLCLOCK = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?"
)

# 文案里「多久之后」的表述：如「5 分钟后重试」。取相对量，避免时区问题。
_RELATIVE = re.compile(r"(\d+)\s*(?:秒|s(?:ec(?:onds?)?)?)\s*(?:后|之后|再)", re.IGNORECASE)
_RELATIVE_MIN = re.compile(r"(\d+)\s*(?:分钟|分|min(?:utes?)?)\s*(?:后|之后|再)", re.IGNORECASE)

# 上游译文里常见的「重置」语境词。命中才认这条文案，避免把回答正文里的
# 日期当成限流时刻（正文里出现 "2026-10-15" 完全正常）。
_RESET_HINT = re.compile(
    r"重置|恢复|再试|重试|retry|reset|限流|限制|配额|quota|rate\s*limit|too\s*many",
    re.IGNORECASE,
)


def _now() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def _parse_wallclock(match: re.Match) -> float | None:
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    suffix = match.group(7)
    try:
        if suffix:
            # RFC3339 风格：按自带偏移解析（最精确）。
            offset_text = suffix
            if suffix == "Z":
                tz = _dt.timezone.utc
            else:
                sign = 1 if offset_text[0] == "+" else -1
                body = offset_text[1:].replace(":", "")
                tz = _dt.timezone(sign * _dt.timedelta(hours=int(body[:2]), minutes=int(body[2:4])))
            moment = _dt.datetime(year, month, day, hour, minute, second, tzinfo=tz)
            return moment.timestamp()
        # 无后缀：固定按上游时区解释，绝不用运行环境的本地时区。
        moment = _dt.datetime(year, month, day, hour, minute, second, tzinfo=_UPSTREAM_TZ)
        return moment.timestamp()
    except (ValueError, OverflowError):
        return None


def parse_reset_at(text: str | None, *, now: float | None = None) -> float | None:
    """从一段上游文案里解析出恢复时刻（unix 秒）。

    只认「明确在说限流/重试/重置」的文本：正文里随手出现的日期不该被当成冷却依据。
    解析不到、或算出已过期 → 返回 None（调用方回落本地退避）。
    """
    if not text or not isinstance(text, str):
        return None
    current = _now() if now is None else now
    if not _RESET_HINT.search(text):
        return None

    # 先试绝对时刻；文案里的相对量（「5 分钟后」）优先级更低，因为绝对时刻
    # 是上游算好的，相对量还要叠加下游的处理延迟。
    match = _WALLCLOCK.search(text)
    if match:
        stamp = _parse_wallclock(match)
        if stamp is not None and stamp > current:
            return stamp

    match = _RELATIVE.search(text)
    if match:
        seconds = int(match.group(1))
        if 0 < seconds < 86400 * 30:
            return current + seconds
    match = _RELATIVE_MIN.search(text)
    if match:
        seconds = int(match.group(1)) * 60
        if 0 < seconds < 86400 * 30:
            return current + seconds
    return None


def parse_retry_after(headers, now: float | None = None) -> float | None:
    """解析响应头里的重试提示 → 绝对时刻。

    支持 ``Retry-After``（秒数或 HTTP-date）、``Retry-After-Ms``（毫秒）、
    ``X-RateLimit-Reset``（epoch 秒，部分实现给相对秒数）。

    ``headers`` 可以是 httpx 响应、Header 映射，或 ``{bytes: bytes}`` 字典——
    三种形态在本项目里都出现过，统一在这里吃掉。
    """
    current = _now() if now is None else now
    getter = _header_getter(headers)
    if getter is None:
        return None

    raw_ms = getter("retry-after-ms")
    if raw_ms:
        try:
            value = float(raw_ms)
        except (TypeError, ValueError):
            value = None
        if value is not None and value > 0:
            return current + value / 1000.0

    raw = getter("retry-after")
    if raw:
        raw = raw.strip()
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            # HTTP-date 形态：上游较少用，但支持了不费事。
            parsed = _parse_http_date(raw)
            if parsed is not None and parsed > current:
                return parsed
        else:
            if seconds > 0:
                return current + seconds

    raw_reset = getter("x-ratelimit-reset")
    if raw_reset:
        try:
            value = float(raw_reset.strip())
        except (TypeError, ValueError):
            value = None
        if value is not None and value > 0:
            # epoch 秒与相对秒数用数量级区分：epoch 秒现在是 1.7e9 量级。
            return value if value >= 1e9 else current + value
    return None


def _header_getter(headers):
    """把三种 header 容器归一成一个 (小写名) -> str 的取值函数。

    支持的容器：``httpx.Headers``/``httpx.Response``（大小写不敏感）、ASGI 的
    ``{bytes: bytes}``、以及普通 ``{str: str}`` 字典。

    统一走「遍历 + 小写比较」而不是多次 ``.get`` 变体：三种容器的键类型与
    大小写规则各不相同，逐个试很容易漏（``httpx.Headers`` 吃不了 bytes 键，
    普通字典的 ``X-RateLimit-Reset`` 也不会被 ``title()`` 命中）。
    """
    if headers is None or not hasattr(headers, "items"):
        return None

    def _get(name):
        target = name.lower()
        for key, value in headers.items():
            if isinstance(key, bytes):
                key = key.decode("latin-1")
            if isinstance(key, str) and key.lower() == target:
                if isinstance(value, bytes):
                    value = value.decode("latin-1")
                return value if isinstance(value, str) else None
        return None

    return _get


def _parse_http_date(text: str) -> float | None:
    from email.utils import parsedate_to_datetime

    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.timestamp()


def earliest_deadline(*candidates: float | None) -> float | None:
    """并发场景下取「更晚」的截止时刻：多个响应乱序到达时绝不缩短已有冷却。"""
    values = [value for value in candidates if value]
    if not values:
        return None
    return max(values)

"""日志脱敏：把凭证从日志里抹掉，让排障可以直接贴日志。

## 为什么

网关的排障现场几乎总要贴日志，而日志里最容易夹带的正是凭证：上游返回的
错误体里带 token、请求头被顺手打进 DEBUG、异常堆栈里带 URL query。
这些东西一旦落到聊天窗口或 issue 里就是泄露。

## 挂在 Filter 上而不是逐处改调用

逐处改 ``_log(...)`` 一定会漏——本项目有 7 处以上打 ``httpx`` 异常的调用点，
还有第三方库（httpx/uvicorn）自己打的日志。挂成 ``logging.Filter`` 装在
handler 上，**所有**经该 handler 的记录都会被过一遍，包括他人的库。

## 保留什么 / 抹掉什么

- 抹掉：Bearer/Basic 凭证、JWT、各厂商 key 前缀、URL query 里的 token/secret、
  Cookie、data URI 的负载（图片 base64 动辄几百 KB，既泄露又刷屏）。
- **保留**：普通 hex / UUID / 请求 id。它们看着像 secret，实际是有用的 trace id，
  一并抹掉会让日志彻底失去排障价值。

## 上限

字符串长度、嵌套深度、节点数都有硬上限：一条恶意超大 body 不该把日志路径拖垮
（脱敏本身也是正则扫描，没有上限就是一个放大器）。
"""

from __future__ import annotations

import logging
import re

_PLACEHOLDER = "[已脱敏]"

# JWT：三段 base64url，第一段必为 eyJ（{" 的 base64）。
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")

# 各厂商的 key 前缀。cb- 是 CodeBuddy 自己的。
_KEY_PREFIX = re.compile(
    r"\b(?:sk|cb|pk|rk|api|key)[-_][A-Za-z0-9_-]{16,}\b"
    r"|\bAIza[0-9A-Za-z_-]{30,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
    r"|\bsk_live_[A-Za-z0-9]{16,}\b"
)

# Assignment 形式：token=... / "Authorization": "Bearer ..."
# 允许键值之间夹引号与冒号（JSON / dict repr 形态），值的字符类**必须**排除
# `&`——否则会把 URL query 的后续参数一起吃掉（`token=a&x=1` 会连 `&x=1` 一起抹掉）。
_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[-_]?key|access[-_]?token|"
    r"refresh[-_]?token|id[-_]?token|password|passwd|secret|client[-_]?secret)\b"
    r"(\"?\s*[:=]\s*\"?)(?:Bearer|Basic)?\s*[^\s&,;\"'\]}]{6,}"
)

# URL query 里的敏感参数。同样必须排除 `&`，只吃这一个参数的值。
_QUERY = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|apikey|key|secret|password|sig|signature)=)"
    r"[^&\s\"']{6,}"
)

# 图片 / 附件的 data URI：保留类型，抹掉负载。
_DATA_URI = re.compile(r"data:([\w/+.-]+);base64,[A-Za-z0-9+/=]{64,}")

# 单条日志消息的处理上限。超过就整体截断，避免脱敏自身变成瓶颈。
_MAX_LEN = 64_000


def scrub(text: str) -> str:
    """脱敏一段文本。无匹配时原样返回（不复制）。"""
    if not text or not isinstance(text, str):
        return text
    truncated = False
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN]
        truncated = True

    out = _DATA_URI.sub(lambda m: f"data:{m.group(1)};base64,{_PLACEHOLDER}", text)
    out = _JWT.sub(_PLACEHOLDER, out)
    out = _KEY_PREFIX.sub(_PLACEHOLDER, out)
    out = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{_PLACEHOLDER}", out)
    out = _QUERY.sub(lambda m: f"{m.group(1)}{_PLACEHOLDER}", out)

    if truncated:
        out += f"…（日志超长已截断，原长 {len(text)} 字符）"
    return out


class RedactingFilter(logging.Filter):
    """抹掉日志记录里的凭证。

    装在 **handler** 上（不是 logger）——只有 handler 级过滤器才能覆盖到所有
    来源的记录，包括 httpx / uvicorn 这些第三方库自己打的日志。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 永远返回 True：只改写，不拦截，日志该有的行一条不少。
        try:
            if isinstance(record.msg, str) and record.msg:
                record.msg = scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _scrub_any(v) for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(_scrub_any(v) for v in record.args)
            # 异常堆栈必须**在这里**渲染并脱敏。
            #
            # Formatter 是懒渲染的：过滤器跑的时候 record.exc_text 还是 None，
            # 堆栈要等 Formatter.format() 才生成。只改写 exc_text 会完全漏掉
            # 堆栈里的凭证（实测过：一条 log.exception 就把 Bearer token 原样输出）。
            # 这里抢先渲染成 exc_text，Formatter 见到非空 exc_text 就不再自己渲染。
            if record.exc_info and not record.exc_text:
                record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = scrub(record.exc_text)
        except Exception:  # 脱敏绝不能让日志本身崩掉
            pass
        return True


def _scrub_any(value):
    if isinstance(value, str):
        return scrub(value)
    # 用 %r 渲染的对象（如异常）也走一遍；其它类型留给 logging 自己处理。
    if isinstance(value, BaseException):
        return scrub(repr(value))
    return value

"""上游失败重试的分类学：什么能重放，什么绝对不能。

## 为什么需要单独一个模块

「失败就重试」在网关里是**默认危险**的：一次重试可能让上游把同一次生成
计费两遍，也可能让客户端收到重复输出。判别依据不是「失败了吗」，而是
**请求体有没有真的发出去**、**上游有没有开始回应**。

现实里踩过的坑是：把所有 `httpx.HTTPError` 一律当成「没发出去」重试。
`ReadTimeout` / `ReadError` / `RemoteProtocolError` 全都发生在**请求体已发完
之后**——上游很可能已经完整处理并计费了，只是回程被掐断。重试它们等于
主动制造重复计费。

## 判定表

    HTTP 状态码
      REPLAYABLE_STATUS    (401, 403, 429, 502, 503, 504)  换号重试有效
      DETERMINISTIC_STATUS (400, 404, 405, 413, 422)       换号也没用，原样返回

    传输异常
      REPLAYABLE_TRANSPORT (ConnectError, ConnectTimeout)  请求体未发出，安全重试
      WRITE_TIMEOUT        (WriteTimeout)                  可能已发出 → 默认**不**重放
      AMBIGUOUS_TRANSPORT  (ReadError, ReadTimeout, WriteError, RemoteProtocolError)
                                                           已发出、上游可能已计费
                                                           → 绝不重放

## 一条铁律

**"Never replay after the upstream response opens."**
一旦拿到了响应状态行，就不能再往上游发第二次同样的请求——那是明确的
重复计费。重试只允许发生在「连接建立 / 请求发送」阶段。
"""

from __future__ import annotations

import httpx

# 换个账号/重发大概率能成功的状态码。
REPLAYABLE_STATUS = frozenset((401, 403, 429, 502, 503, 504))

# 语义错误：请求本身有问题，换账号、重发、换模型都没用。
DETERMINISTIC_STATUS = frozenset((400, 404, 405, 413, 422))

# 上游**明确拒绝**、且没有开始生成内容的状态码：换号重发安全。
#
# 与传输异常的区别在于「上游有没有开始干活」：
#   - 这里的 5xx 是上游**回了一个错误响应**，生成根本没开始，重发不会重复计费；
#   - 传输异常里有一半是「请求体已发出、上游可能已处理并计费」
#     （AMBIGUOUS_TRANSPORT），那种情况重发就是主动制造重复计费，绝不能重试。
#
# 用它做换号重试的判据时，还必须保证**一个字节都还没发给客户端**——
# 否则客户端会先看到一个 503、然后又收到一次完整响应。
TRANSIENT_STATUS = frozenset((502, 503, 504))

# 连接建立阶段失败：请求体一个字节都没发出去，重试绝对安全。
REPLAYABLE_TRANSPORT = (httpx.ConnectError, httpx.ConnectTimeout)

# 写超时：请求体可能只发了一部分，上游是否已开始处理无法确定。
# 默认**不**重放，需要调用方显式开启。
WRITE_TIMEOUT_TRANSPORT = (httpx.WriteTimeout,)

# 请求体已发出后失败：上游可能已完整处理并计费，重放会造成重复计费。
# 这类必须原样把错误抛给客户端，绝不自动重试。
AMBIGUOUS_TRANSPORT = (
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


def is_replayable_status(status: int) -> bool:
    return status in REPLAYABLE_STATUS


def is_deterministic_status(status: int) -> bool:
    return status in DETERMINISTIC_STATUS


def is_transient_status(status) -> bool:
    """这个状态码是不是「上游明确拒绝、可以换号重发」的 5xx。

    转不了数字（None、文本）一律返回 False —— 宁可漏判成「不可重试」，
    也不能把一个说不清的状态码当成可重试的依据。
    """
    if status is None or isinstance(status, bool):
        return False
    try:
        return int(status) in TRANSIENT_STATUS
    except (TypeError, ValueError):
        return False


def classify_transport_error(exc: BaseException) -> str:
    """把传输异常归类为 ``"replayable"`` / ``"write_timeout"`` / ``"ambiguous"``
    / ``"unknown"``。

    只有 ``"replayable"`` 允许自动重试；``"ambiguous"`` 与 ``"unknown"``
    一律按「上游可能已计费」处理。
    """
    if isinstance(exc, REPLAYABLE_TRANSPORT):
        return "replayable"
    if isinstance(exc, WRITE_TIMEOUT_TRANSPORT):
        return "write_timeout"
    if isinstance(exc, AMBIGUOUS_TRANSPORT):
        return "ambiguous"
    return "unknown"


def may_replay_transport(exc: BaseException, *, allow_write_timeout: bool = False) -> bool:
    """这次传输异常能不能安全重试。

    ``allow_write_timeout`` 默认 False——写超时的重放可能重复计费，必须由
    调用方显式承担这个风险（eslxxx 的 ``retry_write_timeout`` 同此语义）。
    """
    kind = classify_transport_error(exc)
    if kind == "replayable":
        return True
    if kind == "write_timeout":
        return bool(allow_write_timeout)
    return False


def explain(exc: BaseException) -> str:
    """给日志用的一句话解释，说明为什么重试/不重试。"""
    kind = classify_transport_error(exc)
    return {
        "replayable": "连接未建立，请求体未发出，可安全重发",
        "write_timeout": "写超时，请求体可能已部分发出，重放有重复计费风险",
        "ambiguous": "请求体已发出、上游可能已处理，绝不重放",
        "unknown": "未知传输错误，按可能已计费处理，不重放",
    }[kind]

"""会话聚合头族：让上游把一次用户发送里的多次工具调用/重试/换号聚合成一条。

背景：上游按请求头里的会话 ID 把零散请求聚成一条对话。官方客户端一次 user
send 内的所有 tool call、重试、换号都复用同一组 ID，上游据此把它们算作同一条
消息的连续片段。本项目此前只向上游发一个每次都新的 ``X-Request-ID``（UUIDv7），
于是一条多轮对话在上游那边被拆成一堆互不相干的请求。

参考实现：linguo2625469/workbuddy2api-panel（``internal/upstream/headers.go``）。

**边界要说清**：代理层能独立做到的只有「同一次客户端请求内的重试/换号聚合成
一条」。客户端多轮 tool call 之间的聚合必须靠客户端自己带
``x-conversation-request-id``——我们只负责透传，不伪造。

ID 的来源（只发有据可依的，不凭空造）：

* ``X-Conversation-ID``：入站 ``x-conversation-id`` → 入站 ``x-session-id`` /
  ``session_id`` → 请求体 ``conversation_id`` / ``session_id`` /
  ``prompt_cache_key``（与 ``admin/pool.py`` 的账号亲和同源）。都没有就不发。
* ``X-Conversation-Request-ID``：入站 ``x-conversation-request-id`` → 回落本
  请求的 ``X-Request-ID``。
* ``X-Conversation-Message-ID`` = 本请求的 ``X-Request-ID``。
* ``X-Root-Request-ID`` = ``X-Conversation-Request-ID``。
* ``X-Trace-ID``：入站 ``x-trace-id`` → 回落 ``X-Conversation-Request-ID``。
* ``X-B3-TraceId`` / ``X-B3-SpanId``：16 位 hex（非法长度回落 message id）。

模式开关 ``WORKBUDDY_SESSION_HEADERS``：

* ``off``：完全不发会话头（连 ``X-Request-ID`` 也不动）。
* ``pass``：发会话头，但 ``X-Request-ID`` 每次尝试都新（保守：不动上游按
  UUIDv7 时间位还原请求时刻的既有语义）。
* ``sticky``（默认）：同一个客户端请求内，重试/换号复用同一个
  ``X-Request-ID``——与官方客户端一致，上游能把这些片段聚成一条。
"""
from __future__ import annotations

import re

# 模式取值。
MODE_OFF = "off"
MODE_PASS = "pass"
MODE_STICKY = "sticky"

# scope 里缓存「本次客户端请求的 X-Request-ID」的键。
# PoolMiddleware 每次尝试都把**同一个** scope dict 传下去，所以这里天然跨
# 重试/换号稳定。
SCOPE_REQUEST_ID = "codebuddy.request_id"

# scope 里缓存「上游实际收到的模型名」的键。
#
# 请求日志里的 model_id 记的是**客户端请求的裸名**，而上游真正收到的是归一后
# 的名字（trae 走 map_model 别名表，workbuddy 走自己的模型目录）。两者不同
# 是常态——排查「日志说的模型和云端不一样」时必须能看到上游那份，所以单独
# 记一列。放在本模块而不是 core.converter：管理端（admin/metrics.py）要读
# 它，但 admin 侧不该为了一个常量去引 converter。
SCOPE_UPSTREAM_MODEL = "codebuddy.upstream_model"

_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,32}$")

# 入站头候选（按优先级）。大小写不敏感。
_CONVERSATION_ID_HEADERS = ("x-conversation-id", "x-session-id", "session_id")
_CONVERSATION_REQUEST_ID_HEADERS = ("x-conversation-request-id",)
_TRACE_ID_HEADERS = ("x-trace-id",)

# 请求体候选（按优先级）。
_CONVERSATION_ID_BODY_KEYS = ("conversation_id", "session_id", "prompt_cache_key")


def _header(headers, name: str):
    """从各种形态的头容器里取一个头（大小写不敏感）。

    支持：Starlette ``Headers``、普通 ``dict``（str 键）、ASGI 原始 bytes 键
    字典。三种在本项目里都出现过（请求端点用前两种，``admin/pool.py`` 用第三种）。
    """
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value:
            return str(value).strip() or None
    try:
        items = headers.items()
    except AttributeError:
        items = ()
    for key, value in items:
        if isinstance(key, bytes):
            try:
                key = key.decode("latin-1")
            except Exception:  # pragma: no cover - 非 latin-1 的畸形键
                continue
        if str(key).lower() == name:
            if isinstance(value, bytes):
                value = value.decode("latin-1", "replace")
            text = str(value).strip()
            if text:
                return text
    return None


def _body_value(body, name: str):
    if not isinstance(body, dict):
        return None
    value = body.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_session_identity(headers, body) -> dict:
    """解析会话身份。只用「有据可依」的来源，一个都取不到就留空。"""
    conversation_id = None
    for name in _CONVERSATION_ID_HEADERS:
        conversation_id = _header(headers, name)
        if conversation_id:
            break
    if not conversation_id:
        for key in _CONVERSATION_ID_BODY_KEYS:
            conversation_id = _body_value(body, key)
            if conversation_id:
                break

    conversation_request_id = None
    for name in _CONVERSATION_REQUEST_ID_HEADERS:
        conversation_request_id = _header(headers, name)
        if conversation_request_id:
            break

    trace_id = None
    for name in _TRACE_ID_HEADERS:
        trace_id = _header(headers, name)
        if trace_id:
            break

    return {
        "conversation_id": conversation_id,
        "conversation_request_id": conversation_request_id,
        "trace_id": trace_id,
    }


def _hex16(value):
    """取 16 位 hex 尾；不是合法 hex 就返回 None（B3 头要求 hex）。"""
    if not isinstance(value, str) or not _HEX_RE.match(value):
        return None
    return value[:16].lower()


def build_session_headers(identity: dict, request_id: str) -> dict:
    """把解析出的身份变成一组上游会话头。

    ``request_id`` 是本请求最终的 ``X-Request-ID``（sticky 模式下是复用的那个）。
    """
    identity = identity or {}
    conversation_id = identity.get("conversation_id")
    conversation_request_id = identity.get("conversation_request_id") or request_id
    message_id = request_id
    trace_id = identity.get("trace_id") or conversation_request_id

    headers = {
        "X-Conversation-Request-ID": conversation_request_id,
        "X-Conversation-Message-ID": message_id,
        "X-Root-Request-ID": conversation_request_id,
        "X-Trace-ID": trace_id,
        "X-B3-Sampled": "1",
    }
    # 会话 ID 有据可依才发——凭空造一个等于给上游喂假指纹。
    if conversation_id:
        headers["X-Conversation-ID"] = conversation_id

    b3_trace = _hex16(conversation_request_id) or _hex16(message_id)
    b3_span = _hex16(message_id)
    if b3_trace:
        headers["X-B3-TraceId"] = b3_trace
    if b3_span:
        headers["X-B3-SpanId"] = b3_span
    return headers

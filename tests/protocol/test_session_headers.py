"""会话聚合头族（``core/session_headers.py`` + converter 接线）。

## 背景

上游按请求头里的会话 ID 把零散请求聚成一条对话。官方客户端一次 user send 内的
所有 tool call、重试、换号都复用同一组 ID，上游据此把它们算作同一条消息的连续
片段。本项目此前只发一个每次都新的 ``X-Request-ID``（UUIDv7），于是一条多轮对话
在上游那边被拆成一堆互不相干的请求。

## 三条边界（都有测试钉住）

1. **只发有据可依的**：会话 ID 有入站头/请求体来源才发，凭空造等于给上游喂假
   指纹。
2. **sticky 靠 scope 缓存**：池中间件每次 attempt 传的是**同一个** scope dict，
   所以「同一客户端请求内重试/换号复用同一 X-Request-ID」天然成立。
3. **Trae 不受影响**：Trae 的头自建（``_solo_headers``），不共用这条路径。

**代理层能独立做到的只有「同一次客户端请求内的重试/换号聚合成一条」**；客户端
多轮 tool call 之间的聚合必须靠客户端自己带 ``x-conversation-request-id``——
我们只透传，不伪造。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core import converter
from core.session_headers import (
    MODE_OFF,
    MODE_PASS,
    MODE_STICKY,
    SCOPE_REQUEST_ID,
    build_session_headers,
    resolve_session_identity,
)

REQUEST_ID = "0192abcd1234ef567890abcdef123456"


class IdentityResolutionTests:
    """会话身份的来源与优先级。"""

    def test_conversation_id_from_header(self):
        identity = resolve_session_identity({"x-conversation-id": "conv-1"}, {})
        assert identity["conversation_id"] == "conv-1"

    def test_session_id_header_is_a_fallback(self):
        identity = resolve_session_identity({"x-session-id": "sess-1"}, {})
        assert identity["conversation_id"] == "sess-1"

    def test_body_field_is_the_last_resort(self):
        identity = resolve_session_identity({}, {"conversation_id": "conv-body"})
        assert identity["conversation_id"] == "conv-body"

    def test_prompt_cache_key_is_a_body_source(self):
        """与 ``admin/pool.py`` 的账号亲和同源——客户端用它表达会话。"""
        identity = resolve_session_identity({}, {"prompt_cache_key": "pck-1"})
        assert identity["conversation_id"] == "pck-1"

    def test_header_beats_body(self):
        identity = resolve_session_identity({"x-conversation-id": "conv-h"},
                                            {"conversation_id": "conv-b"})
        assert identity["conversation_id"] == "conv-h"

    def test_bytes_keyed_headers_are_supported(self):
        """ASGI 原始头是 bytes 键（``admin/pool.py`` 用的就是这一形态）。"""
        identity = resolve_session_identity({b"x-conversation-id": b"conv-b"}, {})
        assert identity["conversation_id"] == "conv-b"

    def test_nothing_available_stays_empty(self):
        """取不到就留空——不伪造。"""
        identity = resolve_session_identity({}, {})
        assert identity["conversation_id"] is None
        assert identity["conversation_request_id"] is None
        assert identity["trace_id"] is None


class HeaderDerivationTests:
    """会话头的派生规则。"""

    def test_conversation_id_header_only_when_known(self):
        headers = build_session_headers({"conversation_id": "conv-1"}, REQUEST_ID)
        assert headers["X-Conversation-ID"] == "conv-1"
        without = build_session_headers({}, REQUEST_ID)
        assert "X-Conversation-ID" not in without

    def test_request_id_falls_back_to_message_id(self):
        headers = build_session_headers({}, REQUEST_ID)
        assert headers["X-Conversation-Request-ID"] == REQUEST_ID
        assert headers["X-Conversation-Message-ID"] == REQUEST_ID
        assert headers["X-Root-Request-ID"] == REQUEST_ID

    def test_inbound_request_id_wins(self):
        headers = build_session_headers({"conversation_request_id": "cr-1"}, REQUEST_ID)
        assert headers["X-Conversation-Request-ID"] == "cr-1"
        assert headers["X-Root-Request-ID"] == "cr-1"

    def test_trace_id_falls_back_to_conversation_request_id(self):
        headers = build_session_headers({"conversation_request_id": "cr-1"}, REQUEST_ID)
        assert headers["X-Trace-ID"] == "cr-1"

    def test_inbound_trace_id_wins(self):
        headers = build_session_headers({"trace_id": "trace-1"}, REQUEST_ID)
        assert headers["X-Trace-ID"] == "trace-1"

    def test_b3_headers_are_16_hex(self):
        headers = build_session_headers({}, REQUEST_ID)
        assert headers["X-B3-TraceId"] == REQUEST_ID[:16]
        assert headers["X-B3-SpanId"] == REQUEST_ID[:16]
        assert headers["X-B3-Sampled"] == "1"

    def test_b3_headers_fall_back_to_message_id_for_non_hex_ids(self):
        """B3 头要求 hex——非法 id 回落 message id，而不是发个上游看不懂的值。"""
        headers = build_session_headers({"conversation_request_id": "not-hex"}, REQUEST_ID)
        assert headers["X-B3-TraceId"] == REQUEST_ID[:16]
        assert headers["X-B3-SpanId"] == REQUEST_ID[:16]


class _FakeRequest:
    """最小 request 替身：只需要 headers 与 scope。"""

    def __init__(self, headers=None, scope=None):
        self.headers = headers or {}
        self.scope = scope if scope is not None else {}


class _FakeBackend:
    tencent_policies = True


class _TraeBackend:
    tencent_policies = False


class SessionHeaderWiringTests:
    """converter 里的接线行为。"""

    def test_sticky_reuses_request_id_across_attempts(self):
        """同一客户端请求内重试/换号复用同一 X-Request-ID。

        前提是池中间件每次 attempt 传同一个 scope dict——这里用同一个 scope
        连续调两次来复现「重试」这个动作。
        """
        scope = {}
        request = _FakeRequest(scope=scope)
        backend = _FakeBackend()
        first = converter._with_session_headers({"X-Request-ID": "a" * 32}, request, {}, backend)
        second = converter._with_session_headers({"X-Request-ID": "b" * 32}, request, {}, backend)
        assert first["X-Request-ID"] == second["X-Request-ID"]
        assert scope[SCOPE_REQUEST_ID] == first["X-Request-ID"]

    def test_pass_mode_keeps_each_attempt_request_id(self):
        """``pass`` 档保守：X-Request-ID 每次尝试都新（不动上游按 uuid7 还原时刻的语义）。"""
        scope = {}
        request = _FakeRequest(scope=scope)
        backend = _FakeBackend()
        converter.CONFIG["session_headers"] = MODE_PASS
        try:
            first = converter._with_session_headers({"X-Request-ID": "a" * 32}, request, {}, backend)
            second = converter._with_session_headers({"X-Request-ID": "b" * 32}, request, {}, backend)
        finally:
            converter.CONFIG["session_headers"] = MODE_STICKY
        assert first["X-Request-ID"] != second["X-Request-ID"]

    def test_off_mode_sends_no_session_headers(self):
        converter.CONFIG["session_headers"] = MODE_OFF
        try:
            headers = converter._with_session_headers(
                {"X-Request-ID": "a" * 32}, _FakeRequest(), {}, _FakeBackend())
        finally:
            converter.CONFIG["session_headers"] = MODE_STICKY
        assert set(headers) == {"X-Request-ID"}

    def test_trae_backend_is_untouched(self):
        """Trae 走私有协议，不该被塞腾讯的会话头。"""
        headers = converter._with_session_headers(
            {"X-Request-ID": "a" * 32}, _FakeRequest(), {}, _TraeBackend())
        assert set(headers) == {"X-Request-ID"}

    def test_session_headers_are_merged(self):
        headers = converter._with_session_headers(
            {"X-Request-ID": "a" * 32}, _FakeRequest({"x-conversation-id": "conv-1"}), {},
            _FakeBackend())
        assert headers["X-Conversation-ID"] == "conv-1"
        assert headers["X-Conversation-Message-ID"] == "a" * 32


class OutboundSanitizeWiringTests:
    """净化只在腾讯后端生效。"""

    def test_tencent_body_is_sanitized(self):
        body = {"messages": [{"role": "user", "content": "code=11128"}]}
        out = converter._sanitize_outbound_body(body, _FakeBackend())
        assert out["messages"][0]["content"] == "code=11-128"

    def test_trae_body_is_untouched(self):
        body = {"messages": [{"role": "user", "content": "code=11128"}]}
        assert converter._sanitize_outbound_body(body, _TraeBackend()) is body

    def test_switch_can_disable_sanitizing(self):
        converter.CONFIG["outbound_sanitize"] = False
        try:
            body = {"messages": [{"role": "user", "content": "code=11128"}]}
            assert converter._sanitize_outbound_body(body, _FakeBackend()) is body
        finally:
            converter.CONFIG["outbound_sanitize"] = True


class EndpointTests:
    """端点级：请求真的带上会话头，body 真的被洗过。"""

    def _patch(self, monkeypatch, captured):
        # 记住真正的 AsyncClient：patch 之后 httpx.AsyncClient 就是这个替身了，
        # 测试自己发请求得用真身（否则 transport 参数会撞车）。
        real_client = httpx.AsyncClient

        def upstream(request):
            captured.append({"headers": dict(request.headers),
                             "body": json.loads(request.content or b"{}")})
            return httpx.Response(200, content=(
                b'data: {"id":"crb-1","model":"m","choices":[{"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
                b'data: [DONE]\n\n'))

        transport = httpx.MockTransport(upstream)
        monkeypatch.setattr(converter.httpx, "AsyncClient",
                            lambda **kw: real_client(transport=transport, **kw))
        monkeypatch.setattr(converter, "_check_auth", lambda *a: None)
        monkeypatch.setattr(converter, "_cred", lambda: type(
            "C", (), {"get_headers": lambda self: {"X-Request-ID": REQUEST_ID},
                      "backend_name": None})())
        monkeypatch.setattr(converter, "_log", lambda *a, **k: None)
        return real_client

    def test_chat_endpoint_sends_session_headers(self, monkeypatch):
        captured = []
        real_client = self._patch(monkeypatch, captured)

        async def run():
            async with real_client(transport=httpx.ASGITransport(app=converter.app),
                                   base_url="http://test") as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"x-conversation-id": "conv-42"})
                assert response.status_code == 200
        asyncio.run(run())
        headers = captured[0]["headers"]
        assert headers["x-conversation-id"] == "conv-42"
        assert headers["x-conversation-request-id"] == REQUEST_ID
        assert headers["x-conversation-message-id"] == REQUEST_ID
        assert headers["x-b3-sampled"] == "1"

    def test_chat_endpoint_sanitizes_body(self, monkeypatch):
        captured = []
        real_client = self._patch(monkeypatch, captured)

        async def run():
            async with real_client(transport=httpx.ASGITransport(app=converter.app),
                                   base_url="http://test") as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "m", "messages": [
                        {"role": "user", "content": "code=11128"}]})
                assert response.status_code == 200
        asyncio.run(run())
        content = captured[0]["body"]["messages"][0]["content"]
        assert content == "code=11-128"

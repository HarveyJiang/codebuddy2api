"""守门回归：WorkBuddy 后端字节级行为保持 + 路由。

这些用例锁死「重构不改变既有生产路径」：
- astream 200 → 原样字节直通；
- astream 非 2xx / 传输错误 → BackendHTTPError（emitted=False / transport）；
- _stream_upstream 200 → 客户端收到的字节与上游 1:1；
- _stream_upstream 非 2xx → 恰好一个 _err_event chunk（旧行为）；
- _collect_stream 聚合结果 == 期望 JSON（固定 SSE fixture）。
"""
import asyncio
import json

import httpx
import pytest

from core import converter
from core.backends.base import BackendHTTPError
from core.backends.registry import backend_name_for, resolve_backend
from core.backends.workbuddy import WorkBuddyBackend

SSE_FIXTURE = (
    b'data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
    b'data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
    b'data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    b'data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"read","arguments":"{\\"p\\":"}}]}}]}\n\n'
    b'data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}\n\n'
    b'data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}\n\n'
    b'data: [DONE]\n\n'
)


def _patch_upstream(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(converter.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(converter, "_log", lambda *a, **k: None)


def test_astream_passes_200_bytes_through_unchanged(monkeypatch):
    _patch_upstream(monkeypatch, lambda req: httpx.Response(200, content=SSE_FIXTURE))

    async def run():
        chunks = b"".join([c async for c in WorkBuddyBackend().astream({}, {"model": "glm-5.2"})])
        assert chunks == SSE_FIXTURE
    asyncio.run(run())


def test_astream_non_2xx_raises_backend_http_error(monkeypatch):
    body = b'{"error":{"message":"limited","code":429}}'
    _patch_upstream(monkeypatch, lambda req: httpx.Response(429, content=body))

    async def run():
        with pytest.raises(BackendHTTPError) as ei:
            async for _ in WorkBuddyBackend().astream({}, {}):
                pass
        assert ei.value.status == 429
        assert ei.value.body == body
        assert ei.value.emitted is False
        assert ei.value.transport is False
    asyncio.run(run())


def test_astream_transport_error_maps_to_502(monkeypatch):
    def handler(req):
        raise httpx.ReadTimeout("boom", request=req)
    _patch_upstream(monkeypatch, handler)

    async def run():
        with pytest.raises(BackendHTTPError) as ei:
            async for _ in WorkBuddyBackend().astream({}, {}):
                pass
        assert ei.value.status == 502
        assert ei.value.transport is True
        assert ei.value.emitted is False
    asyncio.run(run())


def test_stream_upstream_is_byte_identical(monkeypatch):
    _patch_upstream(monkeypatch, lambda req: httpx.Response(200, content=SSE_FIXTURE))

    async def run():
        out = b"".join([c async for c in converter._stream_upstream(WorkBuddyBackend(), {}, {"stream": True}, "glm-5.2", 0.0, "r1")])
        assert out == SSE_FIXTURE
    asyncio.run(run())


def test_stream_upstream_error_yields_exactly_one_err_event(monkeypatch):
    _patch_upstream(monkeypatch, lambda req: httpx.Response(401, content=b'{"error":"unauthorized"}'))

    async def run():
        out = b"".join([c async for c in converter._stream_upstream(WorkBuddyBackend(), {}, {}, "glm-5.2", 0.0, "r2")])
        assert out.count(b"data:") == 1
        assert out == converter._err_event(b'{"error":"unauthorized"}', 401)
        obj = json.loads(out[len(b"data: "):].strip())
        assert obj["error"]["code"] == 401
    asyncio.run(run())


def test_collect_stream_aggregates_tool_calls_and_usage():
    async def run():
        result = await converter._collect_stream(converter._aiter_str_lines(SSE_FIXTURE))
        assert result["model"] == "glm-5.2"
        msg = result["choices"][0]["message"]
        assert msg["content"] == "Hello"
        assert msg["tool_calls"] == [
            {"id": "call_a", "type": "function", "function": {"name": "read", "arguments": '{"p":1}'}}
        ]
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        assert result["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    asyncio.run(run())


def test_collect_stream_keep_reasoning():
    stream = (
        b'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning_content":"more","content":"ans"},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )

    async def run():
        r = await converter._collect_stream(converter._aiter_str_lines(stream), keep_reasoning=True)
        assert r["choices"][0]["message"]["reasoning_content"] == "think more"
        assert r["choices"][0]["message"]["content"] == "ans"
        # 默认不累积 reasoning
        r2 = await converter._collect_stream(converter._aiter_str_lines(stream))
        assert "reasoning_content" not in r2["choices"][0]["message"]
    asyncio.run(run())


@pytest.mark.parametrize("model,expected", [
    ("glm-5.2", "workbuddy"),
    ("auto", "workbuddy"),
    ("", "workbuddy"),
    (None, "workbuddy"),
    ("trae/glm-5.2", "trae"),
])
def test_backend_name_for(model, expected):
    assert backend_name_for(model) == expected


@pytest.mark.parametrize("model", ["glm-5.2", "auto", "deepseek-v4-pro", ""])
def test_resolve_backend_defaults_to_workbuddy(model):
    backend, upstream = resolve_backend(model)
    assert backend.name == "workbuddy"
    assert backend.tencent_policies is True
    assert upstream == model


def test_resolve_backend_non_string_is_safe():
    backend, upstream = resolve_backend(123)
    assert backend.name == "workbuddy"
    assert upstream == ""


def test_estimate_tokens_scales_with_length():
    small = converter._estimate_tokens({"messages": [{"role": "user", "content": "x" * 40}]})
    big = converter._estimate_tokens({"messages": [{"role": "user", "content": "x" * 400}]})
    assert small == 10
    assert big == 100
    assert converter._estimate_tokens({"messages": []}) == 1


def test_aiter_sse_lines_handles_partial_chunks():
    async def run():
        async def chunks():
            yield b"data: a\n\ndata: b"
            yield b"\n\ndata: [DONE]\n\n"
        lines = [x async for x in converter._aiter_sse_lines(chunks())]
        assert lines == ["data: a", "", "data: b", "", "data: [DONE]", ""]
    asyncio.run(run())

# ---------------------------------------------------------------------------
# 分层标记（见 pytest.ini / tests/README.md）
#
# 本文件整体属于「core」。标记写在文件里而不是逐条标注：同一文件的用例
# 保护的是同一块行为，分域跑（`pytest tests/pool`）时就该一起跑。
#   core    : 必须守住的不变量，默认只跑这些
#   e2e     : 起整套 create_app 走真实请求链路
#   network : 经由 mock 传输层模拟上游交互
# 若某条用例其实不属于本文件默认域（例如纯形状断言、只测常量的），
# 请在该用例上单独加 @pytest.mark.<域>，或在下面改用更准确的一档。
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.core]


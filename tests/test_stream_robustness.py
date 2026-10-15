"""流式请求鲁棒性：空闲超时、截断可观测、长流不误杀。

对应 specs/stream-robustness.md。这些测试直接驱动 converter 里的流式生成器，
用一个可控的假上游替换 httpx 的 stream()，避免依赖真实网络。
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from core import converter


class _FakeResponse:
    """假的上游响应：按脚本吐分片，可以「吐完就停」或「吐一半卡住」。"""

    def __init__(self, lines, stall_after=None, status=200):
        self.status_code = status
        self._lines = lines
        self._stall_after = stall_after

    async def aread(self):
        return b"".join(line.encode() for line in self._lines)

    async def aiter_lines(self):
        for index, line in enumerate(self._lines):
            if self._stall_after is not None and index >= self._stall_after:
                await asyncio.sleep(3600)  # 永不返回：模拟上游卡死不关连接
            yield line

    async def aiter_bytes(self):
        for index, line in enumerate(self._lines):
            if self._stall_after is not None and index >= self._stall_after:
                await asyncio.sleep(3600)
            yield line.encode()


class _FakeStreamContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, response, **kwargs):
        self._response = response
        # 断言调用方确实传了受限的超时（不是裸的 timeout=None）。
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return _FakeStreamContext(self._response)


def sse(*payloads):
    """把若干 dict 组装成 OpenAI SSE 行。"""
    return [f"data: {json.dumps(p)}\n" for p in payloads]


def collect_lines(*payloads, done=True):
    lines = sse(*payloads)
    if done:
        lines.append("data: [DONE]\n")
    return lines


def text_chunk(content, finish=None):
    choice = {"delta": {"content": content}}
    if finish:
        choice["finish_reason"] = finish
    return {"choices": [choice]}


async def drain(gen):
    out = []
    async for item in gen:
        out.append(item if isinstance(item, bytes) else item.encode())
    return b"".join(out)


def patch_client(response):
    return patch.object(converter.httpx, "AsyncClient",
                        lambda **kwargs: _FakeClient(response, **kwargs))


async def run_upstream(lines, stall_after=None, idle=1):
    """跑 _stream_upstream 并把输出收成人可读文本。"""
    response = _FakeResponse(lines, stall_after=stall_after)
    with patch_client(response), \
            patch.object(converter.config, "get", lambda: type("C", (), {"stream_idle_timeout": idle})()):
        return await drain(converter._stream_upstream("http://x", {}, {}, "m"))


class IdleTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_upstream_is_aborted_with_error_event(self):
        """上游吐一半就永不返回：必须中止并补发错误事件，而不是永远挂着。"""
        # 两行数据，第二行之前卡住（stall_after=1）；不补 [DONE]。
        lines = sse(text_chunk("你好"), text_chunk("世界"))
        output = await run_upstream(lines, stall_after=1, idle=0.1)
        text = output.decode("utf-8", "replace")
        self.assertIn("stream_truncated", text)
        self.assertIn("无数据", text)

    async def test_long_stream_is_not_killed(self):
        """每个分片都在 idle 内到达的正常长流，不能被误杀。"""
        # 5 个分片，生成时每个之间只 sleep 0.02s，idle 设 0.5s。
        response = _FakeResponse(collect_lines(
            text_chunk("a"), text_chunk("b"), text_chunk("c"),
            text_chunk("d"), text_chunk("e", finish="stop"),
        ))
        with patch_client(response), \
                patch.object(converter.config, "get",
                             lambda: type("C", (), {"stream_idle_timeout": 0.5})()):
            output = await drain(converter._stream_upstream("http://x", {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertNotIn("stream_truncated", text)
        for token in ("a", "b", "c", "d", "e"):
            self.assertIn(f'"content": "{token}"', text)


class TruncationTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_stream_gets_no_truncation_event(self):
        """收到终止信号的流不该被追加错误事件。"""
        output = await run_upstream(collect_lines(text_chunk("完整", finish="stop")))
        self.assertNotIn(b"stream_truncated", output)

    async def test_done_without_finish_reason_is_not_truncation(self):
        """部分上游只发 [DONE] 不给 finish_reason，这仍是正常收尾。"""
        lines = collect_lines(text_chunk("内容"))  # 无 finish_reason，但有 [DONE]
        output = await run_upstream(lines)
        self.assertNotIn(b"stream_truncated", output)

    async def test_stream_ending_without_terminal_signal_is_reported(self):
        """既无 finish_reason 也无 [DONE]：客户端必须能看出响应不完整。"""
        lines = sse(text_chunk("半句"))
        output = await run_upstream(lines)
        self.assertIn(b"stream_truncated", output)

    async def test_error_body_still_returned_on_http_error(self):
        """非 200 仍走原有的错误事件，不受本次改动影响。"""
        response = _FakeResponse([], status=500)
        with patch_client(response):
            output = await drain(converter._stream_upstream("http://x", {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertIn("upstream_error", text)


class TimeoutConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_timeout_is_not_total_timeout(self):
        """httpx 的 timeout 不能退化成 None（那正是永远挂起的根因）。"""
        captured = {}

        class _Capture(_FakeClient):
            def __init__(self, response, **kwargs):
                super().__init__(response, **kwargs)
                captured["timeout"] = kwargs.get("timeout")

        response = _FakeResponse(collect_lines(text_chunk("x", finish="stop")))
        with patch.object(converter.httpx, "AsyncClient",
                          lambda **kwargs: _Capture(response, **kwargs)), \
                patch.object(converter.config, "get",
                             lambda: type("C", (), {"stream_idle_timeout": 5})()):
            await drain(converter._stream_upstream("http://x", {}, {}, "m"))
        timeout = captured["timeout"]
        self.assertIsNotNone(timeout)
        # 读超时必须是 None（长响应合法），连接/写入要有上限。
        self.assertIsNone(timeout.read)
        self.assertIsNotNone(timeout.connect)


class AnthropicTruncationTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_truncation_emits_error_event(self):
        lines = sse({"choices": [{"delta": {"content": "半句"}}]})
        response = _FakeResponse(lines)
        with patch_client(response), \
                patch.object(converter.config, "get",
                             lambda: type("C", (), {"stream_idle_timeout": 5})()):
            output = await drain(converter._stream_anthropic("http://x", {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertIn("event: error", text)
        self.assertIn("不完整", text)

    async def test_anthropic_normal_stream_has_no_error_event(self):
        lines = collect_lines(
            {"choices": [{"delta": {"content": "好的"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
        response = _FakeResponse(lines)
        with patch_client(response), \
                patch.object(converter.config, "get",
                             lambda: type("C", (), {"stream_idle_timeout": 5})()):
            output = await drain(converter._stream_anthropic("http://x", {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertNotIn("event: error", text)
        self.assertIn("message_stop", text)

    async def test_anthropic_idle_timeout_emits_error(self):
        lines = sse({"choices": [{"delta": {"content": "半句"}}]},
                    {"choices": [{"delta": {"content": "后半"}}]})
        response = _FakeResponse(lines, stall_after=1)
        with patch_client(response), \
                patch.object(converter.config, "get",
                             lambda: type("C", (), {"stream_idle_timeout": 0.1})()):
            output = await drain(converter._stream_anthropic("http://x", {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertIn("event: error", text)
        self.assertIn("无数据", text)


class IdleWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_passes_through_when_fast(self):
        async def source():
            for i in range(3):
                yield i

        got = [item async for item in converter._iter_with_idle_timeout(source(), 1)]
        self.assertEqual(got, [0, 1, 2])

    async def test_wrapper_raises_on_gap(self):
        async def source():
            yield 1
            await asyncio.sleep(3600)
            yield 2

        with self.assertRaises(TimeoutError):
            async for _ in converter._iter_with_idle_timeout(source(), 0.05):
                pass

    async def test_wrapper_does_not_rate_lengthen(self):
        """计时随每个分片重置：总分片间距小但总量大时不能超时。"""
        async def source():
            for _ in range(20):
                await asyncio.sleep(0.01)
                yield b"x"

        got = [item async for item in converter._iter_with_idle_timeout(source(), 0.2)]
        self.assertEqual(len(got), 20)


if __name__ == "__main__":
    unittest.main()

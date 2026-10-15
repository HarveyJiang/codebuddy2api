"""流式请求鲁棒性：空闲超时、截断可观测、长流不误杀。

对应 specs/stream-robustness.md。这些测试直接驱动 converter 里的流式生成器，
用一个可控的假后端替换真实上游（backend.astream），避免依赖真实网络。
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from core import converter
from core.backends.base import BackendHTTPError


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


class _FakeBackend:
    """假后端：astream 契约与真实后端一致。

    - 正常：yield 的字节已是 OpenAI Chat SSE 行序列。
    - 非 2xx：raise BackendHTTPError(emitted=False)。
    记录调用方传入的 timeout，供断言「流式端点不按总时长掐断」。
    """

    name = "workbuddy"

    def __init__(self, response):
        self._response = response
        self.seen_timeout = None

    async def astream(self, headers, body, *, timeout=None, raise_on_error=False):
        self.seen_timeout = timeout
        if self._response.status_code != 200:
            raw = await self._response.aread()
            raise BackendHTTPError(self._response.status_code, raw)
        async for chunk in self._response.aiter_bytes():
            if chunk:
                yield chunk


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


def idle_config(idle):
    """把 config.get() 换成只暴露 stream_idle_timeout 的假配置。"""
    return patch.object(converter.config, "get",
                        lambda: type("C", (), {"stream_idle_timeout": idle})())


async def run_upstream(lines, stall_after=None, idle=1):
    """跑 _stream_upstream 并把输出收成人可读文本。"""
    backend = _FakeBackend(_FakeResponse(lines, stall_after=stall_after))
    with idle_config(idle):
        return await drain(converter._stream_upstream(backend, {}, {}, "m"))


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
        backend = _FakeBackend(_FakeResponse(collect_lines(
            text_chunk("a"), text_chunk("b"), text_chunk("c"),
            text_chunk("d"), text_chunk("e", finish="stop"),
        )))
        with idle_config(0.5):
            output = await drain(converter._stream_upstream(backend, {}, {}, "m"))
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

    async def test_finish_reason_without_trailing_newline_is_not_truncation(self):
        """末帧无尾随换行时不能误报截断。

        上游 flush 边界/连接正好在帧尾关闭时，最后一帧常常不带 \\n。旧实现只在
        「有 \\n 时」解析缓冲，会漏掉这帧里的 finish_reason，把正常结束当截断——
        用户明明收全了答案，却额外收到错误事件，后台也记成失败请求。
        """
        lines = ['data: {"choices": [{"delta": {"content": "完整"}, '
                 '"finish_reason": "stop"}]}']  # 无尾随 \n
        output = await run_upstream(lines)
        self.assertNotIn(b"stream_truncated", output)

    async def test_done_without_trailing_newline_is_not_truncation(self):
        """[DONE] 无尾随换行同样不能误报。"""
        output = await run_upstream(["data: [DONE]"])
        self.assertNotIn(b"stream_truncated", output)

    async def test_no_terminal_signal_at_all_still_reported(self):
        """末帧无换行、且确实没有终止信号：仍要报截断（别把 flush 修成「永不报」）。"""
        lines = ['data: {"choices": [{"delta": {"content": "半句"}}]}']
        output = await run_upstream(lines)
        self.assertIn(b"stream_truncated", output)

    async def test_error_body_still_returned_on_http_error(self):
        """非 200 仍走原有的错误事件，不受本次改动影响。"""
        backend = _FakeBackend(_FakeResponse([], status=500))
        output = await drain(converter._stream_upstream(backend, {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertIn("upstream_error", text)


class TimeoutConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_endpoint_uses_no_total_timeout(self):
        """流式端点给后端的 timeout 必须是 None：长响应合法，不能按总时长掐断。

        挂起由空闲超时（_iter_with_idle_timeout 逐分片计时）兜底，不再依赖
        一个会退化成「永远挂起」或「误杀长流」的总时长超时。
        """
        backend = _FakeBackend(_FakeResponse(collect_lines(text_chunk("x", finish="stop"))))
        with idle_config(5):
            await drain(converter._stream_upstream(backend, {}, {}, "m"))
        self.assertIsNone(backend.seen_timeout)


class AnthropicTruncationTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_truncation_emits_error_event(self):
        lines = sse({"choices": [{"delta": {"content": "半句"}}]})
        backend = _FakeBackend(_FakeResponse(lines))
        with idle_config(5):
            output = await drain(converter._stream_anthropic(backend, {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertIn("event: error", text)
        self.assertIn("不完整", text)

    async def test_anthropic_normal_stream_has_no_error_event(self):
        lines = collect_lines(
            {"choices": [{"delta": {"content": "好的"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
        backend = _FakeBackend(_FakeResponse(lines))
        with idle_config(5):
            output = await drain(converter._stream_anthropic(backend, {}, {}, "m"))
        text = output.decode("utf-8", "replace")
        self.assertNotIn("event: error", text)
        self.assertIn("message_stop", text)

    async def test_anthropic_idle_timeout_emits_error(self):
        lines = sse({"choices": [{"delta": {"content": "半句"}}]},
                    {"choices": [{"delta": {"content": "后半"}}]})
        backend = _FakeBackend(_FakeResponse(lines, stall_after=1))
        with idle_config(0.1):
            output = await drain(converter._stream_anthropic(backend, {}, {}, "m"))
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

import pytest  # noqa: E402  (标记需要)

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


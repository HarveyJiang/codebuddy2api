"""admin/reset_at.py 与 core/replay_policy.py 的测试。

两个模块都只做「判定」，不碰 IO，所以能全程离线测——这正是把它们独立出来的原因。
"""

import datetime as dt
import unittest

import httpx

from admin.reset_at import (
    earliest_deadline,
    parse_reset_at,
    parse_retry_after,
)
from core.replay_policy import (
    classify_transport_error,
    explain,
    is_deterministic_status,
    is_replayable_status,
    may_replay_transport,
)

CN = dt.timezone(dt.timedelta(hours=8))


class ResetAtTextTests(unittest.TestCase):
    """上游文案里的恢复时刻解析。"""

    # 固定的「现在」= 2026-10-15 12:00 UTC（= 北京 20:00）。
    NOW = dt.datetime(2026, 10, 15, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()

    def test_wallclock_without_suffix_is_read_as_utc8(self):
        """无后缀墙钟必须按 UTC+8 解释，否则整体偏 8 小时。

        这是最容易错的地方：偏早 8 小时等于冷却立刻到期（反复撞墙），
        偏晚 8 小时等于白扣半天。
        """
        text = "当前模型排队中，将在 2026-10-15 22:00:00 重置"
        got = parse_reset_at(text, now=self.NOW)
        expect = dt.datetime(2026, 10, 15, 22, 0, 0, tzinfo=CN).timestamp()
        self.assertEqual(got, expect)
        # 22:00 UTC+8 == 14:00 UTC，比「按 UTC 解析」晚 8 小时。
        naive_as_utc = dt.datetime(2026, 10, 15, 22, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertNotEqual(got, naive_as_utc)

    def test_rfc3339_offset_is_honoured(self):
        text = "限流，恢复时间 2026-10-15T22:00:00+08:00"
        got = parse_reset_at(text, now=self.NOW)
        self.assertEqual(got, dt.datetime(2026, 10, 15, 22, 0, 0, tzinfo=CN).timestamp())

    def test_z_suffix_is_utc(self):
        text = "rate limit reset at 2026-10-15T14:00:00Z"
        got = parse_reset_at(text, now=self.NOW)
        self.assertEqual(got, dt.datetime(2026, 10, 15, 14, 0, 0, tzinfo=dt.timezone.utc).timestamp())

    def test_relative_seconds(self):
        got = parse_reset_at("请 30 秒后重试", now=self.NOW)
        self.assertEqual(got, self.NOW + 30)

    def test_relative_minutes(self):
        got = parse_reset_at("请求过于频繁，5 分钟后重试", now=self.NOW)
        self.assertEqual(got, self.NOW + 300)

    def test_past_deadline_is_treated_as_absent(self):
        """已过期 → 当作「没有时间信息」，而不是「立刻恢复」。

        立刻恢复会让并发重试同秒再吃一次同样的错误（热循环）。
        """
        text = "将在 2026-10-15 10:00:00 重置"
        self.assertIsNone(parse_reset_at(text, now=self.NOW))

    def test_text_without_reset_context_is_ignored(self):
        """正文里随手出现的日期不该被当成冷却依据。"""
        text = "这段代码创建于 2026-10-15 14:00:00，用于处理请求"
        self.assertIsNone(parse_reset_at(text, now=self.NOW))

    def test_missing_and_garbage(self):
        for text in (None, "", "排队中", "将在 abc 重置", 12345):
            self.assertIsNone(parse_reset_at(text, now=self.NOW))

    def test_bad_calendar_date_does_not_raise(self):
        self.assertIsNone(parse_reset_at("将在 2026-13-45 99:99:99 重置", now=self.NOW))


class ResetAtHeaderTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 15, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()

    def test_retry_after_seconds(self):
        self.assertEqual(parse_retry_after({"Retry-After": "120"}, now=self.NOW), self.NOW + 120)

    def test_retry_after_ms_wins(self):
        headers = {"Retry-After": "120", "Retry-After-Ms": "1500"}
        self.assertEqual(parse_retry_after(headers, now=self.NOW), self.NOW + 1.5)

    def test_retry_after_http_date(self):
        headers = {"Retry-After": "Thu, 15 Oct 2026 12:10:00 GMT"}
        self.assertEqual(parse_retry_after(headers, now=self.NOW), self.NOW + 600)

    def test_x_ratelimit_reset_epoch(self):
        stamp = int(self.NOW + 900)
        headers = {"X-RateLimit-Reset": str(stamp)}
        self.assertEqual(parse_retry_after(headers, now=self.NOW), float(stamp))
        headers = {"X-RateLimit-Reset": "60"}
        self.assertEqual(parse_retry_after(headers, now=self.NOW), self.NOW + 60)

    def test_bytes_header_dict(self):
        """ASGI 的 headers 是 [(bytes, bytes)]，转成 dict 后也吃得住。"""
        headers = {b"retry-after": b"45"}
        self.assertEqual(parse_retry_after(headers, now=self.NOW), self.NOW + 45)

    def test_httpx_headers(self):
        headers = httpx.Headers({"Retry-After": "30"})
        self.assertEqual(parse_retry_after(headers, now=self.NOW), self.NOW + 30)

    def test_absent_and_garbage(self):
        for headers in (None, {}, {"Retry-After": "soon"}, {"Retry-After": "-5"}):
            self.assertIsNone(parse_retry_after(headers, now=self.NOW))


class EarliestDeadlineTests(unittest.TestCase):
    def test_takes_later_of_two(self):
        """并发响应乱序到达时取更晚的截止，绝不缩短已有冷却。"""
        self.assertEqual(earliest_deadline(100.0, 300.0), 300.0)
        self.assertEqual(earliest_deadline(300.0, 100.0), 300.0)

    def test_ignores_missing(self):
        self.assertEqual(earliest_deadline(None, 100.0, None), 100.0)
        self.assertIsNone(earliest_deadline(None, None))


class ReplayPolicyTests(unittest.TestCase):
    def test_status_classes(self):
        for status in (401, 403, 429, 502, 503, 504):
            self.assertTrue(is_replayable_status(status), status)
        for status in (400, 404, 405, 413, 422):
            self.assertTrue(is_deterministic_status(status), status)
        # 200 与未知码都不属于任何一类。
        self.assertFalse(is_replayable_status(200))
        self.assertFalse(is_deterministic_status(200))

    def test_connect_errors_are_replayable(self):
        """连接阶段失败 → 请求体一个字节都没发出去，重试绝对安全。"""
        self.assertEqual(classify_transport_error(httpx.ConnectError("x")), "replayable")
        self.assertEqual(classify_transport_error(httpx.ConnectTimeout("x")), "replayable")
        self.assertTrue(may_replay_transport(httpx.ConnectError("x")))

    def test_ambiguous_errors_are_never_replayed(self):
        """请求体已发出、上游可能已计费 → 绝不重放，否则重复计费。"""
        for exc in (
            httpx.ReadTimeout("x"),
            httpx.ReadError("x"),
            httpx.WriteError("x"),
            httpx.RemoteProtocolError("x"),
        ):
            self.assertEqual(classify_transport_error(exc), "ambiguous", type(exc))
            self.assertFalse(may_replay_transport(exc), type(exc))

    def test_write_timeout_needs_explicit_opt_in(self):
        exc = httpx.WriteTimeout("x")
        self.assertEqual(classify_transport_error(exc), "write_timeout")
        self.assertFalse(may_replay_transport(exc))
        self.assertTrue(may_replay_transport(exc, allow_write_timeout=True))

    def test_unknown_is_conservative(self):
        """未知异常按「可能已计费」处理，不重放。"""
        self.assertEqual(classify_transport_error(RuntimeError("x")), "unknown")
        self.assertFalse(may_replay_transport(RuntimeError("x")))

    def test_every_kind_has_an_explanation(self):
        for exc in (httpx.ConnectError("x"), httpx.WriteTimeout("x"),
                    httpx.ReadTimeout("x"), RuntimeError("x")):
            self.assertTrue(explain(exc))


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


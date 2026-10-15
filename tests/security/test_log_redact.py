"""core/log_redact.py 的测试。"""

import logging
import unittest

from core.log_redact import RedactingFilter, scrub
from core.runtime_logging import setup_logging


class ScrubTests(unittest.TestCase):
    def test_jwt_is_removed(self):
        token = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                 "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                 "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        out = scrub(f"上游返回 401，token={token} 已失效")
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", out)
        self.assertIn("已脱敏", out)
        # 上下文要留住，否则日志失去排障价值。
        self.assertIn("上游返回 401", out)

    def test_vendor_key_prefixes(self):
        for text in (
            "Authorization: Bearer sk-proj-abcdefghijklmnopqrstuvwxyz",
            "key cb-abcdefghijklmnopqrstuvwxyz0123 rejected",
            "AIzaSyA1234567890abcdefghijklmnopqrstu",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "xoxb-1234567890-abcdefghij",
            "sk_live_abcdefghijklmnop",
        ):
            out = scrub(text)
            self.assertIn("已脱敏", out, text)

    def test_authorization_header_value(self):
        out = scrub('headers={"Authorization": "Bearer abcdefghijklmnop"}')
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertIn("Authorization", out)

    def test_cookie_is_removed(self):
        out = scrub("Cookie: admin_session=abcdef123456; other=1")
        self.assertNotIn("abcdef123456", out)

    def test_url_query_token(self):
        out = scrub("GET https://api.example.com/v1?access_token=abcdefgh123&x=1 failed")
        self.assertNotIn("abcdefgh123", out)
        # 非敏感参数保留：否则 URL 认不出来是哪个请求。
        self.assertIn("x=1", out)

    def test_data_uri_payload_is_dropped_but_type_kept(self):
        payload = "A" * 200
        out = scrub(f"image: data:image/png;base64,{payload}")
        self.assertNotIn(payload, out)
        self.assertIn("data:image/png;base64", out)

    def test_plain_hex_and_uuid_are_kept(self):
        """普通 hex / UUID 是有用的 trace id，不能一并抹掉。"""
        rid = "7f3a9c2e1b4d4e8a9f0c1d2e3f4a5b6c"
        uuid = "123e4567-e89b-12d3-a456-426614174000"
        out = scrub(f"request {rid} trace {uuid} done")
        self.assertIn(rid, out)
        self.assertIn(uuid, out)

    def test_normal_text_is_unchanged(self):
        text = "账号 3 触发上游限流，冷却 30 分钟"
        self.assertEqual(scrub(text), text)

    def test_empty_and_non_string(self):
        self.assertEqual(scrub(""), "")
        self.assertIsNone(scrub(None))

    def test_overlong_message_is_truncated(self):
        out = scrub("x" * 100_000)
        self.assertLess(len(out), 100_000)
        self.assertIn("已截断", out)


class FilterTests(unittest.TestCase):
    def test_filter_rewrites_msg_and_args(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1,
                                   "token=%s", ("sk-abcdefghijklmnopqrstuvwxyz",), None)
        RedactingFilter().filter(record)
        rendered = record.getMessage()
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", rendered)

    def test_filter_always_passes_records_through(self):
        """只改写、不拦截：日志行一条都不能少。"""
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        self.assertTrue(RedactingFilter().filter(record))
        self.assertEqual(record.getMessage(), "hello")

    def test_filter_never_raises_on_weird_payload(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", (object(),), None)
        self.assertTrue(RedactingFilter().filter(record))

    def test_exception_traceback_is_redacted(self):
        """异常堆栈必须脱敏。

        Formatter 是懒渲染的：过滤器跑的时候 exc_text 还是 None，所以只改写
        exc_text 会完全漏掉堆栈里的凭证——实测过一条 log.exception 就把
        Bearer token 原样打了出来。
        """
        try:
            raise RuntimeError("Bearer sk-abcdefghijklmnopqrstuvwxyz")
        except RuntimeError:
            import sys
            record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                                       "调用失败", None, sys.exc_info())
        RedactingFilter().filter(record)
        rendered = logging.Formatter().format(record)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", rendered)
        self.assertIn("已脱敏", rendered)
        # 堆栈本身要留住，否则排障没意义。
        self.assertIn("RuntimeError", rendered)


class WiringTests(unittest.TestCase):
    def test_setup_logging_installs_redaction_on_every_handler(self):
        """脱敏必须装在 handler 上——只有这样才能覆盖第三方库的日志。"""
        root = setup_logging(level="DEBUG")
        try:
            managed = [h for h in root.handlers if getattr(h, "_workbuddy", False)]
            self.assertTrue(managed)
            for handler in managed:
                self.assertTrue(
                    any(isinstance(f, RedactingFilter) for f in handler.filters),
                    handler,
                )
        finally:
            for handler in [h for h in list(root.handlers) if getattr(h, "_workbuddy", False)]:
                root.removeHandler(handler)
                handler.close()


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


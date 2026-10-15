"""可观测性：日志初始化与深度健康检查。

对应 specs/observability.md。重点锁住三件容易回归的事：
- 日志**默认即有输出**（此前不传 --log 就完全静默）
- setup_logging **幂等**（不叠加 handler）
- /health 三态判据正确，且默认不泄露账号名
"""
import inspect
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from core.runtime_logging import setup_logging, uvicorn_level
from admin.server import Store, create_app


class LoggingSetupTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_workbuddy", False):
                root.removeHandler(handler)
                handler.close()

    def test_default_output_goes_to_stderr(self):
        """不传 path 也要有输出——这是本次修的核心问题。"""
        root = setup_logging("INFO")
        handlers = [h for h in root.handlers if getattr(h, "_workbuddy", False)]
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], logging.StreamHandler)
        self.assertEqual(handlers[0].stream, __import__("sys").stderr)

    def test_idempotent_across_calls(self):
        """重复初始化不能叠加 handler，否则每行日志会重复输出。"""
        setup_logging("INFO")
        setup_logging("INFO")
        setup_logging("INFO")
        root = logging.getLogger()
        handlers = [h for h in root.handlers if getattr(h, "_workbuddy", False)]
        self.assertEqual(len(handlers), 1)

    def test_level_filters_lower_records(self):
        setup_logging("WARNING")
        logger = logging.getLogger("admin.pool")
        self.assertFalse(logger.isEnabledFor(logging.INFO))
        self.assertTrue(logger.isEnabledFor(logging.WARNING))
        setup_logging("INFO")
        self.assertTrue(logger.isEnabledFor(logging.INFO))
        self.assertFalse(logger.isEnabledFor(logging.DEBUG))

    def test_file_handler_added_only_when_path_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            setup_logging("INFO", str(path))
            root = logging.getLogger()
            files = [h for h in root.handlers
                     if isinstance(h, logging.handlers.RotatingFileHandler)]
            self.assertEqual(len(files), 1)
            logging.getLogger("admin.pool").info("落盘测试")
            for handler in root.handlers:
                handler.flush()
            self.assertIn("落盘测试", path.read_text(encoding="utf-8"))

    def test_unwritable_log_path_does_not_block_startup(self):
        """日志路径不可写只应降级，不能拦住启动。"""
        root = setup_logging("INFO", "/proc/definitely/not/writable/app.log")
        files = [h for h in root.handlers if getattr(h, "_workbuddy", False)]
        self.assertTrue(files, "stderr handler 必须仍然存在")

    def test_third_party_noise_is_suppressed(self):
        setup_logging("DEBUG")
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_uvicorn_level_follows_configured_level(self):
        setup_logging("DEBUG")
        self.assertEqual(uvicorn_level(), "debug")
        setup_logging("INFO")
        self.assertEqual(uvicorn_level(), "info")
        setup_logging("ERROR")
        self.assertEqual(uvicorn_level(), "warning")

    def test_entry_points_disable_uvicorn_access_log(self):
        """访问日志用 uvicorn 自己的 access_log=False 关，而不是改 logger level。

        早先的 configure_uvicorn_access 在 uvicorn.run() 之前改 uvicorn.access 的
        level，但 uvicorn.run() 内部会 dictConfig 把它重置回去，等于没关。
        """
        import core.converter
        import admin.server as server_module

        self.assertIn("access_log=False", inspect.getsource(core.converter.main))
        source = Path(server_module.__file__).read_text(encoding="utf-8")
        self.assertIn("access_log=False", source)
        self.assertNotIn("configure_uvicorn_access", source)


def _write_cred(auth, name, expires_delta=3600):
    # 必须含 accessToken + refreshToken + account.uid + auth.expiresAt：
    # validate_credential 会逐项校验，缺一项这个凭据就被跳过、账号根本建不起来。
    (auth / f"{name}.info").write_text(json.dumps({
        "auth": {"accessToken": f"token-{name}",
                 "expiresAt": int((time.time() + expires_delta) * 1000),
                 "refreshToken": "r"},
        "account": {"nickname": f"昵称-{name}", "uid": name, "enterpriseId": "e"},
    }), encoding="utf-8")


class StoreHealthTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.root / "mgmt").mkdir()
        self.store = Store(str(self.root / "mgmt"), str(self.auth), "k", "admin-key-long-enough-123")

    def _accounts(self, spec):
        self.store.data["accounts"] = {
            aid: {"name": aid, "file": f"{aid}.info", "enabled": enabled, "created": 0}
            for aid, enabled in spec.items()
        }

    def test_all_usable(self):
        for name in ("a1", "a2"):
            _write_cred(self.auth, name)
        self._accounts({"a1": 1, "a2": 1})
        detail = self.store.health_detail()
        self.assertEqual((detail["total"], detail["enabled"], detail["usable"]), (2, 2, 2))

    def test_disabled_account_is_not_usable(self):
        _write_cred(self.auth, "a1")
        self._accounts({"a1": 0})
        detail = self.store.health_detail()
        self.assertEqual(detail["enabled"], 0)
        self.assertEqual(detail["usable"], 0)

    def test_auth_invalid_excluded_and_counted(self):
        for name in ("a1", "a2"):
            _write_cred(self.auth, name)
        self._accounts({"a1": 1, "a2": 1})
        # 生产代码标记认证失效用的是布尔字段 auth_invalid（见 pool.py 的判据），
        # 不是 status="auth_invalid" 这个字符串——测试必须用真实形状，否则锁住错契约。
        self.store.data["account_status"] = {"a2": {"auth_invalid": True}}
        detail = self.store.health_detail()
        self.assertEqual(detail["usable"], 1)
        self.assertEqual(detail["auth_invalid"], 1)

    def test_corrupt_credential_counts_as_auth_invalid(self):
        """凭据文件损坏时 account_rows 标 status=invalid，也应从可用数里剔除。"""
        _write_cred(self.auth, "a1")
        (self.auth / "a2.info").write_text("{ not json", encoding="utf-8")
        self._accounts({"a1": 1, "a2": 1})
        detail = self.store.health_detail()
        self.assertEqual(detail["usable"], 1)
        self.assertEqual(detail["auth_invalid"], 1)

    def test_missing_credential_file_is_not_usable(self):
        _write_cred(self.auth, "a1")
        _write_cred(self.auth, "a2")
        self._accounts({"a1": 1, "a2": 1})
        (self.auth / "a2.info").unlink()
        detail = self.store.health_detail()
        self.assertEqual(detail["usable"], 1)

    def test_corrupt_credential_is_not_usable(self):
        _write_cred(self.auth, "a1")
        (self.auth / "a2.info").write_text("{ not json", encoding="utf-8")
        self._accounts({"a1": 1, "a2": 1})
        detail = self.store.health_detail()
        self.assertEqual(detail["usable"], 1)


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.root / "mgmt").mkdir()
        # 凭据必须在 create_app 之前落到 auth 目录，首次启动会导入为账号
        # （与 test_admin_server 的 asyncSetUp 同一路径）。
        _write_cred(self.auth, "session")
        self.app = create_app(str(self.root / "mgmt"), str(self.auth), "client-key",
                              "admin-key-long-enough-123")
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app)

    def test_health_ok_with_usable_account(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["accounts"]["usable"], 1)

    def test_health_degraded_when_no_usable_account(self):
        # 独立的空 auth 目录：一个账号都没有，但服务本身在跑。
        empty_auth = self.root / "empty-auth"
        empty_auth.mkdir()
        app = create_app(str(self.root / "mgmt2"), str(empty_auth), "k",
                         "admin-key-long-enough-123")
        from fastapi.testclient import TestClient
        client = TestClient(app)
        response = client.get("/health")
        # degraded 仍是 200：重启修不好凭据失效，标 unhealthy 会引发重启循环。
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "degraded")

    def test_health_does_not_leak_account_names_by_default(self):
        body = self.client.get("/health").json()
        self.assertNotIn("detail", body["accounts"])
        self.assertNotIn("昵称", json.dumps(body, ensure_ascii=False))

    def test_health_verbose_includes_detail(self):
        body = self.client.get("/health?verbose=1").json()
        self.assertIn("detail", body["accounts"])


class CredentialSecrecyTests(unittest.TestCase):
    """凭据内容与 API key 明文不得出现在日志里。

    只断言「代码里没有把敏感值写进日志的调用」是不可靠的；这里跑真实的启动与
    失败路径，然后检查日志内容里不含凭据文件中的 secret。
    """

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_workbuddy", False):
                root.removeHandler(handler)
                handler.close()

    def test_credential_secrets_absent_from_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = root / "auth"
            auth.mkdir()
            log_path = root / "app.log"
            secret_token = "PRIVATE-ACCESS-TOKEN-DEADBEEF"
            (auth / "session.info").write_text(json.dumps({
                "account": {"uid": "u1", "nickname": "昵称甲", "enterpriseId": "e"},
                "auth": {"accessToken": secret_token, "refreshToken": "PRIVATE-REFRESH-XYZ",
                         "expiresAt": int(time.time() * 1000) + 3600000},
            }), encoding="utf-8")

            setup_logging("DEBUG", str(log_path))
            create_app(str(root / "mgmt"), str(auth), "client-secret-value",
                       "admin-secret-value-long-enough")
            logging.getLogger("admin.server").info("启动后的额外一行")
            for handler in logging.getLogger().handlers:
                handler.flush()

            text = log_path.read_text(encoding="utf-8")
            self.assertNotIn(secret_token, text)
            self.assertNotIn("PRIVATE-REFRESH-XYZ", text)
            # 日志确实写出来了（避免「空文件所以断言通过」的假阳性）。
            self.assertIn("启动后的额外一行", text)


if __name__ == "__main__":
    unittest.main()

"""配置收敛：.env 解析、优先级、旧名兼容、校验。

对应 specs/config.md。重点锁住两条容易错的语义：
- 真实环境变量**胜过** .env 文件（docker/compose 显式传的必须赢）
- 旧变量名继续生效（用户 compose 里已经写了，改名不能静默失效）
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from conftest import ROOT

# 项目根：由 tests/conftest.py 统一解析。子进程 cwd 与部署布局检查都依赖它。
REPO = ROOT


def run_config(env_extra, script="print(config.get().port)"):
    """在干净子进程里跑配置读取。

    配置在模块导入时读环境，必须用子进程隔离，否则会污染本进程的 os.environ。
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WORKBUDDY_", "CODEBUDDY", "CLIENT_INFO_", "MANAGEMENT_"))
           and k not in ("PORT", "ADMIN_KEY")}
    env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-c", f"from admin import config; {script}"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    return result


class EnvFileTests(unittest.TestCase):
    def test_env_file_values_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# 注释行\n"
                "\n"
                "WORKBUDDY_PORT=9000\n"
                'WORKBUDDY_LOG_LEVEL="debug"\n'
                "WORKBUDDY_DESENSITIZE=1\n",
                encoding="utf-8",
            )
            result = run_config(
                {"WORKBUDDY_ENV_FILE": str(path)},
                "c = config.get(); print(c.port, c.log_level, c.desensitize)",
            )
            self.assertEqual(result.stdout.strip(), "9000 DEBUG True")

    def test_real_env_wins_over_env_file(self):
        """关键语义：docker/compose 显式传的变量必须赢过 .env 文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("WORKBUDDY_PORT=9000\n", encoding="utf-8")
            result = run_config({"WORKBUDDY_ENV_FILE": str(path), "WORKBUDDY_PORT": "7777"})
            self.assertEqual(result.stdout.strip(), "7777")

    def test_missing_env_file_is_not_an_error(self):
        result = run_config({"WORKBUDDY_ENV_FILE": "/nonexistent/.env"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "8787")

    def test_comments_blank_lines_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n\nWORKBUDDY_PORT = 8123 \nWORKBUDDY_LOG_LEVEL='warning'\n",
                encoding="utf-8",
            )
            result = run_config(
                {"WORKBUDDY_ENV_FILE": str(path)},
                "c = config.get(); print(c.port, c.log_level)",
            )
            self.assertEqual(result.stdout.strip(), "8123 WARNING")


class AliasTests(unittest.TestCase):
    def test_legacy_names_still_work(self):
        """旧名必须继续生效，否则用户已有 compose 会静默失效。"""
        result = run_config({"PORT": "9100"}, "print(config.get().port)")
        self.assertEqual(result.stdout.strip(), "9100")

    def test_legacy_name_is_recorded_once(self):
        """旧名被记入 legacy_used 且不重复（提示由启用方在日志就绪后统一打印）。"""
        result = run_config(
            {"PORT": "9100"},
            "config.get(); config.get(); config.get(); print(config.legacy_used())",
        )
        self.assertEqual(result.stdout.strip(), "[('PORT', 'WORKBUDDY_PORT')]")
        # 提示不在配置读取期打印：那时日志还没初始化，裸 stderr 会冲在启动横幅前面。
        self.assertNotIn("已弃用", result.stderr)

    def test_legacy_summary_printed_by_entrypoint(self):
        """启用方在日志就绪后汇总提示一次，不是每个变量一行。"""
        result = run_config(
            {"PORT": "9100", "ADMIN_KEY": "x" * 32},
            "import logging, admin.server as s; "
            "logging.basicConfig(level=logging.INFO); "
            "s._warn_legacy_config()",
        )
        self.assertIn("旧名", result.stderr)
        self.assertEqual(result.stderr.count("旧名"), 1)

    def test_canonical_name_beats_legacy(self):
        result = run_config({"WORKBUDDY_PORT": "8100", "PORT": "9100"})
        self.assertEqual(result.stdout.strip(), "8100")

    def test_legacy_log_and_auth_dir(self):
        result = run_config(
            {"CODEBUDDY_AUTH_DIR": "/tmp/legacy-auth"},
            "print(config.get().auth_dir)",
        )
        self.assertEqual(result.stdout.strip(), "/tmp/legacy-auth")


class ValidationTests(unittest.TestCase):
    def test_non_integer_port_fails_loudly(self):
        result = run_config({"WORKBUDDY_PORT": "abc"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("必须是整数", result.stderr)

    def test_out_of_range_port_fails_loudly(self):
        result = run_config({"WORKBUDDY_PORT": "99999"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1-65535", result.stderr)

    def test_unknown_log_level_falls_back_to_info(self):
        result = run_config(
            {"WORKBUDDY_LOG_LEVEL": "BOGUS"},
            "print(config.get().log_level)",
        )
        self.assertEqual(result.stdout.strip(), "INFO")
        self.assertIn("回退 INFO", result.stderr)


class EntryPointTests(unittest.TestCase):
    def test_port_really_takes_effect(self):
        """PORT 以前写在 env.example 里但被硬编码覆盖，现在必须真的生效。"""
        result = run_config(
            {"WORKBUDDY_PORT": "9314"},
            "import admin.server as s; print(s.config.get().port)",
        )
        self.assertEqual(result.stdout.strip(), "9314")

    def test_create_app_does_not_force_log_path_to_none(self):
        """此前 create_app 无条件把 log_path 覆盖成 None，导致设了日志变量也不记。"""
        result = run_config(
            {"WORKBUDDY_LOG": "/tmp/some.log"},
            "import admin.server as s, core.converter as c; "
            "s.create_app('/tmp/cfgtest-mgmt', '/tmp/cfgtest-auth', 'k', 'admin-key-long-enough-123'); "
            "print(c.CONFIG['log_path'])",
        )
        self.assertEqual(result.stdout.strip(), "/tmp/some.log")

    def test_converter_accepts_config_defaults(self):
        result = run_config(
            {"WORKBUDDY_PORT": "9315", "WORKBUDDY_API_KEY": "from-config"},
            "import inspect, core.converter as c; "
            "src = inspect.getsource(c.main); "
            "print('cfg.port' in src and 'cfg.api_key' in src)",
        )
        self.assertEqual(result.stdout.strip(), "True")


class ShimTests(unittest.TestCase):
    """admin 侧的兼容垫片必须转发 core 的全部公开名字。

    手写转发名单会在 core 新增函数时静默漏掉——曾经漏掉 legacy_used()，
    导致 create_app 直接 AttributeError 起不来。
    """

    def test_admin_shims_forward_all_public_names(self):
        from core import runtime_config, runtime_logging
        from admin import config as admin_config, logging_setup as admin_logging

        for core_module, shim in ((runtime_config, admin_config),
                                  (runtime_logging, admin_logging)):
            for name in dir(core_module):
                if name.startswith("__"):
                    continue
                self.assertTrue(
                    hasattr(shim, name),
                    f"{shim.__name__} 未转发 core 的 {name}",
                )

    def test_shims_point_at_same_implementation(self):
        from core import runtime_config, runtime_logging
        from admin import config as admin_config, logging_setup as admin_logging

        self.assertIs(admin_config.get, runtime_config.get)
        self.assertIs(admin_logging.setup_logging, runtime_logging.setup_logging)


class DeploymentLayoutTests(unittest.TestCase):
    """镜像布局必须能起得来——core 不能**无保护地**依赖 admin。

    deploy/standalone/Dockerfile 只 `COPY core ./core`，所以 core 里模块级的
    `from admin import ...` 会让「独立转换器」镜像直接 ModuleNotFoundError 起不来。
    延迟导入且有 except ImportError 兜底的（如 converter._cred 里取
    REQUEST_CREDENTIAL）是刻意设计：管理部署下用它做每请求凭据绑定，独立部署下
    回落到单账号行为。这里锁住「不能是无保护的模块级导入」这个真正的风险。
    """

    def test_core_has_no_unguarded_module_level_admin_import(self):
        core = REPO / "core"
        offenders = []
        for path in core.glob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not (stripped.startswith(("from admin", "import admin")) or
                        "from admin import" in stripped):
                    continue
                # 带缩进的（函数内）延迟导入允许——调用点自带 ImportError 兜底。
                if line[:1].isspace():
                    continue
                offenders.append(f"{path.name}: {stripped}")
        self.assertEqual(
            offenders, [],
            f"core 不得在模块级导入 admin（standalone 镜像只有 core）：{offenders}",
        )

    def test_converter_imports_without_admin_available(self):
        """按 standalone 布局（只有 core）真跑一次导入与凭据回落路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            shutil.copytree(REPO / "core", app / "core",
                            ignore=shutil.ignore_patterns("__pycache__"))
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("WORKBUDDY_", "CODEBUDDY", "CLIENT_INFO_"))
                   and k not in ("PORT", "ADMIN_KEY")}
            script = (
                "import core.converter as c;"
                "assert 'admin' not in __import__('sys').modules;"
                "c.CONFIG['cred'] = None;"
                "ok=False\n"
                "try:\n"
                "    c._cred()\n"
                "except Exception as exc:\n"
                "    ok='admin' not in str(exc) and 'ModuleNotFound' not in str(exc)\n"
                "assert ok, '缺少 admin 时 _cred 应回落到原有 503，而不是 ImportError'\n"
                "print('ok')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=app, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ok", result.stdout)



class LazyBackendImportTests(unittest.TestCase):
    """导入 admin 侧模块**不得**顺带把 Trae 的私有协议拉进来。

    这是 registry.py 顶部注释明写的设计约束：trae 那套（OAuth 流程、ExchangeToken
    刷新、登录态解析）只在真用到时才 import。它极易被一行「顺手求值的模块级常量」
    破坏 —— 例如 ``TRAE_PREFIX = spec_for("trae").model_prefix``，看着无害，
    却让**所有部署形态**（含不启用 trae 的）在 import 时就构造 TraeBackend。

    必须用子进程：本进程里 trae 早被其他用例 import 过了，测不出来。
    """

    def _run(self, imports):
        code = ("import sys; " + "; ".join(f"import {m}" for m in imports) +
                "; print('core.backends.trae' in sys.modules)")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WORKBUDDY_")}
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    def test_admin_models_import_does_not_pull_trae(self):
        result = self._run(["admin.models"])
        self.assertEqual(result.stdout.strip(), "False",
                         "import admin.models 顺带构造了 TraeBackend —— "
                         "检查有没有模块级 spec_for('trae')/trae_backend() 求值")

    def test_admin_server_import_does_not_pull_trae(self):
        result = self._run(["admin.server"])
        self.assertEqual(result.stdout.strip(), "False",
                         "import admin.server 顺带构造了 TraeBackend")

    def test_registry_import_does_not_pull_trae(self):
        result = self._run(["core.backends.registry"])
        self.assertEqual(result.stdout.strip(), "False",
                         "import registry 本身就该是懒的")


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


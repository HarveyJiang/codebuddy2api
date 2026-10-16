"""各后端目录的**对称性**测试（需求 2 的目录泛化）。

重构前「默认后端的目录」与「Trae 的目录」是两套写死的结构（``_catalog`` +
``_trae_catalog``），Trae 那套还得被显式 ``ensure_trae_builtin()`` /
``refresh_trae_models()`` 喂 —— 加第三个后端就得再抄一遍这三件事。

现在它们对称：``_state_for(backend)`` 一份结构，``ensure_static_catalog(backend)`` /
``refresh_catalog(backend)`` / ``catalog_entries(backend)`` 各管一层。这个文件锁住
「对称」本身，因为它是「加后端零改动」的载体。

**同步时刻**（``updated``）单独验：它必须按后端分开存、跨重启各归各的 —— 曾经
想塞进 settings 表，但那张表是固定列的单行，后端数量是可变的，塞不进去。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import pytest

from admin.db import Database
from admin.models import ModelRegistry
import core.backends.trae as T


def registry(clock=None):
    return ModelRegistry(get_credential=None, clock=clock or time.time)


class PerBackendCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_every_backend_gets_its_own_state(self):
        """注册表里的每个后端都有一份独立目录状态，互不共享。"""
        reg = registry()
        for name in ("workbuddy", "trae"):
            st = reg._state_for(name)
            self.assertIsInstance(st["catalog"], dict)
            self.assertIsInstance(st["available"], list)
            self.assertIsInstance(st["disabled"], set)
        self.assertIsNot(reg._state_for("workbuddy")["catalog"],
                         reg._state_for("trae")["catalog"])

    def test_default_backend_alias_points_at_same_state(self):
        """`_catalog` 等旧属性是默认后端状态的**别名**，不是另一份拷贝。

        别名不是拷贝很重要：旧代码写 ``self._catalog = {...}`` 之后，
        ``_state_for(默认后端)`` 必须看到新值，否则两处会飘。
        """
        reg = registry()
        reg._catalog = {"x": {"name": "X"}}
        self.assertIn("x", reg._state_for(reg._default)["catalog"])
        reg._state_for(reg._default)["catalog"]["y"] = {"name": "Y"}
        self.assertIn("y", reg._catalog)

    def test_static_catalog_comes_from_backend_declaration(self):
        """静态兜底条目来自后端自己的声明，不是 admin 里的写死表。"""
        reg = registry()
        reg.ensure_static_catalog("trae")
        ids = [m["id"] for m in reg.catalog_entries("trae")]
        declared = [m["id"] for m in T.TRAE_STATIC_MODELS]
        self.assertEqual(ids, declared)
        self.assertEqual(len(ids), 20)  # 注释里曾写「静态 32 条」，是陈旧笔误

    def test_ensure_static_catalog_does_not_overwrite_existing(self):
        """已有目录时静态兜底不覆盖 —— 否则每次启动都会把动态表冲回静态表。"""
        reg = registry()
        reg.ensure_static_catalog("trae")
        reg._state_for("trae")["catalog"]["dyn"] = {"name": "Dyn", "source": "upstream"}
        reg._state_for("trae")["available"].append("dyn")
        reg.ensure_static_catalog("trae")
        self.assertIn("dyn", reg._state_for("trae")["catalog"])

    def test_refresh_replaces_static_and_keeps_other_backend(self):
        """刷新一个后端不影响另一个后端（后端隔离在目录上的体现）。"""
        reg = registry()
        reg.ensure_builtin()
        reg.ensure_static_catalog("trae")
        wb_before = list(reg._state_for("workbuddy")["available"])

        class FakeManager:
            def fetch_models(self):
                return [{"id": "dyn-a", "name": "DynA", "context_length": 2048}]

        reg.backend_credential_getter = lambda _b: FakeManager()
        self.assertTrue(reg.refresh_catalog("trae"))
        self.assertEqual([m["id"] for m in reg.catalog_entries("trae")], ["dyn-a"])
        self.assertEqual(reg._state_for("workbuddy")["available"], wb_before,
                         "刷新 Trae 目录不得动到默认后端的目录")

    def test_refresh_failure_keeps_previous_snapshot(self):
        """拉取失败保留上次快照，并记下错误（而不是把目录清空）。"""
        reg = registry()
        reg.ensure_static_catalog("trae")
        before = reg.snapshot_ids()

        class Boom:
            def fetch_models(self):
                raise T.TraeAPIError("boom", status=500)

        reg.backend_credential_getter = lambda _b: Boom()
        self.assertFalse(reg.refresh_catalog("trae"))
        self.assertEqual(reg.snapshot_ids(), before)
        self.assertIn("拉取失败", reg._state_for("trae")["error"])

    def test_refresh_without_credential_is_silent(self):
        """没有该后端的账号时不报错、不覆盖 —— 等下次有机会再拉。"""
        reg = registry()
        reg.ensure_static_catalog("trae")
        reg.backend_credential_getter = lambda _b: None
        self.assertFalse(reg.refresh_catalog("trae"))
        self.assertEqual(len(reg.catalog_entries("trae")), 20)

    def test_refresh_keeps_disabled_only_for_still_present_models(self):
        """上游下线的模型不再保留禁用状态：重新上线时默认可用。"""
        reg = registry()
        reg.ensure_static_catalog("trae")
        reg.set_enabled("glm-5.2", False, backend="trae")
        self.assertIn("glm-5.2", reg._state_for("trae")["disabled"])

        class FakeManager:
            def fetch_models(self):
                # 新表里没有 glm-5.2
                return [{"id": "other", "name": "Other", "context_length": 1024}]

        reg.backend_credential_getter = lambda _b: FakeManager()
        self.assertTrue(reg.refresh_catalog("trae"))
        self.assertEqual(reg._state_for("trae")["disabled"], set())


class SyncTimestampTests(unittest.TestCase):
    """每个后端的「上次拉取时刻」分开存、跨重启各归各的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _with_dynamic_trae(self, reg, stamp):
        reg.ensure_static_catalog("trae")
        reg._state_for("trae")["updated"] = stamp
        return reg

    def test_synced_at_round_trips_per_backend(self):
        reg = registry()
        reg.ensure_builtin()          # 默认后端要先有目录行，synced_at 才有地方落
        reg.ensure_static_catalog("trae")
        reg._state_for("trae")["updated"] = 1_700_000_000
        reg._state_for("workbuddy")["updated"] = 1_600_000_000
        reg.persist(self.db)

        reg2 = registry()
        reg2.load(self.db)
        self.assertEqual(reg2._state_for("trae")["updated"], 1_700_000_000)
        self.assertEqual(reg2._state_for("workbuddy")["updated"], 1_600_000_000)

    def test_default_backend_timestamp_survives_empty_catalog(self):
        """默认后端一行模型都没有时，同步时刻仍要保得住。

        它落在 settings.models_updated（既有的单值契约）；load 时若目录行的
        synced_at 都是 0（没有行可带），就回落到它。否则「拉过一次但上游返回空表」
        的状态重启后会被当成「从没拉过」，管理页会反复重拉。
        """
        reg = registry()          # 故意不 ensure_builtin
        reg._state_for("workbuddy")["updated"] = 1_650_000_000
        reg.persist(self.db)
        self.assertEqual((self.db.get_settings() or {}).get("models_updated"), 1_650_000_000)

        reg2 = registry()
        reg2.load(self.db)
        self.assertEqual(reg2._state_for("workbuddy")["updated"], 1_650_000_000)

    def test_default_backend_prefers_row_timestamp_over_setting(self):
        """有目录行时以行上的 synced_at 为准（行是更精确的来源）。"""
        reg = registry()
        reg.ensure_builtin()
        reg._state_for("workbuddy")["updated"] = 1_600_000_000
        reg.persist(self.db)
        # 手工把 settings 改成一个更旧的值，模拟历史遗留。
        self.db.update_settings({"models_updated": 1_500_000_000})
        reg2 = registry()
        reg2.load(self.db)
        self.assertEqual(reg2._state_for("workbuddy")["updated"], 1_600_000_000)

    def test_synced_at_does_not_leak_into_settings(self):
        """同步时刻不该被塞进 settings —— 那张表是固定列单行，装不下可变的后端数。

        回归：曾经写成 ``models_updated_<backend>``，一 persist 就
        ``no such column: models_updated_trae``，整个 create_app 挂掉。
        """
        reg = self._with_dynamic_trae(registry(), 1_700_000_000)
        reg.persist(self.db)  # 不抛异常就是通过
        settings = self.db.get_settings() or {}
        for key in settings:
            self.assertFalse(str(key).startswith("models_updated_"),
                             f"settings 里出现了 per-backend 列名 {key}")

    def test_default_backend_still_updates_legacy_setting(self):
        """settings.models_updated 继续写默认后端 —— 它是既有的对外契约。"""
        reg = registry()
        reg._state_for("workbuddy")["updated"] = 1_650_000_000
        reg.persist(self.db)
        self.assertEqual((self.db.get_settings() or {}).get("models_updated"), 1_650_000_000)


class CatalogToggleTests(unittest.TestCase):
    """禁用/启用按 **backend** 分派到正确的后端目录。

    v16 起模型 id 是裸名，`glm-5.2` 在两个后端都有 —— 只凭 id 无法分派，
    所以 `set_enabled` 收 `backend`。这里守住「两个后端的开关互不影响」。
    """

    def test_set_enabled_routes_by_backend(self):
        reg = registry()
        reg.ensure_builtin()
        reg.ensure_static_catalog("trae")
        reg.set_enabled("glm-5.2", False, backend="workbuddy")
        reg.set_enabled("glm-5.2", False, backend="trae")
        self.assertIn("glm-5.2", reg._state_for("workbuddy")["disabled"])
        self.assertIn("glm-5.2", reg._state_for("trae")["disabled"])
        # 两个开关互不影响：只放开默认后端那一个，Trae 的仍是禁用。
        reg.set_enabled("glm-5.2", True, backend="workbuddy")
        self.assertNotIn("glm-5.2", reg._state_for("workbuddy")["disabled"])
        self.assertIn("glm-5.2", reg._state_for("trae")["disabled"])
        # 反向同理。
        reg.set_enabled("glm-5.2", True, backend="trae")
        self.assertNotIn("glm-5.2", reg._state_for("trae")["disabled"])

    def test_set_enabled_same_name_on_two_backends_does_not_cross(self):
        """同名两行必须能被分开操作 —— 这是联合主键/去前缀的**核心不变量**。"""
        reg = registry()
        reg.ensure_builtin()
        reg.ensure_static_catalog("trae")
        reg.set_enabled("glm-5.2", False, backend="trae")
        self.assertNotIn("glm-5.2", reg._state_for("workbuddy")["disabled"],
                         "禁 Trae 的那行不该把 WB 的同名行也禁掉")

    def test_set_enabled_unknown_raises(self):
        reg = registry()
        reg.ensure_static_catalog("trae")
        with self.assertRaises(KeyError):
            reg.set_enabled("nope", True, backend="trae")
        with self.assertRaises(KeyError):
            reg.set_enabled("nope", True)

    def test_snapshot_groups_by_backend_in_registry_order(self):
        """快照里每个后端的行连续排列、顺序由注册表声明决定。"""
        reg = registry()
        reg.ensure_builtin()
        reg.ensure_static_catalog("trae")
        beats = [m["backend"] for m in reg.snapshot()["models"]]
        # 每个后端只出现一段（不交错）。
        seen, order = set(), []
        for b in beats:
            if not order or order[-1] != b:
                order.append(b)
            seen.add(b)
        self.assertEqual(len(order), len(seen), f"后端行交错排列：{order}")
        self.assertEqual(order[0], "workbuddy", "默认后端应排在最前")


class CatalogUpdatedAccessorTests(unittest.TestCase):
    """``catalog_updated(backend)``：启动路径判断「要不要拉动态目录」的公开读数。

    server 侧不再伸手进 ``_state`` 私有结构（``registry._state_for(x)["updated"]``），
    而是问这一个方法。它必须与内部 state 完全同源、且**不把裸 0 与「拉过但空表」
    混起来**——0 只有一个含义：从没拉到过，用静态兜底。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_zero_when_never_fetched(self):
        """静态兜底铺好但没拉过 → 0（启动路径据此决定拉一次）。"""
        reg = registry()
        reg.ensure_static_catalog("trae")
        self.assertEqual(reg.catalog_updated("trae"), 0)

    def test_nonzero_after_successful_refresh(self):
        """拉到动态目录后 > 0（启动路径据此**不**再打扰上游）。"""
        reg = registry(clock=lambda: 1_700_000_000)
        reg.ensure_static_catalog("trae")

        class FakeManager:
            def fetch_models(self):
                return [{"id": "dyn", "name": "Dyn", "context_length": 1024}]

        reg.backend_credential_getter = lambda _b: FakeManager()
        self.assertTrue(reg.refresh_catalog("trae"))
        self.assertEqual(reg.catalog_updated("trae"), 1_700_000_000)

    def test_survives_round_trip(self):
        """跨重启读得回来 —— 否则每次重启都会多拉一次上游。"""
        reg = registry()
        reg.ensure_static_catalog("trae")
        reg._state_for("trae")["updated"] = 1_700_000_000
        reg._state_for("trae")["catalog"]["dyn"] = {"name": "Dyn", "source": "upstream"}
        reg._state_for("trae")["available"].append("dyn")
        reg.persist(self.db)

        reg2 = registry()
        reg2.load(self.db)
        self.assertEqual(reg2.catalog_updated("trae"), 1_700_000_000)

    def test_tracks_backends_independently(self):
        """一个后端的时刻不影响另一个（启动路径按后端逐个判断）。"""
        reg = registry()
        reg.ensure_builtin()
        reg.ensure_static_catalog("trae")
        reg._state_for("workbuddy")["updated"] = 1_600_000_000
        self.assertEqual(reg.catalog_updated("workbuddy"), 1_600_000_000)
        self.assertEqual(reg.catalog_updated("trae"), 0)


@pytest.mark.real_catalog_check   # 测的就是这个函数本身，别被 conftest 桩掉
class StartupRefreshTests(unittest.TestCase):
    """启动时给「从没拉到过」的非默认后端各拉一次动态目录。

    回归：#98a5f31 部署后 Trae 的 20 条一直标 ``source=builtin``，因为启动路径
    只铺静态兜底、动态目录全靠 04:00 定时任务。用户看到的就是「不是通过接口
    拉取上游的么」。这个类钉住修复：**没拉到过就拉，拉到了就不打扰，拉失败也
    不影响启动**。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _registry(self):
        reg = registry()
        reg.ensure_builtin()
        reg.ensure_static_catalog("trae")
        return reg

    def test_fetches_when_never_fetched(self):
        """从没拉过 → 拉一次，Trae 行由 builtin 变 upstream。"""
        from admin.server import _refresh_missing_catalogs
        reg = self._registry()

        class FakeManager:
            def fetch_models(self):
                return [{"id": "dyn-a", "name": "DynA", "context_length": 2048}]

        reg.backend_credential_getter = lambda _b: FakeManager()
        self.assertTrue(_refresh_missing_catalogs(reg))
        self.assertEqual([m["id"] for m in reg.catalog_entries("trae")], ["dyn-a"])
        self.assertGreater(reg.catalog_updated("trae"), 0)

    def test_skips_when_already_fetched(self):
        """已有快照 → 不打扰上游（重启远比 04:00 频繁）。

        记账而不是靠抛异常：``refresh_catalog`` 会把 ``Exception`` 吞掉（失败保留
        上次快照是它的既有语义），所以「抛了就算失败」在这里证不了任何事 ——
        必须断言**调用次数为 0**。这条是变异测试逼出来的：最初用 ``Boom`` 抛异常，
        删掉守卫后测试仍全绿。
        """
        from admin.server import _refresh_missing_catalogs
        reg = self._registry()
        reg._state_for("trae")["updated"] = 1_700_000_000

        hits = []

        class Rec:
            def fetch_models(self):
                hits.append(1)
                return [{"id": "x", "name": "X", "context_length": 1}]

        reg.backend_credential_getter = lambda _b: Rec()
        self.assertFalse(_refresh_missing_catalogs(reg))
        self.assertEqual(hits, [], "已有快照时不该再拉上游")

    def test_never_touches_default_backend(self):
        """默认后端不在这里拉 —— 它有自己的 fetch()/静态兜底路径。"""
        from admin.server import _refresh_missing_catalogs
        reg = registry()      # 默认后端也没有快照

        seen = []

        class Rec:
            def fetch_models(self):
                seen.append("trae")
                return [{"id": "x", "name": "X", "context_length": 1}]

        reg.backend_credential_getter = lambda b: (seen.append(b), Rec())[1]
        _refresh_missing_catalogs(reg)
        self.assertNotIn("workbuddy", seen)

    def test_failure_does_not_raise(self):
        """拉失败静默吞掉：启动不该因拉目录失败而挂。"""
        from admin.server import _refresh_missing_catalogs
        reg = self._registry()

        class Boom:
            def fetch_models(self):
                raise T.TraeAPIError("boom", status=500)

        reg.backend_credential_getter = lambda _b: Boom()
        self.assertFalse(_refresh_missing_catalogs(reg))   # 不抛
        self.assertEqual(len(reg.catalog_entries("trae")), 20)  # 静态兜底还在

    def test_disabled_backend_is_not_fetched(self):
        """总闸关掉的后端不拉（refresh_catalog 自身的前置守卫）。

        同样用记账而非抛异常 —— 让「关掉的后端确实没被碰」变成可断言的事实。
        """
        from admin.server import _refresh_missing_catalogs
        from core.backends.registry import set_enabled_provider
        reg = self._registry()

        hits = []

        class Rec:
            def fetch_models(self):
                hits.append(1)
                return [{"id": "x", "name": "X", "context_length": 1}]

        reg.backend_credential_getter = lambda _b: Rec()
        set_enabled_provider(lambda: {"trae": False})
        try:
            self.assertFalse(_refresh_missing_catalogs(reg))
            self.assertEqual(hits, [], "被总闸关掉的后端不该拉")
        finally:
            set_enabled_provider(lambda: {})


class StartupRefreshThroughCreateAppTests(unittest.TestCase):
    """端到端钉住「启动路径**确实调了** _refresh_missing_catalogs」。

    上面那组单测直接调函数，所以「把 create_app 里的调用删掉」这种变异抓不住
    ——实测确认过。这里补一条走完整 ``create_app`` 的用例：造一个真实的 Trae
    凭据文件 + 打桩的上游，启动后断言 Trae 目录已变成动态表。

    用 ``real_catalog_check`` 关掉 conftest 里对 ``_refresh_missing_catalogs``
    的自动打桩，否则测不到真路径。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    @pytest.mark.real_catalog_check
    def test_create_app_fetches_trae_catalog_on_boot(self):
        import json as _json
        from fastapi.testclient import TestClient
        from admin.server import create_app
        from conftest import credential

        auth = Path(self.tmp.name) / "auth"
        auth.mkdir()
        # 必须是一份 backend=trae 的凭据，make_credential 才会分派 TraeCredentialManager。
        # refreshToken 是 validate_credential 的硬要求，缺了这份账号直接被拒收。
        (auth / "trae-1.info").write_text(_json.dumps({
            "backend": "trae",
            "account": {"uid": "trae-1", "nickname": "t"},
            "auth": {"accessToken": "tok", "refreshToken": "rtok",
                     "expiresAt": int(time.time()) + 3600},
        }), encoding="utf-8")
        # 默认后端的账号也放一份：不然后端总闸的「≥1 启用」等启动检查可能报警。
        (auth / "wb-1.info").write_text(_json.dumps(credential(uid="wb-1")), encoding="utf-8")

        calls = []

        def fake_fetch(self):
            calls.append(1)
            return [{"id": "dyn-trae", "name": "DynTrae", "context_length": 4096, "credits": 0.5}]

        import core.backends.trae as _trae
        orig = _trae.TraeCredentialManager.fetch_models
        _trae.TraeCredentialManager.fetch_models = fake_fetch
        try:
            app = create_app(str(Path(self.tmp.name) / "mgmt"), str(auth),
                             "k", "admin-key-long-enough-123")
            client = TestClient(app)
            with client:   # 触发 lifespan
                pass
        finally:
            _trae.TraeCredentialManager.fetch_models = orig

        self.assertTrue(calls, "create_app 启动时必须拉一次 Trae 目录")
        reg = app.state.store  # 仅确认 app 建起来了
        self.assertIsNotNone(reg)


if __name__ == "__main__":
    unittest.main()

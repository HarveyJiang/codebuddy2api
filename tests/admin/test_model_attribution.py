"""模型归属显式化（specs/model-backend-attribution.md）。

覆盖四条承诺：

1. ``models.backend`` 列：persist 写入、load 读出，且与前缀推导**永不分叉**
   （drift 断言 —— 将来有人改写入路径漏设列，它先红）；
2. ``pinned_models`` 声明：三档路由的归属由声明保证，不再是「无前缀所以恰好
   归默认后端」的巧合；
3. 迁移 v12 回填：历史行按前缀推导填一次，之后列即权威；
4. 四层排序：来源 → 归属 → 启用 → 倍率。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admin.db import Database, SCHEMA_VERSION
from admin.models import ModelRegistry
from core.backends.registry import backend_name_for, spec_for

from tests.admin.test_model_registry import FakeManager, upstream_payload


class PinnedModelsTests(unittest.TestCase):
    """三档智能路由的归属由 `BackendSpec.pinned_models` 声明保证。"""

    def test_workbuddy_declares_the_three_routing_tiers(self):
        self.assertEqual(spec_for("workbuddy").pinned_models,
                         ("fast-model", "balanced-model", "deep-model"))

    def test_pinned_models_resolve_to_their_declaring_backend(self):
        """声明优先于「无前缀 ⇒ 默认后端」的兜底：归属是保证，不是巧合。"""
        for mid in spec_for("workbuddy").pinned_models:
            self.assertEqual(backend_name_for(mid), "workbuddy")

    def test_base_declares_empty_pinned_models(self):
        """基类默认空元组：不参与 pin 的后端不受影响。"""
        from core.backends.base import BackendSpec

        self.assertEqual(BackendSpec.pinned_models, ())

    def test_unknown_bare_name_still_falls_back_to_default(self):
        """pin 声明不得吞掉兜底分支：非 pin 的裸名照旧归默认后端。"""
        self.assertEqual(backend_name_for("auto"), "workbuddy")
        self.assertEqual(backend_name_for("glm-5.2"), "workbuddy")

    def test_prefix_still_wins_for_prefixed_ids(self):
        self.assertEqual(backend_name_for("trae/kimi-k3"), "trae")

    def test_pinned_models_are_not_shared_with_trae(self):
        """Trae 没声明 pin，故三档不会漏给 Trae（防将来复制声明时抄错）。"""
        self.assertEqual(spec_for("trae").pinned_models, ())

    def test_pinned_lookup_is_declaration_driven_not_fallback(self):
        """pin 判定必须真的读声明 —— 不能靠「无前缀恰好归默认后端」。

        判据：给**非默认后端**加一个 pin 声明，若实现真在读声明，那个 id 就会
        归它；若实现只走前缀+兜底（或干脆删掉了 pin 查找），它会落到默认后端。
        今天 workbuddy 的 pin 恰好都是无前缀 id、结论与兜底一致，所以只断言
        「三档归 workbuddy」抓不住「pin 查找被删」——这条换一个后端来钉住机制。
        """
        from core.backends.registry import _BACKENDS, _REGISTRY

        trae = spec_for("trae")
        original = trae.pinned_models
        trae.pinned_models = ("pinned-sandbox-model",)
        try:
            self.assertEqual(backend_name_for("pinned-sandbox-model"), "trae",
                             "pin 声明没有被读取 —— 判定退化成了前缀+默认兜底")
        finally:
            trae.pinned_models = original

    def test_pin_beats_prefix(self):
        """pin 的判定在**前缀之前**：一个带前缀的 id 若被某后端 pin，归 pin 的后端。

        钉住 `backend_name_for` 里两块循环的先后顺序。
        """
        trae = spec_for("trae")
        wb = spec_for("workbuddy")
        original = wb.pinned_models
        wb.pinned_models = ("trae/kimi-k3",)
        try:
            self.assertEqual(backend_name_for("trae/kimi-k3"), "workbuddy",
                             "pin 应优先于前缀判定")
        finally:
            wb.pinned_models = original


class BackendColumnTests(unittest.TestCase):
    """`models.backend` 列：写入即定，读不再解析字符串（§3.1）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "state.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _registry(self):
        return ModelRegistry(get_credential=lambda: FakeManager())

    def _fetched(self):
        registry = self._registry()
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        return registry

    def test_persist_writes_backend_column(self):
        registry = self._fetched()
        registry.persist(self.db)
        rows = self.db.load_models(only_available=True)
        self.assertTrue(rows)
        for mid, row in rows.items():
            self.assertEqual(row["backend"], backend_name_for(mid),
                             f"{mid} 的 backend 列与归属不一致")

    def test_load_reads_column_not_prefix(self):
        """列是权威：带前缀的 id 若列声称归别的后端，就以列为准。

        构造 `trae/glm-5.2` 但列写 `workbuddy`（与前缀矛盾）。若 load 还在按前缀
        推导，这行会进 Trae 目录；读列则进 WorkBuddy 目录。断言后者。
        """
        self.db.save_models({
            "trae/glm-5.2": {"name": "GLM-5.2", "source": "upstream",
                             "available": 1, "enabled": 1, "backend": "workbuddy"},
        })
        fresh = self._registry()
        fresh.load(self.db)
        # 读列 → 归 WorkBuddy；此时前缀 "trae/" 不被剥离，bare 就是完整 id。
        self.assertIn("trae/glm-5.2", fresh._state_for("workbuddy")["catalog"])
        self.assertNotIn("glm-5.2", fresh._state_for("trae")["catalog"])

    def test_load_ignores_prefix_when_column_is_set(self):
        """反向对照：列与前缀一致时，行为与从前（按前缀分派）完全相同。

        两条合起来证明「换成读列」对既有数据零行为变化。
        """
        self.db.save_models({
            "trae/kimi-k3": {"name": "Kimi", "source": "upstream",
                             "available": 1, "enabled": 1, "backend": "trae"},
        })
        fresh = self._registry()
        fresh.load(self.db)
        self.assertIn("kimi-k3", fresh._state_for("trae")["catalog"])
        self.assertNotIn("kimi-k3", fresh._state_for("workbuddy")["catalog"])

    def test_no_drift_between_column_and_prefix(self):
        """drift 断言：全表不得存在「列值与前缀推导不符」的行。

        这是结构性断言——将来有人加了一条写模型的路径而忘了设列，它先红。
        例外只有 `pinned_models`：它们的权威是声明而不是前缀（今天两者结论一致）。
        """
        registry = self._fetched()
        registry.persist(self.db)
        pinned = {m for name in ("workbuddy", "trae") for m in spec_for(name).pinned_models}
        for mid, row in self.db.load_models().items():
            if mid in pinned:
                continue
            self.assertEqual(row["backend"], backend_name_for(mid),
                             f"{mid}：列={row['backend']} 前缀推导={backend_name_for(mid)}")

    def test_fresh_database_has_the_column(self):
        """新库走 CREATE TABLE 而不是 ALTER —— 漏改 DDL 会 no such column。

        v10（backend_enabled）踩过同一个坑。
        """
        cols = [r[1] for r in self.db._write.execute("PRAGMA table_info(models)")]
        self.assertIn("backend", cols)

    def test_pinned_models_land_in_default_backend(self):
        """三档（builtin）persist 后列值 = 默认后端，与声明一致。"""
        registry = self._fetched()
        registry.persist(self.db)
        rows = self.db.load_models()
        for mid in spec_for("workbuddy").pinned_models:
            self.assertIn(mid, rows, f"{mid} 应被 persist")
            self.assertEqual(rows[mid]["backend"], "workbuddy")


class MigrationTests(unittest.TestCase):
    """v11 → v12：加列 + 按前缀回填一次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_v11(self):
        """造一个 v11 库：models 表**真的**没有 backend 列，历史行只有带前缀的 model_id。

        不能用 ``ALTER TABLE ... DROP COLUMN``：SQLite 3.35+ 虽然支持，但被
        NOT NULL/默认值等条件限制，且我们就是要验「迁移能补上这个列」，
        直接手写 v11 的 DDL 最忠实。
        """
        import sqlite3

        path = self.root / "state.db"
        # 先让 Database 建出全部表结构（含 schema_meta 与其它表），再打开旧库时
        # 它会走迁移。这里手工把 models 表退回 v11 的形态。
        Database(path).close()
        conn = sqlite3.connect(path)
        conn.executescript(
            "DROP TABLE models;"
            "CREATE TABLE models ("
            " model_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,"
            " credits_micro INTEGER, max_input_tokens INTEGER,"
            " source TEXT NOT NULL DEFAULT 'builtin',"
            " available INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,"
            " sort_order INTEGER NOT NULL DEFAULT 0, promo TEXT,"
            " synced_at INTEGER NOT NULL DEFAULT 0);"
            "INSERT INTO models (model_id,name,source,available,enabled,sort_order,synced_at)"
            " VALUES ('glm-5.2','GLM','upstream',1,1,0,0),"
            "        ('trae/kimi-k3','Kimi','upstream',1,1,0,0);"
            "UPDATE schema_meta SET value='11' WHERE key='version';"
        )
        conn.commit()
        conn.close()
        return path

    def test_migration_backfills_by_prefix(self):
        path = self._make_v11()
        db = Database(path)  # 打开即迁移
        try:
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
            rows = db.load_models()
            self.assertEqual(rows["glm-5.2"]["backend"], "workbuddy")
            self.assertEqual(rows["trae/kimi-k3"]["backend"], "trae")
        finally:
            db.close()

    def test_v12_db_without_deleted_column_is_upgraded(self):
        """已 stamp 成 v12 但没有 deleted 列的库，打开时必须补上该列。

        这是真实踩到的坑：`deleted` 最初写在 `_v11_to_v12` 里，而任何已经跑过
        v12 的库版本号已是 12、迁移不再执行 —— `load_models` 直接
        `no such column: deleted`（本地测试库与 dev 都中招）。
        所以它必须是**独立的 v13**。这条测试造出那个半成品状态。
        """
        import sqlite3

        path = self.root / "half.db"
        Database(path).close()
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS _noop(x);"
            # 退回「v12 已 stamp、但列里没有 deleted」的状态。
            "UPDATE schema_meta SET value='12' WHERE key='version';"
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(models)")]
        if "deleted" not in cols:
            conn.execute("ALTER TABLE models ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        # 现在把 deleted 删掉，模拟「v12 时期还没有这一列」。
        conn.executescript(
            "CREATE TABLE _m AS SELECT model_id,name,description,credits_micro,"
            "max_input_tokens,source,available,enabled,sort_order,promo,synced_at,backend"
            " FROM models;"
            "DROP TABLE models;"
            "ALTER TABLE _m RENAME TO models;"
        )
        conn.commit()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(models)")]
        assert "deleted" not in cols, "构造失败：deleted 列没被去掉"
        conn.close()

        db = Database(path)
        try:
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
            self.assertIn("deleted",
                          [r[1] for r in db._write.execute("PRAGMA table_info(models)")])
            db.load_models(only_available=True, include_custom=True)  # 不得报 no such column
        finally:
            db.close()

    def test_migration_is_idempotent(self):
        path = self._make_v11()
        Database(path).close()
        again = Database(path)
        try:
            self.assertEqual(
                again.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
        finally:
            again.close()


class SortOrderTests(unittest.TestCase):
    """四层排序（§6）：来源 → 归属 → 启用 → 倍率。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "state.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _registry(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        return registry

    def test_source_is_the_first_key(self):
        """自定义项排在最前，即使它是禁用的且倍率最高。

        这条把「来源」钉在「启用」之前——若有人调换顺序会红。
        """
        registry = self._registry()
        registry.add("my-model", enabled=False, credits=9.9)
        ids = [m["id"] for m in registry.snapshot()["models"]]
        self.assertEqual(ids[0], "my-model", "自定义项必须在最前（来源是第一层）")

    def test_builtin_before_upstream(self):
        ids = [m["id"] for m in self._registry().snapshot()["models"]]
        builtin = {"fast-model", "balanced-model", "deep-model"}
        first_upstream = next((i for i, m in enumerate(ids) if m not in builtin), len(ids))
        for m in builtin:
            self.assertLess(ids.index(m), first_upstream,
                            "三档内置整体排在所有上游模型之前")

    def test_enabled_before_disabled_within_same_source(self):
        registry = self._registry()
        registry.set_enabled("glm-5.2", False)
        models = registry.snapshot()["models"]
        upstream = [m for m in models if m["source"] == "upstream"]
        enabled = [i for i, m in enumerate(upstream) if m["enabled"]]
        disabled = [i for i, m in enumerate(upstream) if not m["enabled"]]
        if enabled and disabled:
            self.assertLess(max(enabled), min(disabled))

    def test_credits_ascending_within_same_group(self):
        models = self._registry().snapshot()["models"]
        upstream = [m for m in models if m["source"] == "upstream" and m["enabled"]]
        credits = [m["credits"] if isinstance(m.get("credits"), (int, float)) else 0
                   for m in upstream]
        self.assertEqual(credits, sorted(credits))

    def test_snapshot_does_not_leak_internal_seq_key(self):
        """内部稳定决胜用的 `_seq` 与分组键 `_group` 都不得出现在 API 输出里。"""
        for m in self._registry().snapshot()["models"]:
            self.assertNotIn("_seq", m)
            self.assertNotIn("_group", m)

    def test_credits_sort_uses_group_minimum(self):
        """第 4 层倍率按**同名组内最小值**排，不是单条自己的值。

        用户 2026-10-16 拍板：「如果有倍率，选择较小的一个进行排」。管理页把同名
        模型跨后端合成一行，一行的排序键必须唯一，用组内最小值才与「这行最便宜
        多少钱」的直觉一致。

        构造：`cheap-elsewhere` 在 workbuddy 上贵（9.9）、在 trae 上便宜（0.01），
        组内最小值是 0.01 —— 它必须排在 `mid-price`（0.5，只有 workbuddy）**之前**。
        若退回「按单条 credits 排」，workbuddy 那条 9.9 会把它甩到最后，测试变红。
        """
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        registry.ensure_static_catalog("trae")
        # 两条同名（bare 名相同）、分属两个后端、价格差很大。
        registry.add("cheap-elsewhere", credits=9.9)
        registry.add("trae/cheap-elsewhere", credits=0.01)
        registry.add("mid-price", credits=0.5)

        models = registry.snapshot()["models"]
        custom = [m for m in models if m["source"] == "custom"]
        keys = [m["id"] for m in custom]
        self.assertIn("cheap-elsewhere", keys)
        self.assertIn("mid-price", keys)
        # 归属（第 2 层）在倍率（第 4 层）**之前**，所以两个后端各成一段；
        # 关键在于：workbuddy 段里 cheap-elsewhere(组内 min 0.01) 必须排在
        # mid-price(0.5) 之前 —— 若按单条 9.9 排，它会跑到 mid-price 后面。
        wb = [m["id"] for m in custom if m["backend"] == "workbuddy"]
        self.assertLess(wb.index("cheap-elsewhere"), wb.index("mid-price"),
                        "组内最小值应参与排序（workbuddy 那条 9.9 也要按 0.01 排）")
        # 倍率取「跨后端的组内最小值」，不是「同一后端内」：trae 的 0.01 决定了整组。
        self.assertIn("trae/cheap-elsewhere", keys)

    def test_group_min_spans_backends_not_just_same_backend(self):
        """组内最小值要**跨后端**取：workbuddy 贵、trae 便宜时以 trae 为准。

        反例对照：若把「组」误当成「同一后端内同名」，最小值就只在 workbuddy 内取，
        得到 9.9，于是仍排在 0.5 后面 —— 这条会红。
        """
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        registry.ensure_static_catalog("trae")
        registry.add("only-cheap-on-trae", credits=9.9)
        registry.add("trae/only-cheap-on-trae", credits=0.01)
        registry.add("pricier", credits=0.5)

        keys = [m["id"] for m in registry.snapshot()["models"] if m["source"] == "custom"]
        self.assertLess(keys.index("only-cheap-on-trae"), keys.index("pricier"))

    def test_snapshot_is_json_serializable(self):
        json.dumps(self._registry().snapshot())


if __name__ == "__main__":
    unittest.main()

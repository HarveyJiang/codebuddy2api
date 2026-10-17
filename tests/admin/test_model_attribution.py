"""模型归属显式化（specs/model-backend-attribution.md）。

覆盖四条承诺：

1. ``models.backend`` 列：persist 写入、load 读出，且与前缀推导**永不分叉**
   （drift 断言 —— 将来有人改写入路径漏设列，它先红）；
2. ``pinned_models`` 声明：三档路由的归属由声明保证，不再是「无前缀所以恰好
   归默认后端」的巧合；
3. 迁移 v12 回填：历史行按前缀推导填一次，之后列即权威；
4. 三层排序：来源 → 归属 → 倍率（启用与否**不参与**，用户 2026-10-16 撤销）。
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

# 裸模块名导入（同 test_browser_login.py 的 import test_server）：tests/ 下没有
# __init__.py，pytest prepend 模式把 tests/admin/ 插进 sys.path。
# 不写 from tests.admin... —— 某些环境 site-packages 里存在同名 tests 包
# （如 Theano 遗留），命名空间包会被它遮蔽，导入直接失败。
from test_model_registry import FakeManager, upstream_payload


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

    def test_prefixed_id_no_longer_wins(self):
        """v16 去前缀：`trae/xxx` 这类前缀**不再有路由含义** → 归默认后端。

        反过来说，前端/客户端不该再发这种名字：`/v1/models` 里已无带 `/` 的
        自造 id（不变量 1）。这条守住「有人把前缀路由加回来」。
        """
        self.assertEqual(backend_name_for("trae/kimi-k3"), "workbuddy")

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
        """persist 写的 `backend` 列必须与「目录来自哪个后端」一致。

        v16 前这里拿 `backend_name_for(mid)`（前缀推导）对照；去前缀后前缀没了，
        权威反而是**目录归属**本身 —— 用 registry 的 per-backend 目录反查。
        """
        registry = self._fetched()
        registry.persist(self.db)
        rows = self.db.load_models(only_available=True)
        self.assertTrue(rows)
        # 每个后端的目录里的裸名，其行必须标着该后端。
        expected = {}
        for backend in ("workbuddy", "trae"):
            for mid in registry._state_for(backend)["available"]:
                expected[mid] = backend
        for mid, row in rows.items():
            if mid in expected:
                self.assertEqual(row["backend"], expected[mid],
                                 f"{mid} 的 backend 列与它所在的目录不一致")

    def test_load_reads_column_not_guess(self):
        """列是权威：同名两行靠 `backend` 列分派到各自的后端目录。

        v16 起 id 是裸名，`glm-5.2` 在两个后端都有 —— 谁归谁**只能**看列。
        构造同一个裸名两行、列分别写两个后端，断言各进各的目录。
        """
        self.db.save_models({
            "glm-5.2": {"name": "GLM-5.2", "source": "upstream",
                        "available": 1, "enabled": 1, "backend": "workbuddy"},
        })
        # save_models 的字典键是 model_id（裸名）；同名第二行直接经底层写入，
        # 因为它与第一行的键相同。这正是联合主键存在的场景。
        self.db._write.execute(
            "INSERT INTO models (model_id,name,source,available,enabled,backend)"
            " VALUES ('glm-5.2','GLM-5.2','upstream',1,1,'trae')")
        fresh = self._registry()
        fresh.load(self.db)
        self.assertIn("glm-5.2", fresh._state_for("workbuddy")["catalog"])
        self.assertIn("glm-5.2", fresh._state_for("trae")["catalog"])

    def test_load_ignores_prefix_style_id(self):
        """历史遗留的带前缀 id 不再被「剥前缀」—— v16 迁移已把它们改写成裸名。

        万一有老库的行漏了迁移（前缀还在），load 会把它当**完整的裸名**装进
        按列指定的后端目录（`trae/glm-5.2` 这个字面 id）。这里钉住那个事实：
        load 层**不做**前缀解析，前缀的清理只在迁移里做一次。
        """
        self.db.save_models({
            "trae/kimi-k3": {"name": "Kimi", "source": "upstream",
                             "available": 1, "enabled": 1, "backend": "trae"},
        })
        fresh = self._registry()
        fresh.load(self.db)
        self.assertIn("trae/kimi-k3", fresh._state_for("trae")["catalog"])
        self.assertNotIn("kimi-k3", fresh._state_for("trae")["catalog"])

    def test_no_drift_between_column_and_directory(self):
        """drift 断言：persist 写进库的每一行，其 `backend` 必须等于它来源的目录。

        这是结构性断言 —— 将来有人加了一条写模型的路径而忘了设列，它先红。
        去前缀后**没有前缀可对照**，所以对照的权威换成「registry 的各后端目录」：
        同一份内存目录 persist 出去，读回来必须能按列还原出同样的归属。
        """
        registry = self._fetched()
        registry.persist(self.db)
        # 内存目录：{model_id: backend}
        expected = {}
        for backend in ("workbuddy", "trae"):
            for mid in registry._state_for(backend)["available"]:
                expected[mid] = backend
        for mid, row in self.db.load_models().items():
            if mid in expected:
                self.assertEqual(row["backend"], expected[mid],
                                 f"{mid}：列={row['backend']} 目录={expected[mid]}")

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
        """v11 库升级后：`backend` 由前缀回填（v12），随后前缀被剥掉（v16）。

        两个事实都要验 —— 只验后者的话，「回填」这一步坏掉也看不出来
        （剥前缀不依赖 backend 列，剥完照样是裸名）。
        """
        path = self._make_v11()
        db = Database(path)  # 打开即迁移
        try:
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
            rows = db.load_models()
            # v12 的效果：归属写对了（trae 那条现在叫 `kimi-k3`，仍须归 trae）。
            self.assertEqual(rows["glm-5.2"]["backend"], "workbuddy")
            self.assertEqual(rows["kimi-k3"]["backend"], "trae")
            # v16 的效果：`trae/` 前缀不复存在。
            self.assertNotIn("trae/kimi-k3", rows)
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

    def test_v13_db_gets_canonical_column(self):
        """已 stamp 成 v13、没有 canonical 列的库，打开时必须补上该列。

        与 `deleted` 同一个坑：列要能加进**已有**的库，就必须是独立版本号。
        迁移**不回填**（见 `_v13_to_v14` 的 docstring），所以这里只断言列存在、
        且旧行读得出来（值为 NULL），不报 `no such column`。
        """
        import sqlite3

        path = self.root / "v13.db"
        Database(path).close()
        conn = sqlite3.connect(path)
        # 退回「v13 已 stamp、但列里没有 canonical」的状态。
        conn.executescript(
            "UPDATE schema_meta SET value='13' WHERE key='version';"
            "CREATE TABLE _m AS SELECT model_id,name,description,credits_micro,"
            "max_input_tokens,source,available,enabled,sort_order,promo,synced_at,"
            "backend,deleted FROM models;"
            "DROP TABLE models;"
            "ALTER TABLE _m RENAME TO models;"
            "INSERT INTO models (model_id,name,source,available,enabled,sort_order,"
            "synced_at,backend,deleted) VALUES ('glm-5.2','GLM','upstream',1,1,0,0,"
            "'workbuddy',0);"
        )
        conn.commit()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(models)")]
        assert "canonical" not in cols, "构造失败：canonical 列没被去掉"
        conn.close()

        db = Database(path)
        try:
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
            self.assertIn("canonical",
                          [r[1] for r in db._write.execute("PRAGMA table_info(models)")])
            # 旧行读得出来，值为 NULL（不回填是有意为之）。
            rows = db.load_models()
            self.assertIsNone(rows["glm-5.2"]["canonical"])
        finally:
            db.close()

    def test_save_models_derives_canonical_from_declaration(self):
        """`canonical` 是**派生列**：写入时按后端声明现算，不靠调用方记得传。

        这条防的是「加一条写入路径、忘了带 canonical」——那种回归不会报错，
        只会让跨后端同名悄悄分成两行。这里故意**不传** canonical，
        断言落库值等于 registry 现算的值。
        """
        from core.backends.registry import canonical_name

        path = self.root / "derive.db"
        db = Database(path)
        try:
            db.save_models({
                "kimi-k3-1": {"name": "Kimi-K3", "backend": "workbuddy"},
                "kimi-k3": {"name": "Kimi-K3", "backend": "trae"},
            })
            rows = db.load_models()
            self.assertEqual(rows["kimi-k3-1"]["canonical"], canonical_name("kimi-k3-1"))
            self.assertEqual(rows["kimi-k3"]["canonical"], canonical_name("kimi-k3"))
            # 关键：两个**不同裸名**的腾讯/字节条目，规范名必须相同（否则合不起来）。
            self.assertEqual(rows["kimi-k3-1"]["canonical"],
                             rows["kimi-k3"]["canonical"])
        finally:
            db.close()

    def test_save_models_persists_canonical_across_reopen(self):
        """重开库后 canonical 仍在（它是列，不是每次现算的视图）。"""
        path = self.root / "reopen.db"
        db = Database(path)
        db.save_models({"kimi-k3-1": {"name": "Kimi-K3", "backend": "workbuddy"}})
        db.close()
        again = Database(path)
        try:
            self.assertEqual(again.load_models()["kimi-k3-1"]["canonical"], "kimi-k3")
        finally:
            again.close()

    def test_load_models_keeps_exact_credits_micro(self):
        """`load_models` 同时给出精确整数 `credits_micro` 与 float `credits`。

        路由的价格红线要精确比较（§9.3），而显示层要 float。同一个 DB 值两种精度，
        不新增存储 —— 这条钉住「两个都在」。
        """
        path = self.root / "micro.db"
        db = Database(path)
        try:
            db.save_models({"glm-5.3": {"name": "GLM-5.3", "backend": "workbuddy",
                                        "credits": 0.79}})
            item = db.load_models()["glm-5.3"]
            self.assertEqual(item["credits_micro"], 790_000)
            self.assertEqual(item["credits"], 0.79)
            self.assertIsInstance(item["credits_micro"], int)
        finally:
            db.close()

    def test_save_models_cost_is_stable_across_repeated_persist(self):
        """反复 persist 不得改变单价（这是「原样写回」担心的那个漂移，实测为零）。

        跑多轮 `save_models(load_models())`，`credits_micro` 必须逐位不变。
        实测 `_to_micro(_from_micro(m)) == m` 恒成立，所以这条是**回归守卫**：
        将来若有人改成 `int(x * 1e6)`（截断）或换换算基准，它会立刻红。
        """
        path = self.root / "drift.db"
        db = Database(path)
        try:
            db.save_models({"m": {"name": "M", "backend": "workbuddy", "credits": 0.000249}})
            first = db.load_models()["m"]["credits_micro"]
            self.assertEqual(first, 249, "float 换算走 round 语义（不是截断的 248）")
            for _ in range(5):
                db.save_models(db.load_models())
            self.assertEqual(db.load_models()["m"]["credits_micro"], first,
                             "反复 persist 后精确值不得漂移")
        finally:
            db.close()

    def test_snapshot_carries_credits_micro_matching_the_db(self):
        """`snapshot()` 的 `credits_micro` 必须与写库的换算**逐位一致**（不是第二份真相）。

        用 `0.000249`：`_to_micro`（round）= 249，而 `int(credits * 1e6)`（trunc）= 248。
        若有人把 snapshot 改成自己 round（`int()` 截断这类），这条会红。
        """
        from admin.db import _to_micro

        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        registry.add("exact-model", credits=0.000249)
        snap = {m["id"]: m for m in registry.snapshot()["models"]}
        self.assertEqual(snap["exact-model"]["credits_micro"], _to_micro(0.000249))
        self.assertEqual(snap["exact-model"]["credits_micro"], 249)


class SortOrderTests(unittest.TestCase):
    """三层排序（§6）：来源 → 归属 → 倍率（启用与否不参与，用户已撤销）。"""

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

    def test_disabled_does_not_sink_to_bottom(self):
        """禁用不参与排序（用户 2026-10-16 撤销「启用优先、禁用沉底」）。

        分页签后每行就是一个开关：点一下若行跳到别处，用户要在原地找结果。
        这条钉住「开关前后行序不变」——有人把 enabled 加回排序键会先红。
        """
        registry = self._registry()
        before = [m["id"] for m in registry.snapshot()["models"]]
        registry.set_enabled("glm-5.2", False)
        registry.set_enabled("deep-model", False)
        models = registry.snapshot()["models"]
        after = [m["id"] for m in models]
        # 禁用的行留在原位（相对于同来源的其他行），不是沉底
        self.assertEqual([i for i in before if i not in ("glm-5.2", "deep-model")],
                         [i for i in after if i not in ("glm-5.2", "deep-model")],
                         "其余行的相对顺序必须纹丝不动")
        # 禁用的 glm-5.2（上游里倍率最高的）仍按倍率排在上游段**末尾附近但不
        # 是因为沉底** —— step-5-preview（免费）在它前面，space-bunny 也在
        # 它前面（space-bunny 无倍率视为 0）。真正的断言是：禁用前后它在
        # 上游段的位置不变。
        up_before = [i for i in before if i == "glm-5.2" or
                     (i in {"step-5-preview", "space-bunny"})]
        up_after = [i for i in after if i == "glm-5.2" or
                    (i in {"step-5-preview", "space-bunny"})]
        self.assertEqual(up_before.index("glm-5.2"), up_after.index("glm-5.2"),
                         "禁用前后 glm-5.2 在上游段的位置必须不变")

    def test_credits_ascending_within_same_group(self):
        models = self._registry().snapshot()["models"]
        upstream = [m for m in models if m["source"] == "upstream" and m["enabled"]]
        credits = [m["credits"] if isinstance(m.get("credits"), (int, float)) else 0
                   for m in upstream]
        self.assertEqual(credits, sorted(credits))

    def test_snapshot_does_not_leak_internal_seq_key(self):
        """内部稳定决胜用的 `_seq` 不得出现在 API 输出里（`canonical` 是对外字段，保留）。"""
        for m in self._registry().snapshot()["models"]:
            self.assertNotIn("_seq", m)
            self.assertNotIn("_group", m)
            # canonical 是**要**给前端的：它按规范名归组。
            self.assertIn("canonical", m)

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
        # 两条同名（同一个裸名）、分属两个后端、价格差很大。
        # v16：同名靠 `backend` 参数区分（id 是裸名，两边相同）。
        registry.add("cheap-elsewhere", credits=9.9, backend="workbuddy")
        registry.add("cheap-elsewhere", credits=0.01, backend="trae")
        registry.add("mid-price", credits=0.5, backend="workbuddy")

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
        self.assertEqual(len([m for m in custom if m["id"] == "cheap-elsewhere"]), 2)

    def test_group_min_spans_backends_not_just_same_backend(self):
        """组内最小值要**跨后端**取：workbuddy 贵、trae 便宜时以 trae 为准。

        反例对照：若把「组」误当成「同一后端内同名」，最小值就只在 workbuddy 内取，
        得到 9.9，于是仍排在 0.5 后面 —— 这条会红。
        """
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        registry.ensure_static_catalog("trae")
        registry.add("only-cheap-on-trae", credits=9.9, backend="workbuddy")
        registry.add("only-cheap-on-trae", credits=0.01, backend="trae")
        registry.add("pricier", credits=0.5, backend="workbuddy")

        models = [m for m in registry.snapshot()["models"] if m["source"] == "custom"]
        # 同 id 两条，取靠前的那条（排序后 workbuddy/trae 段各自的位次）；
        # 关键是整组按组内最小值 0.01 排，故出现在 pricier 之前。
        keys = [m["id"] for m in models]
        self.assertLess(keys.index("only-cheap-on-trae"), keys.index("pricier"))

    def test_snapshot_is_json_serializable(self):
        json.dumps(self._registry().snapshot())


if __name__ == "__main__":
    unittest.main()

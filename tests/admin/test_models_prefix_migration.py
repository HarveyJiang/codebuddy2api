"""v15 → v16 迁移：模型表去前缀 + 联合主键（specs/remove-model-prefix-plan.md）。

这里的每条断言都对应方案里的一个不变量。重点是三个**容易静默出错**的地方：

1. **行数不能少**（用户拍板「迁移保数据」）；
2. **同名模型两边都要在**（联合主键的意义所在，8 组实测同名）；
3. **上游自带 `/` 的模型不能被误剥**（`openrouter/free` 是真实的模型名，
   不是我们的前缀 —— 按 `trae/` 前缀剥会把它改成 `free`）。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from admin.db import Database, SCHEMA_VERSION


def _make_v15_models_db(path):
    """造一个 v15 形状的库：models 是**单主键**、trae 行带 `trae/` 前缀。

    只建迁移需要的那几张表 —— 其它表由 `Database.SCHEMA` 在打开时补齐
    （`CREATE TABLE IF NOT EXISTS`），不影响本迁移的判定。
    """
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '15')")
    con.execute("""CREATE TABLE models (
        model_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
        credits_micro INTEGER, max_input_tokens INTEGER,
        source TEXT NOT NULL DEFAULT 'builtin',
        available INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0, promo TEXT,
        synced_at INTEGER NOT NULL DEFAULT 0,
        backend TEXT NOT NULL DEFAULT 'workbuddy',
        deleted INTEGER NOT NULL DEFAULT 0,
        canonical TEXT)""")
    rows = [
        # 两边同名（迁移后必须都活着）
        ("glm-5.3", "GLM-5.3", "workbuddy", 0),
        ("trae/glm-5.3", "GLM-5.3", "trae", 0),
        # 腾讯上游 id 写偏的那条（裸名，本就没有前缀）
        ("kimi-k3-1", "Kimi-K3", "workbuddy", 0),
        ("trae/kimi-k3", "Kimi-K3", "trae", 0),
        # Trae 独有
        ("trae/Doubao-Seed-Code", "Seed-Code", "trae", 0),
        # **上游自带 `/`** —— 这不是我们的前缀，绝不能剥
        ("trae/openrouter/free", "free", "trae", 0),
        # 已删除的保留行（供日志 join）也要一起迁移
        ("trae/deep", "deep", "trae", 1),
    ]
    con.executemany(
        "INSERT INTO models (model_id, name, backend, deleted) VALUES (?,?,?,?)", rows)
    # 日志/用量表：模型名也要剥前缀
    con.execute("""CREATE TABLE request_logs (
        id INTEGER PRIMARY KEY, time INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'api',
        path TEXT NOT NULL, status INTEGER, outcome TEXT NOT NULL,
        duration_ms INTEGER NOT NULL, key_id TEXT, account_id TEXT, account_name TEXT,
        model_id TEXT, model_name TEXT, upstream_request_id TEXT, backend TEXT)""")
    # time 必须是**近期**时间：`Database.__init__` 在迁移后立刻跑
    # `prune_request_logs`（保留窗口外的行会被删），用 1 会让这些行在
    # 断言前就消失 —— 那是清理逻辑干的，与迁移无关。踩过一次。
    import time as _time
    _now = int(_time.time())
    con.executemany(
        "INSERT INTO request_logs (time, path, outcome, duration_ms, model_id) VALUES (?,'/v1/chat/completions','ok',1,?)",
        [(_now, "trae/glm-5.3"), (_now, "glm-5.3"), (_now, "trae/openrouter/free")])
    con.execute("""CREATE TABLE upstream_request_usage (
        request_id TEXT PRIMARY KEY, account_id TEXT, account_name TEXT,
        credit_micro INTEGER NOT NULL DEFAULT 0, model TEXT, client TEXT,
        request_time INTEGER NOT NULL, agent_purpose TEXT)""")
    con.execute("INSERT INTO upstream_request_usage (request_id, model, request_time)"
                " VALUES ('r1','trae/kimi-k3',?)", (_now,))
    con.commit()
    con.close()


class ModelsPrefixMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _migrated(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        return db

    def test_schema_version_is_at_least_16(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 16)

    def test_prefix_is_stripped_from_models(self):
        _make_v15_models_db(self.path)
        db = self._migrated()
        ids = {r[0] for r in db._write.execute("SELECT model_id FROM models")}
        self.assertNotIn("trae/glm-5.3", ids, "我们造的前缀必须被剥掉")
        self.assertNotIn("trae/kimi-k3", ids)
        self.assertNotIn("trae/Doubao-Seed-Code", ids)
        self.assertIn("glm-5.3", ids)
        self.assertIn("kimi-k3", ids)
        self.assertIn("Doubao-Seed-Code", ids)

    def test_primary_key_becomes_composite(self):
        _make_v15_models_db(self.path)
        db = self._migrated()
        pk = [r[1] for r in db._write.execute("PRAGMA table_info(models)") if r[5]]
        self.assertEqual(pk, ["model_id", "backend"],
                         "联合主键 —— 同一个裸名在两边各有一行")

    def test_same_name_on_both_backends_survives(self):
        """同名模型两边都必须在（这是联合主键存在的理由）。"""
        _make_v15_models_db(self.path)
        db = self._migrated()
        rows = db._write.execute(
            "SELECT backend FROM models WHERE model_id='glm-5.3'").fetchall()
        self.assertEqual({r[0] for r in rows}, {"workbuddy", "trae"})
        rows = db._write.execute(
            "SELECT backend FROM models WHERE model_id='kimi-k3'").fetchall()
        self.assertEqual({r[0] for r in rows}, {"trae"})
        # 腾讯那条裸名本来就是 kimi-k3-1（上游命名如此），不该被"修正"
        self.assertEqual(
            db._write.execute("SELECT backend FROM models WHERE model_id='kimi-k3-1'")
            .fetchone()[0], "workbuddy")

    def test_upstream_slash_is_not_stripped(self):
        """`openrouter/free` 是上游真实的模型名，**不是**我们的前缀。

        按 `trae/` 前缀剥会把它变成 `free` —— 这条测试守住那个误伤。
        """
        _make_v15_models_db(self.path)
        db = self._migrated()
        ids = {r[0] for r in db._write.execute("SELECT model_id FROM models")}
        self.assertIn("openrouter/free", ids)
        self.assertNotIn("free", ids)

    def test_no_rows_are_lost(self):
        _make_v15_models_db(self.path)
        db = self._migrated()
        self.assertEqual(
            db._write.execute("SELECT COUNT(*) FROM models").fetchone()[0], 7)

    def test_deleted_rows_are_migrated_too(self):
        """`deleted=1` 的行也要剥前缀 —— 它们为日志 join 而留。"""
        _make_v15_models_db(self.path)
        db = self._migrated()
        row = db._write.execute(
            "SELECT model_id, backend, deleted FROM models WHERE deleted=1").fetchone()
        self.assertEqual(row, ("deep", "trae", 1))

    def test_log_tables_are_stripped(self):
        _make_v15_models_db(self.path)
        db = self._migrated()
        self.assertIsNone(db._write.execute(
            "SELECT 1 FROM request_logs WHERE model_id LIKE 'trae/%'").fetchone(),
            "request_logs 的模型名也要剥前缀（用户拍板「日志也一起改」）")
        names = {r[0] for r in db._write.execute("SELECT model_id FROM request_logs")}
        # 三种输入各自的期望结果：
        self.assertIn("glm-5.3", names, "trae/glm-5.3 → glm-5.3")
        self.assertIn("openrouter/free", names,
                      "trae/openrouter/free → openrouter/free（剥 trae/，保留上游自带的 /）")
        self.assertEqual(
            db._write.execute(
                "SELECT model FROM upstream_request_usage WHERE request_id='r1'")
            .fetchone()[0], "kimi-k3")

    def test_log_row_without_our_prefix_is_untouched(self):
        """日志里本来就是裸名的行**保持原样**（不因迁移而变化）。

        判据是 `LIKE '<prefix>%'`：没有前缀的行原样通过，且**不能**被
        「按 `/` 剥」这种更宽的规则误伤（`openrouter/free` 就是会被那种规则伤到的）。
        这条测试用「迁移后该名字的行数」来验证：原本 1 条裸名 + 1 条带前缀
        （`trae/glm-5.3` → `glm-5.3`）= 2 条，说明原来那条裸名没被改坏或删掉。
        """
        _make_v15_models_db(self.path)
        db = self._migrated()
        rows = db._write.execute(
            "SELECT COUNT(*) FROM request_logs WHERE model_id='glm-5.3'").fetchone()[0]
        self.assertEqual(rows, 2, "1 条原本的裸名 + 1 条由 trae/glm-5.3 迁移而来")

    def test_second_open_is_idempotent(self):
        """版本已是 16 后再打开，不重复剥（不变量：幂等）。"""
        _make_v15_models_db(self.path)
        db = self._migrated()
        ids_first = sorted(r[0] for r in db._write.execute("SELECT model_id FROM models"))
        db.close()
        db2 = Database(self.path)
        self.addCleanup(db2.close)
        ids_second = sorted(r[0] for r in db2._write.execute("SELECT model_id FROM models"))
        self.assertEqual(ids_first, ids_second)


class FreshDatabaseSchemaTests(unittest.TestCase):
    """新库直接建对（不走迁移）—— 否则新装用户的表结构与老库不一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "fresh.db")
        self.addCleanup(self.db.close)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_db_has_composite_primary_key(self):
        pk = [r[1] for r in self.db._write.execute("PRAGMA table_info(models)") if r[5]]
        self.assertEqual(pk, ["model_id", "backend"])

    def test_fresh_db_accepts_same_name_on_two_backends(self):
        """同一个裸名在两个后端各存一行（联合主键的意义）。

        注意 `save_models` 的**字典键**才是 `model_id`（见其实现与
        `ModelRegistry.persist` 的构造方式），item 里的 `model_id` 不参与。
        """
        self.db.save_models({
            "glm-5.3": {"name": "GLM-5.3", "backend": "workbuddy"},
        })
        # 第二个后端同名：字典键相同会互相覆盖，所以调用方（persist）必须
        # 用「同一键、不同 backend」的方式写入 —— 这正说明**键不能只是裸名**。
        # 这里直接验证底层 SQL 允许同名并存。
        self.db._write.execute(
            "INSERT INTO models (model_id, name, backend) VALUES ('glm-5.3','GLM-5.3','trae')")
        rows = self.db._write.execute(
            "SELECT backend FROM models WHERE model_id='glm-5.3'").fetchall()
        self.assertEqual({r[0] for r in rows}, {"workbuddy", "trae"})

    def test_save_models_upserts_on_composite_key(self):
        """重复保存同一 (model_id, backend) 是更新，不是新增。"""
        self.db.save_models({"glm-5.3": {"name": "旧", "backend": "trae"}})
        self.db.save_models({"glm-5.3": {"name": "新", "backend": "trae"}})
        rows = self.db._write.execute(
            "SELECT name FROM models WHERE model_id='glm-5.3'").fetchall()
        self.assertEqual(rows, [("新",)])

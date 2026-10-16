"""裸名路由的**设置入口**（`settings.model_backend_order` / `bare_name_routing`）。

对应 `specs/model-routing-by-policy.md` §9.4（调序入口）与 §8 阶段三（kill switch）。

## 为什么单独测这一层

`core/bare_routing.py` 的规则单测全绿时，用户**仍然改不了顺序** —— 只要
`PATCH /admin/api/pool/settings` 不认识 `model_backend_order` 这个键，它就静默
不生效（返回 200 却什么都没存）。这正是实测中发生的事：设置接口只认白名单里的
键，而白名单里没有新增的两项。行为测试（发请求看落点）也抓不住，因为顺序压根没
被改过 —— 表现是「调了序但没变化」，与「调整生效但恰好还是原后端」无法区分。

所以这里钉三件事：① 提交能存进去并**重启后仍在**；② 形状非法要 400 而不是静默；
③ 内存里是列表/布尔（不是 JSON 串/整数），否则 `build_order` 会按字符遍历。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from admin.db import SCHEMA_VERSION, Database
from admin.server import _decode_backend_order, _dumps_backend_order, _validate_backend_order


class BackendOrderCodecTests(unittest.TestCase):
    """落库形态 ↔ 内存形态的编解码（`_decode_backend_order` / `_dumps_backend_order`）。"""

    def test_dumps_none_when_unconfigured(self):
        """None / 空 → 落库 NULL（不是 '[]'，也不是 'null'）。"""
        self.assertIsNone(_dumps_backend_order(None))
        self.assertIsNone(_dumps_backend_order([]))

    def test_roundtrip_preserves_order(self):
        order = ["trae", "workbuddy"]
        self.assertEqual(_decode_backend_order(_dumps_backend_order(order)), order)

    def test_decode_accepts_none_and_blank(self):
        for raw in (None, "", "   "):
            self.assertIsNone(_decode_backend_order(raw))

    def test_decode_handles_garbage_without_raising(self):
        """脏值当「未配置」：顺序读坏了回落到注册表顺序是最安全的降级。"""
        for raw in ("not json", "{}", '"a string"', "123"):
            self.assertIsNone(_decode_backend_order(raw), raw)

    def test_decode_dedupes_and_keeps_order(self):
        self.assertEqual(_decode_backend_order('["trae","trae","workbuddy"]'),
                         ["trae", "workbuddy"])

    def test_decode_drops_non_string_items(self):
        self.assertEqual(_decode_backend_order('["trae", 5, null, "workbuddy"]'),
                         ["trae", "workbuddy"])


class BackendOrderValidationTests(unittest.TestCase):
    """提交校验（未知后端名要 400，不能静默忽略）。"""

    def test_none_and_empty_mean_unconfigured(self):
        self.assertIsNone(_validate_backend_order(None))
        self.assertIsNone(_validate_backend_order([]))

    def test_unknown_backend_is_rejected(self):
        """未知后端名报错 —— 静默忽略会让用户以为调了一个不存在的后端。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _validate_backend_order(["ghost"])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_list_is_rejected(self):
        from fastapi import HTTPException
        for bad in ("trae", 5, {"trae": 1}):
            with self.assertRaises(HTTPException):
                _validate_backend_order(bad)

    def test_valid_names_pass_and_dedupe(self):
        self.assertEqual(_validate_backend_order(["trae", "trae"]), ["trae"])

    def test_partial_config_is_allowed(self):
        """只配一个后端是合法的：其余由 `build_order` 追加（不在这里补全）。"""
        self.assertEqual(_validate_backend_order(["trae"]), ["trae"])


class SettingsPersistenceTests(unittest.TestCase):
    """v14 → v15 迁移，以及两项设置**重启后仍在**。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_version_is_at_least_15(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 15)

    def test_migration_adds_columns_to_a_v14_database(self):
        """v14 的库（没有这两列）升级后必须有列，且默认值正确。"""
        import sqlite3
        path = self.root / "v14.db"
        # 造一个 v14 的库：schema_meta 记版本（迁移靠它判断当前版本，
        # **不是** PRAGMA user_version —— 实测 user_version 恒为 0）。
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '14')")
        con.execute("""CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            auto_checkin INTEGER NOT NULL DEFAULT 1)""")
        con.execute("INSERT INTO settings (id, auto_checkin) VALUES (1, 0)")
        con.commit()
        con.close()

        db = Database(path)
        try:
            cols = {r[1] for r in db._write.execute("PRAGMA table_info(settings)")}
            self.assertIn("model_backend_order", cols)
            self.assertIn("bare_name_routing", cols)
            row = db._write.execute(
                "SELECT model_backend_order, bare_name_routing FROM settings WHERE id=1"
            ).fetchone()
            self.assertIsNone(row[0], "未配置 = NULL")
            self.assertEqual(row[1], 1, "kill switch 默认**开**（§8 原文）")
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
        finally:
            db.close()

    def test_kill_switch_column_defaults_to_on(self):
        """新库的 `bare_name_routing` 列默认 1（开）—— 用户拍板的默认值。

        注意读的是**列默认值**而不是某一行：`settings` 是「一行宽表」，行由首次
        `save_settings` 建立，新库里 `get_settings()` 本来就是 None（没有行）。
        默认值写在 DDL 里，所以这里直接验证建表结果。
        """
        db = Database(self.root / "fresh.db")
        try:
            row = db._write.execute(
                "SELECT dflt_value FROM pragma_table_info('settings') "
                "WHERE name='bare_name_routing'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(str(row[0]), "1")
        finally:
            db.close()

    def test_order_survives_reopen(self):
        """写进去的调序重启后仍在（落库的是 JSON 文本，读回来是列表）。"""
        path = self.root / "order.db"
        db = Database(path)
        try:
            # `save_settings` 是「一行宽表」的 INSERT OR REPLACE，必须给全列。
            values = {col: None for col in db.SETTINGS_COLUMNS}
            values["model_backend_order"] = _dumps_backend_order(["trae", "workbuddy"])
            db.save_settings(values)
        finally:
            db.close()
        again = Database(path)
        try:
            raw = again.get_settings()["model_backend_order"]
            self.assertEqual(raw, '["trae", "workbuddy"]',
                             "落库是 JSON 文本")
            self.assertEqual(_decode_backend_order(raw), ["trae", "workbuddy"],
                             "读回来是列表")
        finally:
            again.close()


if __name__ == "__main__":
    unittest.main()

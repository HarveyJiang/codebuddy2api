"""密钥「最后使用」时间（specs/keys-last-used-at.md）。

## 这个功能想守住什么

密钥列表能回答两个问题：**这个 key 还有客户端在连吗**（可删了没），
**我刚配的 key 为什么一直不工作**（模型白名单没包含目标模型 / 账号全被冷却 /
后端被禁用 —— 这些请求全部失败，但 key 本身在被高频调用）。

## 为什么测试这么写

上一版这个功能做过一次，做完就 revert 了，原因是 **`_load` 从没恢复
`last_used_at`**：内存里没这个键，下一次 `save_keys` 把缺失值（None）写回 DB，
于是每次重启都永久抹掉一次。功能只在单次进程生命周期内有效，而**零测试**
所以没有任何东西能发现它。

因此这里最重的一条测试是 `test_survives_save_and_reload`——它钉的正是那个 bug：
不经过任何业务路径、纯粹「写入 → 落库 → 重开 Store」，值必须还在。别的测试
再全绿，也只要有一条走完整落库往返就能抓住它。

其余测试钉住 §2 语义表里那些反直觉的分支：403/503/400 算「使用」，
429 不算。前者是这个功能存在的理由，后者是它唯一的例外。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from admin.db import Database, SCHEMA_VERSION
from admin.server import Store, create_app

ADMIN = "admin-key-long-enough-12345"
API = "client-key"


def _fresh_store():
    """造一个带密钥的空 Store，返回 (store, kid, 密钥明文)。

    必须用 add_key 返回的**明文**去调 check_api，不能用构造函数的 initial_key：
    后者落在 ``test_keys`` 分支里，那个分支不做可用性检查也不看配额，直接
    ``return None`` —— 429 / 撤销 / 限额这些分支根本走不到，测试会假绿。
    """
    root = Path(tempfile.mkdtemp())
    auth = root / "auth"
    auth.mkdir()
    store = Store(str(root / "mgmt"), str(auth), API, ADMIN)
    kid, value = store.add_key("测试密钥")
    return store, kid, value


class LastUsedAtPersistenceTests(unittest.TestCase):
    """落库往返 —— 专门钉上一版的那个 bug。"""

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass

    def test_survives_save_and_reload(self):
        """写入 → save → 重开 Store，值必须还在。

        这条是整个文件里最重要的一条。上一版没恢复 `_load`，于是重启后
        `save_keys` 把缺失值写回 DB，功能只在单次进程内有效。
        """
        store, kid, _value = _fresh_store()
        self.store = store
        store.data["keys"][kid]["last_used_at"] = 1700000000
        store.save()

        again = Store(str(store.root), str(store.auth_dir), API, ADMIN)
        try:
            self.assertEqual(again.data["keys"][kid]["last_used_at"], 1700000000,
                             "重启后 last_used_at 丢失 —— _load 没恢复它，"
                             "且下一次 save_keys 会把 None 写回 DB 永久抹掉")
        finally:
            again.db.close()

    def test_new_key_starts_as_none(self):
        """新建密钥显式初始化为 None，而不是靠 .get() 兜底。

        靠 .get() 也能跑通，但那样「从未使用」和「键缺失」两种状态就分不清了，
        将来任何 `if "last_used_at" in item` 的判断都会给出错误答案。
        """
        store, kid, _value = _fresh_store()
        self.store = store
        self.assertIn("last_used_at", store.data["keys"][kid])
        self.assertIsNone(store.data["keys"][kid]["last_used_at"])

    def test_never_used_survives_round_trip_as_null(self):
        """从未使用的密钥往返后仍是 None，不能被写成 0。

        0 会被前端当成年份 1970 显示，且 `last_used_at ? ... : 从未使用`
        这种真值判断里 0 与 None 同义，但一旦将来做「早于某时间」的比较，
        0 会被误判成「1970 年用过」。
        """
        store, kid, _value = _fresh_store()
        self.store = store
        store.save()
        again = Store(str(store.root), str(store.auth_dir), API, ADMIN)
        try:
            self.assertIsNone(again.data["keys"][kid]["last_used_at"])
        finally:
            again.db.close()


class LastUsedAtSchemaTests(unittest.TestCase):
    """v10 → v11 迁移。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "mgmt").mkdir()

    def test_migration_adds_column_without_touching_data(self):
        """旧库升级后列存在、旧行数据不丢。"""
        import sqlite3
        # 用真实 DDL 造一个「没有该列」的 keys 表，避免手搓不完整的表。
        probe = Database(self.root / "tmp.db")
        ddl = probe._write.execute(
            "SELECT sql FROM sqlite_master WHERE name='keys'").fetchone()[0]
        probe.close()
        ddl_v10 = ddl.replace(",\n        last_used_at INTEGER", "")
        self.assertNotIn("last_used_at", ddl_v10)

        c = sqlite3.connect(self.root / "state.db")
        c.executescript("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);")
        c.execute("INSERT INTO schema_meta VALUES ('version','10')")
        c.execute(ddl_v10)
        c.execute("INSERT INTO keys (id,name,hash,hint,created,enabled,usage_count)"
                  " VALUES ('k1','旧密钥','h','ab..cd',111,1,42)")
        c.commit()
        c.close()

        db = Database(self.root / "state.db")
        try:
            self.assertEqual(db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                             str(SCHEMA_VERSION))
            row = db.query("SELECT * FROM keys")[0]
            keys = [r[1] for r in db._write.execute("PRAGMA table_info(keys)")]
            self.assertIn("last_used_at", keys)
            idx = keys.index("last_used_at")
            self.assertIsNone(row[idx], "旧行该列为 NULL（可空、无回填）")
            self.assertEqual(row[keys.index("name")], "旧密钥")
            self.assertEqual(row[keys.index("usage_count")], 42)
        finally:
            db.close()

    def test_fresh_database_has_the_column(self):
        """新库走 CREATE TABLE，不走 ALTER —— 漏改 DDL 会 `no such column`。

        这是 v10（backend_enabled）踩过的同一个坑：只改 SETTINGS_COLUMNS
        没改 CREATE TABLE，新库 save_settings 直接炸。
        """
        db = Database(self.root / "fresh.db")
        try:
            keys = [r[1] for r in db._write.execute("PRAGMA table_info(keys)")]
            self.assertIn("last_used_at", keys)
        finally:
            db.close()

    def test_migration_is_idempotent(self):
        """重复打开不报错、不重复加列。"""
        db = Database(self.root / "state.db")
        db.close()
        again = Database(self.root / "state.db")
        try:
            self.assertEqual(again.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                             str(SCHEMA_VERSION))
        finally:
            again.close()


class LastUsedAtSemanticsTests(unittest.TestCase):
    """§2 语义表：鉴权成功即记，429 与 401 不记。"""

    def setUp(self):
        self.store, self.kid, self.value = _fresh_store()

    def tearDown(self):
        self.store.db.close()

    def test_successful_auth_records_the_call(self):
        """鉴权通过即更新 —— 不需要真的打到上游。"""
        self.assertIsNone(self.store.data["keys"][self.kid]["last_used_at"])
        before = int(time.time())
        got = self.store.check_api("Bearer " + self.value, "")
        after = int(time.time())
        self.assertEqual(got, self.kid)
        recorded = self.store.data["keys"][self.kid]["last_used_at"]
        self.assertIsNotNone(recorded, "鉴权成功就该记")
        self.assertTrue(before <= recorded <= after, "记录值应是当前秒级时间戳")

    def test_auth_marks_keys_dirty_so_it_reaches_the_database(self):
        """必须置 keys_dirty，否则只在内存里，进程退出就没了。

        走的是 record_key_usage 已有的那条 5 秒防抖 flush，所以不新增落库次数。
        """
        self.store.keys_dirty = False
        self.store.check_api("Bearer " + self.value, "")
        self.assertTrue(self.store.keys_dirty,
                        "check_api 改了 last_used_at 却没置脏标记 —— "
                        "防抖 flush 只看脏标记，漏了等于永不落库")

    def test_over_daily_limit_does_not_record(self):
        """429 超配额**不**算「使用」——请求没真正用掉服务。"""
        self.store.data["keys"][self.kid]["daily_limit"] = 1
        self.store.data["keys"][self.kid]["usage"] = {"date": self.store._today(), "count": 1}
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.store.check_api("Bearer " + self.value, "")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIsNone(self.store.data["keys"][self.kid]["last_used_at"],
                          "超配额被拒不该被记成「用过」")

    def test_invalid_key_does_not_record_anything(self):
        """错误 key → 401，且不碰任何密钥的行。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.store.check_api("Bearer wrong-key", "")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIsNone(self.store.data["keys"][self.kid]["last_used_at"])

    def test_revoked_key_does_not_record(self):
        """已撤销的密钥不通过鉴权 → 不记录。"""
        from fastapi import HTTPException
        self.store.data["keys"][self.kid]["revoked_at"] = int(time.time())
        with self.assertRaises(HTTPException) as ctx:
            self.store.check_api("Bearer " + self.value, "")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIsNone(self.store.data["keys"][self.kid]["last_used_at"])


class LastUsedAtApiTests(unittest.TestCase):
    """走真实 HTTP：概览接口要把 last_used_at 透传给前端。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.app = create_app(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        self.client = TestClient(self.app, base_url="https://console.test")
        csrf = self.client.post("/admin/api/login", json={"key": ADMIN}).json()["csrf"]
        self.headers = {"X-CSRF-Token": csrf}

    def tearDown(self):
        self.client.close()

    def _keys(self):
        return {k["id"]: k for k in self.client.get("/admin/api/overview").json()["keys"]}

    def test_overview_exposes_the_field(self):
        """概览接口透传 last_used_at（前端要靠它渲染时间列）。"""
        kid = self._keys().popitem()[0] if self._keys() else None
        before = self.client.post("/v1/chat/completions",
                                  json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                                  headers={"Authorization": "Bearer " + API})
        # 没有可用账号时请求会失败，但鉴权已通过 → 语义表里 503 也算「使用」。
        self.assertIn(before.status_code, (400, 403, 429, 503))
        after = self._keys()
        self.assertTrue(after, "应有密钥返回")
        kid = kid or next(iter(after))
        self.assertIn("last_used_at", after[kid])
        self.assertIsNotNone(after[kid]["last_used_at"],
                             "503 也算「使用」——这正是这个功能能发现坏配置的原因")


if __name__ == "__main__":
    unittest.main()
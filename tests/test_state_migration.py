"""生产升级：旧版 state.json → SQLite 一次性迁移。

对应 admin/server.py 的 Store._migrate_state_json。生产现网长期以 state.json 为持久层，
升级到 SQLite 后首次启动必须把账号 / API Key / 池设置 / 会话绑定全部搬过来——
否则用户手里的客户端 Key 会全部失效（只收养 .info 目录会丢掉 keys）。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from admin.server import Store, digest

ADMIN = "admin-test-credential-long-enough"
API = "sk-wb-legacy-client-key-value"


def credential(uid="legacy", name="主账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "e1"},
            "auth": {"accessToken": "at", "refreshToken": "rt",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


class StateJsonMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data = root / "management"
        self.auth = root / "auth"
        self.data.mkdir()
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.legacy = self.data / "state.json"
        self.legacy.write_text(json.dumps({
            "active": "aid1",
            "accounts": {"aid1": {"file": "session.info", "name": "主账号",
                                  "created": 1700000000, "enabled": True}},
            "account_status": {"aid1": {"weight": 7, "remaining": 123.5}},
            "keys": {"kid1": {"name": "客户端A", "hash": digest(API),
                              "hint": API[:5] + "…" + API[-4:], "created": 1700000001,
                              "daily_limit": 50,
                              "usage": {"date": "2020-01-01", "count": 3, "tokens": 100}}},
            # 新列搬过来；routing / sync_window 是旧字段，应被忽略回落默认。
            "pool": {"auto_checkin": False, "checkin_time": "08:30",
                     "credit_min_interval": 120, "batch_spread": 30,
                     "routing": "manual", "sync_window": ["10:00", "20:00"]},
            "session_bindings": {"sess1": {"account_id": "aid1"}},
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_state_json_is_migrated_into_sqlite(self):
        store = Store(self.data, self.auth, None, ADMIN)

        # 账号（backend 默认 workbuddy）
        self.assertIn("aid1", store.data["accounts"])
        self.assertEqual(store.data["accounts"]["aid1"]["name"], "主账号")
        self.assertEqual(store.data["accounts"]["aid1"]["backend"], "workbuddy")
        # account_status 一并搬过来
        self.assertEqual(store.data["account_status"]["aid1"]["weight"], 7)
        self.assertEqual(store.data["account_status"]["aid1"]["remaining"], 123.5)
        # 密钥：hash 保留 → 客户端 Key 仍然可用（不丢）
        self.assertIn("kid1", store.data["keys"])
        self.assertEqual(store.data["keys"]["kid1"]["daily_limit"], 50)
        self.assertEqual(store.check_api("Bearer " + API, ""), "kid1")
        # 池设置：新列搬过来，旧列忽略回落默认
        self.assertEqual(store.data["pool"]["checkin_time"], "08:30")
        self.assertEqual(store.data["pool"]["credit_min_interval"], 120)
        self.assertEqual(store.data["pool"]["batch_spread"], 30)
        self.assertFalse(store.data["pool"]["auto_checkin"])
        self.assertEqual(store.data["pool"]["default_routing"], "round_robin")  # 旧 routing 被忽略
        # 会话绑定
        self.assertEqual(store.data["session_bindings"]["sess1"]["account_id"], "aid1")
        # 原文件改名留档、数据落库
        self.assertFalse(self.legacy.exists())
        self.assertTrue((self.data / "state.json.migrated").exists())
        self.assertTrue((self.data / "state.db").exists())

    def test_migration_skipped_when_db_already_populated(self):
        Store(self.data, self.auth, None, ADMIN)  # 首次：执行迁移
        # 再造一个 legacy state.json——此时 DB 非空，不应重复导入
        self.legacy.write_text(json.dumps({
            "accounts": {"ghost": {"file": "session.info", "name": "幽灵", "enabled": True}},
            "keys": {"ghost": {"hash": digest("x"), "name": "g"}},
        }), encoding="utf-8")
        store2 = Store(self.data, self.auth, None, ADMIN)
        self.assertNotIn("ghost", store2.data["accounts"])
        self.assertTrue(self.legacy.exists())  # 未被消费

    def test_keys_and_pool_survive_restart_from_db(self):
        Store(self.data, self.auth, None, ADMIN)
        # 模拟重启：新 Store 从 SQLite 水合（不再依赖 state.json）
        store2 = Store(self.data, self.auth, None, ADMIN)
        self.assertEqual(store2.check_api("Bearer " + API, ""), "kid1")
        self.assertEqual(store2.data["pool"]["checkin_time"], "08:30")
        self.assertEqual(store2.data["accounts"]["aid1"]["name"], "主账号")


if __name__ == "__main__":
    unittest.main()

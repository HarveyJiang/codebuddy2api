"""SQLite 持久层：迁移、恢复、并发与脏标记。

按 db-redesign spec：无 kv 表，settings/session_bindings/models 走具名表，
金额以整数最小单位（*_micro）落库，内存保持 float。
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import hashlib
from contextlib import contextmanager
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]

from admin.db import SCHEMA_VERSION, Database, MICRO
from admin.server import Store
from test_admin_server import credential

def _day_offset(days):
    """相对今天偏移 N 天的北京时间日期字符串。"""
    from datetime import datetime, timedelta, timezone
    cn = timezone(timedelta(hours=8))
    return (datetime.now(cn) + timedelta(days=days)).strftime("%Y-%m-%d")


ADMIN = "admin-test-credential-long-enough"
# request_logs 启动时会裁掉 93 天前的行，测试数据必须用「现在」的时间戳。
NOW = int(time.time())


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "management"
        self.auth = Path(self.tmp.name) / "auth"
        self.root.mkdir(parents=True)
        self.auth.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_models(self, db):
        db.save_models({
            "hy3": {"name": "Hy3", "description": "混元", "credits": 0.05, "available": 1, "enabled": 1, "sort_order": 2},
            "gone": {"name": "Gone", "credits": 0.5, "available": 0, "enabled": 1, "sort_order": 9},
        })

    def test_no_kv_table_and_version_written(self):
        db = Database(self.root / "t.db")
        self.assertEqual(db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0], str(SCHEMA_VERSION))
        self.assertFalse(db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"))
        db.close()

    def test_settings_roundtrip(self):
        db = Database(self.root / "t.db")
        db.save_settings({"auto_checkin": 0, "checkin_time": "08:30", "affinity_expiry_days": 5,
                          "credit_min_interval": 45, "sync_start": "11:00", "sync_end": "21:00",
                          "batch_spread": 60, "default_routing": "expiry_first", "models_updated": 9})
        s = db.get_settings()
        self.assertEqual(s["auto_checkin"], 0)
        self.assertEqual(s["checkin_time"], "08:30")
        self.assertEqual(s["sync_start"], "11:00")
        self.assertEqual(s["default_routing"], "expiry_first")
        self.assertEqual(s["models_updated"], 9)
        db.close()
        reopened = Database(self.root / "t.db")
        self.assertEqual(reopened.get_settings()["checkin_time"], "08:30")
        reopened.close()

    def test_accounts_money_micro_roundtrip(self):
        db = Database(self.root / "t.db")
        db.save_accounts({"a1": {"name": "甲", "file": "a.info", "enabled": True, "created": 100}},
                         {"a1": {"weight": 30, "remaining": 154.61, "packages": 4,
                                 "earliest_expiry": 1790000000, "earliest_expiry_remaining": 4.61,
                                 "expiring_soon": 104.61, "consecutive_fails": 2, "last_error": "boom"}})
        row = db.load_accounts()["a1"]
        self.assertAlmostEqual(row["remaining"], 154.61, places=4)
        self.assertAlmostEqual(row["earliest_expiry_remaining"], 4.61, places=4)
        self.assertAlmostEqual(row["expiring_soon"], 104.61, places=4)
        self.assertEqual(row["weight"], 30)
        self.assertEqual(row["consecutive_fails"], 2)
        self.assertEqual(row["last_error"], "boom")
        # DB 里确实是整数微单位。
        micro = db.query("SELECT remaining_micro FROM accounts WHERE id='a1'")[0][0]
        self.assertIsInstance(micro, int)
        self.assertEqual(micro, int(round(154.61 * MICRO)))
        db.close()

    def test_keys_usage_columns_roundtrip(self):
        db = Database(self.root / "t.db")
        db.save_keys({"k1": {"name": "K", "hash": "h" * 64, "hint": "s", "created": 1,
                             "enabled": 1, "revoked_at": None, "expires_at": None, "daily_limit": 10,
                             "usage": {"date": "2026-10-14", "count": 3, "tokens": 99},
                             "models": ["hy3"], "accounts": [], "routing": "random"}})
        k = db.load_keys()["k1"]
        self.assertEqual(k["usage_date"], "2026-10-14")
        self.assertEqual(k["usage_count"], 3)
        self.assertEqual(k["usage_tokens"], 99)
        self.assertEqual(k["models"], ["hy3"])
        self.assertEqual(k["accounts"], [])
        self.assertEqual(k["routing"], "random")
        self.assertEqual(k["daily_limit"], 10)
        db.close()

    def test_key_usage_daily_upsert_and_series(self):
        """当日累计快照：同天覆盖（取更大值），换天新增行。"""
        db = Database(self.root / "t.db")
        keys = {"k1": {"name": "K1", "usage": {"date": "2026-10-14", "count": 3, "tokens": 100}}}
        db.save_key_usage_daily(keys)
        # 同天再写更大的累计值：覆盖，不是累加。
        keys["k1"]["usage"] = {"date": "2026-10-14", "count": 7, "tokens": 250}
        db.save_key_usage_daily(keys)
        rows = db.query("SELECT * FROM key_usage_daily")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 7)
        self.assertEqual(rows[0]["tokens"], 250)
        # 换天 = 新行。
        keys["k1"]["usage"] = {"date": "2026-10-15", "count": 2, "tokens": 50}
        db.save_key_usage_daily(keys)
        self.assertEqual(len(db.query("SELECT * FROM key_usage_daily")), 2)
        # usage_date 为空的 key 不写（避免每天造空行）。
        db.save_key_usage_daily({"k2": {"name": "K2", "usage": {}}})
        self.assertEqual(len(db.query("SELECT * FROM key_usage_daily")), 2)
        # 序列与合计。
        series = db.key_usage_series(days=30)
        self.assertEqual(series["total"]["count"], 9)
        self.assertEqual(series["total"]["tokens"], 300)
        self.assertEqual(len(series["keys"][0]["series"]), 2)
        db.close()

    def test_key_usage_daily_never_goes_backwards(self):
        """重启后内存清零、计数从 0 重新累加，写回的小值不能把当天历史改小。

        这是覆盖语义最容易踩的坑：直接 DO UPDATE SET count=excluded.count
        会让 5000 被回灌的 1 覆盖掉，历史永久丢失。
        """
        db = Database(self.root / "t.db")
        db.save_key_usage_daily({"k1": {"name": "K", "usage": {"date": "2026-10-15", "count": 5000, "tokens": 900}}})
        # 模拟重启：内存 usage 清零后第一次 flush 只写回 1。
        db.save_key_usage_daily({"k1": {"name": "K", "usage": {"date": "2026-10-15", "count": 1, "tokens": 0}}})
        row = db.query("SELECT count, tokens FROM key_usage_daily")[0]
        self.assertEqual(row["count"], 5000)
        self.assertEqual(row["tokens"], 900)
        # 正常递增仍然生效，且名称快照跟随最新。
        db.save_key_usage_daily({"k1": {"name": "K2", "usage": {"date": "2026-10-15", "count": 6000, "tokens": 1000}}})
        row = db.query("SELECT count, tokens, key_name FROM key_usage_daily")[0]
        self.assertEqual((row["count"], row["tokens"], row["key_name"]), (6000, 1000, "K2"))
        db.close()

    def test_usage_report_group_by_account_uses_id(self):
        """按账号分组必须用 account_id：account_name 是可变快照，改名会拆组，
        且 name 与 id 混在同一个命名空间会把不同账号并进同一组。"""
        db = Database(self.root / "t.db")
        db.save_upstream_usage([
            {"request_id": "r1", "account_id": "a1", "account_name": None,
             "credit": 0.0, "model": "hy3", "client": "WorkBuddy",
             "request_time": NOW - 10, "agent_purpose": ""},
            # 名字恰好等于另一个账号的 id：用 name 分组会把 a2 并进 a1。
            {"request_id": "r2", "account_id": "a2", "account_name": "a1",
             "credit": 0.0, "model": "hy3", "client": "WorkBuddy",
             "request_time": NOW - 20, "agent_purpose": ""},
            # 同一账号改名：用 name 分组会拆成两组。
            {"request_id": "r3", "account_id": "a1", "account_name": "新名字",
             "credit": 0.0, "model": "hy3", "client": "WorkBuddy",
             "request_time": NOW - 30, "agent_purpose": ""},
        ])
        report = db.upstream_usage_report(group_by="account")
        buckets = {g["bucket"]: g["count"] for g in report["groups"]}
        self.assertEqual(buckets.get("a1"), 2)   # 改名前后的两条合并
        self.assertEqual(buckets.get("a2"), 1)   # 不被 name 串到 a1
        self.assertEqual(report["total"]["count"], 3)
        # 桶总数与截断标记。
        self.assertEqual(report["bucket_total"], 2)
        self.assertFalse(report["truncated"])
        db.close()

    def test_key_usage_daily_pruned_on_boot(self):
        db = Database(self.root / "t.db")
        old_day = _day_offset(-100)
        db.save_key_usage_daily({"k1": {"name": "K", "usage": {"date": old_day, "count": 1}}})
        self.assertEqual(len(db.query("SELECT * FROM key_usage_daily")), 1)
        db.close()
        # 重开触发裁剪。
        db = Database(self.root / "t.db")
        self.assertEqual(len(db.query("SELECT * FROM key_usage_daily")), 0)
        db.close()

    def test_upstream_usage_paging_and_report(self):
        db = Database(self.root / "t.db")
        db.save_upstream_usage([
            {"request_id": f"r{i}", "account_id": "a1", "account_name": "账号一",
             "credit": 0.0 if i % 2 else 0.5, "model": "hy3" if i % 3 else "step-5",
             "client": "WorkBuddy", "request_time": NOW - i * 60, "agent_purpose": ""}
            for i in range(25)])
        page = db.page_upstream_usage(limit=10, offset=0)
        self.assertEqual(len(page["rows"]), 10)
        self.assertEqual(page["total"], 25)
        self.assertEqual(page["limit"], 10)
        # 倒序：第一页第一条是最新（request_time 最大）。
        self.assertEqual(page["rows"][0]["request_id"], "r0")
        # credit 读回是 float（_from_micro 还原）。
        self.assertIsInstance(page["rows"][1]["credit"], float)
        second = db.page_upstream_usage(limit=10, offset=10)
        self.assertEqual(second["rows"][0]["request_id"], "r10")
        # 时间过滤。
        filtered = db.page_upstream_usage(since=NOW - 5 * 60, limit=100)
        self.assertEqual(filtered["total"], 6)
        # 报表：免费与付费分开计。
        report = db.upstream_usage_report(group_by="day")
        self.assertEqual(report["total"]["count"], 25)
        self.assertEqual(report["total"]["paid"], 13)   # i 为偶数 -> credit 0.5
        self.assertEqual(report["total"]["free"], 12)
        self.assertAlmostEqual(report["total"]["credit"], 13 * 0.5, places=6)
        # 按模型分组，合计与总量一致。
        by_model = db.upstream_usage_report(group_by="model")
        self.assertEqual(sum(g["count"] for g in by_model["groups"]), 25)
        # 非法维度必须报错，不能拼进 SQL。
        with self.assertRaises(ValueError):
            db.upstream_usage_report(group_by="model; DROP TABLE keys")
        db.close()

    def test_models_upsert_and_available_filter(self):
        db = Database(self.root / "t.db")
        self._write_models(db)
        # enabled_models 只看 enabled：gone 的 available=0 也照样可调用。
        self.assertEqual(db.enabled_models(), ["hy3", "gone"])
        # known_model_ids 不再按 available 过滤，否则自定义模型会被当成「未知」。
        self.assertEqual(db.known_model_ids(), {"hy3", "gone"})
        models = db.load_models()
        self.assertIn("gone", models)
        self.assertEqual(models["gone"]["available"], 0)
        # 再写一次不删行。
        db.save_models({"hy3": {"name": "Hy3 v2", "credits": 0.06, "available": 1, "enabled": 1}})
        self.assertIn("gone", db.load_models())
        self.assertEqual(db.load_models()["hy3"]["name"], "Hy3 v2")
        db.close()

    def test_bindings_roundtrip_and_account_cleanup(self):
        db = Database(self.root / "t.db")
        db.save_bindings({"k1": {"account_id": "a1", "expires": 123}, "k2": {"account_id": "a2", "expires": 456}})
        self.assertEqual(db.load_bindings()["k1"]["account_id"], "a1")
        db.delete_bindings_for_account("a1")
        self.assertNotIn("k1", db.load_bindings())
        db.close()

    def test_request_logs_batch_append_and_recent(self):
        db = Database(self.root / "t.db")
        db.append_request_logs([
            {"time": NOW, "source": "api", "path": "/v1/chat/completions", "status": 200,
             "outcome": "success", "duration_ms": 12},
            {"time": NOW + 1, "source": "test", "path": "/v1/messages", "status": 429,
             "outcome": "stream_error", "duration_ms": 30, "account_id": "a1"},
        ])
        recent = db.recent_request_logs()
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["path"], "/v1/messages")
        db.close()

    def test_request_log_stats_total_and_grouping(self):
        db = Database(self.root / "t.db")
        day = 86400
        db.append_request_logs([
            {"time": NOW, "source": "api", "path": "/v1/messages", "status": 200,
             "outcome": "success", "duration_ms": 10, "account_id": "a1", "key_id": "k1", "model_id": "hy3"},
            {"time": NOW + 1, "source": "api", "path": "/v1/messages", "status": 200,
             "outcome": "stream_error", "duration_ms": 30, "account_id": "a1", "key_id": "k1", "model_id": "hy3"},
            # 往前推两天：验证按天分桶与时间窗过滤。
            {"time": NOW - 2 * day, "source": "test", "path": "/v1/responses", "status": 401,
             "outcome": "http_error", "duration_ms": 50, "account_id": "a2", "key_id": "k2", "model_id": "glm"},
        ])
        total = db.request_log_stats()
        self.assertEqual(total["count"], 3)
        self.assertEqual(total["success"], 1)
        # 只取最近一天：两条。
        recent = db.request_log_stats(since=NOW - 3600)
        self.assertEqual(recent["count"], 2)
        self.assertEqual(recent["success"], 1)
        # 按 outcome 分组：3 个桶各 1 条。
        by_outcome = db.request_log_stats(group_by="outcome")
        buckets = {g["key"]: g["count"] for g in by_outcome["groups"]}
        self.assertEqual(buckets, {"success": 1, "stream_error": 1, "http_error": 1})
        # 按账号分组：a1 两条且成功率 50%。
        by_account = db.request_log_stats(group_by="account")
        groups = {g["key"]: g for g in by_account["groups"]}
        self.assertEqual(groups["a1"]["count"], 2)
        self.assertEqual(groups["a1"]["success_rate"], 50.0)
        self.assertEqual(groups["a2"]["count"], 1)
        # 按天分桶：3 天跨度 → 至少 2 个桶。
        by_day = db.request_log_stats(group_by="day")
        self.assertGreaterEqual(len(by_day["groups"]), 2)
        with self.assertRaises(ValueError):
            db.request_log_stats(group_by="no-such-column")
        db.close()

    def test_prune_upstream_usage_keeps_recent_rows(self):
        """上游用量表必须有保留策略：每次同步 UPSERT 30 天窗口，只增不减会无限膨胀。"""
        db = Database(self.root / "t.db")
        day = 86400
        db.save_upstream_usage([
            {"request_id": "old", "account_id": "a1", "credit": 1,
             "request_time": int(time.time()) - 200 * day},
            {"request_id": "keep", "account_id": "a1", "credit": 1,
             "request_time": int(time.time()) - 10 * day},
        ])
        self.assertEqual({r["request_id"] for r in db.load_upstream_usage()}, {"old", "keep"})
        db.prune_upstream_usage()
        self.assertEqual({r["request_id"] for r in db.load_upstream_usage()}, {"keep"})
        db.close()

    def test_upstream_usage_upsert_updates_credit(self):
        db = Database(self.root / "t.db")
        db.save_upstream_usage([{"request_id": "crb-1", "account_id": "a1", "credit": 0,
                                 "model": "step-5-preview", "client": "WorkBuddy", "request_time": 1790000000}])
        db.save_upstream_usage([{"request_id": "crb-1", "account_id": "a1", "credit": 12.5}])
        rows = db.load_upstream_usage("a1")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["credit"], 12.5, places=4)
        db.close()

    def test_close_checkpoints_and_preserves_data(self):
        db = Database(self.root / "t.db")
        db.append_request_logs([{"time": NOW, "path": "/p", "source": "api", "outcome": "success", "duration_ms": 1}])
        wal = self.root / "t.db-wal"
        self.assertTrue(wal.exists())
        db.close()
        # 最后一条连接干净关闭时 SQLite 会删掉 -wal；若仍存在则应被 checkpoint 归零。
        self.assertFalse(wal.exists() and wal.stat().st_size > 0)
        reopened = Database(self.root / "t.db")
        self.assertEqual(len(reopened.recent_request_logs()), 1)
        reopened.close()

    def test_commit_failure_does_not_wedge_the_write_connection(self):
        """回归：COMMIT 抛错后必须回滚，否则连接永久卡在事务里。"""
        db = Database(self.root / "t.db")

        class Failing:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args):
                if sql.strip().upper() == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error")
                return self._inner.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        db._write = Failing(db._write)
        with self.assertRaises(sqlite3.OperationalError):
            db.append_request_logs([{"time": NOW, "path": "/p", "source": "api", "outcome": "success", "duration_ms": 1}])
        db._write = db._write._inner
        self.assertFalse(db._write.in_transaction)
        db.append_request_logs([{"time": NOW + 1, "path": "/p", "source": "api", "outcome": "success", "duration_ms": 1}])
        reopened = Database(self.root / "t.db")
        self.assertEqual(len(reopened.recent_request_logs()), 1)
        reopened.close()

    def test_checkpoint_truncates_wal_outside_transaction(self):
        db = Database(self.root / "t.db")
        for i in range(200):
            db.append_request_logs([{"time": i, "path": "/p" + "x" * 400, "source": "api",
                                     "outcome": "success", "duration_ms": 1}])
        wal = self.root / "t.db-wal"
        self.assertGreater(wal.stat().st_size, 0)
        db.checkpoint()
        self.assertEqual(wal.stat().st_size, 0, "WAL 未被截断")
        db.close()

    def test_v1_database_migrates_to_v2_without_data_loss(self):
        """v1→v2 是唯一会 DROP TABLE 并搬运全量生产数据的路径，必须被 CI 锁住。

        v1 结构与 v2 的差别：accounts/keys 列不同、另有 kv 通用表存五键。
        手工按 v1 DDL 造一个库，再让 Database 打开触发迁移，逐字段比对。
        """
        path = self.root / "v1.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, file TEXT, enabled INTEGER, created INTEGER);
            CREATE TABLE keys (id TEXT PRIMARY KEY, name TEXT, hash TEXT, hint TEXT, created INTEGER,
                              daily_limit INTEGER, models TEXT, accounts TEXT, usage TEXT);
            CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        con.execute("INSERT INTO accounts VALUES ('a1','主号','a1.info',1,111)")
        con.execute("INSERT INTO accounts VALUES ('a2','备号','a2.info',0,222)")
        # v1 的 keys 没有 enabled/revoked_at/expires_at/routing，也没有 usage 三列。
        con.execute("INSERT INTO keys VALUES ('k1','K1','%s','sk…abcd',333,50,'[\"glm-5.2\"]','[\"a1\"]',"
                    "'{\"date\":\"2026-10-14\",\"count\":7,\"tokens\":1234}')" % ("h" * 64))
        kv = {
            "active": "a1",
            "pool": {"checkin_time": "08:30", "sync_window": ["09:00", "21:00"],
                     "affinity_expiry_days": 5, "routing": "manual"},
            "account_status": {"a1": {"remaining": 12.5, "packages": 2, "checkin_date": "2026-10-14",
                                      "cooldown_until": None, "earliest_expiry_remaining": 3.5}},
            "session_bindings": {"sess-1": {"account_id": "a1", "expires": 999}},
            "model_registry": {"available": ["glm-5.2"], "disabled": [],
                               "catalog": {"glm-5.2": {"name": "GLM 5.2", "credits": 3.5,
                                                       "max_input_tokens": 200000},
                                           "ghost": {"name": "已下线", "credits": 1}},
                               "promotions": [{"label": "限时"}]},
        }
        for k, v in kv.items():
            con.execute("INSERT INTO kv VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))
        con.execute("INSERT INTO schema_meta VALUES ('version','1')")
        con.commit()
        con.close()

        db = Database(path)
        try:
            # kv 表必须消失。
            self.assertFalse(db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"))
            self.assertEqual(db.query("SELECT value FROM schema_meta WHERE key='version'")[0]["value"], str(SCHEMA_VERSION))

            accounts = db.load_accounts()
            self.assertEqual(set(accounts), {"a1", "a2"})
            self.assertEqual(accounts["a1"]["name"], "主号")
            self.assertEqual(accounts["a2"]["enabled"], 0)
            self.assertEqual(accounts["a1"]["created"], 111)
            # account_status 的字段并进同一行，一个都不能丢。
            self.assertAlmostEqual(accounts["a1"]["remaining"], 12.5, places=6)
            self.assertEqual(accounts["a1"]["packages"], 2)
            self.assertEqual(accounts["a1"]["checkin_date"], "2026-10-14")
            self.assertAlmostEqual(accounts["a1"]["earliest_expiry_remaining"], 3.5, places=6)

            keys = db.load_keys()
            self.assertEqual(keys["k1"]["daily_limit"], 50)
            self.assertEqual(keys["k1"]["models"], ["glm-5.2"])
            self.assertEqual(keys["k1"]["accounts"], ["a1"])
            # usage 在 v2 里铺平成三列。
            self.assertEqual(keys["k1"]["usage_count"], 7)
            self.assertEqual(keys["k1"]["usage_tokens"], 1234)
            self.assertEqual(keys["k1"]["usage_date"], "2026-10-14")
            # 新增列取约定默认值。
            self.assertEqual(keys["k1"]["enabled"], 1)
            self.assertIsNone(keys["k1"]["revoked_at"])
            self.assertIsNone(keys["k1"]["expires_at"])
            self.assertIsNone(keys["k1"]["routing"])

            settings = db.get_settings()
            self.assertEqual(settings["checkin_time"], "08:30")
            self.assertEqual(settings["sync_start"], "09:00")
            self.assertEqual(settings["sync_end"], "21:00")
            self.assertEqual(settings["affinity_expiry_days"], 5)
            self.assertEqual(settings["default_routing"], "round_robin")
            # 旧的 pool.routing（manual）不迁移，语义下沉到 keys。
            self.assertNotIn("routing", settings)

            self.assertEqual(db.load_bindings()["sess-1"]["account_id"], "a1")
            self.assertEqual(db.load_bindings()["sess-1"]["expires"], 999)

            models = db.load_models()
            self.assertIn("glm-5.2", models)
            self.assertEqual(models["glm-5.2"]["available"], 1)
            self.assertEqual(models["glm-5.2"]["name"], "GLM 5.2")
            self.assertAlmostEqual(models["glm-5.2"]["credits"], 3.5, places=6)
            self.assertEqual(models["glm-5.2"]["promo"], "限时")
            # catalog 里有、available 里没有的行保留成 available=0（供日志 join），不删。
            self.assertIn("ghost", models)
            self.assertEqual(models["ghost"]["available"], 0)
            # available 不再决定可调用性：上游没返回（ghost）但用户没禁用，
            # 一样放行——上游菜单不一定列全，放行与否交给 enabled。
            self.assertIn("glm-5.2", db.enabled_models())
            self.assertIn("ghost", db.enabled_models())
        finally:
            db.close()

    def test_v1_migration_is_idempotent_on_reopen(self):
        """迁移只跑一次：再次打开不能重复搬运或报错。"""
        path = self.root / "v1.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, file TEXT, enabled INTEGER, created INTEGER);
            CREATE TABLE keys (id TEXT PRIMARY KEY, name TEXT, hash TEXT, hint TEXT, created INTEGER,
                              daily_limit INTEGER, models TEXT, accounts TEXT, usage TEXT);
            CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        con.execute("INSERT INTO accounts VALUES ('a1','主号','a1.info',1,111)")
        con.execute("INSERT INTO keys VALUES ('k1','K1','%s','sk…abcd',333,50,'[]','[]','{}')" % ("h" * 64))
        con.execute("INSERT INTO kv VALUES ('pool',?)", (json.dumps({"checkin_time": "08:30"}),))
        con.execute("INSERT INTO schema_meta VALUES ('version','1')")
        con.commit()
        con.close()

        first = Database(path)
        first.close()
        second = Database(path)
        try:
            accounts = second.load_accounts()
            self.assertEqual(set(accounts), {"a1"})
            self.assertEqual(set(second.load_keys()), {"k1"})
            self.assertEqual(second.get_settings()["checkin_time"], "08:30")
        finally:
            second.close()

    def test_legacy_state_json_is_imported_once(self):
        legacy = {
            "accounts": {"a1": {"name": "主号", "file": "a1.info", "enabled": True, "created": 1}},
            "active": "a1",
            "keys": {"k1": {"name": "K", "hash": "h" * 64, "hint": "sk…abcd", "created": 2,
                            "daily_limit": 50, "usage": {"date": "2026-10-14", "count": 3, "tokens": 9}}},
            "pool": {"routing": "manual", "checkin_time": "08:30"},
            "account_status": {"a1": {"remaining": 12.5}},
            "model_registry": {"available": ["glm-5.2"], "catalog": {}, "disabled": []},
        }
        (self.root / "state.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        store = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(set(store.data["accounts"]), {"a1"})
        self.assertEqual(store.data["keys"]["k1"]["usage"]["count"], 3)
        self.assertEqual(store.data["keys"]["k1"]["daily_limit"], 50)
        self.assertEqual(store.data["pool"]["checkin_time"], "08:30")
        # routing 不再迁入 pool；manual 被 default_routing 取代。
        self.assertNotIn("routing", store.data["pool"])
        self.assertEqual(store.data["pool"]["default_routing"], "round_robin")
        self.assertEqual(store.data["account_status"]["a1"]["remaining"], 12.5)
        # 迁移后原文件备份、不再重复导入。
        self.assertTrue((self.root / "state.json.bak").exists())
        self.assertFalse((self.root / "state.json").exists())
        store.save()
        again = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(again.data["keys"]["k1"]["usage"]["count"], 3)

    def test_corrupt_legacy_json_is_ignored(self):
        (self.root / "state.json").write_text("{ not json", encoding="utf-8")
        store = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(store.data["accounts"], {})
        self.assertTrue((self.root / "state.db").exists())

    def test_persists_across_restart(self):
        store = Store(self.root, self.auth, "", ADMIN)
        aid = store.save_browser_account(credential("one"), "一号")["account_id"]
        kid, value = store.add_key("客户端", daily_limit=7)
        store.save()
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(set(reloaded.data["accounts"]), {aid})
        self.assertEqual(set(reloaded.data["keys"]), {kid})
        self.assertEqual(reloaded.data["keys"][kid]["daily_limit"], 7)
        self.assertEqual(reloaded.data["keys"][kid]["hash"], store.data["keys"][kid]["hash"])

    def test_plaintext_key_never_hits_disk(self):
        store = Store(self.root, self.auth, "", ADMIN)
        kid, value = store.add_key("客户端")
        store.save()
        for suffix in ("", "-wal"):
            blob = store.db.path.with_name(store.db.path.name + suffix).read_bytes()
            self.assertNotIn(value.encode(), blob, f"明文密钥泄漏到 {suffix or '主库'}")

    def test_inplace_mutation_is_still_persisted(self):
        store = Store(self.root, self.auth, "", ADMIN)
        aid = store.save_browser_account(credential("one"), "一号")["account_id"]
        store.save()
        with store.lock:
            store.data["account_status"].setdefault(aid, {})["remaining"] = 100
        store.save()
        with store.lock:
            store.data["account_status"][aid]["remaining"] = 200
        store.save()
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertAlmostEqual(reloaded.data["account_status"][aid]["remaining"], 200)

    def test_concurrent_usage_is_serialized_and_persisted(self):
        store = Store(self.root, self.auth, "", ADMIN)
        kid, _ = store.add_key("压测")
        errors = []

        def worker():
            try:
                for _ in range(20):
                    store.record_key_usage(kid, tokens=1)
            except Exception as e:  # pragma: no cover - 仅失败时记录
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        store.save()
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(reloaded.data["keys"][kid]["usage"]["count"], 120)

    def test_stale_open_transaction_fails_loudly(self):
        """回归：连接上残留未决事务时必须报错，不能静默把 save() 变成 no-op。"""
        store = Store(self.root, self.auth, "", ADMIN)
        kid, _ = store.add_key("K")
        store.save()
        store.db._write.execute("BEGIN IMMEDIATE")
        store.db._write.execute("UPDATE keys SET name='stuck' WHERE id=?", (kid,))

        with self.assertRaises(RuntimeError):
            store.save()
        self.assertFalse(store.db._write.in_transaction)
        store.save()
        store.db.close()
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(reloaded.data["keys"][kid]["usage"]["count"], 0)

    def test_close_waits_for_inflight_transaction(self):
        """回归：close() 必须等待正在跑的事务，不能当场打断它。"""
        store = Store(self.root, self.auth, "", ADMIN)
        kid, _ = store.add_key("K")
        store.save()

        entered = threading.Event()
        gate = threading.Event()
        original = store.db.transaction

        @contextmanager
        def slow_transaction():
            with original() as conn:
                entered.set()
                gate.wait(5)
                yield conn

        store.db.transaction = slow_transaction
        errors = []

        def worker():
            try:
                store.data["keys"][kid]["usage"]["count"] = 1
                store.save()
            except Exception as exc:  # pragma: no cover - 仅失败时记录
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(5), "工作线程未进入事务")
            store.db.close()
        finally:
            gate.set()
            thread.join()
        self.assertEqual(errors, [])
        reopened = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(reopened.data["keys"][kid]["usage"]["count"], 1)

    def test_write_after_close_fails_loudly(self):
        db = Database(self.root / "t.db")
        db.close()
        with self.assertRaises(RuntimeError):
            db.append_request_logs([{"time": NOW, "path": "/p", "source": "api", "outcome": "success", "duration_ms": 1}])
        db.close()  # 幂等

    def test_failed_write_rolls_back_instead_of_wiping_table(self):
        """回归：autocommit 下 DELETE 后崩溃会把整表清空，必须真回滚。"""
        store = Store(self.root, self.auth, "", ADMIN)
        for i in range(3):
            store.save_browser_account(credential("u%d" % i), "号%d" % i)
        store.save()
        self.assertEqual(len(store.data["accounts"]), 3)

        original = Database.save_accounts

        def half_save(self, entities, statuses):
            with self.transaction():
                self._write.execute("DELETE FROM accounts")
                raise KeyboardInterrupt("进程恰好死在 DELETE 之后、INSERT 之前")

        Database.save_accounts = half_save
        try:
            with self.assertRaises(KeyboardInterrupt):
                store.save()
        finally:
            Database.save_accounts = original
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(set(reloaded.data["accounts"]), set(store.data["accounts"]))

    def test_save_is_atomic_across_all_tables(self):
        """回归：save() 必须是单事务，不能账号提交了而密钥没提交。"""
        store = Store(self.root, self.auth, "", ADMIN)
        store.save_browser_account(credential("one"), "一号")
        store.save()
        self.assertEqual(len(store.data["accounts"]), 1)

        original = Database.save_keys

        def boom(self, keys):
            raise KeyboardInterrupt("进程恰好死在 save_keys")

        Database.save_keys = boom
        try:
            store.data["accounts"]["zz"] = dict(list(store.data["accounts"].values())[0])
            with self.assertRaises(KeyboardInterrupt):
                store.save()
        finally:
            Database.save_keys = original
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(len(reloaded.data["accounts"]), 1)
        self.assertNotIn("zz", reloaded.data["accounts"])

    def test_initial_key_is_not_recreated_on_every_boot(self):
        initial = "sk-wb-" + "A" * 40
        for _ in range(3):
            store = Store(self.root, self.auth, initial, ADMIN)
        self.assertEqual(len(store.data["keys"]), 1, f"密钥被重复创建：{store.data['keys']}")

    def test_corrupt_database_fails_loudly(self):
        store = Store(self.root, self.auth, "", ADMIN)
        store.add_key("重要密钥")
        store.save()
        store.db.close()
        (self.root / "state.db").write_bytes(b"corrupted")
        (self.root / "state.db-wal").write_bytes(b"corrupted")
        (self.root / "state.db-shm").unlink(missing_ok=True)
        with self.assertRaises(sqlite3.DatabaseError):
            Store(self.root, self.auth, "", ADMIN)

    def test_initial_key_hash_matches_env_value(self):
        initial = "sk-wb-" + "B" * 40
        store = Store(self.root, self.auth, initial, ADMIN)
        (kid,) = store.data["keys"]
        self.assertEqual(store.data["keys"][kid]["hash"], hashlib.sha256(initial.encode()).hexdigest())

    def test_save_is_fast_enough_for_request_path(self):
        store = Store(self.root, self.auth, "", ADMIN)
        store.add_key("压测")
        store.save()
        store.data["keys"][list(store.data["keys"])[0]]["usage"]["count"] = 1
        store.save()
        started = time.perf_counter()
        for _ in range(30):
            store.save()
        per_call = (time.perf_counter() - started) / 30
        self.assertLess(per_call, 0.05, f"save 过慢：{per_call*1000:.1f} ms")


if __name__ == "__main__":
    unittest.main()

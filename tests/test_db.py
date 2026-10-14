"""SQLite 持久层：迁移、恢复、并发与脏标记。"""
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

from admin.db import Database
from admin.server import Store
from test_admin_server import credential

ADMIN = "admin-test-credential-long-enough"


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "management"
        self.auth = Path(self.tmp.name) / "auth"
        self.root.mkdir(parents=True)
        self.auth.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_kv_roundtrip_and_nested_types(self):
        db = Database(self.root / "t.db")
        db.set_kv("nested", {"a": [1, 2, {"b": None}], "c": "中文"})
        db.set_kv("empty_list", [])
        db.set_kv("empty_dict", {})
        self.assertEqual(db.get_kv("nested"), {"a": [1, 2, {"b": None}], "c": "中文"})
        self.assertEqual(db.get_kv("empty_list"), [])
        self.assertEqual(db.get_kv("empty_dict"), {})
        self.assertIsNone(db.get_kv("missing"))
        self.assertEqual(db.get_kv("missing", {"d": 1}), {"d": 1})
        db.close()

    def test_close_checkpoints_and_preserves_data(self):
        db = Database(self.root / "t.db")
        db.set_kv("k", {"v": 1})
        wal = self.root / "t.db-wal"
        self.assertTrue(wal.exists())
        db.close()
        # 最后一条连接干净关闭时 SQLite 会删掉 -wal；若仍存在则应被 checkpoint 归零。
        self.assertFalse(wal.exists() and wal.stat().st_size > 0)
        reopened = Database(self.root / "t.db")
        self.assertEqual(reopened.get_kv("k"), {"v": 1})
        reopened.close()

    def test_commit_failure_does_not_wedge_the_write_connection(self):
        """回归：COMMIT 抛错后必须回滚，否则连接永久卡在事务里。"""
        db = Database(self.root / "t.db")
        db.set_kv("k", {"v": 1})
        real = db._write.execute

        def failing(sql, *args):
            if sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return real(sql, *args)

        # 无法替换 Connection.execute，用包装类注入失败。
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
            db.set_kv("k2", {"v": 2})
        db._write = db._write._inner
        # 连接必须还能用：否则之后每次 BEGIN 都 cannot start a transaction
        self.assertFalse(db._write.in_transaction)
        db.set_kv("k3", {"v": 3})
        reopened = Database(self.root / "t.db")
        self.assertEqual(reopened.get_kv("k3"), {"v": 3})
        reopened.close()

    def test_checkpoint_truncates_wal_outside_transaction(self):
        """回归：wal_checkpoint 在 BEGIN IMMEDIATE 里会 locked 失败，WAL 截不掉。"""
        db = Database(self.root / "t.db")
        for i in range(200):
            db.set_kv("k%d" % i, {"v": "x" * 400})
        wal = self.root / "t.db-wal"
        self.assertGreater(wal.stat().st_size, 0)
        db.checkpoint()
        self.assertEqual(wal.stat().st_size, 0, "WAL 未被截断")
        reopened = Database(self.root / "t.db")
        self.assertEqual(reopened.get_kv("k199"), {"v": "x" * 400})
        reopened.close()

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
        self.assertEqual(store.data["active"], "a1")
        self.assertEqual(store.data["keys"]["k1"]["usage"]["count"], 3)
        self.assertEqual(store.data["keys"]["k1"]["daily_limit"], 50)
        self.assertEqual(store.data["pool"]["routing"], "manual")
        self.assertEqual(store.data["account_status"]["a1"]["remaining"], 12.5)
        self.assertEqual(store.data["model_registry"]["available"], ["glm-5.2"])
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
        # 损坏的 legacy 文件被跳过，但数据库本体必须已建好。
        self.assertTrue((self.root / "state.db").exists())

    def test_persists_across_restart(self):
        store = Store(self.root, self.auth, "", ADMIN)
        aid = store.save_browser_account(credential("one"), "一号")["account_id"]
        kid, value = store.add_key("客户端", daily_limit=7)
        store.save()
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(set(reloaded.data["accounts"]), {aid})
        self.assertEqual(reloaded.data["active"], aid)
        self.assertEqual(set(reloaded.data["keys"]), {kid})
        self.assertEqual(reloaded.data["keys"][kid]["daily_limit"], 7)
        # hash 一致才能通过鉴权。
        self.assertEqual(reloaded.data["keys"][kid]["hash"], store.data["keys"][kid]["hash"])

    def test_plaintext_key_never_hits_disk(self):
        store = Store(self.root, self.auth, "", ADMIN)
        kid, value = store.add_key("客户端")
        store.save()
        # 主库和 -wal 都要查：WAL 模式下未 checkpoint 的帧只在 -wal 里。
        for suffix in ("", "-wal"):
            blob = store.db.path.with_name(store.db.path.name + suffix).read_bytes()
            self.assertNotIn(value.encode(), blob, f"明文密钥泄漏到 {suffix or '主库'}")

    def test_inplace_mutation_is_still_persisted(self):
        """脏标记存快照：原地修改 self.data 后必须仍能落库（引用相等会漏写）。"""
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
        self.assertEqual(reloaded.data["account_status"][aid]["remaining"], 200)

    def test_concurrent_writes_are_serialized(self):
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
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(reloaded.data["keys"][kid]["usage"]["count"], 120)

    def test_stale_open_transaction_fails_loudly(self):
        """回归：连接上残留未决事务时必须报错，不能静默把 save() 变成 no-op。

        上一次次事务回滚失败（磁盘掉线后恢复）会留下未决事务。若 transaction()
        只看 in_transaction 就以为是被嵌套调用而直接让出，之后每次 save() 都不写
        任何东西却正常返回——调用方无感知，数据全部丢失。
        """
        store = Store(self.root, self.auth, "", ADMIN)
        kid, _ = store.add_key("K")
        store.save()
        # 手工留下一个未决事务（没有对应的 _depth 记录）。
        store.db._write.execute("BEGIN IMMEDIATE")
        store.db._write.execute("UPDATE keys SET name='stuck' WHERE id=?", (kid,))

        with self.assertRaises(RuntimeError):
            store.record_key_usage(kid, tokens=1)
        # 残局已被回滚，连接恢复可用，后续写入正常落库。
        # 注意内存计数在报错前已经自增过，所以是 2 而不是 1。
        self.assertFalse(store.db._write.in_transaction)
        store.record_key_usage(kid, tokens=1)
        store.db.close()
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(reloaded.data["keys"][kid]["usage"]["count"], 2)

    def test_close_waits_for_inflight_transaction(self):
        """回归：close() 必须等待正在跑的事务，不能当场打断它。

        lifespan 关闭时 cancel() 只取消 await 点，正在 to_thread 里 save() 的
        池线程不会被中断。旧代码 close() 不取锁，_write.close() 会硬关连接，
        那一次保存直接回滚丢失。
        """
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
                store.record_key_usage(kid, tokens=1)
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
        reloaded = Store(self.root, self.auth, "", ADMIN)
        # 正在跑的那一次保存必须保住。
        self.assertEqual(reloaded.data["keys"][kid]["usage"]["count"], 1)

    def test_write_after_close_fails_loudly(self):
        """回归：关闭后继续写必须明确报错，不能静默丢弃。"""
        db = Database(self.root / "t.db")
        db.set_kv("k", {"v": 1})
        db.close()
        with self.assertRaises(RuntimeError):
            db.set_kv("k2", {"v": 2})
        # 幂等：重复 close 不炸。
        db.close()

    def test_failed_write_rolls_back_instead_of_wiping_table(self):
        """回归：autocommit 下 ``with conn:`` 不开事务，DELETE 后崩溃会把整表清空。"""
        store = Store(self.root, self.auth, "", ADMIN)
        for i in range(3):
            store.save_browser_account(credential("u%d" % i), "号%d" % i)
        store.save()
        self.assertEqual(len(store.data["accounts"]), 3)

        original = Database.replace_accounts

        def half_replace(self, accounts):
            with self.transaction():
                self._write.execute("DELETE FROM accounts")
                raise KeyboardInterrupt("进程恰好死在 DELETE 之后、INSERT 之前")

        Database.replace_accounts = half_replace
        try:
            with self.assertRaises(KeyboardInterrupt):
                store.save()
        finally:
            Database.replace_accounts = original
        # 崩溃不得留下半写状态：账号必须原样还在。
        reloaded = Store(self.root, self.auth, "", ADMIN)
        self.assertEqual(set(reloaded.data["accounts"]), set(store.data["accounts"]))

    def test_save_is_atomic_across_all_tables(self):
        """回归：save() 必须是单事务，不能账号提交了而 active 没提交。"""
        store = Store(self.root, self.auth, "", ADMIN)
        store.save_browser_account(credential("one"), "一号")
        store.save()
        self.assertEqual(len(store.data["accounts"]), 1)

        original = Database.replace_keys

        def boom(self, keys):
            raise KeyboardInterrupt("进程恰好死在 replace_keys")

        Database.replace_keys = boom
        try:
            store.data["accounts"]["zz"] = dict(list(store.data["accounts"].values())[0])
            with self.assertRaises(KeyboardInterrupt):
                store.save()
        finally:
            Database.replace_keys = original
        reloaded = Store(self.root, self.auth, "", ADMIN)
        # 崩溃必须整体回滚：新增的账号不得残留。
        self.assertEqual(len(reloaded.data["accounts"]), 1)
        self.assertNotIn("zz", reloaded.data["accounts"])

    def test_initial_key_is_not_recreated_on_every_boot(self):
        """回归：auth 目录为空时 accounts 恒为空，旧逻辑每次重启都新建一个同值密钥。"""
        initial = "sk-wb-" + "A" * 40
        for _ in range(3):
            store = Store(self.root, self.auth, initial, ADMIN)
        self.assertEqual(len(store.data["keys"]), 1, f"密钥被重复创建：{store.data['keys']}")

    def test_corrupt_database_fails_loudly(self):
        """坏库必须直接报错，而不是静默启动成空库把数据悄悄丢掉。

        检测/隔离/重建属于运维侧（定时备份 + 人工恢复），业务层不猜：
        打开失败就抛，让故障在启动时暴露。
        """
        store = Store(self.root, self.auth, "", ADMIN)
        store.add_key("重要密钥")
        store.save()
        store.db.close()
        (self.root / "state.db").write_bytes(b"corrupted")
        (self.root / "state.db-wal").write_bytes(b"corrupted")
        (self.root / "state.db-shm").unlink()
        with self.assertRaises(sqlite3.DatabaseError):
            Store(self.root, self.auth, "", ADMIN)

    def test_initial_key_hash_matches_env_value(self):
        initial = "sk-wb-" + "B" * 40
        store = Store(self.root, self.auth, initial, ADMIN)
        (kid,) = store.data["keys"]
        # hash 一致才能让 CODEBUDDY2OPENAI_KEY 直接通过鉴权。
        self.assertEqual(store.data["keys"][kid]["hash"], hashlib.sha256(initial.encode()).hexdigest())

    def test_save_is_fast_enough_for_request_path(self):
        store = Store(self.root, self.auth, "", ADMIN)
        kid, _ = store.add_key("压测")
        store.save()
        for _ in range(3):
            store.record_key_usage(kid, tokens=1)
        started = time.perf_counter()
        for _ in range(30):
            store.record_key_usage(kid, tokens=1)
        per_call = (time.perf_counter() - started) / 30
        # 远低于旧 JSON 全量写（4–8 ms）；留足余量防慢机器误报。
        self.assertLess(per_call, 0.05, f"save 过慢：{per_call*1000:.1f} ms")


if __name__ == "__main__":
    unittest.main()

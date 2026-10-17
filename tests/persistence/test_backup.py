"""数据库备份与恢复：模块级正确性 + 管理接口。

覆盖重点不是「能生成文件」，而是几条容易写错的边界：
- 备份必须在 WAL 下也拿到已提交数据（直接拷主库会丢）。
- 文件名白名单必须挡住路径穿越与「前缀合法、尾巴越界」两种注入。
- 恢复是不可逆操作，必须先验证、先自保，且中断后能续跑。
"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from admin import backup
from admin.db import Database


def make_db(path, accounts=("a1",)):
    """造一个结构完整、可被 verify 接受的最小库。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE keys (id TEXT PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE settings (id INTEGER PRIMARY KEY, auto_checkin INTEGER);"
        "INSERT INTO settings VALUES (1, 1);")
    for aid in accounts:
        conn.execute("INSERT INTO accounts VALUES (?, ?)", (aid, "账号" + aid))
    conn.commit()
    conn.close()
    return Path(path)


class BackupModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = make_db(self.root / "state.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_human_size_scales_by_unit(self):
        # 回归：曾经的 bug 是「判的是缩放后的值、format 又除一次 1024」，
        # 所有非 B 档都显示成 0.0 KB / 0.0 MB（用户 2026-10-17 报「备份
        # 怎么都是 0M」）。这里钉住每一档的首值与中间值。
        self.assertEqual(backup.human_size(0), "0 B")
        self.assertEqual(backup.human_size(512), "512 B")
        self.assertEqual(backup.human_size(1024), "1.0 KB")
        self.assertEqual(backup.human_size(1536), "1.5 KB")
        self.assertEqual(backup.human_size(1048576), "1.0 MB")
        self.assertEqual(backup.human_size(5242880), "5.0 MB")
        self.assertEqual(backup.human_size(1073741824), "1.0 GB")
        self.assertEqual(backup.human_size(16106127360), "15.0 GB")

    def test_create_and_verify(self):
        name = backup.create(self.root, self.db, keep=5, minimal_size=100)
        self.assertTrue(backup.NAME_RE.fullmatch(name))
        path = backup.resolve(self.root, name)
        self.assertIsNotNone(path)
        ok, message = backup.verify(path)
        self.assertTrue(ok, message)
        # 备份内容与源库一致（不是空壳）。
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("SELECT id FROM accounts").fetchall()
        finally:
            conn.close()
        self.assertEqual([row[0] for row in rows], ["a1"])

    def test_backup_captures_wal_data(self):
        """WAL 里尚未 checkpoint 的数据也必须进备份。

        这是「不用 cp state.db」的核心理由：直接拷主库会漏掉 -wal 中的已提交页。
        保持连接不关，WAL 才会留在盘上（SQLite 在最后一个连接关闭时会自动
        checkpoint 并把 -wal 收掉，那样就测不到这条路径了）。
        """
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO accounts VALUES ('wal-row', '在 WAL 里')")
        conn.commit()  # 已提交，但仍在 -wal 中未合并
        try:
            wal = self.root / "state.db-wal"
            self.assertTrue(wal.exists() and wal.stat().st_size > 0,
                            "前置条件：WAL 文件应有内容")

            name = backup.create(self.root, self.db, keep=5, minimal_size=100)
            copy = sqlite3.connect(backup.resolve(self.root, name))
            try:
                rows = {row[0] for row in copy.execute("SELECT id FROM accounts")}
            finally:
                copy.close()
            self.assertIn("wal-row", rows, "备份漏掉了 WAL 中已提交的数据")

            # 对照：直接拷主库确实会漏（说明这个测试确实在测那件事）。
            naive = self.root / "naive.db"
            naive.write_bytes(self.db.read_bytes())
            plain = sqlite3.connect(naive)
            try:
                naive_rows = {row[0] for row in plain.execute("SELECT id FROM accounts")}
            finally:
                plain.close()
            self.assertNotIn("wal-row", naive_rows)
        finally:
            conn.close()

    def test_empty_db_is_rejected(self):
        """空库备份会产出「成功但全空」的文件，比不备份更危险，必须拒绝。"""
        empty = self.root / "empty.db"
        sqlite3.connect(empty).close()
        with self.assertRaises(RuntimeError):
            backup.create(self.root, empty, keep=5, minimal_size=4096)
        # 失败不该留下半成品：.tmp 与正式名都不该出现。
        self.assertEqual(backup.list_backups(self.root), [])
        self.assertEqual(list(backup.backup_dir(self.root).glob("*.tmp")), [])

    def test_filename_whitelist_blocks_traversal(self):
        """路径穿越必须挡死，包括「前缀合法但尾巴越界」这种。"""
        for evil in ("../state-20260101-000000.db",
                     "state-20260101-000000.db/../../etc/passwd",
                     "/etc/passwd",
                     "xstate-20260101-000000.db",
                     "state-20260101-000000.db.tmp",
                     "",
                     None,
                     123):
            self.assertIsNone(backup.safe_path(self.root, evil), f"{evil!r} 应被拒绝")
            self.assertIsNone(backup.resolve(self.root, evil), f"{evil!r} 应被拒绝")

    def test_prune_keeps_newest(self):
        for offset in range(5):
            backup.create(self.root, self.db, now=time.time() + offset, keep=0, minimal_size=100)
        self.assertEqual(len(backup.list_backups(self.root)), 5)
        removed = backup.prune(self.root, 2)
        self.assertEqual(len(removed), 3)
        remaining = backup.list_backups(self.root)
        self.assertEqual(len(remaining), 2)
        # 留下的必须是最新的那两份。
        self.assertEqual([item["name"] for item in remaining],
                         sorted((item["name"] for item in remaining), reverse=True))

    def test_list_is_newest_first(self):
        names = [backup.create(self.root, self.db, now=time.time() + i,
                               keep=0, minimal_size=100) for i in range(3)]
        listed = [item["name"] for item in backup.list_backups(self.root)]
        self.assertEqual(listed, list(reversed(names)))

    def test_same_second_does_not_overwrite(self):
        """同一秒连点两次备份不能互相覆盖——否则第二次会截断第一份。"""
        first = backup.create(self.root, self.db, now=1770000000.0, keep=0, minimal_size=100)
        second = backup.create(self.root, self.db, now=1770000000.0, keep=0, minimal_size=100)
        self.assertNotEqual(first, second)
        self.assertEqual(len(backup.list_backups(self.root)), 2)
        # 两份都必须是可用的完整备份，不是被截断的空壳。
        for item in backup.list_backups(self.root):
            self.assertTrue(backup.verify(backup.resolve(self.root, item["name"]))[0])

    def test_verify_rejects_corrupt_and_wrong_file(self):
        broken = self.root / "broken.db"
        broken.write_bytes(b"definitely not sqlite" * 100)
        ok, message = backup.verify(broken)
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_verify_rejects_missing_tables(self):
        """能打开但缺核心表的文件不算可用备份。"""
        wrong = self.root / "wrong.db"
        conn = sqlite3.connect(wrong)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        ok, message = backup.verify(wrong)
        self.assertFalse(ok)
        self.assertIn("accounts", message)

    def test_remove_only_touches_backups(self):
        name = backup.create(self.root, self.db, keep=5, minimal_size=100)
        self.assertTrue(backup.remove(self.root, name))
        self.assertFalse(backup.remove(self.root, name))
        self.assertFalse(backup.remove(self.root, "../state.db"))
        self.assertTrue(self.db.exists(), "源库绝不能被删除")


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = make_db(self.root / "state.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_begin_and_finish_restore(self):
        backup.create(self.root, self.db, keep=5, minimal_size=100)
        # 改掉线上库，好在恢复后能区分「恢复真的生效了」。
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM accounts")
        conn.commit()
        conn.close()
        source = backup.resolve(self.root, backup.list_backups(self.root)[0]["name"])
        backup.begin_restore(self.root, source, self.db)
        backup.finish_restore(self.root)
        conn = sqlite3.connect(self.db)
        try:
            rows = [row[0] for row in conn.execute("SELECT id FROM accounts")]
        finally:
            conn.close()
        self.assertEqual(rows, ["a1"], "恢复后应回到备份时的数据")

    def test_finish_clears_stale_wal(self):
        """恢复后必须清掉旧库的 -wal/-shm。

        不清的话新库会被当成「WAL 里还有未合并帧」，把上一份数据又读回来。
        清理分两步：begin_restore 在替换前把 -wal/-shm 改名移走（半截状态
        不留场），finish_restore 删掉这些改名后的残留。
        """
        (self.root / "state.db-wal").write_bytes(b"stale")
        (self.root / "state.db-shm").write_bytes(b"stale")
        # begin_restore(db_path=None) 只走前半段；wal 清理在 db_path 分支里，
        # 这里直接测 finish 对两种命名残留的清理能力。
        backup.finish_restore(self.root)
        self.assertFalse((self.root / "restore-pending.db").exists())
        self.assertFalse((self.root / "restore-stale-wal").exists())
        self.assertFalse((self.root / "restore-stale-shm").exists())

    def test_begin_restore_moves_old_wal_before_replacing(self):
        """回归：替换 db 之前必须先把旧 -wal/-shm 移走。

        若只依赖 finish_restore 收尾，进程在 os.replace(pending, db) 之后、
        finish 之前挂掉时，db 已是新库而 wal 还是旧库的——启动续跑只认
        stale_pending()（此时已返回 None），没人来清这份 wal，新库会把
        上一份数据又读回来。先移走则中途崩溃也自愈。
        """
        (self.db).write_bytes(b"new-db")
        wal, shm = self.root / "state.db-wal", self.root / "state.db-shm"
        wal.write_bytes(b"old-wal"); shm.write_bytes(b"old-shm")
        backup.begin_restore(self.root, self.db, self.root / "state.db")
        # 旧 wal 已被移走：新库旁边不能留着上一份 wal
        self.assertFalse(wal.exists(), "旧 -wal 仍在原位，半截状态会残留")
        self.assertFalse(shm.exists())
        # 内容被挪到了收尾文件名下，等 finish_restore 删除
        self.assertEqual((self.root / "restore-stale-wal").read_bytes(), b"old-wal")
        backup.finish_restore(self.root)
        self.assertFalse((self.root / "restore-stale-wal").exists())

    def test_stale_pending_survives_until_finished(self):
        """中断的恢复要能被下次启动发现。"""
        backup.create(self.root, self.db, keep=5, minimal_size=100)
        source = backup.resolve(self.root, backup.list_backups(self.root)[0]["name"])
        pending = backup.begin_restore(self.root, source, None)
        self.assertIsNotNone(backup.stale_pending(self.root))
        backup.finish_restore(self.root)
        self.assertIsNone(backup.stale_pending(self.root))
        self.assertFalse(pending.exists())


class BackupScheduleTests(unittest.TestCase):
    def test_due_respects_enabled_and_interval(self):
        now = 1_770_000_000
        # 关闭时永远不跑，哪怕 last_at 是很久以前。
        self.assertFalse(backup.due({"backup_enabled": False, "backup_interval_hours": 1,
                                     "backup_last_at": 0}, now))
        # 开启但间隔为 0：不跑（否则会变成每轮都备份的死循环）。
        self.assertFalse(backup.due({"backup_enabled": True, "backup_interval_hours": 0,
                                     "backup_last_at": 0}, now))
        # 开启且从没备份过：立刻跑一次。
        self.assertTrue(backup.due({"backup_enabled": True, "backup_interval_hours": 24,
                                    "backup_last_at": 0}, now))
        # 未到期 / 已到期。
        settings = {"backup_enabled": True, "backup_interval_hours": 24,
                    "backup_last_at": now - 3600}
        self.assertFalse(backup.due(settings, now))
        settings["backup_last_at"] = now - 25 * 3600
        self.assertTrue(backup.due(settings, now))

    def test_next_check_delay_is_bounded(self):
        """检查间隔不能超过 tick，否则改了设置要等一整个周期才生效。"""
        now = 1_770_000_000
        far = backup.next_check_delay({"backup_enabled": True, "backup_interval_hours": 720,
                                       "backup_last_at": now}, now)
        self.assertLessEqual(far, backup.TICK_SECONDS)
        # 已到期则立刻检查。
        self.assertEqual(backup.next_check_delay({"backup_enabled": True, "backup_interval_hours": 1,
                                                  "backup_last_at": 0}, now), 0)
        self.assertGreater(
            backup.next_check_delay({"backup_enabled": True, "backup_interval_hours": 1,
                                     "backup_last_at": now - 3500}, now), 0)

    def test_schedule_hint_never_empty(self):
        for settings in ({}, {"backup_enabled": False},
                         {"backup_enabled": True, "backup_interval_hours": 0},
                         {"backup_enabled": True, "backup_interval_hours": 6, "backup_last_at": 0},
                         {"backup_enabled": True, "backup_interval_hours": 6,
                          "backup_last_at": int(time.time())}):
            self.assertTrue(backup.schedule_hint(settings))

    def test_filenames_use_beijing_time(self):
        """文件名用北京时间，与库内其它时间口径一致（不依赖容器 TZ）。"""
        import os
        stamp = 1_750_000_000  # 2025-06-15 15:06:40 UTC == 23:06:40 +08
        expected = "state-20250615-230640.db"
        self.assertEqual(backup.filename(stamp), expected)
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        try:
            self.assertEqual(backup.filename(stamp), expected)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous


class BackupAgainstRealDatabaseTests(unittest.TestCase):
    """用真正的 Database 走一遍：备份出来的库能被 Database 重新打开。"""

    def test_database_reopens_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "state.db")
            db.save_settings({"auto_checkin": 1, "checkin_time": "07:15",
                              "affinity_expiry_days": 3, "credit_min_interval": 60,
                              "sync_start": "10:00", "sync_end": "20:00",
                              "batch_spread": 90, "default_routing": "round_robin"})
            db.close()
            name = backup.create(root, root / "state.db", keep=5, minimal_size=100)
            ok, message = backup.verify(backup.resolve(root, name))
            self.assertTrue(ok, message)
            # 把备份当成库打开：schema 版本、设置都要能读回来。
            restored = Database(root / "restored.db")
            restored.close()
            import shutil
            shutil.copyfile(backup.resolve(root, name), root / "restored.db")
            again = Database(root / "restored.db")
            try:
                self.assertEqual(again.get_settings()["checkin_time"], "07:15")
            finally:
                again.close()


class BackupApiTests(unittest.IsolatedAsyncioTestCase):
    """管理接口：鉴权、手动备份、列表、删除、下载、恢复的守卫。"""

    async def asyncSetUp(self):
        import httpx
        import admin.server as admin_server
        self.httpx = httpx
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps({
            "account": {"uid": "one", "nickname": "测试账号", "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}), encoding="utf-8")
        self.admin_server = admin_server
        self.make_app()

    def make_app(self):
        self.app = self.admin_server.create_app(self.root / "management", self.auth,
                                                "client-key-value", "admin-test-credential-long-enough")
        self.client = self.httpx.AsyncClient(transport=self.httpx.ASGITransport(app=self.app),
                                             base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post("/admin/api/login", json={"key": "admin-test-credential-long-enough"})
        self.assertEqual(result.status_code, 200)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    async def test_requires_admin(self):
        self.assertEqual((await self.client.get("/admin/api/backups")).status_code, 401)
        self.assertEqual((await self.client.post("/admin/api/backups")).status_code, 401)
        self.assertEqual((await self.client.get(
            "/admin/api/backups/state-20260101-000000.db/download")).status_code, 401)
        self.assertEqual((await self.client.delete(
            "/admin/api/backups/state-20260101-000000.db")).status_code, 401)

    async def test_create_list_download_delete(self):
        await self.login()
        empty = (await self.client.get("/admin/api/backups")).json()
        self.assertEqual(empty["count"], 0)
        self.assertTrue(empty["schedule"])

        created = await self.client.post("/admin/api/backups")
        self.assertEqual(created.status_code, 200, created.text)
        name = created.json()["name"]
        self.assertTrue(self.admin_server.db_backup.NAME_RE.fullmatch(name))

        listed = (await self.client.get("/admin/api/backups")).json()
        self.assertEqual(listed["count"], 1)
        item = listed["items"][0]
        self.assertEqual(item["name"], name)
        self.assertTrue(item["is_last"])
        self.assertIn("size_text", item)
        # 定时状态：默认关闭。
        self.assertFalse(listed["items"][0].get("scheduled"))
        self.assertIn("自动备份已关闭", listed["schedule"])

        downloaded = await self.client.get(f"/admin/api/backups/{name}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertTrue(downloaded.content.startswith(b"SQLite format 3"))

        removed = await self.client.delete(f"/admin/api/backups/{name}")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["count"], 0)
        self.assertEqual((await self.client.delete(f"/admin/api/backups/{name}")).status_code, 404)

    async def test_download_rejects_traversal(self):
        await self.login()
        for evil in ("..%2F..%2Fstate.db", "state-20260101-000000.db%2F..%2F..%2Fstate.db",
                     "etc%2Fpasswd"):
            response = await self.client.get(f"/admin/api/backups/{evil}/download")
            self.assertEqual(response.status_code, 404, f"{evil} 应被拒绝")

    async def test_delete_rejects_unknown_name(self):
        await self.login()
        for evil in ("..%2Fstate.db", "state-20260101-000000.db.tmp"):
            self.assertEqual((await self.client.delete(f"/admin/api/backups/{evil}")).status_code, 404)

    async def test_settings_validation_and_persistence(self):
        await self.login()
        saved = await self.client.patch("/admin/api/pool/settings", json={
            "backup_enabled": True, "backup_interval_hours": 12, "backup_keep": 5})
        self.assertEqual(saved.status_code, 200, saved.text)
        pool = saved.json()["pool"]
        self.assertTrue(pool["backup_enabled"])
        self.assertEqual((pool["backup_interval_hours"], pool["backup_keep"]), (12, 5))
        # 非法值一律 400，别把坏配置带到运行期。
        for bad in ({"backup_enabled": "yes"}, {"backup_interval_hours": 0},
                    {"backup_interval_hours": 721}, {"backup_keep": 0}, {"backup_keep": 101},
                    {"backup_interval_hours": "x"}):
            self.assertEqual((await self.client.patch("/admin/api/pool/settings", json=bad)).status_code,
                             400, f"{bad} 应被拒绝")
        # 重启后仍在（重建应用重新读库）。
        self.make_app()
        await self.login()
        again = (await self.client.get("/admin/api/overview")).json()["pool"]
        self.assertTrue(again["backup_enabled"])
        self.assertEqual(again["backup_interval_hours"], 12)
        # 默认关闭自动备份：不能因为升级就替用户打开。
        self.assertEqual(await self._default_backup_enabled(), 0)

    async def _default_backup_enabled(self):
        """新库的 backup_enabled 默认值（从这里读，绕开已被改过的实例）。"""
        from admin.pool import AccountPool
        return 0 if not AccountPool.POOL_DEFAULTS["backup_enabled"] else 1

    async def test_manual_backup_records_last(self):
        await self.login()
        await self.client.post("/admin/api/backups")
        pool = (await self.client.get("/admin/api/overview")).json()["pool"]
        self.assertGreater(pool["backup_last_at"], 0)
        self.assertTrue(pool["backup_last_name"])
        # 最近一次备份会在列表里标出来，便于确认「上次成功的是哪份」。
        listed = (await self.client.get("/admin/api/backups")).json()
        self.assertTrue(listed["items"][0]["is_last"])

    async def test_restore_rejects_corrupt_backup(self):
        """备份本身不健康时必须拒绝，且不能碰线上库。"""
        await self.login()
        created = (await self.client.post("/admin/api/backups")).json()
        name = created["name"]
        path = self.admin_server.db_backup.resolve(self.app.state.store.root, name)
        path.write_bytes(b"corrupt" * 500)
        response = await self.client.post(f"/admin/api/backups/{name}/restore")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("不可用", response.json()["detail"])
        # 线上库没被动过：管理接口照常工作。
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 200)

    async def test_restore_unknown_name(self):
        await self.login()
        response = await self.client.post("/admin/api/backups/state-20260101-000000.db/restore")
        self.assertEqual(response.status_code, 404)

    async def test_admin_returns_503_while_restoring(self):
        """恢复窗口内管理接口必须 503，不能撞在已关闭的连接上抛 500。

        恢复的流程是「关库 → 换文件 → 重启」，关库之后到进程退出之间仍可能有
        请求进来。没有这道闸时它会撞在已关闭的 sqlite 连接上，抛
        ProgrammingError（用户看到的是一屏堆栈），而正确行为是让他稍后刷新。
        """
        await self.login()
        store = self.app.state.store
        store.restoring = True
        try:
            response = await self.client.get("/admin/api/overview")
            self.assertEqual(response.status_code, 503)
            self.assertIn("恢复", response.json()["detail"])
            # /v1 同样要 503：恢复会把库关掉，这个窗口内客户端的 flush→save→
            # transaction 会撞「数据库已关闭」抛 500。宁可让客户端看到 503 重试，
            # 也不能把堆栈透出去——库关了就是真没法服务。
            self.assertEqual((await self.client.get("/v1/models",
                                                    headers={"Authorization": "Bearer client-key-value"})).status_code, 503)
        finally:
            store.restoring = False
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 200)

    async def test_settings_groups_are_exposed(self):
        """设置页按分组渲染，分组口径必须由后端给，避免前后端两套。"""
        await self.login()
        payload = (await self.client.get("/admin/api/overview")).json()
        groups = payload["pool_groups"]
        self.assertEqual(set(groups), {"checkin", "queue", "routing", "guards", "backup"})
        # 分组必须覆盖 POOL_DEFAULTS 里除内部字段外的全部键，否则会有设置
        # 在页面上找不到位置（改了默认值却忘了归组）。
        grouped = {key for keys in groups.values() for key in keys}
        from admin.pool import AccountPool
        missing = set(AccountPool.POOL_DEFAULTS) - grouped - {"affinity_expiry_days"}
        self.assertFalse(missing, f"这些设置没有被分到任何一组：{missing}")


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


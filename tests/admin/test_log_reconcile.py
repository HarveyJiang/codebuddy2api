"""日志与云端逐条对账（v26 的三列）。

用户 2026-10-20 报：「日志记录的是 A 账号，但是云端账号其实是请求的 B 账号。
而且模型也和选择的不一样。」

排查结论是**代码没记错，是两边标识体系不同**（生产库实证）：

- **账号**：日志里是本地备注名（如「新号」），云端认的是上游昵称/uid ——
  同一个号在云端显示成手机号 `15882308279`。**只有 uid 在两边稳定对齐**。
- **模型**：日志里是客户端请求的裸名（`deepseek-v4.1-flash`），上游按自己的
  目录记（`deepseek-v4-flash`，trae 还要再过一遍别名表）。
- **请求 ID**：原先只存上游聊天流里的 `cmb-…`；而上游的用量报表认的是
  **我们发过去的那套 id**（`X-Request-ID`）。实测按 id JOIN
  1442 条日志 × 8827 条上游行，**命中 0 条** —— 日志与云端永远对不上账。

v26 因此给 `request_logs` 补三列，把「两边各自的名字」都记下来：
`upstream_model` / `account_uid` / `client_request_id`。三列都可空：存量行
一律 NULL，不猜、不回填。

本文件钉三件事：
1. **迁移**：v25 的库升级后有三列，存量行是 NULL —— 升级不改任何既有行为；
2. **落库**：scope / log_context 里的三个值原样进对应列，取不到就是 NULL；
3. **上游视角可查**：`GET /admin/api/logs/upstream` 返回上游自己记的流水，
   行里带上 uid 与请求 id，能和 request_logs 拼起来对账。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from admin.db import SCHEMA_VERSION, Database
from admin.metrics import MetricsMiddleware, RequestMetrics
from admin.pool import REQUEST_LOG_CONTEXT
from admin.server import create_app
from core.session_headers import SCOPE_REQUEST_ID, SCOPE_UPSTREAM_MODEL

from conftest import credential

API = "sk-test-client-key"
ADMIN = "admin-key-long-enough-1234"

# v25 的 request_logs（还没有 upstream_model / account_uid / client_request_id）。
_V25_REQUEST_LOGS_DDL = """CREATE TABLE request_logs (
    id INTEGER PRIMARY KEY,
    time INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'api', path TEXT NOT NULL,
    status INTEGER, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL,
    key_id TEXT, account_id TEXT, account_name TEXT,
    model_id TEXT, model_name TEXT, upstream_request_id TEXT,
    backend TEXT,
    tokens_in INTEGER, tokens_out INTEGER,
    tokens_cache_read INTEGER, tokens_cache_creation INTEGER,
    error_code TEXT)"""


# --------------------------------------------------------------------------
# 1. 迁移：v25 → v26
# --------------------------------------------------------------------------

class ReconcileMigrationTests(unittest.TestCase):
    """加三列，存量行不动。"""

    def _make_v25_db(self, path):
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '25')")
            con.execute(_V25_REQUEST_LOGS_DDL)
            con.execute(
                "INSERT INTO request_logs (time, source, path, status, outcome, duration_ms)"
                " VALUES (?, 'api', '/v1/chat/completions', 200, 'success', 12)",
                (int(time.time()),))
            con.commit()
        finally:
            con.close()

    def test_upgrade_adds_three_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            self._make_v25_db(path)
            db = Database(path)
            try:
                cols = {row[1] for row in db.query("PRAGMA table_info(request_logs)")}
                for column in ("upstream_model", "account_uid", "client_request_id"):
                    self.assertIn(column, cols, f"v26 必须加 {column} 列")
                row = db.query("SELECT * FROM request_logs")[0]
                for column in ("upstream_model", "account_uid", "client_request_id"):
                    self.assertIsNone(row[column],
                                      f"存量行的 {column} 应为 NULL（不猜、不回填）")
                version = db.query("SELECT value FROM schema_meta WHERE key='version'")[0]["value"]
                self.assertEqual(int(version), SCHEMA_VERSION)
            finally:
                db.close()

    def test_fresh_database_has_three_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "state.db")
            try:
                cols = {row[1] for row in db.query("PRAGMA table_info(request_logs)")}
                for column in ("upstream_model", "account_uid", "client_request_id"):
                    self.assertIn(column, cols)
            finally:
                db.close()


# --------------------------------------------------------------------------
# 2. 落库：三列原样进对应位置
# --------------------------------------------------------------------------

class ReconcileColumnTests(unittest.IsolatedAsyncioTestCase):
    """scope / log_context → 三列。"""

    async def _row(self, context, scope_extra=None):
        queued = []

        class FakeStore:
            def queue_request_log(self, row):
                queued.append(row)

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': b'{}', 'more_body': False})

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(m):
            pass

        scope = {'type': 'http', 'method': 'POST', 'path': '/v1/messages',
                 REQUEST_LOG_CONTEXT: context}
        scope.update(scope_extra or {})
        await MetricsMiddleware(app, RequestMetrics(), 'api', store=FakeStore())(scope, receive, send)
        return queued[0]

    async def test_three_reconcile_fields_land_in_their_own_columns(self):
        row = await self._row(
            {'account_uid': 'uid-abc'},
            {SCOPE_UPSTREAM_MODEL: 'Deepseek-V4-Pro',
             SCOPE_REQUEST_ID: '0198f0c1-uuid7-hex'})
        self.assertEqual(row['upstream_model'], 'Deepseek-V4-Pro',
                         '上游实际收到的模型名要单独记（裸名 ≠ 上游目录名）')
        self.assertEqual(row['account_uid'], 'uid-abc',
                         'uid 才是「本地备注名 ↔ 云端昵称」的对齐键')
        self.assertEqual(row['client_request_id'], '0198f0c1-uuid7-hex',
                         '我们发给上游的 X-Request-ID：没有它就永远对不上账')

    async def test_missing_values_are_null_not_empty_string(self):
        row = await self._row({})
        for column in ('upstream_model', 'account_uid', 'client_request_id'):
            self.assertIsNone(row[column], f'{column} 取不到就是 NULL')


# --------------------------------------------------------------------------
# 3. 上游视角的流水也能查（对账的另一半）
# --------------------------------------------------------------------------

class UpstreamLogEndpointTests(unittest.TestCase):
    """`GET /admin/api/logs/upstream`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.app = create_app(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        # https：管理 cookie 带 Secure，http 下浏览器语义不回传（实测 401）。
        self.client = TestClient(self.app, base_url="https://console.test")
        login = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.headers = {"X-CSRF-Token": login.json()["csrf"]}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_upstream_rows_are_shaped_for_reconciliation(self):
        store = self.app.state.store
        aid = store.save_browser_account(credential("one"), "one")["account_id"]
        store.db.save_upstream_usage([{
            "request_id": "crb-abc123", "account_id": aid, "account_name": "one",
            "credit": 1.5, "model": "deepseek-v4-flash", "client": "WorkBuddy",
            "request_time": int(time.time()), "agent_purpose": "chat",
            "input_trunc": "帮我改一下这个函数",
        }])

        r = self.client.get("/admin/api/logs/upstream", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        payload = r.json()
        self.assertEqual(payload["total"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["source"], "upstream")
        self.assertEqual(row["outcome"], "upstream_record")
        self.assertEqual(row["upstream_request_id"], "crb-abc123")
        self.assertEqual(row["model_id"], "deepseek-v4-flash")
        self.assertEqual(row["client"], "WorkBuddy")
        self.assertIsNone(row["duration_ms"], "上游记录没有耗时，显示「—」而不是 0 ms")
        self.assertEqual(row["account_uid"], "one",
                         "上游视角的行也要带 uid，才能和云端对上号")

    def test_endpoint_requires_admin(self):
        # setUp 里登录过，cookie 还在 —— 先清掉才是「未登录」这一档。
        self.client.cookies.clear()
        r = self.client.get("/admin/api/logs/upstream")
        self.assertIn(r.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()

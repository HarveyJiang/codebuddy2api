"""请求日志的错误码分类（v24：`status` 与 `error_code` 分列 + outcome 细化）。

## 为什么单开一层测试

改动前 `request_logs.status` 一列身兼两职：HTTP 状态码与上游业务码挤在
一起 —— `admin/metrics.py` 会用 SSE 里的 `error.code` **覆盖** HTTP 状态码
（11128 把 200 盖掉），于是「HTTP 200 但流内报错」的行看不出客户端拿到的
到底是什么；纯本地的 503（模型在冷却）与上游回的 503 又混作一团。

`outcome` 同时只有四类（success / stream_error / interrupted / http_error），
「限流 / 鉴权失效 / 额度耗尽 / 上游挂了 / 根本没打上游」全归成「流式错误」
或「请求失败」。看日志的人分不清「模型在冷却被跳过」和「上游报错了」——
这两件事的处理方式完全不同。

本文件钉四件事：

1. **分类优先级**：本地拒绝 > 上游排队 > 成功 > WAF > 业务码 > HTTP 状态；
2. **两列分开**：`status` 只放 HTTP 状态码，`error_code` 放业务码原样；
3. **模型冷却能落进日志**：本地拒绝（压根没打上游）此前压根不进
   `request_logs` —— 池子空、模型全冷这类事在日志里是**看不见的**；
4. **迁移 v23 → v24**：加列，存量行一律 NULL（不猜历史行的业务码）。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from admin.db import SCHEMA_VERSION, Database
from admin.metrics import MetricsMiddleware, RequestMetrics, classify_outcome
from admin.pool import AccountPool, PoolMiddleware
from admin.server import Store
from conftest import credential

# v23 的 request_logs（还没有 error_code 列）。
_V23_REQUEST_LOGS_DDL = """CREATE TABLE request_logs (
    id INTEGER PRIMARY KEY,
    time INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'api', path TEXT NOT NULL,
    status INTEGER, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL,
    key_id TEXT, account_id TEXT, account_name TEXT,
    model_id TEXT, model_name TEXT, upstream_request_id TEXT,
    backend TEXT,
    tokens_in INTEGER, tokens_out INTEGER,
    tokens_cache_read INTEGER, tokens_cache_creation INTEGER)"""


# --------------------------------------------------------------------------
# 1. 分类函数的优先级
# --------------------------------------------------------------------------

class ClassifyOutcomeTests(unittest.TestCase):
    """`classify_outcome` 的判定顺序（纯函数，不碰 IO）。"""

    def test_local_reject_wins_over_everything(self):
        """本地拒绝优先级最高：根本没打上游，别的事都往后排。

        优先级排错的后果很具体：模型冷却（本地拒绝）被归成「http_error」，
        看日志的人会以为上游挂了，去查上游；实际上是本地所有号都在冷却。
        """
        for reason in ("model_cooling", "no_account", "client_error"):
            self.assertEqual(
                classify_outcome(status=503, error_code="503", ok=False, failed=True,
                                 streaming=True, completed=False, local_reject=reason),
                reason, reason)

    def test_queued_beats_failure(self):
        """上游排队：拿到的是「你在排队中」，不是失败。"""
        self.assertEqual(
            classify_outcome(status=200, ok=False, failed=True, queued=True),
            "queue")

    def test_ok_is_success(self):
        self.assertEqual(
            classify_outcome(status=200, ok=True, completed=True, terminal=True),
            "success")

    def test_waf_code_is_its_own_class(self):
        """11128 这类通道拦截：换遍全池都会被拦，问题在出口 IP 不在账号。

        归成「鉴权失败」会让人去查账号凭据 —— 查半天发现凭据没问题。
        """
        self.assertEqual(
            classify_outcome(status=200, error_code="11128", ok=False, failed=True),
            "waf_blocked")

    def test_business_code_beats_http_status(self):
        """上游在 SSE 里报 429 时 HTTP 还是 200 —— 那**就是**限流。"""
        self.assertEqual(
            classify_outcome(status=200, error_code="429", ok=False, failed=True),
            "rate_limited")

    def test_auth_quota_and_upstream_classes(self):
        self.assertEqual(
            classify_outcome(status=401, error_code="401", ok=False), "auth_error")
        self.assertEqual(
            classify_outcome(status=403, error_code="403", ok=False), "auth_error")
        self.assertEqual(
            classify_outcome(status=402, error_code="402", ok=False), "quota_exhausted")
        self.assertEqual(
            classify_outcome(status=503, error_code="503", ok=False), "upstream_error")

    def test_non_numeric_code_falls_back_to_http_status(self):
        """业务码是文本（"USER_LIMIT"）时转不成数字 → 退回看 HTTP 状态。

        不猜：文本码不映射到任何已知类别（上游的文本码含义各不相同），
        硬映射会把「额度超了」误判成「限流」，处理方式完全不同。
        """
        self.assertEqual(
            classify_outcome(status=429, error_code="USER_LIMIT", ok=False),
            "rate_limited")   # HTTP 状态是 429，仍然是限流
        self.assertEqual(
            classify_outcome(status=200, error_code="USER_LIMIT", ok=False, failed=True),
            "stream_error")    # HTTP 是 200 且流内报错 → 流式错误

    def test_plain_http_error_when_no_business_code(self):
        self.assertEqual(
            classify_outcome(status=418, ok=False), "http_error")

    def test_stream_error_and_interrupted(self):
        self.assertEqual(
            classify_outcome(status=200, ok=False, failed=True, streaming=True,
                             completed=True, terminal=False),
            "stream_error")
        self.assertEqual(
            classify_outcome(status=200, ok=False, failed=False, streaming=True,
                             completed=False, terminal=False),
            "interrupted")


# --------------------------------------------------------------------------
# 2. status 与 error_code 分列（metrics 层）
# --------------------------------------------------------------------------

class LogColumnSplitTests(unittest.IsolatedAsyncioTestCase):
    """落库那一行：status 是 HTTP 码，error_code 是业务码。"""

    async def _row(self, context):
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

        from admin.pool import REQUEST_LOG_CONTEXT
        scope = {'type': 'http', 'method': 'POST', 'path': '/v1/messages',
                 REQUEST_LOG_CONTEXT: context}
        await MetricsMiddleware(app, RequestMetrics(), 'api', store=FakeStore())(scope, receive, send)
        return queued[0]

    async def test_business_code_does_not_overwrite_http_status(self):
        row = await self._row({'error_code': '429'})
        self.assertEqual(row['status'], 200, 'status 只放 HTTP 状态码')
        self.assertEqual(row['error_code'], '429', '业务码原样进 error_code')
        self.assertEqual(row['outcome'], 'rate_limited')

    async def test_non_numeric_code_stored_verbatim(self):
        row = await self._row({'error_code': 'USER_LIMIT'})
        self.assertEqual(row['error_code'], 'USER_LIMIT')

    async def test_missing_code_is_null_not_zero(self):
        row = await self._row({})
        self.assertIsNone(row['error_code'], '拿不到就是 NULL，不写 0')
        self.assertEqual(row['status'], 200, 'status 仍由 HTTP 状态码决定')

    async def test_model_cooling_local_reject_is_logged(self):
        row = await self._row({'local_reject': 'model_cooling'})
        self.assertEqual(row['outcome'], 'model_cooling')


# --------------------------------------------------------------------------
# 3. 端到端：模型冷却这次请求**根本没打上游**，但要落进日志
# --------------------------------------------------------------------------

class ModelCoolingLandsInLogTests(unittest.IsolatedAsyncioTestCase):
    """模型全冷 → 503 → 日志里能看到 model_cooling。

    改动前这类请求压根不进 `request_logs`：`log_context` 是在 `select()`
    **之后**才建的，池子空 / 模型全冷时它还不存在，异常直接抛出去。
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test',
                           'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.now = 1800000000
        self.pool = AccountPool(self.store, clock=lambda: self.now,
                                client_factory=lambda: httpx.Client(
                                    transport=httpx.MockTransport(lambda req: httpx.Response(200))))
        for aid, item in self.store.data['accounts'].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={'X-Domain': 'www.codebuddy.cn'})

    async def test_model_cooling_request_is_logged(self):
        # 把唯一那个号在这个模型上冷掉 → select() 必然抛 ModelCoolingError。
        self.pool.record(self.a, 429, model='step-5-preview')

        async def app(scope, receive, send):
            raise AssertionError('模型全冷时不该打到上游')

        inner = PoolMiddleware(app, self.pool)
        stack = MetricsMiddleware(inner, RequestMetrics(), source='api', store=self.store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stack),
                                     base_url='http://test') as client:
            response = await client.post('/v1/chat/completions',
                                         json={'model': 'step-5-preview'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 503)
        self.store.save()
        row = self.store.db.recent_request_logs(1)[0]
        self.assertEqual(row['outcome'], 'model_cooling')
        self.assertEqual(row['status'], 503, 'status 是本地回的 HTTP 状态码')
        self.assertEqual(row['model_id'], 'step-5-preview')


# --------------------------------------------------------------------------
# 4. 迁移 v23 → v24
# --------------------------------------------------------------------------

class ErrorCodeMigrationTests(unittest.TestCase):
    """request_logs 补 `error_code` 列（v24）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_version_is_at_least_24(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 24)

    def test_migration_adds_column_to_a_v23_database(self):
        """v23 的库升级后必须有列，且**存量行是 NULL**。

        存量行的业务码无从得知（改动前那一列被 HTTP 状态码占着），一律
        NULL —— 不拿旧的 status 反推，那会把「HTTP 200」写成「业务码 200」。
        """
        path = self.root / 'v23.db'
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)')
        con.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '23')")
        con.execute(_V23_REQUEST_LOGS_DDL)
        # 时间戳要够新：Database 启动时会按保留期（93 天）裁剪 request_logs，
        # 用远古时间戳的行会被裁掉，测的就不是迁移了。
        con.execute("INSERT INTO request_logs (time, source, path, status, outcome, duration_ms)"
                    " VALUES (?, 'api', '/v1/messages', 200, 'success', 12)",
                    (int(time.time()),))
        con.commit()
        con.close()

        db = Database(path)
        try:
            cols = {r[1] for r in db._write.execute('PRAGMA table_info(request_logs)')}
            self.assertIn('error_code', cols)
            row = db._write.execute('SELECT status, error_code FROM request_logs').fetchone()
            self.assertEqual(row[0], 200)
            self.assertIsNone(row[1], '存量行的业务码无从得知，一律 NULL')
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
        finally:
            db.close()

    def test_fresh_database_has_the_column(self):
        db = Database(self.root / 'fresh.db')
        try:
            cols = {r[1] for r in db._write.execute('PRAGMA table_info(request_logs)')}
            self.assertIn('error_code', cols)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()

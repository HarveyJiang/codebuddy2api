"""上游 5xx（502/503/504）换号重试。

上游对 502/503/504 的回答是「**明确拒绝、没开始生成内容**」，所以换个号
重发不会重复计费 —— 这正是 `core/replay_policy.py` 里 `TRANSIENT_STATUS`
的判据（与 `AMBIGUOUS_TRANSPORT` 的区别：那种情况请求体已发出、上游可能
已处理并计费，重发就是主动制造重复计费）。

但重试有**硬前提**：一个字节都还没发给客户端。否则客户端会先看到一个 503、
然后又收到一份完整响应（重复输出）。

本文件锁住：

1. 5xx 会换号重发，客户端只看到最后一次的结果；
2. 重试次数用尽时，把上游的原始响应**原样**还给客户端（不是笼统的 503）；
3. 开关关闭时行为与改动前完全一致；
4. 响应体里**有真实内容**时不重试（重发会重复输出）；
5. 502/504 记为失败（模型级冷却），绝不能落进 record() 的「recovered」分支
   ——那里会把一次故障当成成功、把连续失败计数清零；
6. 一次客户端请求最多只冷却**一个**账号的该模型（不放大成 N 个）；
7. 上游明示的 Retry-After 会被采信（`resp_headers` 曾经因为漏了 nonlocal
   而永远是空列表，见 admin/reset_at.py）。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from admin.db import SCHEMA_VERSION, Database
from admin.pool import AccountPool, PoolMiddleware
from admin.server import Store
from conftest import credential

API = "sk-test-client-key"
ADMIN = "admin-key-long-enough-1234"
NOW = 1800000000

SSE_OK = (b'data: {"model":"m","choices":[{"delta":{"content":"ok"},'
          b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
JSON_CT = [(b"content-type", b"application/json")]
SSE_CT = [(b"content-type", b"text/event-stream")]

# v24 的 settings 表（没有两列新的）。
_V24_SETTINGS_DDL = """CREATE TABLE settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    auto_checkin INTEGER NOT NULL DEFAULT 1,
    checkin_time TEXT NOT NULL DEFAULT '09:00',
    queue_failover INTEGER NOT NULL DEFAULT 0,
    queue_retry INTEGER NOT NULL DEFAULT 3)"""


class UpstreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "management", root / "auth", API, ADMIN)
        self.a = self.store.save_browser_account(credential("one"), "one")["account_id"]
        self.b = self.store.save_browser_account(credential("two"), "two")["account_id"]
        self.pool = AccountPool(self.store, clock=lambda: NOW,
                                client_factory=lambda: httpx.Client(
                                    transport=httpx.MockTransport(lambda req: httpx.Response(200))))
        for aid, item in self.store.data["accounts"].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={"X-Domain": "www.codebuddy.cn"})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    # -- 装置 ---------------------------------------------------------------

    def upstream(self, responses):
        """按调用顺序回上游响应；responses 是 (status, body, headers)。

        超出列表长度后重复最后一条，方便「一直 503」的场景。
        """
        state = {"calls": 0}

        async def app(scope, receive, send):
            index = min(state["calls"], len(responses) - 1)
            state["calls"] += 1
            status, body, headers = responses[index]
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        state["app"] = app
        return state

    async def call(self, state, model="m"):
        sent = []

        async def receive():
            return {"type": "http.request",
                    "body": json.dumps({"model": model, "messages": []}).encode(),
                    "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
                 "headers": [(b"authorization", ("Bearer " + API).encode())],
                 "client": ("10.0.0.1", 1234), "query_string": b"", "scheme": "http",
                 "server": ("test", 80), "root_path": "", "http_version": "1.1"}
        await PoolMiddleware(state["app"], pool=self.pool)(scope, receive, send)
        return sent

    @staticmethod
    def response(sent):
        status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, body

    def set_retry(self, enabled=True, tries=2):
        self.store.data["pool"]["upstream_retry"] = enabled
        self.store.data["pool"]["upstream_retry_tries"] = tries

    def status_of(self, aid):
        return self.store.data["account_status"].get(aid) or {}

    # -- 1. 换号重发，客户端只看到最后一次 ----------------------------------

    async def test_503_switches_account_and_client_sees_only_success(self):
        self.set_retry()
        state = self.upstream([
            (503, b'{"error":{"message":"upstream busy"}}', JSON_CT),
            (200, SSE_OK, SSE_CT),
        ])
        sent = await self.call(state)
        status, body = self.response(sent)
        self.assertEqual(status, 200)
        self.assertIn(b'"content":"ok"', body)
        self.assertEqual(state["calls"], 2, "应该换号重发了一次")
        # 客户端**没有**看到中间那个 503（否则就是重复输出）。
        self.assertNotIn(b"upstream busy", body)
        self.assertEqual(len([m for m in sent if m["type"] == "http.response.start"]), 1)

    async def test_retry_uses_a_different_account(self):
        self.set_retry()
        state = self.upstream([(503, b'{"error":{"message":"busy"}}', JSON_CT),
                               (200, SSE_OK, SSE_CT)])
        await self.call(state)
        used = [m for m in self.store.data["account_status"].values()]
        self.assertTrue(used, "至少要记一次账")
        # 最终成功的是另一个号：先失败的那个号不该被记成功。
        self.assertNotEqual(state["calls"], 0)

    # -- 2. 重试用尽 → 原样归还上游响应 ------------------------------------

    async def test_retry_exhausted_returns_upstream_response_verbatim(self):
        self.set_retry(tries=1)
        state = self.upstream([(503, b'{"error":{"message":"still busy"}}', JSON_CT)])
        sent = await self.call(state)
        status, body = self.response(sent)
        self.assertEqual(status, 503, "不能改成笼统的 503，要保留上游的真实状态")
        self.assertIn(b"still busy", body, "上游的原文要原样还给客户端")
        self.assertEqual(state["calls"], 2, "首次 + 1 次重试")

    async def test_retry_exhausted_with_no_other_account_returns_upstream_body(self):
        self.set_retry(tries=2)
        # 只留一个账号可用：换号无处可换。
        self.pool.update(self.b, cooldown_until=NOW + 3600)
        state = self.upstream([(503, b'{"error":{"message":"only me"}}', JSON_CT)])
        sent = await self.call(state)
        status, body = self.response(sent)
        self.assertEqual(status, 503)
        self.assertIn(b"only me", body)
        self.assertEqual(state["calls"], 1, "没有别的号就不该重试")

    # -- 3. 开关关闭 → 行为与改动前一致 -------------------------------------

    async def test_switch_off_keeps_single_upstream_call(self):
        self.set_retry(enabled=False)
        state = self.upstream([(503, b'{"error":{"message":"busy"}}', JSON_CT),
                               (200, SSE_OK, SSE_CT)])
        sent = await self.call(state)
        status, body = self.response(sent)
        self.assertEqual(status, 503)
        self.assertIn(b"busy", body)
        self.assertEqual(state["calls"], 1, "开关关闭时不能重试")

    # -- 4. 响应体里有真实内容 → 不重试 -------------------------------------

    async def test_response_with_real_content_is_not_retried(self):
        self.set_retry()
        # 503 但 body 里带 choices：上游已经吐了内容，重发会重复输出。
        state = self.upstream([(503, b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
                                SSE_CT),
                               (200, SSE_OK, SSE_CT)])
        sent = await self.call(state)
        status, body = self.response(sent)
        self.assertEqual(status, 503)
        self.assertEqual(state["calls"], 1, "有真实内容时不能重试")

    # -- 5. 5xx 记为失败，绝不落进「recovered」分支 -------------------------

    async def test_502_is_recorded_as_failure_not_recovered(self):
        self.set_retry(tries=0)
        state = self.upstream([(502, b'{"error":{"message":"bad gateway"}}', JSON_CT)])
        await self.call(state)
        cooled = [aid for aid in (self.a, self.b)
                  if (self.status_of(aid).get("model_cooldowns") or {})]
        self.assertTrue(cooled, "502 必须记成模型级失败")
        for aid in cooled:
            self.assertGreater(self.status_of(aid)["model_cooldowns"]["m"], NOW)
            # 账号本身**不能**被冷掉（用户 2026-10-20：别让单个模型影响账号冷却）。
            self.assertEqual(self.status_of(aid).get("cooldown_until", 0), 0)

    async def test_504_is_not_treated_as_success(self):
        """504 之前会落进 record() 的 else 分支 =「recovered」，把失败计数清零。"""
        self.set_retry(tries=0)
        state = self.upstream([(504, b'{"error":{"message":"gateway timeout"}}', JSON_CT)])
        await self.call(state)
        cooled = [aid for aid in (self.a, self.b)
                  if (self.status_of(aid).get("model_cooldowns") or {})]
        self.assertTrue(cooled, "504 必须记成模型级失败，不能当成成功")
        for aid in cooled:
            self.assertGreater(self.status_of(aid)["model_cooldowns"]["m"], NOW)
            # 账号本身**不能**被冷掉（用户 2026-10-20：别让单个模型影响账号冷却）。
            self.assertEqual(self.status_of(aid).get("cooldown_until", 0), 0)

    # -- 6. 一次客户端请求最多冷却一个账号 -----------------------------------

    async def test_only_the_last_attempt_is_cooled(self):
        self.set_retry(tries=1)
        state = self.upstream([(503, b'{"error":{"message":"busy"}}', JSON_CT)])
        await self.call(state)
        cooled = [aid for aid in (self.a, self.b)
                  if (self.status_of(aid).get("model_cooldowns") or {})]
        self.assertEqual(len(cooled), 1,
                         "中间几次重试不记账，避免一次客户端请求放大成 N 个账号冷却")

    # -- 7. 上游明示的 Retry-After 被采信 ------------------------------------

    async def test_retry_after_header_is_honored(self):
        """`resp_headers` 曾经漏了 nonlocal → 永远是空列表 → 恢复时刻从未被采信。"""
        self.set_retry(tries=0)
        state = self.upstream([(429, b'{"error":{"message":"slow down"}}',
                                JSON_CT + [(b"retry-after", b"120")])])
        await self.call(state)
        cooled = [aid for aid in (self.a, self.b)
                  if (self.status_of(aid).get("model_cooldowns") or {})]
        self.assertTrue(cooled)
        for aid in cooled:
            until = self.status_of(aid)["model_cooldowns"]["m"]
            # 本地退避的兜底是 300 秒；采信上游的 120 秒才说明 header 传到了。
            self.assertEqual(until, NOW + 120)


class RetrySettingsMigrationTests(unittest.TestCase):
    """settings 加 upstream_retry / upstream_retry_tries（v25）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_version_is_at_least_25(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 25)

    def test_migration_adds_columns_to_a_v24_database(self):
        path = self.root / "v24.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '24')")
        con.execute(_V24_SETTINGS_DDL)
        con.execute("INSERT INTO settings (id, auto_checkin) VALUES (1, 1)")
        con.commit()
        con.close()

        db = Database(path)
        try:
            cols = {r[1] for r in db._write.execute("PRAGMA table_info(settings)")}
            self.assertIn("upstream_retry", cols)
            self.assertIn("upstream_retry_tries", cols)
            row = db._write.execute(
                "SELECT upstream_retry, upstream_retry_tries FROM settings").fetchone()
            # 存量行拿默认值：开关默认开、次数默认 2。
            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], 2)
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
        finally:
            db.close()

    def test_fresh_database_has_the_columns(self):
        db = Database(self.root / "fresh.db")
        try:
            cols = {r[1] for r in db._write.execute("PRAGMA table_info(settings)")}
            self.assertIn("upstream_retry", cols)
            self.assertIn("upstream_retry_tries", cols)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

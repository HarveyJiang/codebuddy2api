"""Trae 账号的上游用量同步：走完整链路（pool → 后端声明 → 后端实现 → 落库）。

守住三件事：
1. Trae 账号**进**同步队列（它有自己的用量接口，不是「只有默认后端能对账」）；
2. 拉回来的是 Trae 形状的响应（无 code 字段、按 session 聚合），落库后
   字段仍与 upstream_request_usage 表一一对应；
3. 同一会话重复拉到时更新同一条（不因 usage_time 变化而多出一行、重复计费）。
"""
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import httpx

from admin.pool import CN, AccountPool
from admin.server import Store

from core.backends import trae as T


def trae_credential(uid="trae-user"):
    return {
        "backend": "trae",
        "account": {"uid": uid, "nickname": "Trae", "enterpriseId": "ent"},
        "auth": {
            "accessToken": "TRAE-ACCESS",
            "refreshToken": "TRAE-REF",
            "expiresAt": int(time.time()) + 30 * 86400,
            "domain": "trae.cn",
            "apiHost": T.OAUTH_HOST,
            "machineId": "m" * 32,
            "deviceId": "d" * 32,
        },
    }


def _session(session_id="sess-1", usage_time=1792073300, credits=0.642):
    return {
        "session_id": session_id,
        "usage_time": usage_time,
        "model_name": "DeepSeek-V4.1-Flash",
        "mode": "",
        "credits_float": credits,
        "user_input_preview": "用户：请只回复：连接成功",
        "usage_group_details": [
            {"group_key": "default", "model_display_name": "DeepSeek-V4.1-Flash",
             "credits_float": credits},
        ],
    }


class TraeUsageSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "management").mkdir()
        (root / "auth").mkdir()
        self.store = Store(root / "management", root / "auth", "api-test",
                           "admin-long-enough-test-key")
        self.tr = self.store.save_browser_account(trae_credential(), "trae")["account_id"]
        self.now = int(time.time())
        self.requests = []
        self.payload = {"total": 1, "user_usage_group_by_sessions": [_session()]}
        self.pool = AccountPool(self.store, clock=lambda: self.now,
                                client_factory=lambda: httpx.Client(
                                    transport=httpx.MockTransport(self._respond)))
        manager = self.store.manager_for(self.tr, self.store.data["accounts"][self.tr])
        manager.ug_headers = Mock(return_value={"Cloud-IDE-JWT": "jwt"})

    def _respond(self, req):
        self.requests.append(req)
        return httpx.Response(200, json=self.payload)

    def test_sync_pulls_trae_usage_and_writes_rows(self):
        count = self.pool.sync_upstream_usage(self.tr)
        self.assertEqual(count, 1)
        # 打的确实是 Trae 自己的用量接口（api.trae.cn，不是 WorkBuddy 的计费域）。
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(str(self.requests[0].url),
                         T.UG_HOST + T.EP_USAGE_GROUP)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["usage_type"], [7])
        self.assertEqual(body["page_num"], 1)

        rows = {r["request_id"]: r for r in self.store.db.load_upstream_usage(self.tr)}
        self.assertEqual(set(rows), {"trae-sess-1-default"})
        row = rows["trae-sess-1-default"]
        self.assertAlmostEqual(row["credit"], 0.642, places=4)
        self.assertEqual(row["model"], "DeepSeek-V4.1-Flash")
        self.assertEqual(row["client"], "trae")
        self.assertEqual(row["request_time"], 1792073300)
        # 请求内容摘要有值（前端「请求内容」列直接用它）。
        self.assertEqual(row["input_trunc"], "用户：请只回复：连接成功")
        # 同步成功写留痕。
        self.assertEqual(self.store.data["account_status"][self.tr]["usage_synced_at"],
                         int(self.now))

    def test_same_session_growth_updates_one_row(self):
        """同一会话用量增长（usage_time 变、credits 变大）时仍是同一条 ——
        否则积分会被重复计一次。"""
        self.pool.sync_upstream_usage(self.tr)
        self.payload = {"total": 1,
                        "user_usage_group_by_sessions": [_session(usage_time=1792080000,
                                                                  credits=1.5)]}
        # 手动同步要过 60 秒节流，这里直接走内部方法。
        self.pool._pull_upstream_usage(
            self.tr, self.store.data["accounts"][self.tr],
            datetime.fromtimestamp(self.now - 86400, CN),
            datetime.fromtimestamp(self.now, CN))
        rows = self.store.db.load_upstream_usage(self.tr)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["credit"], 1.5, places=4)
        # 时间跟着最新的 usage_time 走（这一行要落在正确的天里）。
        self.assertEqual(rows[0]["request_time"], 1792080000)

    async def test_trae_account_enters_the_daily_sync_queue(self):
        """Trae 账号进每日同步队列（它有上游用量接口）。"""
        result = await self.pool.sync_upstream_usage_batch()
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertIsNone(self.store.data["account_status"][self.tr].get("usage_sync_error"))

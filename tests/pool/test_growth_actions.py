"""成长任务在 pool 层的分派：动作路由、错误翻法、每日去重。

接口契约在 tests/protocol/test_growth_fetchers.py 守；这里守的是
**pool 这一层**的三件事：

- 动作名 → 后端 fetcher 的分派（``GROWTH_ACTIONS`` 声明表）；
- 后端没声明这个能力时，NotImplementedError 要翻成「该后端暂不支持成长任务」
  ——它是 RuntimeError 的子类，漏接住就会变成「操作失败」这种没用的文案；
- 「活跃每号每天一次」「旅行一天一轮」的去重落在 accounts 的状态列上。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from admin.pool import AccountPool
from admin.server import Store
from conftest import credential


class GrowthActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test', 'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.now = 1800000000
        self.calls = []

        def respond(req):
            self.calls.append((req.method, str(req.url), req.content))
            if req.url.path.endswith("/activity/growth/tasks"):
                return httpx.Response(200, json={"data": {"tasks": [
                    {"task_code": "t1", "title": "每日登录", "accept_status": "not_accepted"}]}})
            if req.url.path.endswith("/tasks/accept"):
                return httpx.Response(200, json={"data": {"results": [
                    {"task_code": "t1", "status": "success", "message": ""}]}})
            if req.url.path.endswith("/tasks/t1/claim"):
                return httpx.Response(200, json={"data": {"already_claimed": False, "credit": 12.5}})
            if req.url.path.endswith("/buddy/travel/config"):
                return httpx.Response(200, json={"data": {"locations": [
                    {"id": 4, "name": "古镇客栈", "duration_hours": 12}]}})
            if req.url.path.endswith("/buddy/travel/status"):
                return httpx.Response(200, json={"data": {"state": "idle"}})
            if req.url.path.endswith("/buddy/travel/depart"):
                return httpx.Response(200, json={"data": {"location": {"name": "古镇客栈"}}})
            if req.url.path.endswith("/buddy/travel/claim"):
                return httpx.Response(200, json={"data": {"location": {"name": "古镇客栈"}}})
            if req.url.path.endswith("/v2/report"):
                return httpx.Response(200, json={"code": 0})
            return httpx.Response(404)

        self.pool = AccountPool(self.store, clock=lambda: self.now,
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
        for aid, item in self.store.data['accounts'].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={'X-Domain': 'www.codebuddy.cn', 'Authorization': 'Bearer t'})

    def test_unknown_action_is_rejected(self):
        result = self.pool.growth(self.a, "nope")
        self.assertFalse(result["ok"])
        self.assertIn("未知的成长任务动作", result["message"])

    def test_tasks_action_lists_upstream_tasks(self):
        result = self.pool.growth(self.a, "tasks")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"][0]["code"], "t1")
        self.assertTrue(any("/activity/growth/tasks" in url for _, url, _ in self.calls))

    def test_accept_and_claim_reach_the_right_endpoints(self):
        result = self.pool.growth(self.a, "accept", task_codes=["t1"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["results"][0]["status"], "success")

        result = self.pool.growth(self.a, "claim", task_code="t1")
        self.assertTrue(result["ok"], result)
        # 实发积分优先于任务列表里的配置值。
        self.assertEqual(result["data"]["credit"], 12.5)

    def test_activity_report_records_the_date(self):
        result = self.pool.growth(self.a, "activity")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.pool.snapshot(self.a)[1]["activity_date"], "2027-01-15")
        # 每号每天一次：状态列记住今天报过了。
        self.assertTrue(self.pool.rows(self.store.account_rows())[0]["today_activity_reported"])

    def test_travel_status_is_read_only(self):
        result = self.pool.growth(self.a, "travel_status")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["state"], "idle")

    def test_unsupported_backend_gets_a_readable_message(self):
        """后端没声明成长能力 → NotImplementedError 要翻成人话，不是「操作失败」。"""
        from core.backends.registry import spec_for
        spec = spec_for("workbuddy")
        spec.travel_fetcher = ""
        try:
            result = self.pool.growth(self.a, "travel_status")
        finally:
            spec.travel_fetcher = "fetch_travel"
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "该后端暂不支持成长任务")

    async def test_tick_growth_runs_travel_and_activity_once_per_day(self):
        with self.store.lock:
            self.store.data["pool"]["growth_travel_hours"] = "9"
            self.store.data["pool"]["growth_activity_hours"] = "9"
        now = 1800000000
        # 2027-01-15 08:00 UTC → 北京 16:00，小时 16 不在配置里。
        self.pool.clock = lambda: now
        await self.pool._tick_growth(dict(self.store.data["pool"]), self._now(16), "2027-01-15")
        self.assertEqual(self.calls, [])

        # 命中时点：跑旅行与活跃各一次。
        self.calls = []
        await self.pool._tick_growth(dict(self.store.data["pool"]), self._now(9), "2027-01-15")
        paths = [url for _, url, _ in self.calls]
        self.assertTrue(any("/buddy/travel/status" in p for p in paths), paths)
        self.assertTrue(any("/v2/report" in p for p in paths), paths)
        # 旅行的出发（idle → depart）。
        self.assertTrue(any("/buddy/travel/depart" in p for p in paths), paths)
        # 去重：今天跑过了就不再跑。
        status = self.pool.snapshot(self.a)[1]
        self.assertEqual(status["travel_date"], "2027-01-15")
        self.calls = []
        await self.pool._tick_growth(dict(self.store.data["pool"]), self._now(9), "2027-01-15")
        self.assertEqual(self.calls, [])

    @staticmethod
    def _now(hour):
        from datetime import datetime, timedelta, timezone
        cn = timezone(timedelta(hours=8))
        return datetime(2027, 1, 15, hour, 0, 0, tzinfo=cn)


if __name__ == "__main__":
    unittest.main()

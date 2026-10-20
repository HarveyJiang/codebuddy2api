"""成长中心的四个 fetcher：任务列表 / 接单领奖 / 旅行 / 活跃上报。

这些接口是本项目从三个参考项目（linguo2625469/workbuddy2api-panel、
88lin/workbuddy-auto-signin）逆向出来的，**契约细节极容易记错**，所以
每条都单独钉一个用例：

- 成长中心**没有 code 信封**（与 billing 域不同）：成功就是 200 + JSON。
- 接单的 body 是**复数数组** ``{"task_codes": [...]}``，单数一律 400。
- 领奖走 **web 域**（``www.workbuddy.cn``）、**路径带 code**、body 空；
  CLI 域的 ``/reward/claim``（code 放 body）实测恒 400，不能用。
- 领奖的头缺 ``x-client-platform: web`` / growth-center 的 Referer，
  上游会回 task not found。
- 旅行的路径前缀两家参考项目不一致（一家带 /v2 一家不带）→ 先试不带，
  **仅 404 时**再试带 /v2 的。
- 活跃上报打 **billing 域**（``www.codebuddy.cn/v2/report``，不是
  workbuddy.cn），body 是**数组**，且 userId 必填（缺了上游 200 静默丢弃）。
"""
from __future__ import annotations

import json
import unittest

import httpx

from core.backends import workbuddy as W
from core.backends.base import BackendUsageError


class _FakeManager:
    """凭据管理器替身：只提供 fetcher 用到的方法。"""

    def __init__(self, uid="u-1"):
        self.uid = uid
        self.headers_calls = 0

    def get_headers(self):
        self.headers_calls += 1
        return {"Authorization": "Bearer t", "X-User-Id": self.uid,
                "X-CodeBuddy-Request": "1"}

    def summary(self):
        return {"uid": self.uid, "nickname": "n"}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class GrowthTaskListTests(unittest.TestCase):
    def test_lists_and_normalizes_tasks(self):
        seen = {}

        def respond(req):
            seen["url"] = str(req.url)
            seen["headers"] = dict(req.headers)
            return httpx.Response(200, json={"data": {"tasks": [
                {"task_code": "t1", "title": "每日登录", "description": "d",
                 "reward_credit": 12.5, "reward_energy": 3, "accept_status": "not_accepted",
                 "locked": False, "progress": {"current": 0, "target": 1},
                 "jump_url": "app://x"},
                {"task_code": "t2", "title": "写代码", "accept_status": "completed",
                 "locked": True, "reward_credit": 8},
            ]}})

        with _client(respond) as client:
            rows = W.fetch_growth_tasks(_FakeManager(), client)

        self.assertEqual(seen["url"], "https://copilot.tencent.com/activity/growth/tasks")
        # 官方客户端的固定头：少一个上游就当未授权。
        self.assertEqual(seen["headers"]["x-requested-with"], "XMLHttpRequest")
        self.assertEqual(seen["headers"]["x-codebuddy-request"], "1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["code"], "t1")
        self.assertEqual(rows[0]["title"], "每日登录")
        self.assertEqual(rows[0]["status"], "not_accepted")
        self.assertEqual(rows[0]["target"], 1)
        self.assertFalse(rows[0]["locked"])
        self.assertTrue(rows[1]["locked"])
        self.assertEqual(rows[1]["status"], "completed")

    def test_rejects_malformed_payload(self):
        def respond(req):
            return httpx.Response(200, json={"data": {"tasks": "not-a-list"}})

        with _client(respond) as client:
            with self.assertRaises(BackendUsageError):
                W.fetch_growth_tasks(_FakeManager(), client)

    def test_http_error_becomes_backend_usage_error(self):
        def respond(req):
            return httpx.Response(503, text="boom")

        with _client(respond) as client:
            with self.assertRaises(BackendUsageError) as ctx:
                W.fetch_growth_tasks(_FakeManager(), client)
        self.assertEqual(ctx.exception.status, 503)


class GrowthTaskActionTests(unittest.TestCase):
    def test_accept_uses_plural_array_body(self):
        seen = {}

        def respond(req):
            seen["url"] = str(req.url)
            seen["body"] = json.loads(req.content)
            seen["method"] = req.method
            return httpx.Response(200, json={"data": {"results": [
                {"task_code": "t1", "status": "success", "message": ""},
                {"task_code": "t2", "status": "failed",
                 "message": "prerequisite not met: first_buddy"},
            ]}})

        with _client(respond) as client:
            result = W.fetch_growth_task_action(
                _FakeManager(), client, None, "accept", task_codes=["t1", "t2"])

        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["url"].endswith("/activity/growth/tasks/accept"))
        # 复数数组：单数 {"task_code": x} 新服务端一律 400。
        self.assertEqual(seen["body"], {"task_codes": ["t1", "t2"]})
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][1]["status"], "failed")

    def test_accept_requires_codes(self):
        with _client(lambda req: httpx.Response(200, json={})) as client:
            with self.assertRaises(BackendUsageError):
                W.fetch_growth_task_action(_FakeManager(), client, None, "accept")

    def test_claim_uses_web_domain_path_and_web_headers(self):
        seen = {}

        def respond(req):
            seen["url"] = str(req.url)
            seen["headers"] = dict(req.headers)
            seen["body"] = req.content
            seen["method"] = req.method
            return httpx.Response(200, json={"data": {
                "already_claimed": False, "credit": 12.5, "energy": 3}})

        with _client(respond) as client:
            result = W.fetch_growth_task_action(
                _FakeManager(), client, None, "claim", task_code="t1")

        # 领奖走 web 域：路径带 code、body 空。
        self.assertEqual(seen["url"], "https://www.workbuddy.cn/activity/growth/tasks/t1/claim")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(json.loads(seen["body"]), {})
        # 少了这两个头上游回 task not found。
        self.assertEqual(seen["headers"]["origin"], "https://www.workbuddy.cn")
        self.assertEqual(seen["headers"]["referer"],
                         "https://www.workbuddy.cn/profile/growth-center")
        self.assertEqual(seen["headers"]["x-client-platform"], "web")
        # 实发值优先于列表里的活动配置值。
        self.assertEqual(result["credit"], 12.5)
        self.assertEqual(result["energy"], 3)
        self.assertFalse(result["already_claimed"])

    def test_unknown_action_raises(self):
        with _client(lambda req: httpx.Response(200, json={})) as client:
            with self.assertRaises(BackendUsageError):
                W.fetch_growth_task_action(_FakeManager(), client, None, "nope")


class TravelTests(unittest.TestCase):
    def test_status_normalizes_state(self):
        seen = {}

        def respond(req):
            seen["url"] = str(req.url)
            return httpx.Response(200, json={"data": {
                "state": "traveling", "record_id": 42, "location": "古镇客栈",
                "arrive_at": 1792073300, "server_now": 1792070000,
                "daily_limit_reached": False}})

        with _client(respond) as client:
            result = W.fetch_travel(_FakeManager(), client, None, "status")

        # 先试不带 /v2 的前缀（另一家参考项目带 /v2）。
        self.assertEqual(seen["url"],
                         "https://copilot.tencent.com/activity/growth/buddy/travel/status")
        self.assertEqual(result["state"], "traveling")
        self.assertEqual(result["record_id"], 42)
        self.assertEqual(result["location"], "古镇客栈")
        self.assertFalse(result["daily_limit_reached"])

    def test_status_falls_back_to_v2_prefix_on_404(self):
        urls = []

        def respond(req):
            urls.append(str(req.url))
            if "/v2/activity/growth/" not in str(req.url):
                return httpx.Response(404, text="nope")
            return httpx.Response(200, json={"data": {"state": "idle"}})

        with _client(respond) as client:
            result = W.fetch_travel(_FakeManager(), client, None, "status")

        self.assertEqual(len(urls), 2)
        self.assertIn("/v2/activity/growth/buddy/travel/status", urls[1])
        self.assertEqual(result["state"], "idle")

    def test_depart_takes_first_location_from_config(self):
        calls = []

        def respond(req):
            calls.append((str(req.url), json.loads(req.content or b"{}")))
            if req.url.path.endswith("/travel/config"):
                return httpx.Response(200, json={"data": {"locations": [
                    {"id": 4, "name": "古镇客栈", "duration_hours": 12},
                    {"id": 5, "name": "雪山", "duration_hours": 24}]}})
            return httpx.Response(200, json={"data": {
                "location": {"name": "古镇客栈"}, "duration_hours": 12,
                "arrive_at": 1792073300}})

        with _client(respond) as client:
            result = W.fetch_travel(_FakeManager(), client, None, "depart")

        self.assertEqual(calls[0][0].endswith("/buddy/travel/config"), True)
        self.assertEqual(calls[1][1], {"location_id": 4})
        self.assertEqual(result["location"], "古镇客栈")
        self.assertEqual(result["duration_hours"], 12)

    def test_claim_requires_record_id(self):
        with _client(lambda req: httpx.Response(200, json={})) as client:
            with self.assertRaises(BackendUsageError):
                W.fetch_travel(_FakeManager(), client, None, "claim")

    def test_claim_posts_record_id(self):
        seen = {}

        def respond(req):
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"data": {"reward_credit": 6.5}})

        with _client(respond) as client:
            result = W.fetch_travel(_FakeManager(), client, None, "claim", record_id=42)

        self.assertEqual(seen["body"], {"record_id": 42})
        self.assertEqual(result["reward_credit"], 6.5)


class ActivityReportTests(unittest.TestCase):
    def test_posts_array_event_with_user_id(self):
        seen = {}

        def respond(req):
            seen["url"] = str(req.url)
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"code": 0})

        with _client(respond) as client:
            result = W.fetch_activity_report(_FakeManager("uid-9"), client)

        # billing 域，不是 workbuddy.cn。
        self.assertEqual(seen["url"], "https://www.codebuddy.cn/v2/report")
        # body 是数组：一条事件一个对象。
        self.assertIsInstance(seen["body"], list)
        self.assertEqual(len(seen["body"]), 1)
        event = seen["body"][0]
        self.assertEqual(event["eventCode"], "chat_request_send")
        self.assertEqual(event["userId"], "uid-9")
        self.assertEqual(event["agentName"], "default")
        self.assertEqual(event["agentType"], "conversation")
        # 字段全量照抄客户端事件：不能用最小三字段。
        for field in ("timestamp", "mode", "conversationId", "requestId", "inputLength",
                      "requestModelId", "mentionContexts", "knowledgeId", "presentAt",
                      "rootRequestId", "parentConversationID"):
            self.assertIn(field, event)
        self.assertEqual(result["conversation_id"], event["conversationId"])

    def test_missing_user_id_raises_before_request(self):
        called = []

        def respond(req):
            called.append(req)
            return httpx.Response(200, json={})

        with _client(respond) as client:
            with self.assertRaises(BackendUsageError):
                W.fetch_activity_report(_FakeManager(""), client)
        # 缺 userId 上游会 200 静默丢弃，必须在本地就挡住。
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()

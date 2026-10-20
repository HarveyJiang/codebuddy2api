"""各后端自己的「上游用量明细」拉取实现。

两套接口的差别全在这里守住（它们是本项目最难猜对的部分）：
- WorkBuddy：计费域、驼峰 pageNum/pageSize、响应被 code==0 包裹、
  requestTime 是**北京时间无时区**字符串、行粒度＝单次请求；
- Trae：api.trae.cn、下划线 page_num/page_size、响应**没有 code 字段**、
  行粒度＝会话（会话内再按 usage_group_details 分组）。

还守住两条口径：
- 归一化行的字段名与 upstream_request_usage 表一一对应；
- Trae 的 request_id 用「会话 id + 分组 key」而不是含 usage_time ——
  否则同一会话随用量增长会不断变成新行，积分被重复计。
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from core.backends import registry
from core.backends import trae as T
from core.backends import workbuddy as W
from core.backends.base import BackendUsageError

CN = timezone(timedelta(hours=8))
START = datetime(2026, 10, 1, 0, 0, 0, tzinfo=CN)
END = datetime(2026, 10, 8, 0, 0, 0, tzinfo=CN)


class _FakeManager:
    """只提供两个 fetcher 用到的方法。"""

    def __init__(self):
        self.headers_calls = 0

    def get_headers(self):
        self.headers_calls += 1
        return {"X-Domain": "www.codebuddy.cn"}

    def ug_headers(self):
        self.headers_calls += 1
        return {"Cloud-IDE-JWT": "jwt", "x-app-type": "trae"}


class WorkBuddyUsageFetchTests(unittest.TestCase):
    def test_paginates_and_normalizes(self):
        pages = []

        def respond(req):
            body = json.loads(req.content)
            pages.append((body["pageNum"], body["pageSize"]))
            if body["pageNum"] == 1:
                rows = [
                    {"requestId": "crb-1", "credit": "0", "model": "step-5-preview",
                     "client": "WorkBuddy", "requestTime": "2026-10-04 09:30:00",
                     "agentPurpose": "chat", "inputTrunc": "帮我改一下",
                     "input": "完整正文不该落库"},
                    {"requestId": "crb-2", "credit": "12.5", "model": "hy3",
                     "client": "WorkBuddy", "requestTime": "2026-10-04 10:00:00"},
                ]
                total = 3
            else:
                rows = [{"requestId": "crb-3", "credit": "1", "model": "hy3",
                         "client": "WorkBuddy", "requestTime": "2026-10-04 11:00:00"}]
                total = 3
            return httpx.Response(200, json={"code": 0, "data": {"total": total, "data": rows}})

        manager = _FakeManager()
        client = httpx.Client(transport=httpx.MockTransport(respond))
        rows = W.fetch_upstream_usage(manager, client, START, END)

        # 驼峰分页参数 + 按 total 翻到第二页即停。
        self.assertEqual([p[0] for p in pages], [1, 2])
        self.assertTrue(all(p[1] == 100 for p in pages))
        self.assertEqual([r["request_id"] for r in rows], ["crb-1", "crb-2", "crb-3"])
        # requestTime 无时区标记，按北京时间解析。
        self.assertEqual(rows[0]["request_time"],
                         int(datetime(2026, 10, 4, 9, 30, tzinfo=CN).timestamp()))
        self.assertEqual(rows[0]["model"], "step-5-preview")
        self.assertEqual(rows[0]["agent_purpose"], "chat")
        self.assertEqual(rows[0]["input_trunc"], "帮我改一下")
        # 上游没给 inputTrunc 时落 None（不是空串）。
        self.assertIsNone(rows[1]["input_trunc"])
        self.assertEqual(rows[1]["credit"], "12.5")
        # 走的是凭据管理器的头（chat 与账单共用同一套凭据）。
        self.assertEqual(manager.headers_calls, 1)

    def test_request_times_are_sent_as_beijing_strings(self):
        seen = {}

        def respond(req):
            seen.update(json.loads(req.content))
            return httpx.Response(200, json={"code": 0, "data": {"total": 0, "data": []}})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        W.fetch_upstream_usage(_FakeManager(), client, START, END)
        self.assertEqual(seen["startTime"], "2026-10-01 00:00:00")
        self.assertEqual(seen["endTime"], "2026-10-08 00:00:00")

    def test_failures_raise_usage_error(self):
        cases = [
            (httpx.Response(500), "上游用量接口请求失败"),
            (httpx.Response(200, json={"code": 1}), "上游未返回用量明细"),
            (httpx.Response(200, content=b"not json"), "上游用量接口返回格式异常"),
            (httpx.Response(200, json={"code": 0, "data": {"total": 1, "data": "x"}}),
             "上游用量明细格式异常"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                client = httpx.Client(transport=httpx.MockTransport(lambda req: response))
                with self.assertRaises(BackendUsageError) as ctx:
                    W.fetch_upstream_usage(_FakeManager(), client, START, END)
                self.assertIn(message, str(ctx.exception))

    def test_rows_without_id_or_time_are_dropped(self):
        def respond(req):
            return httpx.Response(200, json={"code": 0, "data": {"total": 2, "data": [
                {"requestId": "", "credit": "1", "requestTime": "2026-10-04 09:30:00"},
                {"requestId": "crb-x", "credit": "1", "requestTime": "不是时间"},
            ]}})
        client = httpx.Client(transport=httpx.MockTransport(respond))
        rows = W.fetch_upstream_usage(_FakeManager(), client, START, END)
        self.assertEqual(rows, [])


class TraeUsageFetchTests(unittest.TestCase):
    SESSION = {
        "session_id": "6ad0de4e67e307253f910922",
        "usage_time": 1792073300,
        "model_name": "DeepSeek-V4.1-Flash",
        "mode": "",
        "credits_float": 0.642,
        "user_input_preview": "用户：请只回复：连接成功",
        "usage_group_details": [
            {"group_key": "default", "model_display_name": "DeepSeek-V4.1-Flash",
             "credits_float": 0.642},
        ],
    }

    def test_normalizes_one_row_per_group(self):
        seen = {}

        def respond(req):
            seen.update(json.loads(req.content))
            return httpx.Response(200, json={"total": 1,
                                             "user_usage_group_by_sessions": [self.SESSION]})
        manager = _FakeManager()
        client = httpx.Client(transport=httpx.MockTransport(respond))
        rows = T.fetch_upstream_usage(manager, client, START, END)

        # 下划线分页参数 + epoch 秒时间窗 + usage_type。
        self.assertEqual(seen["page_num"], 1)
        self.assertEqual(seen["page_size"], 20)
        self.assertEqual(seen["usage_type"], [7])
        # 窗口不足 30 天时会被撑宽（会话级聚合，窄窗口拿到的积分更小）。
        self.assertEqual(seen["start_time"],
                         int((END - timedelta(days=30)).timestamp()))
        self.assertEqual(seen["end_time"], int(END.timestamp()))
        # 用的是 ug 头（api.trae.cn 专用），不是 chat 那套。
        self.assertEqual(manager.headers_calls, 1)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["request_id"], "trae-6ad0de4e67e307253f910922-default")
        self.assertEqual(row["credit"], 0.642)
        self.assertEqual(row["model"], "DeepSeek-V4.1-Flash")
        self.assertEqual(row["client"], "trae")
        self.assertEqual(row["request_time"], 1792073300)
        self.assertEqual(row["agent_purpose"], "trae")
        self.assertEqual(row["input_trunc"], "用户：请只回复：连接成功")

    def test_request_id_ignores_usage_time_so_growth_does_not_double_count(self):
        """同一会话随用量增长时 usage_time 会变；request_id 若含它就会变成新行，
        积分被重复计。"""
        first = dict(self.SESSION, usage_time=1792073300)
        second = dict(self.SESSION, usage_time=1792080000, credits_float=1.5,
                      usage_group_details=[{"group_key": "default",
                                            "model_display_name": "DeepSeek-V4.1-Flash",
                                            "credits_float": 1.5}])
        for payload, label in (((first,), "第一次"), ((second,), "第二次")):
            client = httpx.Client(transport=httpx.MockTransport(
                lambda req, p=payload: httpx.Response(200, json={"total": 1,
                                                                "user_usage_group_by_sessions": list(p)})))
            rows = T.fetch_upstream_usage(_FakeManager(), client, START, END)
            self.assertEqual(rows[0]["request_id"], "trae-6ad0de4e67e307253f910922-default", label)

    def test_multiple_groups_become_multiple_rows(self):
        session = dict(self.SESSION, usage_group_details=[
            {"group_key": "default", "model_display_name": "DeepSeek-V4.1-Flash",
             "credits_float": 0.5},
            {"group_key": "agent", "model_display_name": "GLM-5.3", "credits_float": 0.25},
        ])
        client = httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"total": 1,
                                                 "user_usage_group_by_sessions": [session]})))
        rows = T.fetch_upstream_usage(_FakeManager(), client, START, END)
        self.assertEqual([r["request_id"] for r in rows], [
            "trae-6ad0de4e67e307253f910922-default",
            "trae-6ad0de4e67e307253f910922-agent",
        ])
        self.assertEqual([r["model"] for r in rows], ["DeepSeek-V4.1-Flash", "GLM-5.3"])
        self.assertEqual([r["credit"] for r in rows], [0.5, 0.25])

    def test_session_without_groups_falls_back_to_session_level(self):
        session = {k: v for k, v in self.SESSION.items() if k != "usage_group_details"}
        client = httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"total": 1,
                                                 "user_usage_group_by_sessions": [session]})))
        rows = T.fetch_upstream_usage(_FakeManager(), client, START, END)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], "trae-6ad0de4e67e307253f910922-default")
        self.assertEqual(rows[0]["credit"], 0.642)
        self.assertEqual(rows[0]["model"], "DeepSeek-V4.1-Flash")

    def test_paginates_by_total(self):
        pages = []

        def respond(req):
            body = json.loads(req.content)
            pages.append(body["page_num"])
            if body["page_num"] == 1:
                return httpx.Response(200, json={"total": 2, "user_usage_group_by_sessions": [
                    dict(self.SESSION, session_id="s1")]})
            return httpx.Response(200, json={"total": 2, "user_usage_group_by_sessions": [
                dict(self.SESSION, session_id="s2")]})
        client = httpx.Client(transport=httpx.MockTransport(respond))
        rows = T.fetch_upstream_usage(_FakeManager(), client, START, END)
        self.assertEqual(pages, [1, 2])
        self.assertEqual([r["request_id"] for r in rows],
                         ["trae-s1-default", "trae-s2-default"])

    def test_wide_window_is_not_narrowed(self):
        """窗口本来就够宽时原样使用（不把用户要的区间改小）。"""
        seen = {}

        def respond(req):
            seen.update(json.loads(req.content))
            return httpx.Response(200, json={"total": 0, "user_usage_group_by_sessions": []})
        client = httpx.Client(transport=httpx.MockTransport(respond))
        wide_start = END - timedelta(days=90)
        T.fetch_upstream_usage(_FakeManager(), client, wide_start, END)
        self.assertEqual(seen["start_time"], int(wide_start.timestamp()))

    def test_rows_without_session_or_time_are_dropped(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json={"total": 2, "user_usage_group_by_sessions": [
                {"session_id": "", "usage_time": 1792073300},
                {"session_id": "s9", "usage_time": 0},
            ]})))
        rows = T.fetch_upstream_usage(_FakeManager(), client, START, END)
        self.assertEqual(rows, [])

    def test_failures_raise_usage_error(self):
        cases = [
            (httpx.Response(500), "上游用量接口请求失败"),
            (httpx.Response(200, content=b"not json"), "上游用量接口返回格式异常"),
            (httpx.Response(200, json={"total": 1, "user_usage_group_by_sessions": "x"}),
             "上游用量明细格式异常"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                client = httpx.Client(transport=httpx.MockTransport(lambda req: response))
                with self.assertRaises(BackendUsageError) as ctx:
                    T.fetch_upstream_usage(_FakeManager(), client, START, END)
                self.assertIn(message, str(ctx.exception))


class DeclarationTests(unittest.TestCase):
    def test_each_backend_declares_its_own_fetcher(self):
        for name in ("workbuddy", "trae"):
            spec = registry.spec_for(name)
            self.assertTrue(spec.supports_upstream_usage, name)
            self.assertTrue(spec.usage_fetcher, name)

    def test_spec_dispatch_reaches_the_backend_module(self):
        """spec.fetch_usage_rows 不 import 后端模块，直接从 sys.modules 取函数。"""
        spec = registry.spec_for("trae")
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json={"total": 0, "user_usage_group_by_sessions": []})))
        rows = spec.fetch_usage_rows(_FakeManager(), client, START, END)
        self.assertEqual(rows, [])

    def test_spec_without_declaration_raises(self):
        class _Bare(registry.BackendSpec):
            name = "bare"
        with self.assertRaises(NotImplementedError):
            _Bare().fetch_usage_rows(_FakeManager(), None, START, END)

    def test_unknown_fetcher_name_raises_type_error(self):
        class _Broken(registry.BackendSpec):
            name = "broken"
            usage_fetcher = "not_here"
        # 声明了个模块里不存在的名字：报「未提供实现」而不是裸 AttributeError
        # （fetch_usage_rows 跑在上游调用路径上，最不该在这里出难懂的二次异常）。
        with self.assertRaises(NotImplementedError):
            _Broken().fetch_usage_rows(_FakeManager(), None, START, END)

    def test_undeclared_fetchers_raise_not_implemented(self):
        """基类默认四个账号操作 fetcher 都没声明 → 调用即 NotImplementedError。"""
        plain = registry.BackendSpec()
        with self.assertRaises(NotImplementedError):
            plain.fetch_credits(None, None, {})
        with self.assertRaises(NotImplementedError):
            plain.fetch_checkin_status(None, None, {})
        with self.assertRaises(NotImplementedError):
            plain.fetch_checkin_claim(None, None, {})
        with self.assertRaises(NotImplementedError):
            plain.fetch_billing(None, None, "x")

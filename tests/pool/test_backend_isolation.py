"""后端隔离：腾讯（workbuddy）与字节（trae）必须互不影响。

背景：两个后端共用一个账号池、靠 ``accounts.backend`` 字段区分。共用本身没问题
（凭据、冷却、排队、护栏都是按账号记的），但**任何一处漏传 backend，都会让一个
后端的请求拿到另一个后端的账号**——凭据格式、上游域名、模型目录全都不兼容，
表现是一个莫名其妙的 4xx，而不是「换号重试」。

这四类漏过的地方，每一条都补一个回归测试。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from admin.pool import AccountPool, PoolMiddleware, WafIpGate
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


def wb_credential(uid="wb-user"):
    return {
        "account": {"uid": uid, "nickname": "WB", "enterpriseId": "ent"},
        "auth": {
            "accessToken": "WB-ACCESS",
            "refreshToken": "WB-REF",
            "expiresAt": int(time.time() * 1000) + 3600000,
        },
    }


class BackendIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "management").mkdir()
        (root / "auth").mkdir()
        self.store = Store(root / "management", root / "auth", "api-test",
                           "admin-long-enough-test-key")
        self.wb = self.store.save_browser_account(wb_credential("wb-1"), "wb")["account_id"]
        self.tr = self.store.save_browser_account(trae_credential("trae-1"), "trae")["account_id"]
        self.now = time.time()
        self.pool = AccountPool(self.store, clock=lambda: self.now,
                                client_factory=lambda: httpx.Client(
                                    transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
        for aid in (self.wb, self.tr):
            mgr = self.store.manager_for(aid, self.store.data["accounts"][aid])
            mgr.get_headers = Mock(return_value={"X-Domain": "www.codebuddy.cn"})

    # -- 1. try_select 必须原样透传 backend ---------------------------------
    def test_try_select_keeps_backend_isolation(self):
        """回归：排队切号重试时漏传 backend，腾讯请求会拿到字节账号。

        select() 有后端过滤，try_select() 曾经没透传 —— 只在**重试**路径上漏，
        首次请求完全正常，所以极难从表面症状看出来。
        """
        for backend, expected, other in (("workbuddy", self.wb, self.tr),
                                        ("trae", self.tr, self.wb)):
            picked = self.pool.try_select(None, None, exclude=[expected], backend=backend)
            self.assertIsNone(picked,
                              f"{backend} 排除掉本侧账号后不该拿到对侧的 {other}")

    def test_try_select_stays_in_backend_even_when_same_side_is_exhausted(self):
        """本侧账号全试完后应返回 None（交由降级/原样返回），而不是借用对侧账号。"""
        for backend, mine, theirs in (("workbuddy", self.wb, self.tr),
                                      ("trae", self.tr, self.wb)):
            self.assertIsNone(
                self.pool.try_select(None, None, exclude=[mine], backend=backend),
                f"{backend} 不该在耗尽后借用 {theirs}")

    def test_select_without_backend_is_still_unfiltered_for_legacy_callers(self):
        """backend=None 保持「不过滤」——老调用方与测试的行为不变。"""
        self.assertIn(self.pool.select()[0], {self.wb, self.tr})

    # -- 2. WAF gate 按后端分桶 ---------------------------------------------
    def test_waf_gate_does_not_cross_backends(self):
        """字节被 WAF 拦到 IP 级，不得停掉腾讯的轮转（反之亦然）。"""
        gate = WafIpGate(clock=lambda: 1000.0)
        self.assertFalse(gate.note_waf("t1", backend="trae"))
        self.assertTrue(gate.note_waf("t2", backend="trae"))
        self.assertTrue(gate.active(backend="trae"))
        self.assertFalse(gate.active(backend="workbuddy"),
                         "字节的 IP 级封锁不该牵连腾讯")
        # 腾讯侧仍可正常判定，两个后端互不影响。
        self.assertFalse(gate.note_waf("w1", backend="workbuddy"))
        self.assertTrue(gate.note_waf("w2", backend="workbuddy"))
        self.assertTrue(gate.active(backend="workbuddy"))
        self.assertTrue(gate.active(backend="trae"))

    def test_waf_gate_same_backend_still_activates(self):
        """分桶后，同一后端内的判定语义没有放松。"""
        gate = WafIpGate(clock=lambda: 1000.0)
        self.assertFalse(gate.note_waf("a", backend="workbuddy"))
        self.assertTrue(gate.note_waf("b", backend="workbuddy"))
        self.assertTrue(gate.active(backend="workbuddy"))

    def test_waf_gate_single_account_never_activates(self):
        """单号反复被拦仍不判定为 IP 级（口径不变）。"""
        gate = WafIpGate(clock=lambda: 1000.0)
        for i in range(5):
            self.assertFalse(gate.note_waf("a", backend="workbuddy"))
        self.assertFalse(gate.active(backend="workbuddy"))

    def test_waf_snapshot_reports_each_backend(self):
        gate = WafIpGate(clock=lambda: 1000.0)
        gate.note_waf("t1", backend="trae")
        gate.note_waf("t2", backend="trae")
        snap = gate.snapshot()
        self.assertIn("by_backend", snap)
        self.assertTrue(snap["by_backend"]["trae"]["active"])
        self.assertFalse(snap["by_backend"]["workbuddy"]["active"])

    # -- 3. tick 逐账号隔离：一个坏账号不能拖垮整轮 -------------------------
    async def test_one_bad_trae_account_does_not_starve_tencent_checkin(self):
        """核心隔离点：Trae 账号抛任意异常，不得打断整轮 tick。

        operate 对 Trae 会抛 TraeAPIError / JSON / 网络错误等**任意**异常；
        原来只 catch HTTPException，一个坏掉的字节账号就会让排在它后面的
        腾讯账号这轮既不签到也不刷新。
        """
        called = []

        def operate(aid, action):
            called.append(aid)
            if aid == self.tr:
                raise RuntimeError("字节上游炸了")
            return {"id": aid, "ok": True, "message": "签到成功"}

        self.pool.operate = operate
        self.store.data["pool"]["auto_checkin"] = True
        self.store.data["pool"]["checkin_time"] = "09:00"
        # 账号顺序里 trae 排在前面，才能压到后面的腾讯账号。
        self.store.data["accounts"] = {self.tr: self.store.data["accounts"][self.tr],
                                       self.wb: self.store.data["accounts"][self.wb]}
        # 直接钉住签到时段（与既有签到测试同法），避免随机排期把两个账号排到
        # 判定时刻之后，那样 due 为空、测的就不是「异常打断」而是「压根没到期」。
        from datetime import datetime, timedelta
        from admin.models import CN
        for aid in (self.tr, self.wb):
            self.store.data["account_status"].setdefault(aid, {}).update(
                checkin_slot="上午", checkin_when="09:10", checkin_offset_date="2026-10-16",
                auto_attempt=0, checkin_date=None, checkin_fails=0)
        self.now = datetime(2026, 10, 16, 9, 30, tzinfo=CN).timestamp()
        await self.pool.tick()
        self.assertIn(self.wb, called, "腾讯账号必须照常执行，不能被字节账号连累")
        self.assertIn(self.tr, called)

    # -- 4. 端到端：腾讯请求在排队切号时不得拿到字节账号 --------------------
    async def test_end_to_end_failover_never_crosses_backend(self):
        """走真实 PoolMiddleware：腾讯请求重试时只用腾讯账号。

        构造一个腾讯账号（被排除）+ 一个字节账号的场景。若隔离失效，
        腾讯请求会改用字节凭据，真实上游会因凭据格式不兼容而报错。
        """
        self.store.data["pool"]["queue_failover"] = True
        self.store.data["pool"]["queue_model_fallback"] = False
        self.store.data["pool"]["queue_retry"] = 3
        used = []

        async def app(scope, receive, send):
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                if not msg.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": json.dumps(
                {"code": "WAITING", "message": "排队中"}).encode()})

        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)),
                base_url="http://test") as client:
            await client.post("/v1/chat/completions",
                              json={"model": "deepseek-v4-flash",
                                    "messages": [{"role": "user", "content": "hi"}]},
                              headers={"Authorization": "Bearer api-test"})

        # 腾讯账号被排队让位后，**不应**回退到字节账号；候选耗尽即停手。
        self.assertFalse(self.pool.queued(self.tr),
                         "字节账号被腾讯请求拿来重试了 —— 后端隔离失效")
        self.assertTrue(self.pool.queued(self.wb))


if __name__ == "__main__":
    unittest.main()

import pytest  # noqa: E402  (标记需要)

# ---------------------------------------------------------------------------
# 分层标记（见 pytest.ini / tests/README.md）
#
# 本文件整体属于「core + e2e」。标记写在文件里而不是逐条标注：同一文件的用例
# 保护的是同一块行为，分域跑（`pytest tests/pool`）时就该一起跑。
#   core    : 必须守住的不变量，默认只跑这些
#   e2e     : 起整套 create_app 走真实请求链路
#   network : 经由 mock 传输层模拟上游交互
# 若某条用例其实不属于本文件默认域（例如纯形状断言、只测常量的），
# 请在该用例上单独加 @pytest.mark.<域>，或在下面改用更准确的一档。
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.core, pytest.mark.e2e]


"""裸名路由端到端：一次真实请求落到了哪个后端（specs/model-routing-by-policy.md §5）。

`tests/unit/test_bare_routing.py` 测的是规则本身（纯函数）；这里测**接线**：

- `admin/pool.py` 的中间件是否为裸名请求调用策略选择；
- 选定结果是否传给了 `converter`（否则凭据一致性闸门会 503）；
- §5.4 大前提「只有默认后端也能跑」在真实 app 上成立；
- kill switch 关掉后是否真的回到「裸名走默认后端」。

## 为什么必须有这一层

纯函数全绿**证明不了**它们被调用 —— 这正是文档 §8 要求的变异场景：
「选择逻辑被改回硬编码默认后端时测试必须变红」。如果只在 pool 里写
`backend = backend_name_for(model)`（今天的行为），纯函数测试照样全绿。
所以这里用真实 app 发请求、看**实际选中的账号**落在哪个后端。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from core.backends import trae as T
from core.backends.registry import spec_for

API = "sk-test-client-key"
ADMIN = "admin-key-long-enough-1234"

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _wb_credential():
    """WorkBuddy 凭据：`auth` 内嵌，`expiresAt` 是**毫秒**（与 test_trae_backend 一致）。"""
    return {
        "account": {"uid": "wb-user", "nickname": "WB测试", "enterpriseId": "ent"},
        "auth": {"accessToken": "WB-ACCESS", "refreshToken": "WB-REFRESH",
                 "expiresAt": int(time.time() * 1000) + 3_600_000},
    }


def _trae_credential():
    """Trae 凭据：`expiresAt` 是 **Unix 秒**（归一为毫秒由后端声明决定）。"""
    return {
        "backend": "trae",
        "account": {"uid": "trae-user", "nickname": "Trae测试", "enterpriseId": "ent"},
        "auth": {"accessToken": "TRAE-ACCESS", "refreshToken": "TRAE-REFRESH",
                 "expiresAt": int(time.time()) + 30 * 86400,
                 "domain": "trae.cn", "apiHost": T.OAUTH_HOST,
                 "machineId": "m" * 32, "deviceId": "d" * 32},
    }


class BareNameRoutingEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import admin.server as admin_server
        import admin.models as admin_models

        self.admin_server = admin_server
        self.admin_models = admin_models
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "wb.info").write_text(json.dumps(_wb_credential()), encoding="utf-8")
        (self.auth / "trae-u1.info").write_text(json.dumps(_trae_credential()), encoding="utf-8")
        self._orig_fetch = admin_models.ModelRegistry.fetch
        admin_models.ModelRegistry.fetch = lambda self: False   # 免网络：只用内置目录
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")
        self._seen = []

    async def asyncTearDown(self):
        await self.client.aclose()
        self.admin_server.converter.httpx.AsyncClient = _REAL_ASYNC_CLIENT
        self.admin_models.ModelRegistry.fetch = self._orig_fetch
        self.temp.cleanup()

    def patch_upstream(self, handler):
        def wrapped(req):
            self._seen.append(req)
            return handler(req)
        transport = httpx.MockTransport(wrapped)
        self.admin_server.converter.httpx.AsyncClient = \
            lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)

    def _last_log_backend(self):
        """最近一次请求实际落在哪个后端（§5.3：看 `request_logs.backend`）。

        日志先进 `store.log_buffer`（5 秒防抖批量落库），所以测试直接读缓冲 ——
        比落库路径更贴近「这一请求刚记了什么」，也不用等 flush。
        """
        rows = [r for r in self.app.state.store.log_buffer if r.get("path")]
        if not rows:
            return None
        return rows[-1].get("backend")

    async def _post(self, model):
        sse = b'data: {"model":"m","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        self.patch_upstream(lambda req: httpx.Response(200, content=sse))
        return await self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + API},
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]})

    async def test_bare_name_auto_selects_the_strategy_winner(self):
        """裸名 `glm-5.3` 两边都有 → 走策略第一名（默认后端 WorkBuddy）。

        这条**是接线测试**：先把 WorkBuddy 的账号禁掉，只剩 Trae 可用，
        于是「有账号」这道过滤会把候选收窄到 Trae —— 裸名请求必须落到 Trae。
        若 `pool.py` 退回 `backend_name_for(model)`（硬编码默认后端），
        它会去选 WorkBuddy 账号、发现没有可用账号而 503，这条就红了。
        """
        # 只留 Trae 账号可用。
        for aid, item in list(self.app.state.store.data["accounts"].items()):
            if item.get("backend", "workbuddy") == "workbuddy":
                item["enabled"] = False
        r = await self._post("glm-5.3")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "trae",
                         "WorkBuddy 没有可用账号时，裸名必须落到 Trae（候选集过滤）")

    async def test_kill_switch_restores_default_backend(self):
        """kill switch 关掉 → 裸名回到「走默认后端」，即今天的行为。

        这里把默认后端账号禁掉、只留 Trae：关掉开关后裸名仍去默认后端
        （没可用账号 → 503），证明开关真的短路了策略选择。
        """
        self.app.state.store.data["pool"]["bare_name_routing"] = False
        for aid, item in list(self.app.state.store.data["accounts"].items()):
            if item.get("backend", "workbuddy") == "workbuddy":
                item["enabled"] = False
        r = await self._post("glm-5.3")
        self.assertEqual(r.status_code, 503,
                         "关掉开关后裸名不再走策略（回落默认后端 → 没账号 → 503）")

    async def test_single_backend_deployment_still_works(self):
        """§5.4 大前提：只启用默认后端时，裸名请求照常可用（不写特例也能对）。"""
        # 关掉 Trae：走 store 的公开 setter（内存 map 与 pool JSON 串两处同步，
        # registry 立刻生效 —— 直接改 pool dict 不会同步 _backend_flags）。
        self.app.state.store.set_backend_flags({"workbuddy": True, "trae": False})
        r = await self._post("glm-5.3")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "workbuddy")

    async def test_auto_still_goes_to_default_backend(self):
        """§4：`auto` 不是目录里的模型，必须仍走默认后端（不能变成 400）。"""
        r = await self._post("auto")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "workbuddy")

    async def test_prefixed_request_bypasses_strategy(self):
        """§4：`trae/xxx` 是用户点名要 Trae，绕过自动选择（行为不变）。"""
        r = await self._post("trae/glm-5.2")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "trae")

    async def test_models_list_lists_every_backend_row_with_its_owner(self):
        """`/v1/models` 每行按**自己的后端**报 `owned_by`（用户 2026-10-16 拍板）。

        去前缀后同名模型在两个后端各有一行（不变量 1），`owned_by` 因此必须逐行取
        自己的后端，而不是按 id 判一个「策略第一名」——后者对两条同 id 的条目
        只能给同一个答案，客户端分不出来。

        历史：这里原先断言「裸名条目 owned_by = 当前策略第一名」（§9.6）。那条
        契约建立在「一个 id 一条」之上；改成「按后端各一条」后它不再成立，
        故整体改写（不是删掉 —— 「展示层要说清会走谁」这个诉求由裸名**请求**路径
        的测试继续守着）。
        """
        r0 = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        data = r0.json()["data"]
        glm53 = [m for m in data if m["id"] == "glm-5.3"]
        self.assertEqual(len(glm53), 2, "glm-5.3 两边都有 → 应列出两条")
        self.assertEqual({m["owned_by"] for m in glm53},
                         {spec_for("workbuddy").owned_by, spec_for("trae").owned_by})
        # 逐行对照 registry 的行清单：任何一行报错归属都会红。
        rows = self.app.state.model_registry.listed_models()
        self.assertEqual([(m["id"], m["owned_by"]) for m in data],
                         [(mid, spec_for(b).owned_by) for mid, b in rows])

    async def test_models_list_owned_by_unaffected_by_backend_order(self):
        """行归属来自**它属于哪个后端**，与用户配的后端顺序无关。

        对比旧契约：那时裸名的 `owned_by` 会随 `model_backend_order` 变（因为它报的是
        「策略第一名」）。现在报的是「这一行是谁的」，调序不该改变它 ——
        调序影响的是**请求**落到谁（由 `test_bare_name_routing` 系列守着）。
        """
        r0 = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        before = {(m["id"], m["owned_by"]) for m in r0.json()["data"]}

        self.app.state.store.data["pool"]["model_backend_order"] = ["trae", "workbuddy"]
        r1 = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        after = {(m["id"], m["owned_by"]) for m in r1.json()["data"]}
        self.assertEqual(before, after, "调序不该改变 /v1/models 的行归属")

    async def test_models_list_never_errors_when_no_candidate(self):
        """展示路径不因「此刻没有可用候选」而失败（它列的是目录，不是可用性）。

        把两个后端的账号全禁掉 → 候选集为空。`/v1/models` 仍必须 200：它是
        只读展示，客户端此刻发请求会 400/503，但「你们家有哪些模型」不该跟着 500。
        """
        for item in self.app.state.store.data["accounts"].values():
            item["enabled"] = False
        r = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"])

    async def test_models_list_ids_have_no_slash(self):
        """不变量 1：`/v1/models` 里**不允许出现任何带 `/` 的 id**。

        `trae/xxx` 这类前缀是我们自己拼的（上游只返回裸名），去前缀改造的
        核心诉求就是让它从系统里消失。允许有 `/` 的只有上游真实模型名
        （如 `openrouter/free`）—— 那是它们的原名，不是我们的前缀。
        """
        r = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        data = r.json()["data"]
        prefixed = [m["id"] for m in data if m["id"].startswith("trae/")]
        self.assertEqual(prefixed, [], f"不得再有 trae/ 前缀的 id：{prefixed}")

    async def test_bare_name_is_actually_routed_by_strategy(self):
        """裸名 `glm-5.3` 两边都有 → 请求按策略落到默认后端（WB）。

        与展示层对照：`/v1/models` 会**列出两条** `glm-5.3`（每后端一条），
        但一次**请求**只能落一处 —— 这里用实际选中的账号钉住那处是 WB
        （默认顺序下 order 0）。
        """
        r = await self._post("glm-5.3")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "workbuddy")

    async def test_unknown_model_lists_every_rejection(self):
        """§5.1 末段：候选为空 → 400，且逐条说明每个后端为什么出局。"""
        r = await self._post("no-such-model-anywhere")
        self.assertEqual(r.status_code, 400)
        body = r.text
        self.assertIn("no-such-model-anywhere", body)
        # 两个后端都要出现在出局说明里（排障要看「谁为什么出局」）。
        # 用后端名（`workbuddy`/`trae`）而不是展示名：这条路径不带 i18n，
        # 名字是稳定标识；展示名在别处（/v1/models 的 owned_by）。
        self.assertIn("workbuddy", body)
        self.assertIn("trae", body)


class StrategyProviderIsolationTests(unittest.TestCase):
    """策略查询入口的**故障隔离**契约（`admin/bare_routing_provider.py`）。

    `/v1/models` 是只读展示：策略算不出来（provider 抛异常、还没注入）时，
    它必须**回落**静态归属而不是把异常抛给客户端。若有人把那层 try/except
    删掉（看起来像「多余的防御」），这条会红 —— 这是那个 else 存在的理由。
    """

    def tearDown(self):
        from admin.bare_routing_provider import set_strategy_winner
        set_strategy_winner(None)   # 别把注入泄漏给其它测试

    def test_returns_none_when_not_injected(self):
        from admin.bare_routing_provider import set_strategy_winner, strategy_winner_for
        set_strategy_winner(None)
        self.assertIsNone(strategy_winner_for("glm-5.3"))

    def test_swallows_provider_exception(self):
        """provider 抛异常 → 返回 None（调用方回落），异常不外泄。"""
        from admin.bare_routing_provider import set_strategy_winner, strategy_winner_for

        def boom(model):
            raise RuntimeError("候选集为空")

        set_strategy_winner(boom)
        self.assertIsNone(strategy_winner_for("glm-5.3"))

    def test_forwards_the_provider_answer(self):
        from admin.bare_routing_provider import set_strategy_winner, strategy_winner_for
        set_strategy_winner(lambda model: "trae")
        self.assertEqual(strategy_winner_for("glm-5.3"), "trae")

    def test_models_endpoint_survives_a_raising_provider(self):
        """端到端：provider 炸了，`/v1/models` 仍 200（回归到 500 会红）。

        走真实 `_owner_backend_for`（不是直接调 provider），证明那层兜底真的接上了。
        """
        from admin.bare_routing_provider import set_strategy_winner
        from core.converter import _owner_backend_for

        def boom(model):
            raise RuntimeError("boom")

        set_strategy_winner(boom)
        # 裸名：策略炸了 → 回落静态归属（默认后端），不抛。
        got = _owner_backend_for("glm-5.3")
        self.assertEqual(got, "workbuddy")
        # 带前缀的：根本不查策略（用户已点名）。
        self.assertEqual(_owner_backend_for("trae/glm-5.3"), "trae")


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

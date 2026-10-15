"""`GET /admin/api/backends` 的契约（需求 2 / 5 / 7 的前端依赖）。

前端**不再登记后端元数据**：图标、显示名、颜色、顺序、模型前缀、登录方式全部
从这里取。所以这个端点的字段名是前端与后端的接口 —— 少一个字段，页面上就是
一个「静默显示成默认值」的坑（正是第 3 阶段要消灭的那类 bug），必须锁住。

同时验「账号数按后端统计」：需求 5/6 的账号表格与筛选要用它。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from admin import server as admin_server

API = "client-existing-key"
ADMIN = "admin-key-long-enough-1234"


def credential(uid="one", backend=None):
    doc = {
        "account": {"uid": uid, "nickname": "N" + uid, "enterpriseId": "ent"},
        "auth": {
            "accessToken": "ACCESS-" + uid,
            "refreshToken": "REFRESH-" + uid,
            "expiresAt": 4102444800000,  # 2100 年，毫秒
        },
    }
    if backend:
        doc["backend"] = backend
    return doc


def trae_credential(uid="trae-one"):
    doc = credential(uid)
    doc["backend"] = "trae"
    doc["auth"]["expiresAt"] = 4102444800  # 秒级（Trae 的格式）
    return doc


class BackendMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        r = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(r.status_code, 200)
        self.client.headers["X-CSRF-Token"] = r.json()["csrf"]

    async def test_requires_admin(self):
        """元数据不是公开信息（含各后端的账号数），必须走管理会话。"""
        self.assertEqual((await self.client.get("/admin/api/backends")).status_code, 401)

    async def test_shape_matches_frontend_contract(self):
        await self.login()
        data = (await self.client.get("/admin/api/backends")).json()
        self.assertIn("backends", data)
        rows = data["backends"]
        self.assertTrue(rows)
        for row in rows:
            # 前端 renderLoginModes / backendBadge / backendTag 逐字段用。
            for key in ("name", "label", "short", "icon", "color", "order",
                        "prefix", "login_kind", "enabled", "default", "accounts"):
                self.assertIn(key, row, f"{row.get('name')} 缺字段 {key}")
            self.assertIsInstance(row["accounts"], int)
            self.assertIsInstance(row["enabled"], bool)

    async def test_order_is_stable_and_default_first(self):
        """顺序稳定是前端渲染的前提（每次刷新行序不能跳）。"""
        await self.login()
        rows = (await self.client.get("/admin/api/backends")).json()["backends"]
        orders = [r["order"] for r in rows]
        self.assertEqual(orders, sorted(orders))
        defaults = [r for r in rows if r["default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(rows[0]["name"], defaults[0]["name"])

    async def test_prefix_and_login_kind_come_from_backends(self):
        await self.login()
        rows = {r["name"]: r for r in (await self.client.get("/admin/api/backends")).json()["backends"]}
        self.assertEqual(rows["trae"]["prefix"], "trae/")
        self.assertEqual(rows["trae"]["login_kind"], "trae")
        self.assertEqual(rows["workbuddy"]["prefix"], "")
        self.assertEqual(rows["workbuddy"]["login_kind"], "browser")

    async def test_accounts_counted_per_backend(self):
        """账号数按后端分别统计 —— 前端「账号类型」筛选与概览卡片要用。"""
        await self.login()
        before = {r["name"]: r["accounts"]
                  for r in (await self.client.get("/admin/api/backends")).json()["backends"]}
        self.assertEqual(before["workbuddy"], 1)
        self.assertEqual(before["trae"], 0)

        r = await self.client.post("/admin/api/accounts",
                                   json={"name": "T", "credential": trae_credential()})
        self.assertEqual(r.status_code, 200, r.text)
        after = {r["name"]: r["accounts"]
                 for r in (await self.client.get("/admin/api/backends")).json()["backends"]}
        self.assertEqual(after["workbuddy"], 1)
        self.assertEqual(after["trae"], 1)

    async def test_kill_switch_marks_backend_disabled(self):
        """Trae 被 kill switch 关掉时仍列出（结构稳定），但 enabled=False。"""
        await self.login()
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"CODEBUDDY_ENABLE_TRAE": "0"}):
            rows = {r["name"]: r for r in (await self.client.get("/admin/api/backends")).json()["backends"]}
            self.assertIn("trae", rows, "被禁用的后端也要列出，否则前端要处理桶时有时无")
            self.assertFalse(rows["trae"]["enabled"])
            self.assertTrue(rows["workbuddy"]["enabled"])


class BackendFieldValidationTests(unittest.IsolatedAsyncioTestCase):
    """导入凭据时的 backend 白名单**由注册表推导**，不是写死的两个名字。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        r = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.client.headers["X-CSRF-Token"] = r.json()["csrf"]

    async def test_known_backends_accepted(self):
        await self.login()
        for backend in ("workbuddy", "trae"):
            doc = trae_credential() if backend == "trae" else credential()
            doc["backend"] = backend
            r = await self.client.post("/admin/api/accounts", json={"name": backend, "credential": doc})
            self.assertEqual(r.status_code, 200, f"{backend}: {r.text}")

    async def test_unknown_backend_rejected_with_actionable_message(self):
        await self.login()
        doc = credential()
        doc["backend"] = "qwen-office"   # 尚未支持
        r = await self.client.post("/admin/api/accounts", json={"name": "X", "credential": doc})
        self.assertEqual(r.status_code, 400)
        message = json.dumps(r.json(), ensure_ascii=False)
        # 错误信息要列出**当前支持的后端**，否则用户不知道该怎么改。
        self.assertIn("workbuddy", message)
        self.assertIn("trae", message)


if __name__ == "__main__":
    unittest.main()

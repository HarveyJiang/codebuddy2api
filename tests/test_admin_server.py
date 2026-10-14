import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

import admin.server as admin_server

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"}, "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token", "expiresAt": int(time.time() * 1000) + 3600000}}


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.make_app()

    def make_app(self):
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(result.status_code, 200)
        cookie = result.headers["set-cookie"].lower()
        for flag in ["secure", "httponly", "samesite=strict", "path=/admin"]:
            self.assertIn(flag, cookie)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    async def test_access_boundaries(self):
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 401)
        self.assertEqual((await self.client.get("/admin/api/overview", headers={"Authorization": "Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models")).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})).status_code, 200)
        self.assertEqual((await self.client.get("/health")).json(), {"status": "ok"})
        await self.login()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self.client.post("/admin/api/keys", json={"name": "bad"})).status_code, 403)

    async def test_account_import_switch_disable_delete_and_restart(self):
        await self.login()
        first = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        result = await self.client.post("/admin/api/accounts", json={"name": "Second", "credential": credential("two")})
        self.assertEqual(result.status_code, 200)
        aid = result.json()["id"]
        self.assertTrue(first["active"])
        result = await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(admin_server.converter.CONFIG["cred"].path.name, aid + ".info")
        await self.client.patch("/admin/api/accounts/" + aid, json={"enabled": False})
        self.assertIsNone(admin_server.converter.CONFIG["cred"])
        self.assertEqual((await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})).status_code, 400)
        await self.client.patch("/admin/api/accounts/" + first["id"], json={"active": True, "name": "Primary"})
        await self.client.delete("/admin/api/accounts/" + aid)
        self.assertTrue(list((self.root / "management/trash").glob("*/credential.json")))
        await self.client.aclose()
        self.make_app()
        await self.login()
        accounts = (await self.client.get("/admin/api/overview")).json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0]["active"])
        self.assertEqual(accounts[0]["name"], "Primary")

    async def test_key_revocation_persists_and_no_secret_leak(self):
        await self.login()
        overview = await self.client.get("/admin/api/overview")
        for forbidden in [API, "private-access-token", "private-refresh-token", '"hash"']:
            self.assertNotIn(forbidden, overview.text)
        old = overview.json()["keys"][0]["id"]
        self.assertEqual((await self.client.delete("/admin/api/keys/" + old)).status_code, 400)
        result = await self.client.post("/admin/api/keys", json={"name": "New"})
        newkey = result.json()["key"]
        await self.client.delete("/admin/api/keys/" + old)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + newkey})).status_code, 200)
        self.assertNotIn(newkey, self.app.state.store.path.read_text(encoding="utf-8"))
        await self.client.aclose()
        self.make_app()
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + newkey})).status_code, 200)

    async def test_credit_refresh_endpoint_and_settings(self):
        # 未登录不可用。
        self.assertEqual((await self.client.post("/admin/api/pool/refresh-credits")).status_code, 401)
        await self.login()
        result = await self.client.post("/admin/api/pool/refresh-credits")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("refreshed", result.json())
        # 拉取间隔可配置并持久化。
        saved = await self.client.patch("/admin/api/pool/settings", json={"credit_min_interval": 120})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["pool"]["credit_min_interval"], 120)
        self.assertEqual((await self.client.patch("/admin/api/pool/settings", json={"credit_min_interval": 99999})).status_code, 400)
        # 旧的积分同步间隔字段已移除。
        self.assertNotIn("sync_interval", (await self.client.get("/admin/api/overview")).json()["pool"])

    def test_stale_catalog_detection(self):
        """旧格式缓存（描述含英文、或未按单价升序）需重新拉取；新格式不需要。"""
        from admin.models import ModelRegistry, BUILTIN_ORDER
        registry = ModelRegistry(get_credential=lambda: None)
        registry.ensure_builtin()
        # 内置列表：三档智能路由排在末尾，未按单价升序 -> 判定为旧格式。
        self.assertTrue(admin_server._stale_catalog(registry))
        # 描述换成纯中文、顺序按单价升序后，不再判定为旧格式。
        catalog = {mid: dict(info, description="中文描述") for mid, info in registry._catalog.items()}
        registry._catalog = catalog
        registry._available = sorted(BUILTIN_ORDER, key=lambda m: (
            catalog[m].get("credits") if isinstance(catalog[m].get("credits"), (int, float)) else 0))
        self.assertFalse(admin_server._stale_catalog(registry))

    async def test_invalid_credentials_and_limits(self):
        await self.login()
        for doc in [{}, {"auth": {}, "account": {}}, {**credential(), "account": {"uid": "bad\r\nheader"}}, {**credential(), "auth": {**credential()["auth"], "expiresAt": "not-time"}}]:
            r = await self.client.post("/admin/api/accounts", json={"credential": doc})
            self.assertEqual(r.status_code, 400, r.text)
        r = await self.client.post("/admin/api/accounts", content=b"x" * (1024 * 1024 + 1))
        self.assertEqual(r.status_code, 413)
        r = await self.client.get("/admin/assets/unknown.js")
        self.assertEqual(r.status_code, 404)

    async def test_logout_and_rate_limit(self):
        await self.login()
        self.assertEqual((await self.client.post("/admin/api/logout")).status_code, 200)
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 401)
        for _ in range(8):
            self.assertEqual((await self.client.post("/admin/api/login", json={"key":"wrong"})).status_code, 401)
        self.assertEqual((await self.client.post("/admin/api/login", json={"key":ADMIN})).status_code, 429)


if __name__ == "__main__":
    unittest.main()

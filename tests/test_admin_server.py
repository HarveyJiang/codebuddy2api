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

    async def test_account_import_disable_delete_and_restart(self):
        await self.login()
        first = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        result = await self.client.post("/admin/api/accounts", json={"name": "Second", "credential": credential("two")})
        self.assertEqual(result.status_code, 200)
        aid = result.json()["id"]
        # 全局 active 已移除：账号行不再带 active 徽标。
        self.assertNotIn("active", first)
        # CONFIG['cred'] 现在指向「任一启用账号」，只用来判断有没有可用账号。
        self.assertIsNotNone(admin_server.converter.CONFIG["cred"])
        await self.client.patch("/admin/api/accounts/" + aid, json={"enabled": False})
        await self.client.patch("/admin/api/accounts/" + first["id"], json={"name": "Primary"})
        # 全部停用后不再有可用凭据。
        await self.client.patch("/admin/api/accounts/" + first["id"], json={"enabled": False})
        self.assertIsNone(admin_server.converter.CONFIG["cred"])
        await self.client.patch("/admin/api/accounts/" + first["id"], json={"enabled": True})
        await self.client.delete("/admin/api/accounts/" + aid)
        self.assertTrue(list((self.root / "management/trash").glob("*/credential.json")))
        await self.client.aclose()
        self.make_app()
        await self.login()
        accounts = (await self.client.get("/admin/api/overview")).json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertNotIn("active", accounts[0])
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
        # 完整密钥明文不得出现在持久层中。主库和 -wal 都要查：
        # WAL 模式下未 checkpoint 的帧只在 -wal 里。
        path = self.app.state.store.db.path
        for suffix in ("", "-wal"):
            blob = path.with_name(path.name + suffix).read_bytes()
            self.assertNotIn(newkey.encode(), blob, f"明文密钥泄漏到 {suffix or '主库'}")
        await self.client.aclose()
        self.make_app()
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + newkey})).status_code, 200)

    async def test_key_routing_enabled_and_expiry(self):
        await self.login()
        created = await self.client.post("/admin/api/keys", json={"name": "策略测试"})
        self.assertEqual(created.status_code, 200)
        kid, value = created.json()["id"], created.json()["key"]
        # 默认 routing 为 NULL，跟随 settings.default_routing。
        keys = (await self.client.get("/admin/api/overview")).json()["keys"]
        item = next(k for k in keys if k["id"] == kid)
        self.assertIsNone(item["routing"])
        self.assertEqual(item["enabled"], 1)
        self.assertIsNone(item["expires_at"])
        # 设置策略。
        for routing in ("round_robin", "random", "expiry_first", None):
            resp = await self.client.patch("/admin/api/keys/" + kid, json={"routing": routing})
            self.assertEqual(resp.status_code, 200)
            keys = (await self.client.get("/admin/api/overview")).json()["keys"]
            self.assertEqual(next(k for k in keys if k["id"] == kid)["routing"], routing)
        # 非法策略被拒。
        self.assertEqual((await self.client.patch("/admin/api/keys/" + kid, json={"routing": "manual"})).status_code, 400)
        # 停用后立即无法调用。
        self.assertEqual((await self.client.patch("/admin/api/keys/" + kid, json={"enabled": False})).status_code, 200)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + value})).status_code, 401)
        self.assertEqual((await self.client.patch("/admin/api/keys/" + kid, json={"enabled": True})).status_code, 200)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + value})).status_code, 200)
        # 过期时间设为过去 → 调用被拒；设回 NULL → 恢复。
        self.assertEqual((await self.client.patch("/admin/api/keys/" + kid, json={"expires_at": 1})).status_code, 200)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + value})).status_code, 401)
        self.assertEqual((await self.client.patch("/admin/api/keys/" + kid, json={"expires_at": None})).status_code, 200)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + value})).status_code, 200)

    async def test_key_scope_models_and_accounts(self):
        """密钥的模型/账号白名单：可写入、可读回、空白名单表示不限制。"""
        await self.login()
        created = await self.client.post("/admin/api/keys", json={"name": "范围测试"})
        kid = created.json()["id"]
        overview = (await self.client.get("/admin/api/overview")).json()
        account_id = overview["accounts"][0]["id"]
        models = [m["id"] for m in overview["models"]["models"] if m["enabled"]][:2]
        result = await self.client.patch("/admin/api/keys/" + kid, json={"models": models, "accounts": [account_id]})
        self.assertEqual(result.status_code, 200, result.text)
        keys = (await self.client.get("/admin/api/overview")).json()["keys"]
        item = next(k for k in keys if k["id"] == kid)
        self.assertEqual(item["models"], models)
        self.assertEqual(item["accounts"], [account_id])
        # 空列表 = 不限制。
        await self.client.patch("/admin/api/keys/" + kid, json={"models": [], "accounts": []})
        keys = (await self.client.get("/admin/api/overview")).json()["keys"]
        item = next(k for k in keys if k["id"] == kid)
        self.assertEqual(item["models"], [])
        self.assertEqual(item["accounts"], [])
        # 创建时可直接指定范围。
        created = await self.client.post("/admin/api/keys", json={"name": "带范围", "models": models, "accounts": [account_id]})
        keys = (await self.client.get("/admin/api/overview")).json()["keys"]
        item = next(k for k in keys if k["id"] == created.json()["id"])
        self.assertEqual(item["models"], models)
        self.assertEqual(item["accounts"], [account_id])

    async def test_logs_pagination_and_sqlite_summary(self):
        """logs 分页接口与 overview 的累计统计都来自 request_logs。"""
        await self.login()
        store = self.app.state.store
        now = int(time.time())
        store.db.append_request_logs([
            {"time": now - i, "source": "api" if i % 2 else "test", "path": "/v1/chat/completions",
             "status": 200, "outcome": "success" if i % 3 else "stream_error",
             "duration_ms": 100 + i, "key_id": "k1", "account_id": "a1", "account_name": "账号",
             "model_id": "deepseek-v4-flash", "model_name": "V4", "upstream_request_id": None}
            for i in range(25)])
        # 分页：第一页 10 条，按 id 倒序 => 最新一条在最前。
        page = await self.client.get("/admin/api/logs?limit=10&offset=0")
        self.assertEqual(page.status_code, 200, page.text)
        data = page.json()
        self.assertEqual(len(data["rows"]), 10)
        self.assertEqual(data["total"], 25)
        self.assertTrue(all(r["ok"] == (r["outcome"] == "success") for r in data["rows"]))
        second = (await self.client.get("/admin/api/logs?limit=10&offset=10")).json()
        self.assertEqual(len(second["rows"]), 10)
        self.assertNotEqual(data["rows"][0]["id"], second["rows"][0]["id"])
        # 非法参数被拒（不落成 500）。
        self.assertEqual((await self.client.get("/admin/api/logs?limit=abc")).status_code, 400)
        # source 过滤。
        only_test = (await self.client.get("/admin/api/logs?source=test&limit=50")).json()
        self.assertTrue(all(r["source"] == "test" for r in only_test["rows"]))
        # overview 的计数来自库，不随进程重启清零。
        metrics = (await self.client.get("/admin/api/overview")).json()["metrics"]
        self.assertEqual(metrics["completed"], 25)
        self.assertEqual(metrics["failed"], 25 - sum(1 for i in range(25) if i % 3))
        self.assertIn("first_time", metrics)
        await self.client.aclose()
        self.make_app()
        await self.login()
        metrics = (await self.client.get("/admin/api/overview")).json()["metrics"]
        self.assertEqual(metrics["completed"], 25)

    async def test_usage_endpoints_paging_report_and_throttle(self):
        """用量三个接口：分页只读本地、报表聚合、手动拉取的防抖与不写 usage_synced_at。"""
        await self.login()
        store = self.app.state.store
        now = int(time.time())
        store.db.save_upstream_usage([
            {"request_id": f"r{i}", "account_id": "a1", "account_name": "账号一",
             "credit": 0.0 if i % 2 else 0.5, "model": "hy3", "client": "WorkBuddy",
             "request_time": now - i * 60, "agent_purpose": ""}
            for i in range(25)])
        # 分页：默认 100，这里显式 10。
        page = (await self.client.get("/admin/api/usage?limit=10&offset=0")).json()
        self.assertEqual(len(page["rows"]), 10)
        self.assertEqual(page["total"], 25)
        self.assertEqual(page["rows"][0]["request_id"], "r0")
        # 非法参数 400，不能变成 500。
        self.assertEqual((await self.client.get("/admin/api/usage?limit=abc")).status_code, 400)
        self.assertEqual((await self.client.get("/admin/api/usage?start=昨天")).status_code, 400)
        # 报表：四种维度。
        for group in ("day", "account", "model", "client"):
            resp = await self.client.get(f"/admin/api/usage/report?group_by={group}")
            self.assertEqual(resp.status_code, 200, resp.text)
            report = resp.json()
            self.assertEqual(report["group_by"], group)
            self.assertEqual(report["total"]["count"], 25)
            self.assertEqual(sum(g["count"] for g in report["groups"]), 25)
        # 非法维度 400。
        self.assertEqual((await self.client.get("/admin/api/usage/report?group_by=nope")).status_code, 400)

        # 手动拉取：起止时间非法 -> 400。
        self.assertEqual((await self.client.post("/admin/api/usage/sync", json={
            "start": now, "end": now - 3600})).status_code, 400)
        # 手动拉取不写 usage_synced_at（否则后台每日同步会误以为已同步而留空洞）。
        store.data["accounts"]["a1"] = {
            "name": "一", "file": "session.info", "enabled": True, "created": now}
        store.data["account_status"]["a1"] = {}
        pool = self.app.state.pool
        called = []
        pool._pull_upstream_usage = lambda aid, item, st, en: called.append(aid) or 0
        result = await self.client.post("/admin/api/usage/sync", json={
            "start": now - 7200, "end": now})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("a1", called)   # setUp 里已有 session.info 账号，不止一个
        self.assertIsNone(store.data["account_status"]["a1"].get("usage_synced_at"))
        self.assertIsNotNone(store.data["account_status"]["a1"].get("usage_manual_synced_at"))
        # 防抖：60 秒内重复拉取被节流（HTTPException 转成 skipped，不是 500）。
        again = (await self.client.post("/admin/api/usage/sync", json={
            "start": now - 7200, "end": now})).json()
        self.assertEqual(again["synced"], 0)
        self.assertTrue(any("刚拉取过" in (x.get("reason") or "") for x in again["skipped"]))

    async def test_key_usage_history_endpoint(self):
        """密钥按日用量历史：换天留新行，接口能按 key 取到序列。"""
        await self.login()
        store = self.app.state.store
        kid, _ = store.add_key("历史测试")
        store.data["keys"][kid]["usage"] = {"date": "2026-10-14", "count": 3, "tokens": 100}
        store.save()
        store.data["keys"][kid]["usage"] = {"date": "2026-10-15", "count": 5, "tokens": 200}
        store.save()
        data = (await self.client.get("/admin/api/keys/usage?days=30")).json()
        mine = next(k for k in data["keys"] if k["id"] == kid)
        self.assertEqual([d["count"] for d in mine["series"]], [5, 3])  # 按 day DESC
        self.assertEqual(data["total"]["count"], 8)
        self.assertEqual((await self.client.get("/admin/api/keys/usage?days=0")).status_code, 400)

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

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
import pytest

import admin.server as admin_server
from admin.metrics import MetricsMiddleware, RequestMetrics
from conftest import credential  # 跨模块共用 helper，见 tests/conftest.py

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(message):
    return None


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        # 启动时的上游目录拉取已由 tests/conftest.py 的 autouse 装置统一桩掉（见那里
        # 的说明：真连网在丢包环境下每次能撞满 15 秒超时）。
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
        # /health 是深度检查：status 字段保留，另带账号可用性明细（加字段不改字段）。
        health = (await self.client.get("/health")).json()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["accounts"]["usable"], 1)
        # 默认响应不含账号名，避免未鉴权端点泄露信息。
        self.assertNotIn("detail", health["accounts"])
        # 版本标识：未鉴权端点也带，登录页要靠它显示当前部署。
        self.assertIn("version", health)
        await self.login()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self.client.post("/admin/api/keys", json={"name": "bad"})).status_code, 403)

    async def test_version_reflects_injected_image_tag(self):
        """注入镜像 tag 后，/health 与 overview 都要显示它。

        首屏（登录页）只能读未鉴权的 /health，会话内读 overview；两处必须一致，
        否则登录前后版本显示对不上。
        """
        with mock.patch.dict(os.environ, {"WORKBUDDY_IMAGE_TAG": "main-deadbeef"}, clear=True):
            health = (await self.client.get("/health")).json()
            self.assertEqual(health["version"], "main-deadbeef")
            await self.login()
            overview = (await self.client.get("/admin/api/overview")).json()
            self.assertEqual(overview["version"]["label"], "main-deadbeef")
            self.assertEqual(overview["version"]["image_tag"], "main-deadbeef")

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

    async def test_key_reset_rotates_value_keeps_config(self):
        """重置密钥：轮换值、旧值立即失效；kid 与全部配置/用量历史保留。"""
        await self.login()
        store = self.app.state.store
        overview = (await self.client.get("/admin/api/overview")).json()
        account_id = overview["accounts"][0]["id"]
        models = [m["id"] for m in overview["models"]["models"] if m["enabled"]][:2]
        created = await self.client.post("/admin/api/keys", json={"name": "重置测试", "daily_limit": 5})
        self.assertEqual(created.status_code, 200)
        kid, value = created.json()["id"], created.json()["key"]
        # 配置全套：限额/路由/有效期/模型与账号白名单。
        expires = int(time.time()) + 86400
        result = await self.client.patch("/admin/api/keys/" + kid, json={
            "routing": "expiry_first", "expires_at": expires,
            "models": models, "accounts": [account_id]})
        self.assertEqual(result.status_code, 200, result.text)
        # 留用量历史（直接记一次）与 last_used_at（打一个真实请求）。
        store.record_key_usage(kid, tokens=42)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + value})).status_code, 200)

        async def snapshot():
            return next(k for k in (await self.client.get("/admin/api/overview")).json()["keys"] if k["id"] == kid)

        before = await snapshot()
        self.assertEqual(before["daily_limit"], 5)
        self.assertEqual(before["routing"], "expiry_first")
        self.assertEqual(before["expires_at"], expires)
        self.assertEqual(before["models"], models)
        self.assertEqual(before["accounts"], [account_id])
        self.assertEqual(before["usage"]["count"], 1)
        self.assertEqual(before["usage"]["tokens"], 42)
        self.assertIsNotNone(before["last_used_at"])

        # 重置：旧值立即失效、新值可用、kid 不变。
        reset = await self.client.post("/admin/api/keys/" + kid + "/reset")
        self.assertEqual(reset.status_code, 200, reset.text)
        newkey = reset.json()["key"]
        self.assertEqual(reset.json()["id"], kid)
        self.assertNotEqual(newkey, value)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + value})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + newkey})).status_code, 200)

        # 除值本身外一切保留；hint 跟着换（前端靠它认「哪一枚」）。
        after = await snapshot()
        for field in ("daily_limit", "routing", "expires_at", "models", "accounts", "usage", "last_used_at"):
            self.assertEqual(after[field], before[field], f"{field} 应在重置后保留")
        self.assertNotEqual(after["hint"], before["hint"])
        self.assertEqual(after["hint"], newkey[:5] + "…" + newkey[-4:])

        # 停用态保留：重置后仍停用（新值也 401），手动启用才恢复。
        self.assertEqual((await self.client.patch("/admin/api/keys/" + kid, json={"enabled": False})).status_code, 200)
        reset2 = await self.client.post("/admin/api/keys/" + kid + "/reset")
        self.assertEqual(reset2.status_code, 200)
        newkey2 = reset2.json()["key"]
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + newkey2})).status_code, 401)
        self.assertEqual((await self.client.patch("/admin/api/keys/" + kid, json={"enabled": True})).status_code, 200)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + newkey2})).status_code, 200)

        # 已撤销的密钥不可重置（撤销不可逆）；不存在的 kid 报 404。
        self.assertEqual((await self.client.delete("/admin/api/keys/" + kid)).status_code, 200)
        denied = await self.client.post("/admin/api/keys/" + kid + "/reset")
        self.assertEqual(denied.status_code, 400)
        self.assertIn("撤销", denied.json()["detail"])
        self.assertEqual((await self.client.post("/admin/api/keys/deadbeef/reset")).status_code, 404)

        # 完整密钥明文不得出现在持久层中。主库和 -wal 都要查：
        # WAL 模式下未 checkpoint 的帧只在 -wal 里。
        # 重启保留另取一枚未撤销的密钥（上面那枚已撤销，重启后必然 401）。
        created2 = await self.client.post("/admin/api/keys", json={"name": "重启保留"})
        self.assertEqual(created2.status_code, 200)
        value3 = created2.json()["key"]
        reset3 = await self.client.post("/admin/api/keys/" + created2.json()["id"] + "/reset")
        newkey3 = reset3.json()["key"]
        path = store.db.path
        for suffix in ("", "-wal"):
            blob = path.with_name(path.name + suffix).read_bytes()
            for leak in (newkey, newkey2, newkey3):
                self.assertNotIn(leak.encode(), blob, f"明文密钥泄漏到 {suffix or '主库'}")

        # 重置结果跨重启保留（路由内已同步 save）；撤销态同样跨重启保留。
        await self.client.aclose()
        self.make_app()
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + newkey3})).status_code, 200)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + value3})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + newkey2})).status_code, 401)

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

    async def test_logs_filter_by_key(self):
        """按 key_id 过滤请求记录：需求「点 API Key 查看该 key 的最近请求」。

        分页与总数必须都受限（total 算错会让前端画错页码）；不存在的 key 返回空而不是报错。
        """
        await self.login()
        store = self.app.state.store
        now = int(time.time())
        store.db.append_request_logs([
            {"time": now - i, "source": "api", "path": "/v1/chat/completions",
             "status": 200, "outcome": "success", "duration_ms": 100 + i,
             # 每 3 条一条属于 k2，其余 k1；最后 2 条无 key（管理页测试路径）。
             "key_id": ("k2" if i % 3 == 0 else "k1") if i < 23 else None,
             "account_id": "a1", "account_name": "账号",
             "model_id": "deepseek-v4-flash", "model_name": "V4", "upstream_request_id": None}
            for i in range(25)])

        p1 = (await self.client.get("/admin/api/logs?key_id=k1&limit=50")).json()
        self.assertTrue(p1["rows"], "k1 应当有记录")
        self.assertTrue(all(r["key_id"] == "k1" for r in p1["rows"]), "不得混入其它 key 的记录")
        self.assertEqual(p1["total"], len(p1["rows"]))

        p2 = (await self.client.get("/admin/api/logs?key_id=k2&limit=50")).json()
        self.assertTrue(all(r["key_id"] == "k2" for r in p2["rows"]))
        # 两个 key 的记录数加起来不应超过总数，且互不重叠。
        self.assertEqual(p1["total"] + p2["total"], 23)

        # 分页时 total 仍是「不带 limit/offset 的同条件总数」，不能退化成整表总数。
        page = (await self.client.get("/admin/api/logs?key_id=k1&limit=5&offset=0")).json()
        self.assertEqual(len(page["rows"]), 5)
        self.assertEqual(page["total"], p1["total"])

        # 不存在的 key：空结果，不是 500。
        empty = (await self.client.get("/admin/api/logs?key_id=nope")).json()
        self.assertEqual(empty["rows"], [])
        self.assertEqual(empty["total"], 0)

        # 不带 key_id 时行为不变（回归：不能把所有请求都过滤掉）。
        all_rows = (await self.client.get("/admin/api/logs?limit=50")).json()
        self.assertEqual(all_rows["total"], 25)
        await self.client.aclose()

    async def test_logs_expose_backend(self):
        """request_logs.backend 必须透到 /admin/api/logs：最近请求要能区分 trae / workbuddy。

        这是「后端隔离」在**可观测性**上的对应项——光写入不返回，界面依然分不清链路。
        老记录（账号已删）的 backend 为 NULL，接口要原样返回而不是替成 'workbuddy'。

        这里刻意**走真实请求链路**（MetricsMiddleware 写日志）而不是直接 append 行：
        直接插库只能证明读接口透传，证明不了 backend 真的在写入点被取到。
        """
        await self.login()
        store = self.app.state.store
        # 两个账号，各属一个后端。
        store.db.save_accounts(
            {"w1": {"name": "腾讯号", "file": "w.info", "enabled": True, "created": 1,
                    "backend": "workbuddy"},
             "t1": {"name": "字节号", "file": "t.info", "enabled": True, "created": 1,
                    "backend": "trae"}},
            {})
        seen = {}

        async def inner(scope, receive, send):
            # MetricsMiddleware 的外层，模拟 PoolMiddleware 挂上下文。
            seen["scope"] = scope
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = MetricsMiddleware(inner, RequestMetrics(), source="api", store=store)
        from admin.pool import REQUEST_LOG_CONTEXT
        for account_id, backend in (("w1", "workbuddy"), ("t1", "trae")):
            scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
                     "headers": [],
                     REQUEST_LOG_CONTEXT: {"key_id": "k1", "account_id": account_id,
                                           "account_name": None, "backend": backend,
                                           "model_id": "hy3", "model_name": "hy3",
                                           "error_code": None}}
            await middleware(scope, _empty_receive, _noop_send)

        # 缓冲落库（平时由 5 秒防抖触发）。
        store.save()
        rows = (await self.client.get("/admin/api/logs?limit=10")).json()["rows"]
        by_account = {r["account_id"]: r["backend"] for r in rows}
        self.assertEqual(by_account["w1"], "workbuddy")
        self.assertEqual(by_account["t1"], "trae",
                         "trae 账号的请求必须记成 trae，否则界面会把字节请求标成腾讯")
        await self.client.aclose()

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
        # 关键：留痕必须**落库**。只断言内存字典会漏掉「字段不在白名单里、
        # 重启即丢」这类 bug（usage_manual_synced_at 曾长期落不了库）。
        store.save()
        rows = store.db.query("SELECT usage_manual_synced_at FROM accounts WHERE id='a1'")
        self.assertTrue(rows, "账号未落库，留痕断言无意义")
        self.assertIsNotNone(rows[0]["usage_manual_synced_at"])
        # 防抖：60 秒内重复拉取被节流（HTTPException 转成 skipped，不是 500）。
        again = (await self.client.post("/admin/api/usage/sync", json={
            "start": now - 7200, "end": now})).json()
        self.assertEqual(again["synced"], 0)
        self.assertTrue(any("刚拉取过" in (x.get("reason") or "") for x in again["skipped"]))

        # Trae 账号也进同步队列：现在每个后端各有自己的上游用量接口
        # （WorkBuddy 打计费域、Trae 打 api.trae.cn 的按 session 聚合），
        # 由后端自己声明、pool 只读声明 —— 不再是「只有默认后端能对账」。
        store.data["accounts"]["t1"] = {
            "name": "Trae号", "file": "session.info", "enabled": True,
            "created": now, "backend": "trae"}
        called.clear()
        result = await self.client.post("/admin/api/usage/sync", json={
            "start": now - 7200, "end": now})
        self.assertIn("t1", called)
        # 显式指定「没有用量接口的后端」：给出明确原因，而不是「没有可拉取的账号」
        # 让人以为选错人。后端没有该能力时，给出的是声明，不是名字。
        import admin.pool as pool_mod
        real_spec_for = pool_mod.spec_for

        def spec_without_usage(name):
            spec = real_spec_for(name)
            if getattr(spec, "supports_upstream_usage", False):
                class _NoUsage:
                    supports_upstream_usage = False
                return _NoUsage()
            return spec
        pool_mod.spec_for = spec_without_usage
        try:
            picked = (await self.client.post("/admin/api/usage/sync", json={
                "start": now - 7200, "end": now, "account_id": "t1"})).json()
        finally:
            pool_mod.spec_for = real_spec_for
        self.assertEqual(picked["message"], "该后端没有上游用量对账接口")

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

    async def test_queue_failover_settings_and_validation(self):
        """容量队列自动切换的设置项：默认关闭、可开启、非法值拒绝、重启保留。"""
        await self.login()
        overview = (await self.client.get("/admin/api/overview")).json()
        # 默认关闭：这是唯一会重放请求的功能，必须用户显式打开。
        self.assertFalse(overview["pool"]["queue_failover"])
        self.assertTrue(overview["pool"]["queue_model_fallback"])
        saved = await self.client.patch("/admin/api/pool/settings", json={
            "queue_failover": True, "queue_retry": 4, "queue_cooldown": 90,
            "queue_hold_ms": 1500, "queue_model_fallback": False,
            "queue_fallback_model": "fast-model"})
        self.assertEqual(saved.status_code, 200, saved.text)
        pool = saved.json()["pool"]
        self.assertTrue(pool["queue_failover"])
        self.assertEqual((pool["queue_retry"], pool["queue_cooldown"]), (4, 90))
        self.assertEqual(pool["queue_hold_ms"], 1500)
        self.assertFalse(pool["queue_model_fallback"])
        # 非法值一律 400，不要把坏配置带到运行期。
        for bad in ({"queue_retry": 0}, {"queue_retry": 99}, {"queue_cooldown": -1},
                    {"queue_cooldown": 99999}, {"queue_hold_ms": -5},
                    {"queue_hold_ms": 60000}, {"queue_failover": "yes"},
                    {"queue_model_fallback": 1}, {"queue_fallback_model": ""}):
            response = await self.client.patch("/admin/api/pool/settings", json=bad)
            self.assertEqual(response.status_code, 400, f"{bad} 应被拒绝")
        # 重启后设置仍在：重建整个应用（重新读库）也必须读回同一个值。
        self.make_app()
        await self.login()
        again = (await self.client.get("/admin/api/overview")).json()["pool"]
        self.assertTrue(again["queue_failover"])
        self.assertEqual(again["queue_retry"], 4)
        self.assertEqual(again["queue_hold_ms"], 1500)

    async def test_guard_settings_default_off_and_validated(self):
        """账号限额护栏：默认全关、可开启、非法值拒绝、重启保留。"""
        await self.login()
        overview = (await self.client.get("/admin/api/overview")).json()
        # 默认全 0 = 关闭。护栏会主动拒绝请求，默认必须无为。
        for field in ("guard_reserve_credits", "guard_daily_token_limit",
                      "guard_daily_credit_limit", "guard_model_daily_token_limit"):
            self.assertEqual(overview["pool"][field], 0, field)
        saved = await self.client.patch("/admin/api/pool/settings", json={
            "guard_reserve_credits": 100, "guard_daily_token_limit": 5000,
            "guard_daily_credit_limit": 50, "guard_model_daily_token_limit": 2000})
        self.assertEqual(saved.status_code, 200, saved.text)
        pool = saved.json()["pool"]
        self.assertEqual(pool["guard_reserve_credits"], 100)
        self.assertEqual(pool["guard_daily_token_limit"], 5000)
        self.assertEqual(pool["guard_daily_credit_limit"], 50)
        self.assertEqual(pool["guard_model_daily_token_limit"], 2000)
        # 非法值一律 400：负数会绕过 0=关闭 的语义，必须挡在入口。
        for bad in ({"guard_reserve_credits": -1}, {"guard_daily_token_limit": "abc"},
                    {"guard_daily_credit_limit": 10**12},
                    {"guard_model_daily_token_limit": -100}):
            response = await self.client.patch("/admin/api/pool/settings", json=bad)
            self.assertEqual(response.status_code, 400, f"{bad} 应被拒绝")
        # 重启后设置仍在。
        self.make_app()
        await self.login()
        again = (await self.client.get("/admin/api/overview")).json()["pool"]
        self.assertEqual(again["guard_reserve_credits"], 100)
        self.assertEqual(again["guard_model_daily_token_limit"], 2000)

    async def test_custom_model_add_hide_and_callable(self):
        """手动添加模型：能保存、不被拉取冲掉、来源区分上游与自定义。"""
        await self.login()
        # 不存在的 id -> 新建为自定义。
        result = await self.client.post("/admin/api/models", json={
            "id": "my-hidden-model", "name": "我的隐藏模型", "credits": 0.5,
            "max_input_tokens": 64000, "enabled": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["source"], "custom")

        async def catalog():
            return {m["id"]: m for m in
                    (await self.client.get("/admin/api/overview")).json()["models"]["models"]}

        models = await catalog()
        self.assertTrue(models["my-hidden-model"]["custom"])
        self.assertEqual(models["my-hidden-model"]["source"], "custom")
        # 幂等：重复提交同一 id 不报错，视为更新。
        self.assertEqual((await self.client.post("/admin/api/models",
                         json={"id": "my-hidden-model", "name": "改名"})).status_code, 200)
        self.assertEqual((await catalog())["my-hidden-model"]["name"], "改名")
        # 上游已有的模型只改字段，来源保持上游（否则它就不跟着上游刷新了）。
        upstream_id = next(mid for mid, m in models.items() if not m["custom"])
        kept = await self.client.post("/admin/api/models", json={"id": upstream_id, "name": "改过的名字"})
        self.assertEqual(kept.status_code, 200)
        self.assertNotEqual(kept.json()["source"], "custom")
        self.assertFalse((await catalog())[upstream_id]["custom"])
        # 删除：自定义项可删，上游模型不可删。
        self.assertEqual((await self.client.delete("/admin/api/models/my-hidden-model")).status_code, 200)
        self.assertNotIn("my-hidden-model", await catalog())
        self.assertEqual((await self.client.delete("/admin/api/models/" + upstream_id)).status_code, 400)

    async def test_delete_prefixed_custom_model(self):
        """带后端前缀的自定义模型必须能删（`{mid:path}` 转换器）。

        模型 id 的线上格式可以带前缀（`trae/xxx`）。默认路径转换器不匹配斜杠，
        前端 `encodeURIComponent` 送来的 `%2F` 也变不成分隔符 —— 路由不改成
        `:path` 就会 404。归属多选让这类行变得常见，所以这条必须有。
        """
        await self.login()
        created = await self.client.post("/admin/api/models",
                                         json={"id": "trae/probe-model", "name": "探针"})
        self.assertEqual(created.status_code, 200)
        catalog = self.app.state.model_registry
        self.assertIn("trae/probe-model", catalog.snapshot_ids())
        resp = await self.client.delete("/admin/api/models/trae%2Fprobe-model")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("trae/probe-model", catalog.snapshot_ids())

    async def test_custom_model_is_callable(self):
        """上游菜单没列出的模型：加进来之后能调，禁用才被拦。"""
        await self.login()
        await self.client.post("/admin/api/models", json={"id": "ghost-callable", "enabled": True})
        registry = self.app.state.model_registry
        # 可调用列表含它 —— 上游没返回不影响放行。
        self.assertIn("ghost-callable", registry.enabled_models())
        # 已知 id 必须含它，否则 PoolMiddleware 会当「未知模型」放行，禁用标记失效。
        self.assertIn("ghost-callable", registry.snapshot_ids())
        await self.client.patch("/admin/api/models/ghost-callable", json={"enabled": False})
        self.assertNotIn("ghost-callable", registry.enabled_models())
        self.assertIn("ghost-callable", registry.snapshot_ids())

    async def test_model_add_rejects_bad_input(self):
        await self.login()
        for body in [{"id": ""}, {"id": "   "}, {"id": "has space"}, {"id": "x" * 129},
                     {"id": "ok-id", "credits": -1}, {"id": "ok-id", "credits": True},
                     {"id": "ok-id", "max_input_tokens": 0},
                     {"id": "ok-id", "max_input_tokens": 10 ** 9},
                     {"id": "ok-id", "enabled": "yes"}]:
            resp = await self.client.post("/admin/api/models", json=body)
            self.assertEqual(resp.status_code, 400, f"{body} -> {resp.status_code}")
        self.assertEqual((await self.client.delete("/admin/api/models/nope")).status_code, 404)


    @pytest.mark.real_catalog_check  # 本用例测的就是 _stale_catalog 本身，别被桩掉
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


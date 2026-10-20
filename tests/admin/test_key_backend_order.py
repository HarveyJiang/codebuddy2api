"""单 key 的后端优先顺序（`keys.model_backend_order`）。

对应 `specs/bare-name-routing-plan.md` §6.2(a)（用户 2026-10-16 拍板范围）。

## 为什么单开一层测试

裸名路由的**顺序**有两层：全局（`settings.model_backend_order`）与单 key
覆盖（`keys.model_backend_order`）。路由判定（`admin/pool.py` 的
`_resolve_backend_for` → `build_order(key_configured=...)`）**早就能读**单 key
覆盖 —— 只是没有列可存、也没有入口可改，于是这个功能一直是「代码支持、
产品上没有」。

同款问题在全局层已经踩过一次：`PATCH /admin/api/pool/settings` 的白名单里
没有这个键，提交返回 200 却什么都没存，表现为「调了序但落点没变」
（见 `tests/admin/test_bare_routing_settings.py`）。所以这里钉四件事：

1. **迁移**：v22 的库升级后有列，存量行是 NULL —— 升级不得改变任何路由行为；
2. **落库往返**：写进去是 JSON 文本，读回来必须是**列表**（`build_order` 直接
   吃列表；收到字符串会静默按字符遍历，顺序永远错乱却不报错）；
3. **接口**：存得进去、清得掉、非法形状 400 —— 且非法时**不留半个 key**；
4. **生效**：单 key 的覆盖真的改变该 key 的落点，没配的 key 仍跟随全局。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from admin.db import SCHEMA_VERSION, Database
from admin.server import create_app
from core.bare_routing import decode_backend_order, encode_backend_order

API = "sk-test-client-key"
ADMIN = "admin-key-long-enough-1234"

_REAL_ASYNC_CLIENT = httpx.AsyncClient

# v22 的 keys 表（还没有 model_backend_order 列）。
_V22_KEYS_DDL = """CREATE TABLE keys (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL, hint TEXT NOT NULL,
    created INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
    revoked_at INTEGER, expires_at INTEGER, daily_limit INTEGER,
    usage_date TEXT, usage_count INTEGER NOT NULL DEFAULT 0,
    usage_tokens INTEGER NOT NULL DEFAULT 0,
    models TEXT NOT NULL DEFAULT '[]', accounts TEXT NOT NULL DEFAULT '[]', routing TEXT,
    last_used_at INTEGER)"""


# --------------------------------------------------------------------------
# 1. 编解码：全局与单 key 共用同一份规则
# --------------------------------------------------------------------------

class BackendOrderCodecTests(unittest.TestCase):
    """落库形态 ↔ 内存形态的编解码（`core/bare_routing`）。"""

    def test_server_reexports_the_core_codec(self):
        """admin/server 的别名必须**就是** core 里的那一份。

        两层顺序（全局 / 单 key）共用一套编解码，靠的就是「只有一份实现」。
        哪天 admin 侧又抄了一份，两层就会漂移 —— 所以这里钉住它们是同一个函数
        对象，而不是「行为恰好一样」。
        """
        from admin import server
        self.assertIs(server._decode_backend_order, decode_backend_order)
        self.assertIs(server._dumps_backend_order, encode_backend_order)

    def test_unconfigured_is_null_not_empty_json(self):
        """None / 空列表 → 落库 NULL（不是 '[]'，也不是 'null'）。"""
        self.assertIsNone(encode_backend_order(None))
        self.assertIsNone(encode_backend_order([]))

    def test_roundtrip_preserves_order(self):
        order = ["trae", "workbuddy"]
        self.assertEqual(decode_backend_order(encode_backend_order(order)), order)

    def test_decode_garbage_is_treated_as_unconfigured(self):
        """脏值当「未配置」：顺序读坏了回落到默认顺序是最安全的降级。"""
        for raw in ("not json", "{}", '"a string"', "123", ""):
            self.assertIsNone(decode_backend_order(raw), raw)

    def test_decode_dedupes_and_keeps_order(self):
        self.assertEqual(decode_backend_order('["trae","trae","workbuddy"]'),
                         ["trae", "workbuddy"])


# --------------------------------------------------------------------------
# 2. 迁移：v22 → v23
# --------------------------------------------------------------------------

class KeyOrderMigrationTests(unittest.TestCase):
    """keys 补 `model_backend_order` 列（v23）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_version_is_at_least_23(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 23)

    def test_migration_adds_column_to_a_v22_database(self):
        """v22 的库升级后必须有列，且**存量行是 NULL**。

        NULL = 未配置 = 跟随全局：升级本身不得改变任何 key 的路由行为。
        """
        path = self.root / "v22.db"
        con = sqlite3.connect(path)
        # 迁移靠 schema_meta 判断当前版本（不是 PRAGMA user_version —— 那恒为 0）。
        con.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '22')")
        con.execute(_V22_KEYS_DDL)
        con.execute("INSERT INTO keys (id, name, hash, hint) VALUES "
                    "('k1', '旧 key', 'hash-1', 'sk-w…')")
        con.commit()
        con.close()

        db = Database(path)
        try:
            cols = {r[1] for r in db._write.execute("PRAGMA table_info(keys)")}
            self.assertIn("model_backend_order", cols)
            row = db._write.execute(
                "SELECT model_backend_order FROM keys WHERE id='k1'").fetchone()
            self.assertIsNone(row[0], "存量 key 升级后必须仍是「未配置」")
            self.assertIsNone(db.load_keys()["k1"]["model_backend_order"])
            self.assertEqual(
                db.query("SELECT value FROM schema_meta WHERE key='version'")[0][0],
                str(SCHEMA_VERSION))
        finally:
            db.close()

    def test_fresh_database_has_the_column(self):
        """新库直接带这列（KEYS_DDL 里就有，不是靠迁移补的）。"""
        db = Database(self.root / "fresh.db")
        try:
            cols = {r[1] for r in db._write.execute("PRAGMA table_info(keys)")}
            self.assertIn("model_backend_order", cols)
        finally:
            db.close()


# --------------------------------------------------------------------------
# 3. 落库往返
# --------------------------------------------------------------------------

class KeyOrderPersistenceTests(unittest.TestCase):
    """写进去的是 JSON 文本，读回来是**列表**。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _key(self, kid, order=None):
        return {"id": kid, "name": kid, "hash": "h-" + kid, "hint": "sk-w…",
                "created": 0, "enabled": 1, "revoked_at": None, "expires_at": None,
                "daily_limit": None, "usage": {}, "models": [], "accounts": [],
                "routing": None, "last_used_at": None, "model_backend_order": order}

    def test_order_survives_reopen(self):
        path = self.root / "keys.db"
        db = Database(path)
        try:
            db.save_keys({"k1": self._key("k1", ["trae", "workbuddy"])})
        finally:
            db.close()
        again = Database(path)
        try:
            raw = again._write.execute(
                "SELECT model_backend_order FROM keys WHERE id='k1'").fetchone()[0]
            self.assertEqual(json.loads(raw), ["trae", "workbuddy"], "落库是 JSON 文本")
            value = again.load_keys()["k1"]["model_backend_order"]
            self.assertIsInstance(value, list, f"读回来必须是 list，实际 {type(value)}")
            self.assertEqual(value, ["trae", "workbuddy"])
        finally:
            again.close()

    def test_unconfigured_key_stays_null(self):
        path = self.root / "keys2.db"
        db = Database(path)
        try:
            db.save_keys({"k1": self._key("k1")})
        finally:
            db.close()
        again = Database(path)
        try:
            raw = again._write.execute(
                "SELECT model_backend_order FROM keys WHERE id='k1'").fetchone()[0]
            self.assertIsNone(raw, "未配置 = NULL")
            self.assertIsNone(again.load_keys()["k1"]["model_backend_order"])
        finally:
            again.close()


# --------------------------------------------------------------------------
# 4. 接口：存得进去、清得掉、非法形状 400
# --------------------------------------------------------------------------

class KeyOrderApiTests(unittest.TestCase):
    """`POST /admin/api/keys` 与 `PATCH /admin/api/keys/{kid}`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.app = create_app(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        # https：管理 cookie 带 Secure，http 下浏览器语义不回传（实测 401）。
        self.client = TestClient(self.app, base_url="https://console.test")
        login = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.headers = {"X-CSRF-Token": login.json()["csrf"]}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def _create(self, body):
        return self.client.post("/admin/api/keys", json=body, headers=self.headers)

    def test_new_key_stores_its_order(self):
        r = self._create({"name": "有序 key", "model_backend_order": ["trae", "workbuddy"]})
        self.assertEqual(r.status_code, 200, r.text)
        kid = r.json()["id"]
        item = self.app.state.store.data["keys"][kid]
        self.assertEqual(item["model_backend_order"], ["trae", "workbuddy"],
                         "内存里必须是列表（build_order 直接吃列表）")
        self.assertEqual(self.app.state.store.db.load_keys()[kid]["model_backend_order"],
                         ["trae", "workbuddy"], "并且真的落库了")

    def test_new_key_without_order_follows_global(self):
        """不配 = 跟随全局（NULL），不是「空列表」也不是「默认顺序写死」。"""
        r = self._create({"name": "普通 key"})
        self.assertEqual(r.status_code, 200, r.text)
        kid = r.json()["id"]
        self.assertIsNone(self.app.state.store.data["keys"][kid]["model_backend_order"])

    def test_invalid_order_is_rejected_and_no_key_is_created(self):
        """非法形状整单拒绝 —— 不能先建出半个 key 再报错。"""
        before = set(self.app.state.store.data["keys"])
        for bad in (["ghost"], "trae", 5):
            r = self._create({"name": "坏 key", "model_backend_order": bad})
            self.assertEqual(r.status_code, 400, f"{bad!r} 应当被拒")
        self.assertEqual(set(self.app.state.store.data["keys"]), before,
                         "被拒的请求不得留下 key")

    def test_unknown_backend_name_is_rejected(self):
        r = self._create({"name": "坏 key", "model_backend_order": ["ghost"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ghost", r.json()["detail"])

    def test_patch_sets_and_clears_the_order(self):
        kid = self._create({"name": "可改 key"}).json()["id"]
        r = self.client.patch(f"/admin/api/keys/{kid}",
                              json={"model_backend_order": ["trae"]}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.app.state.store.data["keys"][kid]["model_backend_order"],
                         ["trae"])
        # 清空 = 回到「跟随全局」。
        r = self.client.patch(f"/admin/api/keys/{kid}",
                              json={"model_backend_order": None}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.app.state.store.data["keys"][kid]["model_backend_order"])
        self.assertIsNone(self.app.state.store.db.load_keys()[kid]["model_backend_order"])

    def test_patch_rejects_unknown_backend_and_keeps_the_old_value(self):
        kid = self._create({"name": "可改 key",
                            "model_backend_order": ["trae"]}).json()["id"]
        r = self.client.patch(f"/admin/api/keys/{kid}",
                              json={"model_backend_order": ["ghost"]}, headers=self.headers)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.app.state.store.data["keys"][kid]["model_backend_order"],
                         ["trae"], "被拒的改动不得把原值冲掉")

    def test_order_survives_restart(self):
        """重启后仍在（否则「配了但重启就没了」是最阴的一类 bug）。"""
        kid = self._create({"name": "重启 key",
                            "model_backend_order": ["trae", "workbuddy"]}).json()["id"]
        self.client.close()
        again = create_app(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        try:
            value = again.state.store.data["keys"][kid]["model_backend_order"]
            self.assertIsInstance(value, list, f"重启后必须是 list，实际 {type(value)}")
            self.assertEqual(value, ["trae", "workbuddy"])
        finally:
            again.state.store.db.close()


# --------------------------------------------------------------------------
# 5. 生效：单 key 的顺序真的改变落点
# --------------------------------------------------------------------------

def _wb_credential():
    """WorkBuddy 凭据：`expiresAt` 是**毫秒**。"""
    return {
        "account": {"uid": "wb-user", "nickname": "WB测试", "enterpriseId": "ent"},
        "auth": {"accessToken": "WB-ACCESS", "refreshToken": "WB-REFRESH",
                 "expiresAt": int(time.time() * 1000) + 3_600_000},
    }


def _trae_credential():
    """Trae 凭据：`expiresAt` 是 **Unix 秒**。"""
    from core.backends import trae as T
    return {
        "backend": "trae",
        "account": {"uid": "trae-user", "nickname": "Trae测试", "enterpriseId": "ent"},
        "auth": {"accessToken": "TRAE-ACCESS", "refreshToken": "TRAE-REFRESH",
                 "expiresAt": int(time.time()) + 30 * 86400,
                 "domain": "trae.cn", "apiHost": T.OAUTH_HOST,
                 "machineId": "m" * 32, "deviceId": "d" * 32},
    }


class KeyOrderRoutingTests(unittest.IsolatedAsyncioTestCase):
    """端到端：单 key 的顺序改变**这一枚 key** 的落点。

    这是接线测试：把 `admin/pool.py` 里 `build_order(key_configured=...)` 那行
    删掉（回到「只有全局顺序」），下面的用例就会红 —— 纯函数测试抓不住这种
    回归，因为函数本身没变。
    """

    async def asyncSetUp(self):
        import admin.models as admin_models
        import admin.server as admin_server

        self.admin_server = admin_server
        self.admin_models = admin_models
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "wb.info").write_text(json.dumps(_wb_credential()), encoding="utf-8")
        (self.auth / "trae-u1.info").write_text(json.dumps(_trae_credential()), encoding="utf-8")
        self._orig_fetch = admin_models.ModelRegistry.fetch
        admin_models.ModelRegistry.fetch = lambda self: False   # 免网络：只用内置目录
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=self.app),
                                         base_url="https://console.test")
        # 全局顺序：Trae 优先。没配覆盖的 key 应当跟着它走。
        self.app.state.store.data["pool"]["model_backend_order"] = ["trae", "workbuddy"]

    async def asyncTearDown(self):
        await self.client.aclose()
        self.admin_server.converter.httpx.AsyncClient = _REAL_ASYNC_CLIENT
        self.admin_models.ModelRegistry.fetch = self._orig_fetch
        self.tmp.cleanup()

    def _patch_upstream(self):
        sse = (b'data: {"model":"m","choices":[{"delta":{"content":"ok"},'
               b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=sse))
        self.admin_server.converter.httpx.AsyncClient = \
            lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)

    def _last_log_backend(self):
        """最近一次请求实际落在哪个后端（看 `request_logs.backend` 的缓冲）。"""
        rows = [r for r in self.app.state.store.log_buffer if r.get("path")]
        return rows[-1].get("backend") if rows else None

    async def _create_key(self, name, order=None):
        login = await self.client.post("/admin/api/login", json={"key": ADMIN})
        csrf = login.json()["csrf"]
        body = {"name": name}
        if order is not None:
            body["model_backend_order"] = order
        r = await self.client.post("/admin/api/keys", json=body,
                                   headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["key"]

    async def _post(self, key, model="glm-5.3"):
        self._patch_upstream()
        return await self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]})

    async def test_key_without_override_follows_the_global_order(self):
        """没配覆盖 → 跟随全局顺序（这里全局是 Trae 优先）。"""
        key = await self._create_key("跟随全局")
        r = await self._post(key)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "trae")

    async def test_key_override_wins_over_the_global_order(self):
        """单 key 的覆盖**压过**全局顺序：这一枚 key 落到 WorkBuddy。"""
        key = await self._create_key("自带顺序", ["workbuddy", "trae"])
        r = await self._post(key)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "workbuddy")

    async def test_override_only_affects_its_own_key(self):
        """覆盖是**按 key** 的：另一枚没配的 key 仍跟着全局走。"""
        pinned = await self._create_key("自带顺序", ["workbuddy", "trae"])
        plain = await self._create_key("跟随全局")
        r = await self._post(pinned)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "workbuddy")
        r = await self._post(plain)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._last_log_backend(), "trae",
                         "没配覆盖的 key 不得被别人的配置带偏")


if __name__ == "__main__":
    unittest.main()

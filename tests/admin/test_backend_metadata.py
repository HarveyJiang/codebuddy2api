"""`GET /admin/api/backends` 的契约（需求 2 / 5 / 7 的前端依赖）。

前端**不再登记后端元数据**：图标、显示名、颜色、顺序、模型前缀、登录方式全部
从这里取。所以这个端点的字段名是前端与后端的接口 —— 少一个字段，页面上就是
一个「静默显示成默认值」的坑（正是第 3 阶段要消灭的那类 bug），必须锁住。

同时验「账号数按后端统计」：需求 5/6 的账号表格与筛选要用它。
"""
from __future__ import annotations

import json
import re
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
            for key in ("name", "label", "short", "icon", "icon_svg", "color", "order",
                        "prefix", "login_kind", "enabled", "default", "accounts"):
                self.assertIn(key, row, f"{row.get('name')} 缺字段 {key}")
            self.assertIsInstance(row["accounts"], int)
            self.assertIsInstance(row["enabled"], bool)

    async def test_every_backend_declares_a_sized_svg_icon(self):
        """每个后端都要有 icon_svg，且是 24×24 viewBox 的单个 <svg>。

        为什么必须有：字符图标的 EAW 不一致（◈ 窄、⚡ 宽且是 emoji 字形），
        同字号下字形盒能差一倍 —— 只有 SVG 能让两个后端的图标真正同尺寸。
        这条钉住「声明齐了」，前端 backendIconSvg 才不会退化成字符回落。
        """
        await self.login()
        rows = (await self.client.get("/admin/api/backends")).json()["backends"]
        for row in rows:
            svg = row["icon_svg"]
            self.assertTrue(svg, f"{row['name']} 没声明 icon_svg")
            self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"),
                            f"{row['name']} 的 icon_svg 不是单个 <svg> 元素")
            self.assertIn('viewBox="0 0 24 24"', svg, f"{row['name']} 的 viewBox 不是 24×24")
            # 尺寸交给 CSS，不由 SVG 自带 width/height —— 否则两个后端各写一个尺寸，
            # 又回到「大小不统一」。fill 用 currentColor 才能接住后端声明的颜色。
            self.assertNotIn("<svg", svg.replace("<svg", "", 1))
            head = svg.split(">", 1)[0]
            self.assertIsNone(re.search(r'(?<![\w-])(?:width|height)\s*=', head),
                              f"{row['name']} 的 <svg> 自带 width/height（应交给 CSS）")
            self.assertIn("currentColor", svg)

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

    async def test_canonical_for_only_declares_non_identity_mappings(self):
        """各后端的 `canonical_for` 只允许声明**非恒等**映射（§4 填写规则）。

        恒等映射（`{"glm-5.3": "glm-5.3"}`）没有信息量，却让「加一个模型」变成
        「记得也来这里抄一遍 id」——抄漏不报错、只是静默不合组，是最难查的那种。
        默认规则已经覆盖「剥前缀即规范名」，所以拒绝显式写恒等。
        """
        from core.backends.registry import known_names, spec_for

        for name in known_names():
            spec = spec_for(name)
            prefix = spec.model_prefix
            for upstream, canon in (spec.canonical_for or {}).items():
                self.assertNotEqual(
                    upstream, canon,
                    f"{name} 声明了恒等映射 {upstream!r} → {canon!r}：删掉即可，"
                    "默认规则已经覆盖它")
                # 声明键应写**上游 id**（不带上本后端前缀），值是非空字符串。
                self.assertFalse(
                    prefix and str(upstream).startswith(prefix),
                    f"{name} 的 canonical_for 键 {upstream!r} 带上了本后端前缀 "
                    f"{prefix!r}：键写上游 id，前缀是本后端的属性不必抄进来")
                self.assertTrue(str(canon).strip(), f"{name} 的 {upstream!r} 规范名为空")

    async def test_canonical_merges_kimi_k3_across_backends(self):
        """真实数据回归：腾讯 `kimi-k3-1` 与字节 `trae/kimi-k3` 必须同规范名。

        这是这套机制存在的理由（用户 2026-10-16：「trae/kimi-k3，kimi-k3-1 怎么
        没有合并」）：两边上游 id 命名不一致，只靠字符串规则永远合不起来，
        必须在腾讯侧显式声明 `kimi-k3-1 → kimi-k3`。
        """
        from core.backends.registry import canonical_name, strip_prefix

        wb, trae = canonical_name("kimi-k3-1"), canonical_name("trae/kimi-k3")
        self.assertEqual(wb, trae, "kimi-k3 在两个后端必须归到同一规范名")
        self.assertEqual(wb, "kimi-k3")
        # 反例守卫：规范化之后仍然是**各自剥前缀**，不能把裸名也改掉
        # （裸名是路由键与账单 join 键，动它会断账本）。
        self.assertEqual(strip_prefix("kimi-k3-1"), "kimi-k3-1")
        self.assertEqual(strip_prefix("trae/kimi-k3"), "kimi-k3")

    async def test_canonical_does_not_merge_distinct_models(self):
        """反例守卫：`hy3` 与 `hy3-x` 是**两个**真模型，规范名必须不同。

        它们展示名都叫「Hy3」（实测），若按展示名归并就会被并成一个 ——
        这正是不能用展示名当归一化键的原因。规范名来自显式声明，
        没声明就是各自的裸名，天然不同。
        """
        from core.backends.registry import canonical_name

        self.assertNotEqual(canonical_name("hy3"), canonical_name("hy3-x"))


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

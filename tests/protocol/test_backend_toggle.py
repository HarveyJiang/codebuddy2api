"""后端总闸：设置页启用/禁用后端（specs/backend-toggle.md）。

## 这个功能想守住什么

用户能在设置页把某个后端整体下线（不再拉目录、不参与选号签到、模型列表里
消失），数据全部保留，重新启用即恢复。它同时是「大前提」的用户侧落地——
**下线一个后端不等于下线服务**。

## 为什么测试写成这样

这个功能的每一条语义都**只靠 UI 点不出来**：要验证「禁用后行为消失」得真把
后端关掉，而关掉的手段就是被测对象本身。所以这里大量使用**注入 provider**
直接改内存状态，而不是走 HTTP——这样测试与「怎么保存」解耦，改存储实现时
语义测试不必跟着重写。

三条不变量各自单独钉死，因为它们都是**行为测试抓不住**的那类：
1. 「启用集合恒 ≥ 1」——违反它不会让任何别的测试变红，只会让线上服务
   活着但没功能；
2. 「环境逃生门优先于总闸」——两层开关的优先级；
3. 「关掉再启用原样回来」——验证数据真的没被动过（只看行为容易放过
   「重新拉了一遍目录」这种副作用）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from admin.server import Store, create_app
from core.backends import registry
from core.backends.base import BackendSpec

ADMIN = "admin-key-long-enough-12345"
API = "client-key"


def _write_cred(auth, name, backend="workbuddy", expires_delta=3600):
    """造一个能通过 validate_credential 的凭据文件（缺字段账号根本建不起来）。"""
    (auth / f"{name}.info").write_text(json.dumps({
        "backend": backend,
        "auth": {"accessToken": f"token-{name}",
                 "expiresAt": int((time.time() + expires_delta) * 1000),
                 "refreshToken": "r"},
        "account": {"nickname": f"昵称-{name}", "uid": name, "enterpriseId": "e"},
    }), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_provider():
    """每个用例前后清掉注入点：registry 是模块级全局，漏清会串到别的测试。"""
    yield
    registry.set_enabled_provider(None)
    os.environ.pop("CODEBUDDY_ENABLE_TRAE", None)


# --------------------------------------------------------------------------
# 1. 声明式：is_enabled 不认后端名
# --------------------------------------------------------------------------

def test_kill_switch_is_declared_not_name_branched():
    """逃生门由 ``BackendSpec.kill_switch_env`` 声明，``is_enabled`` 里不许出现后端名。

    变异验证：把 ``is_enabled`` 改回 ``if name == "trae": return False``，
    现有行为测试**全绿**（当前就两个后端，认名字和读声明结果一样）。所以这条
    只能靠结构守——它守的是「加第三个后端时不用改这里」这个承诺。
    """
    import inspect
    src = inspect.getsource(registry.is_enabled)
    assert "trae" not in src and "workbuddy" not in src, (
        "is_enabled 出现了后端名，请改成读 BackendSpec 声明")


def test_only_trae_declares_kill_switch_env():
    """声明自洽：逃生门环境变量名不重复，且 Trae 声明了它。"""
    envs = [(n, registry.spec_for(n).kill_switch_env) for n in registry.known_names()]
    declared = [e for _, e in envs if e]
    assert len(declared) == len(set(declared)), f"逃生门环境变量重复：{envs}"
    assert registry.spec_for("trae").kill_switch_env == "CODEBUDDY_ENABLE_TRAE"
    # 默认后端没有逃生门：总闸是它唯一的下线手段（对称，见 specs §4）。
    assert registry.spec_for("workbuddy").kill_switch_env is None


def test_default_is_all_enabled_without_provider():
    """没注入数据源时全启用——总闸上线前的行为，也是脚本/测试的默认。"""
    registry.set_enabled_provider(None)
    assert all(registry.is_enabled(n) for n in registry.known_names())


# --------------------------------------------------------------------------
# 2. 语义：禁用后行为消失 / 启用后恢复
# --------------------------------------------------------------------------

class _StoreFixture(unittest.TestCase):
    """带两个后端各一个账号的 Store。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.root / "mgmt").mkdir()
        _write_cred(self.auth, "wb", "workbuddy")
        _write_cred(self.auth, "tr", "trae")
        self.store = Store(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        registry.set_enabled_provider(self.store.backend_flags)
        # 不依赖凭据收养：直接塞内存，省掉一套凭据格式的样板。
        self.store.data["accounts"] = {
            "wb1": {"name": "wb", "file": "wb.info", "enabled": True, "created": 0, "backend": "workbuddy"},
            "tr1": {"name": "tr", "file": "tr.info", "enabled": True, "created": 0, "backend": "trae"},
        }

    def tearDown(self):
        registry.set_enabled_provider(None)
        self.store.db.close()

    def _rows_backends(self):
        return {r["backend"] for r in self.store.account_rows()}

    def test_account_rows_hides_disabled_backend(self):
        """拍板项①：账号行**隐藏**（不是置灰）。"""
        self.assertEqual(self._rows_backends(), {"workbuddy", "trae"})
        self.store.set_backend_flags({"trae": False})
        self.assertEqual(self._rows_backends(), {"workbuddy"})
        self.store.set_backend_flags({"trae": True})
        self.assertEqual(self._rows_backends(), {"workbuddy", "trae"})

    def test_health_count_matches_visible_rows(self):
        """计数与表格行数必须对得上：否则用户看到「共 4 个」而表格只有 2 行。"""
        self.store.set_backend_flags({"trae": False})
        detail = self.store.health_detail()
        self.assertEqual(detail["total"], len(detail["rows"]))
        self.assertEqual(detail["total"], 1)
        self.assertEqual({r["backend"] for r in detail["rows"]}, {"workbuddy"})

    def test_disabled_backend_accounts_are_not_selected(self):
        """选号不选禁用后端的账号（account_rows 已过滤，select 复用它）。"""
        from fastapi import HTTPException
        from admin.pool import AccountPool
        pool = AccountPool(self.store)
        aid, _manager = pool.select(backend="trae")
        self.assertEqual(aid, "tr1")
        self.store.set_backend_flags({"trae": False})
        with self.assertRaises(HTTPException) as ctx:
            pool.select(backend="trae")
        # 明确「无可用账号」而不是静默串到别的后端的账号（隔离不变量）。
        self.assertEqual(ctx.exception.status_code, 503)

    def test_resolve_backend_rejects_disabled_backend(self):
        """带禁用后端前缀的请求 400，文案说「已被管理员禁用」（拍板项③）。"""
        from fastapi import HTTPException
        # 先确认启用时这个请求是通的（否则下面那个 400 可能来自别的原因）。
        spec, raw = registry.resolve_backend("trae/glm-5.2")
        self.assertEqual(spec.name, "trae")
        self.store.set_backend_flags({"trae": False})
        with self.assertRaises(HTTPException) as ctx:
            registry.resolve_backend("trae/glm-5.2")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("已被管理员禁用", str(ctx.exception.detail))

    def test_is_enabled_tracks_the_toggle_in_both_directions(self):
        """``is_enabled`` 跟着总闸变（snapshot / enabled_models / resolve_backend
        三个既有分支都读它，这里验数据源本身）。"""
        self.store.set_backend_flags({"trae": False})
        self.assertFalse(registry.is_enabled("trae"))
        self.assertTrue(registry.is_enabled("workbuddy"))
        self.store.set_backend_flags({"trae": True})
        self.assertTrue(registry.is_enabled("trae"))
        # 缺键即启用：只提交部分键时，未提到的后端不受影响。
        self.store.set_backend_flags({"workbuddy": True})
        self.assertTrue(registry.is_enabled("trae"))

    def test_data_survives_toggle_round_trip(self):
        """关掉再启用：账号行原样回来（数据库全程没被动过）。"""
        before = dict(self.store.data["accounts"])
        self.store.set_backend_flags({"trae": False})
        self.assertNotIn("tr1", {r["id"] for r in self.store.account_rows()})
        self.store.set_backend_flags({"trae": True})
        self.assertEqual(self.store.data["accounts"], before)
        self.assertIn("tr1", {r["id"] for r in self.store.account_rows()})

    def test_flags_persist_across_restart(self):
        """落库往返：重启后总闸状态还在（否则总闸形同虚设）。"""
        self.store.set_backend_flags({"trae": False})
        self.store.save()
        again = Store(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        try:
            self.assertFalse(again.backend_flags().get("trae", True))
        finally:
            again.db.close()

    def test_dirty_flag_value_is_tolerated_as_all_enabled(self):
        """脏值（JSON 坏了/手改了）当全启用——不是全关。

        方向性很重要：读失败当成「全关」会让线上后端集体消失、大面积 400；
        当成「全开」最多是总闸暂时失效。
        """
        from admin.server import _decode_backend_flags
        self.assertEqual(_decode_backend_flags("{ 这不是 json"), {})
        self.assertEqual(_decode_backend_flags("[1,2]"), {})
        self.assertEqual(_decode_backend_flags('{"不存在的后端": false}'), {})
        self.assertEqual(_decode_backend_flags('{"trae": "yes"}'), {})
        self.assertEqual(_decode_backend_flags('{"trae": false}'), {"trae": False})


# --------------------------------------------------------------------------
# 3. 不变量：启用集合恒 ≥ 1
# --------------------------------------------------------------------------

class _ToggleApiTests(unittest.TestCase):
    """走真实 HTTP 打 PATCH /admin/api/pool/settings。

    不变量必须在**服务端**守：只靠前端置灰的话，curl 直打接口就能清空启用
    集合，绕过所有 UI 保护。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.app = create_app(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        # 必须 https：管理会话 cookie 带 Secure 标志（create_app 的
        # secure_cookie 默认 True），http base_url 下浏览器语义是不回传。
        self.client = TestClient(self.app, base_url="https://console.test")
        login = self.client.post("/admin/api/login", json={"key": ADMIN})
        assert login.status_code == 200, login.text
        self.headers = {"X-CSRF-Token": login.json()["csrf"]}

    def tearDown(self):
        registry.set_enabled_provider(None)
        self.client.close()

    def _patch(self, flags):
        return self.client.patch("/admin/api/pool/settings",
                                 json={"backend_enabled": flags}, headers=self.headers)

    def test_all_backends_disabled_is_rejected(self):
        """把两个后端都关掉 → 400（不变量第一条）。"""
        r = self._patch({"workbuddy": False, "trae": False})
        self.assertEqual(r.status_code, 400)
        self.assertIn("至少", r.json()["detail"])

    def test_single_backend_remaining_is_allowed(self):
        """只剩一个是**合法**的（对称语义：关谁都能跑，见 specs §4）。"""
        r = self._patch({"workbuddy": True, "trae": False})
        self.assertEqual(r.status_code, 200)
        store = self.app.state.store
        self.assertTrue(registry.is_enabled("workbuddy"))
        self.assertFalse(registry.is_enabled("trae"))

    def test_partial_submit_does_not_disable_unmentioned_backends(self):
        """只提交 trae 的 PATCH 不应顺带关掉 workbuddy。

        缺失的键按「启用」补齐 —— 否则 curl 只改一个后端时，另一个会被静默下线。
        前端提交的是**完整 map**（backendFlagsBody 遍历全部复选框），所以真实
        界面上不会出现「只提交一个键」；这条守的是接口本身的可预期性。
        """
        self.assertEqual(self._patch({"trae": False}).status_code, 200)
        self.assertEqual(self._patch({"trae": True}).status_code, 200)
        self.assertEqual(self._patch({"trae": False}).status_code, 200)
        self.assertTrue(registry.is_enabled("workbuddy"),
                        "未提及的后端不该被部分提交顺带关掉")

    def test_rejection_leaves_state_untouched(self):
        """被拒的提交**不改任何状态**（400 不是「部分生效」）。"""
        self.assertEqual(self._patch({"trae": False}).status_code, 200)
        self.assertEqual(self._patch({"workbuddy": False, "trae": False}).status_code, 400)
        self.assertTrue(registry.is_enabled("workbuddy"))
        self.assertFalse(registry.is_enabled("trae"))
        self.assertEqual(self.app.state.store.backend_flags(), {"workbuddy": True, "trae": False})

    def test_unknown_backend_name_is_rejected_not_ignored(self):
        """未知后端名报错而不是静默忽略——静默忽略会让用户以为关掉了某个后端。"""
        r = self._patch({"workbuddy": True, "trae": True, "qwen": False})
        self.assertEqual(r.status_code, 400)
        self.assertIn("未知后端", r.json()["detail"])

    def test_non_bool_value_is_rejected(self):
        r = self._patch({"workbuddy": True, "trae": "no"})
        self.assertEqual(r.status_code, 400)

    def test_env_escape_hatch_wins_over_toggle(self):
        """不变量第二条：环境逃生门优先级更高。

        逃生门是运维止血手段，不经过 UI——设成 0 时即使总闸显示「开」，
        该后端也必须出局的。
        """
        os.environ["CODEBUDDY_ENABLE_TRAE"] = "0"
        r = self._patch({"workbuddy": True, "trae": True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(registry.is_enabled("trae"))
        os.environ.pop("CODEBUDDY_ENABLE_TRAE")
        self.assertTrue(registry.is_enabled("trae"), "逃生门解除后应恢复")

    def test_env_disabled_backend_does_not_count_toward_the_invariant(self):
        """逃生门关掉的后端不算「启用」，所以不必把它开着来凑够一个。"""
        os.environ["CODEBUDDY_ENABLE_TRAE"] = "0"
        # trae 在界面上是关的，workbuddy 也关 → 会被不变量拒绝吗？
        # 不会：workbuddy 必须能开，否则逃生门就等于把服务关死了。
        r = self._patch({"workbuddy": True, "trae": False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(registry.is_enabled("workbuddy"))

    def test_backends_endpoint_reflects_toggle(self):
        """/admin/api/backends 的 enabled 字段跟着总闸走（前端就靠它渲染）。"""
        self._patch({"trae": False})
        data = self.client.get("/admin/api/backends").json()["backends"]
        flags = {b["name"]: b["enabled"] for b in data}
        self.assertFalse(flags["trae"])
        self.assertTrue(flags["workbuddy"])


# --------------------------------------------------------------------------
# 4. 声明落点：目录拉取与签到跳过
# --------------------------------------------------------------------------

def test_refresh_catalog_skips_disabled_backend():
    """禁用后端不拉目录：**连凭据都不取**，更不该发上游请求。

    守卫写在 ``refresh_catalog`` 方法第一句，所以「谁调它」都绕不过去——
    启动时的一次性刷新、后台定时刷新、管理页手动刷新都走这一条。
    """
    from admin.models import ModelRegistry
    taken = []

    def getter(backend):
        # 注意契约：backend_credential_getter(backend) **直接返回凭据管理器**
        # （credential_getter_for 只是把它再包一层）。返回 None 表示「问了但没有」，
        # refresh_catalog 会因此提前返回 False —— 记录 taken 就够证明问过了。
        taken.append(backend)
        return None
    reg = ModelRegistry(backend_credential_getter=getter)
    registry.set_enabled_provider(lambda: {"trae": False})
    try:
        assert reg.refresh_catalog("trae") is False
        assert taken == [], "禁用的后端不该去取凭据（更不该发请求）"
    finally:
        registry.set_enabled_provider(None)


def test_refresh_catalog_still_runs_for_enabled_backend():
    """反例对照：启用时总闸不干扰 refresh_catalog 的既有前置检查。

    没有这条，上一条可能因为「守卫写错位置 / 方法压根没走到」而假绿。
    """
    from admin.models import ModelRegistry
    taken = []

    def getter(backend):
        # 注意契约：backend_credential_getter(backend) **直接返回凭据管理器**
        # （credential_getter_for 只是把它再包一层）。返回 None 表示「问了但没有」，
        # refresh_catalog 会因此提前返回 False —— 记录 taken 就够证明问过了。
        taken.append(backend)
        return None
    reg = ModelRegistry(backend_credential_getter=getter)
    registry.set_enabled_provider(lambda: {"trae": True})
    try:
        # 取到凭据后才会继续往下（这里是 None，故返回 False）——taken 有记录即
        # 证明总闸没有误伤启用路径。
        reg.refresh_catalog("trae")
        assert taken == ["trae"], "启用路径不该被总闸误伤"
    finally:
        registry.set_enabled_provider(None)


def test_tick_skips_checkin_for_disabled_backend():
    """禁用后端的账号不参与自动签到：后台任务不该去打已下线后端的上游。"""
    root = Path(tempfile.mkdtemp())
    auth = root / "auth"
    auth.mkdir()
    (root / "mgmt").mkdir()
    _write_cred(auth, "tr", "trae")
    store = Store(str(root / "mgmt"), str(auth), API, ADMIN)
    try:
        from admin.pool import AccountPool
        pool = AccountPool(store)
        calls = []
        pool.operate = lambda aid, action: (calls.append(aid), {"ok": True})[1]
        store.data["accounts"] = {
            "tr1": {"name": "tr", "file": "tr.info", "enabled": True, "created": 0, "backend": "trae"},
        }
        registry.set_enabled_provider(store.backend_flags)
        pool.store.data["account_status"]["tr1"] = {"checkin_date": "", "auto_attempt": 0, "checkin_fails": 0}
        # checkin_offset 会给账号随机挑当天某个时段，落在未来就不签到——
        # 固定成 00:00 让「此刻该签」恒成立，否则这条测试是随机红的。
        pool.checkin_offset = lambda aid, day, index: (0, "00:00")
        def _fresh_due():
            # 每轮都要把「今天还没签到」复位：tick 成功后会把 auto_attempt 写成
            # 当前时刻，而 due 条件里有 ``auto_attempt < clock-1800``——不复位的话
            # 第二轮压根进不了签到分支，这条测试就变成空转（删掉守卫也照样绿）。
            store.data["account_status"]["tr1"].update(
                {"checkin_date": "", "auto_attempt": 0, "checkin_fails": 0})

        import asyncio
        _fresh_due()
        asyncio.run(pool.tick())
        assert "tr1" in calls, "启用时应该签到"
        calls.clear()
        store.set_backend_flags({"trae": False})
        _fresh_due()
        asyncio.run(pool.tick())
        assert calls == [], "禁用后不该再签到"
    finally:
        registry.set_enabled_provider(None)
        store.db.close()
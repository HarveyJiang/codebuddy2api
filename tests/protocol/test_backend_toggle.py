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

from admin.server import Store, create_app, _decode_backend_order
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

    def test_resolve_backend_rejects_disabled_routed_backend(self):
        """中间件选定了一个**已被管理员禁用**的后端时，报 400 且文案可读（拍板项③）。

        v16 去前缀后，`resolve_backend` 不再按前缀分派；「禁用后端」的判定发生在
        `routed` 分支（中间件决策后立刻被禁用这种竞态）。
        """
        from fastapi import HTTPException
        # 先确认启用时按 routed 解析是通的（否则下面那个 400 可能来自别的原因）。
        spec, raw = registry.resolve_backend("glm-5.2", routed="trae")
        self.assertEqual(spec.name, "trae")
        self.store.set_backend_flags({"trae": False})
        with self.assertRaises(HTTPException) as ctx:
            registry.resolve_backend("glm-5.2", routed="trae")
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

class _BareRoutingSettingsApiTests(unittest.TestCase):
    """`model_backend_order` / `bare_name_routing` 的设置入口（§9.4 / §8 阶段三）。

    **为什么必须有**：实测发现原实现的白名单里没有这两个键 —— 提交返回 200 却
    什么都不存，表现为「调了序但落点没变」。这类「静默 no-op」行为测试抓不住
    （顺序没被改过，请求落点自然不变，与「生效了但恰好还是原后端」无法区分），
    所以直接在接口层断言「存进去了」。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.app = create_app(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        # https：管理 cookie 带 Secure，http 下浏览器语义不回传（实测 401）。
        self.client = TestClient(self.app, base_url="https://console.test")
        login = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.headers = {"X-CSRF-Token": login.json()["csrf"]}

    def tearDown(self):
        registry.set_enabled_provider(None)
        self.client.close()

    def test_order_round_trips_through_the_api(self):
        r = self.client.patch("/admin/api/pool/settings",
                              json={"model_backend_order": ["trae", "workbuddy"]},
                              headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        # 内存里必须是**列表**（build_order 的入参），不是 JSON 串。
        self.assertEqual(self.app.state.store.data["pool"]["model_backend_order"],
                         ["trae", "workbuddy"])
        # 且真的落库了（save 之后重读）。
        self.assertEqual(
            _decode_backend_order(self.app.state.store.db.get_settings()["model_backend_order"]),
            ["trae", "workbuddy"])

    def test_order_can_be_cleared_back_to_default(self):
        """置空 = 回到注册表顺序（NULL 语义）。"""
        self.client.patch("/admin/api/pool/settings",
                          json={"model_backend_order": ["trae", "workbuddy"]},
                          headers=self.headers)
        r = self.client.patch("/admin/api/pool/settings",
                              json={"model_backend_order": None}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.app.state.store.data["pool"]["model_backend_order"])
        self.assertIsNone(self.app.state.store.db.get_settings()["model_backend_order"])

    def test_unknown_backend_in_order_is_rejected(self):
        r = self.client.patch("/admin/api/pool/settings",
                              json={"model_backend_order": ["ghost"]},
                              headers=self.headers)
        self.assertEqual(r.status_code, 400)
        self.assertIn("ghost", r.json()["detail"])

    def test_kill_switch_round_trips(self):
        r = self.client.patch("/admin/api/pool/settings",
                              json={"bare_name_routing": False}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        # 内存里是 bool（不是 0/1 整数），生成器与路由都按真值判断。
        self.assertIs(self.app.state.store.data["pool"]["bare_name_routing"], False)
        self.assertEqual(self.app.state.store.db.get_settings()["bare_name_routing"], 0)

    def test_kill_switch_rejects_non_bool(self):
        r = self.client.patch("/admin/api/pool/settings",
                              json={"bare_name_routing": "no"}, headers=self.headers)
        self.assertEqual(r.status_code, 400)

    def test_order_from_db_is_normalized_to_a_list_in_memory(self):
        """从库里读出来后，内存里的 `model_backend_order` 必须是**列表**。

        `Store._load` 把 DB 的 JSON 文本归一成 list。若去掉那步归一，`build_order`
        会收到 `'["trae","workbuddy"]'` 这个**字符串** —— 遍历它得到一堆单字符的
        「后端名」，排序静默错乱（不报错、不崩溃，只是顺序永远是默认）。
        所以这里断言**类型**，而不只是「能读出来」。
        """
        root = Path(tempfile.mkdtemp())
        auth = root / "auth"
        auth.mkdir()
        # 先建库并写入调序（DB 里存 JSON 文本）。
        app = create_app(str(root / "mgmt"), str(auth), API, ADMIN)
        client = TestClient(app, base_url="https://console.test")
        login = client.post("/admin/api/login", json={"key": ADMIN})
        client.patch("/admin/api/pool/settings",
                     json={"model_backend_order": ["trae", "workbuddy"]},
                     headers={"X-CSRF-Token": login.json()["csrf"]})
        client.close()

        # 重启（重新 create_app 读同一个库）→ 内存里应是 list。
        again = create_app(str(root / "mgmt"), str(auth), API, ADMIN)
        try:
            value = again.state.store.data["pool"].get("model_backend_order")
            self.assertIsInstance(value, list, f"内存里应是 list，实际是 {type(value)}")
            self.assertEqual(value, ["trae", "workbuddy"])
        finally:
            registry.set_enabled_provider(None)

    def test_kill_switch_from_db_is_normalized_to_bool(self):
        """库里的 0（INTEGER）读进内存后必须是 `False`（bool）而不是 0。

        `build_order`/`_resolve_backend_for` 用 `if not ...` 真值判断，0 与 False
        在这里等价；但 `_dumps_backend_order` 那类以为「内存是布尔」的代码一旦遇到
        整数会出错。断言类型是为了让「库里是 INTEGER」这件事止步于 `_load`。
        """
        root = Path(tempfile.mkdtemp())
        auth = root / "auth"
        auth.mkdir()
        app = create_app(str(root / "mgmt"), str(auth), API, ADMIN)
        client = TestClient(app, base_url="https://console.test")
        login = client.post("/admin/api/login", json={"key": ADMIN})
        client.patch("/admin/api/pool/settings", json={"bare_name_routing": False},
                     headers={"X-CSRF-Token": login.json()["csrf"]})
        client.close()

        again = create_app(str(root / "mgmt"), str(auth), API, ADMIN)
        try:
            value = again.state.store.data["pool"].get("bare_name_routing")
            self.assertIsInstance(value, bool, f"内存里应是 bool，实际是 {type(value)}")
            self.assertFalse(value)
        finally:
            registry.set_enabled_provider(None)

    def test_kill_switch_defaults_to_enabled(self):
        """没配过时 pool settings 里 `bare_name_routing` 是 True（§8 阶段三默认开）。"""
        self.assertTrue(self.app.state.store.data["pool"].get("bare_name_routing", True))


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

# --------------------------------------------------------------------------
# 5. 端到端：文案必须真的送到客户端
# --------------------------------------------------------------------------

class _EndToEndTests(unittest.TestCase):
    """打真实的 ``/v1/chat/completions``，验用户**实际收到**的响应。

    为什么必须走 e2e：原实现的「XX 后端已被管理员禁用」写在
    ``registry.resolve_backend`` 里，而 PoolMiddleware 的**模型门**先于它命中
    （``enabled_models`` 已排除禁用后端、``snapshot_ids`` 没有），于是用户实际
    收到的是「模型 X 已在管理页禁用」。只单测 ``resolve_backend`` 全绿——
    因为那条路径在生产上根本不可达。这条测试就是为了钉住「用户看到的那个字符串」。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = self.root / "auth"
        self.auth.mkdir()
        for name, backend in (("wb", "workbuddy"), ("tr", "trae")):
            _write_cred(self.auth, name, backend)
        self.app = create_app(str(self.root / "mgmt"), str(self.auth), API, ADMIN)
        self.client = TestClient(self.app, base_url="https://console.test")
        csrf = self.client.post("/admin/api/login", json={"key": ADMIN}).json()["csrf"]
        self.headers = {"X-CSRF-Token": csrf}

    def tearDown(self):
        registry.set_enabled_provider(None)
        self.client.close()

    def _disable(self, backend):
        flags = {b: True for b in registry.known_names()}
        flags[backend] = False
        r = self.client.patch("/admin/api/pool/settings", json={"backend_enabled": flags},
                              headers=self.headers)
        assert r.status_code == 200, r.text

    def _ask(self, model):
        return self.client.post("/v1/chat/completions",
                                json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                                headers={"Authorization": "Bearer " + API})

    def test_disabled_backend_request_says_backend_disabled(self):
        """关掉 Trae 后，请求只有 Trae 有的模型要说**「后端已被管理员禁用」**。

        v16 去前缀后请求发**裸名**；为了确保策略会选中 Trae，用一个 Trae 独有的
        模型（`Doubao-Seed-Code`）——若用 `glm-5.2` 这种两边都有的名字，
        关掉 Trae 后请求会被 WB 接住（那是**正确**行为，见下一条测试）。

        回归背景（这条测试存在的理由，见类 docstring）：原因原先写在
        `registry.resolve_backend`，而 `PoolMiddleware` 的模型门先命中，用户实际
        收到的却是「模型 X 已在管理页禁用」。修复后归属在模型门**之前**由策略算出，
        「后端下线」不会再被说成「你禁用了某个模型」。
        """
        self._disable("trae")
        r = self._ask("Doubao-Seed-Code")
        assert r.status_code == 400, f"{r.status_code} {r.text}"
        assert "后端已被管理员禁用" in r.text, r.text
        # 关键：不能是「模型已在管理页禁用」——那是把后端下线说成模型禁用，
        # 排障时会跑去模型页找一个从没被动过的开关。
        assert "已在管理页禁用" not in r.text, f"命中了模型门的误导文案：{r.text}"

    def test_unknown_model_lists_every_backend_reason(self):
        """两边目录都没有的名字 → 400，且逐后端说明原因（而不是笼统「未知模型」）。"""
        r = self._ask("压根不存在的模型")
        assert r.status_code == 400, f"{r.status_code} {r.text}"
        # 每个后端各一条原因，用户据此判断「是不是我禁错/关了谁」。
        for name in registry.known_names():
            assert name in r.text, f"{r.text} 缺少 {name} 的原因"

    def test_disabled_backend_falls_back_when_name_exists_elsewhere(self):
        """同名模型在另一个后端也有时，关掉一方**不该**让请求失败 —— 它会落到另一方。

        这是 §5 裸名策略的直接推论，也是「一个归属」设计的大前提：
        关掉 Trae 之后 `glm-5.2` 仍然可用（策略从候选里剔掉被禁用的后端、选中 WB）。
        旧的前缀时代这条请求会 400（用户点名了被禁的后端）；去前缀后「点名」不再存在。

        判据取**策略结果**而不是 HTTP 状态码：本 fixture 没有假上游，真请求会打到
        线上并 401 —— 那不是这条测试要验的东西。直接用与生产同一份代码的
        `_resolve_backend_for` 问「落到了谁」。
        """
        self._disable("trae")
        from admin.pool import PoolMiddleware

        mw = PoolMiddleware.__new__(PoolMiddleware)
        mw.pool = self.app.state.pool
        mw.model_registry = self.app.state.model_registry
        backend = mw._resolve_backend_for("glm-5.2", None)
        assert backend == "workbuddy", f"关掉 Trae 后 glm-5.2 应落到 WB，实际 {backend}"
        # 对照：Trae 独有模型在 Trae 关闭后**没有**候选（这才是 400 的那类）。
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            mw._resolve_backend_for("Doubao-Seed-Code", None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("后端已被管理员禁用", str(ctx.exception.detail))

    def test_disabling_default_backend_also_reports_correctly(self):
        """对称：关掉默认后端（WorkBuddy）走同一条路（specs §4 的对称语义）。

        v16：请求发裸名；用**只在 WB 有**的模型（`hy3`）确保策略选中它 ——
        `glm-5.2` 在两个后端都有，关掉 WB 后它会被 Trae 接住（那是正确行为）。
        """
        self._disable("workbuddy")
        r = self._ask("hy3")  # WB 独有 → 关掉 WB 后没有候选
        assert r.status_code == 400, f"{r.status_code} {r.text}"
        assert "已被管理员禁用" in r.text, r.text
        assert "已在管理页禁用" not in r.text, r.text

    def test_v1_models_hides_disabled_backend(self):
        """/v1/models 也不该再列出禁用后端的模型（已有 is_enabled 派生，e2e 钉住）。

        v16：id 是裸名，不再能靠 `startswith("trae/")` 筛，改用 `owned_by` 声明。
        """
        self._disable("trae")
        data = self.client.get("/v1/models", headers={"Authorization": "Bearer " + API}).json()["data"]
        assert not [m for m in data if m["owned_by"] == "trae-solo"], "禁用后端仍在 /v1/models 里"
        assert [m for m in data if m["owned_by"] == "codebuddy"], "默认后端的模型不该被牵连"

    def test_env_gate_state_survives_an_unrelated_save(self):
        """逃生门开着时保存任意设置，**不得**抹掉用户对总闸的显式选择。

        前端永远提交完整 map，所以每次保存设置都会带上后端开关。逃生门关着的
        后端被渲染成未勾选，若照单写回，撤掉环境变量后它就自动恢复启用——
        用户的显式选择被一次无关的保存动作静默抹掉。
        """
        self._disable("trae")
        store = self.app.state.store
        assert store.backend_flags().get("trae") is False
        os.environ["CODEBUDDY_ENABLE_TRAE"] = "0"
        try:
            r = self.client.patch("/admin/api/pool/settings",
                                  json={"checkin_time": "10:00",
                                        "backend_enabled": {"workbuddy": True, "trae": False}},
                                  headers=self.headers)
            assert r.status_code == 200, r.text
            assert store.backend_flags().get("trae") is False, "逃生门开着时的保存抹掉了用户选择"
        finally:
            os.environ.pop("CODEBUDDY_ENABLE_TRAE", None)
        assert not registry.is_enabled("trae"), "撤掉逃生门后应恢复为用户选定的「禁用」"

    def test_backends_endpoint_exposes_env_disabled_separately(self):
        """前端要能区分「被环境门禁用」与「用户关掉了」——靠这个独立字段置灰。"""
        os.environ["CODEBUDDY_ENABLE_TRAE"] = "0"
        try:
            data = self.client.get("/admin/api/backends").json()["backends"]
            flags = {b["name"]: b for b in data}
            assert flags["trae"]["env_disabled"] is True
            assert flags["trae"]["enabled"] is False
            assert flags["workbuddy"]["env_disabled"] is False
            assert flags["workbuddy"]["enabled"] is True
        finally:
            os.environ.pop("CODEBUDDY_ENABLE_TRAE", None)


def test_load_time_invariant_guard():
    """加载期兜底：库里记着「全关」时回落全启用并告警（最后一道防线）。

    HTTP 层校验防手滑，防不了手改库 / 坏备份 / 将来直接写 data["pool"] 的代码。
    到那时进程活着、健康绿、所有请求失败——正是 §4.1 要杀的状态。
    """
    from admin.db import Database
    from admin.pool import AccountPool
    root = Path(tempfile.mkdtemp())
    (root / "mgmt").mkdir()
    auth = root / "auth"
    auth.mkdir()
    db = Database(root / "mgmt" / "state.db")
    db.save_settings({**AccountPool.POOL_DEFAULTS,
                      "backend_enabled": '{"workbuddy": false, "trae": false}'})
    db.close()
    store = Store(str(root / "mgmt"), str(auth), API, ADMIN)
    try:
        assert store.backend_flags() == {}, "全关应回落为全启用"
        registry.set_enabled_provider(store.backend_flags)
        assert registry.is_enabled("workbuddy"), "回落后服务必须可用"
    finally:
        registry.set_enabled_provider(None)
        store.db.close()


def _pool_with_one_trae_account():
    """造一个带单个 Trae 账号的池，返回 (store, pool, acted)。"""
    from admin.pool import AccountPool
    root = Path(tempfile.mkdtemp())
    auth = root / "auth"
    auth.mkdir()
    (root / "mgmt").mkdir()
    _write_cred(auth, "tr", "trae")
    store = Store(str(root / "mgmt"), str(auth), API, ADMIN)
    acted = []
    pool = AccountPool(store)
    pool.operate = lambda aid, action: (acted.append(aid), {"ok": True})[1]
    pool.pool_status = lambda aid: (acted.append(aid), {"ok": True})[1]
    store.data["accounts"] = {
        "tr1": {"name": "tr", "file": "tr.info", "enabled": True, "created": 0, "backend": "trae"},
    }
    store.data["account_status"]["tr1"] = {"credits_updated": 0}
    # 批量签到默认有 0~90 秒随机错峰延迟，不归零的话这条测试要跑一分半——
    # 而且随机延迟会让「有没有调用过」变得依赖时序，慢且脆。
    store.data["pool"]["batch_spread"] = 0
    registry.set_enabled_provider(store.backend_flags)
    return store, pool, acted


def test_batch_checkin_skips_disabled_backend():
    """概览「批量签到」也不打禁用后端。

    账号表已隐藏这些账号，批量动作却去签到，会让用户看到「表格里不存在的行在报
    签到成功」，正好破坏本次刻意对齐的「计数与表格行数一致」。
    """
    import asyncio
    store, pool, acted = _pool_with_one_trae_account()
    try:
        asyncio.run(pool.batch("checkin"))
        assert acted == ["tr1"], "启用时应批量签到"
        acted.clear()
        store.set_backend_flags({"trae": False})
        asyncio.run(pool.batch("checkin"))
        assert acted == [], "禁用后不该再批量签到"
    finally:
        registry.set_enabled_provider(None)
        store.db.close()


def test_refresh_credits_skips_disabled_backend():
    """页面每次加载都会 `pool/refresh-credits` —— 不该对已下线后端逐账号打上游。"""
    import asyncio
    store, pool, acted = _pool_with_one_trae_account()
    try:
        asyncio.run(pool.refresh_credits())
        assert acted == ["tr1"], "启用时应刷新积分"
        acted.clear()
        store.set_backend_flags({"trae": False})
        store.data["account_status"]["tr1"]["credits_updated"] = 0  # 复位节流窗口
        asyncio.run(pool.refresh_credits())
        assert acted == [], "禁用后不该再刷新积分"
    finally:
        registry.set_enabled_provider(None)
        store.db.close()


def test_credential_pre_refresh_skips_disabled_backend():
    """凭据预刷新（低峰时段）也不打禁用后端——与签到/积分刷新同一口径。

    这条曾经是盲区：tick 里签到有测试、凭据预刷新没有，删掉守卫照样全绿。
    """
    import asyncio
    from datetime import datetime, timezone, timedelta
    store, pool, acted = _pool_with_one_trae_account()
    try:
        # 预刷新只在 sync_start~sync_end 窗口内动作，且要账号两小时内到期。
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        store.data["pool"].update({"sync_start": "00:00", "sync_end": "23:59"})
        store.data["account_status"]["tr1"].update(
            # refresh_slot_date 必须是**今天**：不等就会在 tick 里重抽一个随机时段
            # （那个时刻多半还没到 → 直接 continue），测试于是随机红。
            {"refresh_slot": "00:00", "refresh_slot_date": today,
             "refresh_window_date": "", "token_refreshed": 0})
        _write_cred(store.auth_dir, "tr", "trae", expires_delta=3600)
        row = store.account_rows()[0]
        row["expires_at"] = int((time.time() + 3600) * 1000)
        pool.store.account_rows = lambda: [row]
        asyncio.run(pool.tick())
        assert "tr1" in acted, "启用时应预刷新凭据"
        acted.clear()
        store.set_backend_flags({"trae": False})
        # 两轮之间复位「今天还没预刷新」，否则第二轮被当天标记直接跳过，
        # 断言会因「压根没走到守卫」而假绿。
        store.data["account_status"]["tr1"].update(
            {"refresh_window_date": "", "token_refreshed": 0})
        asyncio.run(pool.tick())
        assert acted == [], "禁用后不该再预刷新凭据"
    finally:
        registry.set_enabled_provider(None)
        store.db.close()


def test_test_file_ends_with_newline():
    """末尾换行：POSIX 文本文件约定，也让 git 不再报 no newline at end of file。"""
    assert Path(__file__).read_text(encoding="utf-8").endswith("\n")

"""后端注册表驱动（需求 2）的回归测试。

这次重构的核心承诺是：**「加第三个后端」= 新增一个 BackendSpec 子类 + 注册，
pool / server / models / converter / 前端零改动。**

承诺最容易被下次改动悄悄破坏的方式，是有人图省事又在调用方写一句
``if backend == "trae"`` —— 代码照跑、测试照绿，直到加后端时才发现。
所以这里的断言分三类：

1. 注册表的**声明**是否自洽（前缀不冲突、只有一个默认后端、元数据齐全）；
2. 派生量是否真的由声明算出（``known_names`` / ``default_name`` / 元数据）；
3. 用**假后端**做端到端验证：注册一个声明为「前缀 fake/、顺序 2」的假后端，
   看路由、分组、元数据是否全部自动跟上 —— 这是对「零改动」最直接的证明。
"""
from __future__ import annotations

import pytest

from core.backends import registry
from core.backends.base import BackendSpec


# --------------------------------------------------------------------------
# 1. 声明自洽
# --------------------------------------------------------------------------

def test_every_backend_has_unique_prefix():
    """前缀必须互不冲突，否则同一模型名会被两个后端争抢（路由不确定）。"""
    prefixes = [registry.spec_for(n).model_prefix for n in registry.known_names()]
    nonempty = [p for p in prefixes if p]
    assert len(nonempty) == len(set(nonempty)), f"前缀重复：{prefixes}"


def test_at_most_one_default_backend():
    """空前缀的后端只能有一个：它是所有无前缀模型名的兜底。"""
    defaults = [n for n in registry.known_names() if registry.spec_for(n).default_for_missing_backend]
    assert len(defaults) <= 1, f"多个默认后端：{defaults}"


def test_default_prefix_is_empty():
    """默认后端必须声明空前缀，否则无前缀模型名无处可去。"""
    spec = registry.spec_for(registry.default_name())
    assert spec.model_prefix == ""
    assert spec.default_for_missing_backend is True


def test_known_names_includes_configured_backends():
    """已注册的后端都要出现在 known_names 里（WAF 分桶 / 白名单 / 元数据共用）。"""
    names = registry.known_names()
    assert "workbuddy" in names and "trae" in names
    assert len(names) == len(set(names)), "known_names 有重复"


def test_backend_meta_shape_and_order():
    """元数据字段齐全（前端按字段名取值）、顺序稳定、每个后端都在。"""
    meta = registry.backend_meta()
    assert {m["name"] for m in meta} == set(registry.known_names())
    for m in meta:
        for key in ("name", "label", "short", "icon", "color", "order", "prefix", "login_kind", "enabled", "default"):
            assert key in m, f"{m['name']} 缺字段 {key}"
        assert m["label"] and m["short"] and m["icon"], f"{m['name']} 展示字段为空"
    # 顺序：order 升序，默认后端在最前。
    orders = [m["order"] for m in meta]
    assert orders == sorted(orders), f"元数据未按 order 排序：{orders}"
    assert meta[0]["name"] == registry.default_name()


def test_backend_meta_trae_carries_prefix_and_kind():
    """Trae 的前缀与登录方式来自声明，前端据此渲染。"""
    trae = next(m for m in registry.backend_meta() if m["name"] == "trae")
    assert trae["prefix"] == "trae/"
    assert trae["login_kind"] == "trae"
    assert trae["default"] is False


# --------------------------------------------------------------------------
# 2. 派生量由声明算出
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("glm-5.2", "workbuddy"),
    ("auto", "workbuddy"),
    ("", "workbuddy"),
    (None, "workbuddy"),
    (123, "workbuddy"),
    ("trae/glm-5.2", "trae"),
    ("trae/", "trae"),
    ("mytrae/x", "workbuddy"),
])
def test_backend_name_for_routes_by_declared_prefix(model, expected):
    assert registry.backend_name_for(model) == expected


def test_prefix_helpers_round_trip():
    assert registry.prefix_for("trae/kimi") == "trae/"
    assert registry.prefix_for("kimi") == ""
    assert registry.strip_prefix("trae/kimi") == "kimi"
    assert registry.strip_prefix("kimi") == "kimi"
    # 前缀之外的空白要吃掉，避免 "trae/ kimi" 带着空格进 map_model。
    assert registry.strip_prefix("trae/  kimi  ") == "kimi"


def test_spec_for_unknown_falls_back_to_default():
    """未知后端名不抛错：账号里出现历史上没有的后端时页面不该整片炸掉。"""
    assert registry.spec_for("nope").name == registry.default_name()
    assert registry.spec_for(None).name == registry.default_name()


def test_backends_dict_is_cached():
    """每次调用返回同一个对象，避免反复构造（Trae 构造会读环境与缓存）。"""
    assert registry.backends() is registry.backends()


# --------------------------------------------------------------------------
# 3. 「加后端 = 新增类 + 注册」的端到端证明
# --------------------------------------------------------------------------

class _FakeBackend(BackendSpec):
    """一个最小的假后端：只声明能力，不实现任何上游调用。"""

    name = "fake"
    owned_by = "fake-org"
    model_prefix = "fake/"
    label = "Fake"
    short = "FK"
    icon = "★"
    color = "#123456"
    order = 2
    login_kind = "import"
    soft_cooldown = {418: 120}

    def chat_url(self) -> str:
        return "https://fake.invalid/v1/chat"

    def make_credential(self, path):
        return None


@pytest.fixture()
def fake_backend(monkeypatch):
    """把假后端塞进注册表，测试结束后还原。

    只改 ``_BACKENDS`` 与 ``_load``：这正是「注册一个后端」需要做的全部事情，
    若将来注册需要改更多地方，这个 fixture 会先失败。
    """
    monkeypatch.setitem(registry._BACKENDS, "fake", _FakeBackend())
    real_load = registry._load

    def load(name):
        if name == "fake":
            return registry._BACKENDS["fake"]
        return real_load(name)

    monkeypatch.setattr(registry, "_load", load)
    return _FakeBackend


def test_new_backend_is_routed_without_touching_callers(fake_backend):
    """路由自动认新前缀 —— 调用方一行没改。"""
    assert registry.backend_name_for("fake/whatever") == "fake"
    assert registry.prefix_for("fake/whatever") == "fake/"
    assert registry.strip_prefix("fake/whatever") == "whatever"
    spec, upstream = registry.resolve_backend("fake/whatever")
    assert spec.name == "fake"
    assert upstream == "whatever"


def test_new_backend_appears_in_known_names_and_meta(fake_backend):
    """白名单 / WAF 分桶 / 前端元数据都自动跟上。"""
    assert "fake" in registry.known_names()
    meta = {m["name"]: m for m in registry.backend_meta()}
    assert meta["fake"]["label"] == "Fake"
    assert meta["fake"]["icon"] == "★"
    assert meta["fake"]["color"] == "#123456"
    # order=2 排在 workbuddy(0) / trae(1) 之后。
    assert [m["name"] for m in registry.backend_meta()][-1] == "fake"
    # 默认后端没有被新后端抢走。
    assert registry.default_name() == "workbuddy"


def test_new_backend_soft_cooldown_is_read_from_declaration(fake_backend):
    """软冷却时长来自声明，pool 不需要为它加分支。"""
    assert registry.spec_for("fake").soft_cooldown_for(418) == 120
    assert registry.spec_for("fake").soft_cooldown_for(404) is None
    # 默认后端没有软冷却：404 按硬失败处理（既有行为，不能被顺手改掉）。
    assert registry.spec_for("workbuddy").soft_cooldown_for(404) is None
    # Trae 的 404 软冷却 10 分钟，来自它自己的声明。
    assert registry.spec_for("trae").soft_cooldown_for(404) == 600


def test_make_credential_dispatches_by_backend_field(fake_backend, tmp_path):
    """凭据工厂按 .info 的 backend 字段分派到对应后端；未知/缺失回落默认后端。"""
    import json

    f = tmp_path / "a.info"
    f.write_text(json.dumps({"backend": "fake", "auth": {}, "account": {}}), encoding="utf-8")
    assert registry.make_credential(f) is None  # _FakeBackend.make_credential

    f.write_text(json.dumps({"backend": "nope", "auth": {}, "account": {}}), encoding="utf-8")
    from core import converter

    assert isinstance(registry.make_credential(f), converter.CredentialManager)


def test_unknown_prefix_is_not_mistaken_for_a_backend():
    """只有**声明过的**前缀才路由；别的名字照常走默认后端。"""
    assert registry.backend_name_for("notfake/x") == "workbuddy"
    assert registry.strip_prefix("notfake/x") == "notfake/x"


# --------------------------------------------------------------------------
# 4. 能力声明本身
# --------------------------------------------------------------------------

def test_tencent_policies_only_on_default_backend():
    """腾讯内容策略只对 WorkBuddy 生效；Trae 是私有协议，全跳过。"""
    assert registry.spec_for("workbuddy").tencent_policies is True
    assert registry.spec_for("trae").tencent_policies is False


def test_reasoning_and_count_tokens_flags():
    """Trae 保留 reasoning 且 count_tokens 走本地估算（走真实请求会白烧额度）。"""
    assert registry.spec_for("trae").keep_reasoning_delta is True
    assert registry.spec_for("trae").count_tokens_upstream is False
    assert registry.spec_for("workbuddy").keep_reasoning_delta is False
    assert registry.spec_for("workbuddy").count_tokens_upstream is True


def test_expires_at_unit_is_declared_per_backend():
    """expiresAt 单位换算系数由后端声明（Trae 是 Unix 秒，要 ×1000）。"""
    assert registry.spec_for("trae").expires_at_to_ms == 1000
    assert registry.spec_for("workbuddy").expires_at_to_ms == 1


def test_credential_name_matches_backend_by_default():
    """凭据归属默认等于后端名；一致性闸门据此比对。"""
    assert registry.spec_for("workbuddy").credential_name() == "workbuddy"
    assert registry.spec_for("trae").credential_name() == "trae"


def test_account_ops_backend_declares_implementation():
    """积分/签到由谁实现，由声明决定：默认后端走 pool 的默认实现，Trae 自己接管。"""
    assert registry.spec_for("workbuddy").account_ops_backend is None
    assert registry.spec_for("trae").account_ops_backend == "trae"


def test_auto_checkin_participation_is_declared():
    """参与自动签到的后端由声明决定（可关掉）。"""
    assert registry.spec_for("workbuddy").supports_auto_checkin is True
    assert registry.spec_for("trae").supports_auto_checkin is True


def test_base_spec_defaults_are_the_plainest_backend():
    """基类默认值 = 一个最朴素的后端；子类只覆盖与自己不同的项。"""
    plain = BackendSpec()
    assert plain.model_prefix == ""
    assert plain.tencent_policies is False
    assert plain.keep_reasoning_delta is False
    assert plain.count_tokens_upstream is True
    assert plain.soft_cooldown == {}
    assert plain.soft_cooldown_for(404) is None
    assert plain.account_ops_backend is None
    assert plain.credential_name() == plain.name
    with pytest.raises(NotImplementedError):
        plain.make_credential("x")
    with pytest.raises(NotImplementedError):
        plain.chat_url()

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

from pathlib import Path

import pytest

from core.backends import registry
from core.backends.base import BackendSpec


# --------------------------------------------------------------------------
# 1. 声明自洽
# --------------------------------------------------------------------------

def test_every_backend_has_unique_prefix():
    """**历史前缀**（仅供迁移读取）必须互不冲突，否则迁移会剥错行。

    v16 起运行期不再用前缀路由，所以这条已从「路由唯一性」降级为「迁移数据的
    可判定性」：两个后端声明同一个历史前缀，剥前缀时无法区分是哪一家的行。
    """
    prefixes = [registry.spec_for(n).model_prefix for n in registry.known_names()]
    nonempty = [p for p in prefixes if p]
    assert len(nonempty) == len(set(nonempty)), f"历史前缀重复：{prefixes}"


def test_default_backend_comes_from_the_declaration(monkeypatch):
    """默认后端由 ``default_for_missing_backend`` 决定，**不是**字面量兜底。

    变异验证发现的漏洞：把 ``default_name()`` 末尾的
    ``raise RuntimeError(...)`` 改回 ``return "workbuddy"``，全量 684 条**全绿**
    —— 当前只有一个后端声明自己是默认后端，兜底分支永远走不到。这正是
    「重构做了一半」最典型的形状：声称由声明驱动、实际由字面量兜底，
    且没有任何测试能发现。

    这里把「谁声明了默认归属」换掉，看 ``default_name()`` 是否跟着换。
    """
    class _Picker(BackendSpec):
        name = "picker"
        model_prefix = "pick/"
        default_for_missing_backend = True

        def chat_url(self):
            return "https://pick.invalid"

        def make_credential(self, path):
            return None

    class _PlainWB(BackendSpec):
        """workbuddy 的替身：**不**声明自己是默认后端。"""

        name = "workbuddy"
        default_for_missing_backend = False

        def chat_url(self):
            return "https://wb.invalid"

        def make_credential(self, path):
            return None

    specs = {"workbuddy": _PlainWB(), "picker": _Picker()}
    monkeypatch.setattr(registry, "known_names", lambda: ("workbuddy", "picker"))
    monkeypatch.setattr(registry, "spec_for", lambda name: specs.get(str(name), specs["workbuddy"]))

    # 声明者是 picker，尽管 workbuddy 才是注册表里的第一个后端。
    assert registry.default_name() == "picker", (
        "default_name 没有跟着声明走 —— 它多半在某个地方兜底成了字面量"
    )


def test_default_name_refuses_when_nobody_declares():
    """没有任何后端声明默认归属时宁可**报错**，也不要猜一个名字。

    「猜」在生产里表现为：新后端接进来、忘了声明默认归属，于是所有无前缀模型名
    被静默送给第一个注册的后端 —— 请求打到错的 endpoint，报的却是上游的错误。
    """
    class _NoDefault(BackendSpec):
        name = "nodefault"

        def chat_url(self):
            return "https://x.invalid"

        def make_credential(self, path):
            return None

    real_known, real_spec = registry.known_names, registry.spec_for
    registry.known_names = lambda: ("nodefault",)
    registry.spec_for = lambda name: _NoDefault()
    try:
        with pytest.raises(RuntimeError, match="default_for_missing_backend"):
            registry.default_name()
    finally:
        registry.known_names, registry.spec_for = real_known, real_spec


def test_registration_is_a_single_table():
    """注册面收敛在 ``registry._REGISTRY`` 一张表上。

    这是 P1 修过的形状：此前「注册一个新后端」要同时改 ``_load``（if name ==
    "trae"）、``known_names``（order 字典）、``default_name``（return "workbuddy"）、
    ``spec_for``（if name in ("trae",)）四处 —— 每处都要记得改，漏一处就是
    「新后端能跑但顺序/默认/白名单不对」的静默故障。

    现在这四个函数全部从 ``_REGISTRY`` 派生。这条测试钉死「表还在、且每个条目
    自洽」，避免有人下次又在某个函数里回到按名字写 if。
    """
    table = registry._REGISTRY
    assert len(table) >= 2, "注册表至少应有默认后端与一个带前缀的后端"
    for entry in table:
        assert set(entry) >= {"name", "order", "module", "attr", "lazy"}, entry
        assert entry["module"].startswith("core.backends."), entry
        assert isinstance(entry["lazy"], bool), entry
    # 名字不重复、order 不重复（顺序要稳定）。
    names = [e["name"] for e in table]
    assert len(names) == len(set(names)), names
    orders = [e["order"] for e in table]
    assert len(orders) == len(set(orders)), orders


def test_known_names_order_derives_from_the_table(monkeypatch):
    """``known_names()`` 的顺序必须由 ``_REGISTRY`` 的 ``order`` 算出。

    变异验证发现的漏洞：把 ``known_names`` 里的
    ``order = {e["name"]: e["order"] for e in _REGISTRY}`` 改回硬编码字典
    ``{"workbuddy": 0, "trae": 1}``，全量 686 条**全绿** —— 因为当前表的 order
    恰好就是那两个值。「有表」和「表被用于派生」是两件事，测试必须验后者。
    """
    flipped = tuple({**e, "order": 1 - e["order"]} for e in registry._REGISTRY)
    monkeypatch.setattr(registry, "_REGISTRY", flipped)
    monkeypatch.setattr(registry, "_BACKENDS", {})
    assert registry.known_names()[:2] == ("trae", "workbuddy"), (
        "known_names 的顺序没有跟着 _REGISTRY 的 order 变 —— "
        "它多半还在读内建的 order 字典"
    )


def test_default_backend_comes_from_the_declaration(monkeypatch):
    """默认后端由 ``default_for_missing_backend`` 决定，**不是**字面量兜底。

    变异验证发现的漏洞：把 ``default_name()`` 末尾的
    ``raise RuntimeError(...)`` 改回 ``return "workbuddy"``，全量 684 条**全绿**
    —— 当前只有一个后端声明自己是默认后端，兜底分支永远走不到。这正是
    「重构做了一半」最典型的形状：声称由声明驱动、实际由字面量兜底，
    且没有任何测试能发现。

    这里把「谁声明了默认归属」换掉，看 ``default_name()`` 是否跟着换。
    """
    class _Picker(BackendSpec):
        name = "picker"
        model_prefix = "pick/"
        default_for_missing_backend = True

        def chat_url(self):
            return "https://pick.invalid"

        def make_credential(self, path):
            return None

    class _PlainWB(BackendSpec):
        """workbuddy 的替身：**不**声明自己是默认后端。"""

        name = "workbuddy"
        default_for_missing_backend = False

        def chat_url(self):
            return "https://wb.invalid"

        def make_credential(self, path):
            return None

    specs = {"workbuddy": _PlainWB(), "picker": _Picker()}
    monkeypatch.setattr(registry, "known_names", lambda: ("workbuddy", "picker"))
    monkeypatch.setattr(registry, "spec_for", lambda name: specs.get(str(name), specs["workbuddy"]))

    # 声明者是 picker，尽管 workbuddy 才是注册表里的第一个后端。
    assert registry.default_name() == "picker", (
        "default_name 没有跟着声明走 —— 它多半在某个地方兜底成了字面量"
    )


def test_default_name_refuses_when_nobody_declares():
    """没有任何后端声明默认归属时宁可**报错**，也不要猜一个名字。

    「猜」在生产里表现为：新后端接进来、忘了声明默认归属，于是所有无前缀模型名
    被静默送给第一个注册的后端 —— 请求打到错的 endpoint，报的却是上游的错误。
    """
    class _NoDefault(BackendSpec):
        name = "nodefault"

        def chat_url(self):
            return "https://x.invalid"

        def make_credential(self, path):
            return None

    real_known, real_spec = registry.known_names, registry.spec_for
    registry.known_names = lambda: ("nodefault",)
    registry.spec_for = lambda name: _NoDefault()
    try:
        with pytest.raises(RuntimeError, match="default_for_missing_backend"):
            registry.default_name()
    finally:
        registry.known_names, registry.spec_for = real_known, real_spec


def test_registration_is_a_single_table():
    """注册面收敛在 ``registry._REGISTRY`` 一张表上。

    这是 P1 修过的形状：此前「注册一个新后端」要同时改 ``_load``（if name ==
    "trae"）、``known_names``（order 字典）、``default_name``（return "workbuddy"）、
    ``spec_for``（if name in ("trae",)）四处 —— 每处都要记得改，漏一处就是
    「新后端能跑但顺序/默认/白名单不对」的静默故障。

    现在这四个函数全部从 ``_REGISTRY`` 派生。这条测试钉死「表还在、且每个条目
    自洽」，避免有人下次又在某个函数里回到按名字写 if。
    """
    table = registry._REGISTRY
    assert len(table) >= 2, "注册表至少应有默认后端与一个带前缀的后端"
    for entry in table:
        assert set(entry) >= {"name", "order", "module", "attr", "lazy"}, entry
        assert entry["module"].startswith("core.backends."), entry
        assert isinstance(entry["lazy"], bool), entry
    # 名字不重复、order 不重复（顺序要稳定）。
    names = [e["name"] for e in table]
    assert len(names) == len(set(names)), names
    orders = [e["order"] for e in table]
    assert len(orders) == len(set(orders)), orders


def test_known_names_order_derives_from_the_table(monkeypatch):
    """``known_names()`` 的顺序必须由 ``_REGISTRY`` 的 ``order`` 算出。

    变异验证发现的漏洞：把 ``known_names`` 里的
    ``order = {e["name"]: e["order"] for e in _REGISTRY}`` 改回硬编码字典
    ``{"workbuddy": 0, "trae": 1}``，全量 686 条**全绿** —— 因为当前表的 order
    恰好就是那两个值。「有表」和「表被用于派生」是两件事，测试必须验后者。
    """
    monkeypatch.setattr(registry, "_REGISTRY", tuple(
        {"name": e["name"], "order": 99 - e["order"] if False else e["order"],
         "module": e["module"], "attr": e["attr"], "lazy": e["lazy"]}
        for e in registry._REGISTRY))
    # 把 workbuddy 的 order 改到 trae 后面，输出顺序必须跟着翻。
    flipped = []
    for e in registry._REGISTRY:
        e = dict(e)
        e["order"] = {0: 1, 1: 0}.get(e["order"], e["order"])
        flipped.append(e)
    monkeypatch.setattr(registry, "_REGISTRY", tuple(flipped))
    monkeypatch.setattr(registry, "_BACKENDS", {})
    assert registry.known_names()[:2] == ("trae", "workbuddy"), (
        "known_names 的顺序没有跟着 _REGISTRY 的 order 变 —— "
        "它多半还在读内建的 order 字典"
    )


def test_registry_modules_have_no_stray_backend_branches():
    """注册表里除白名单外的「按后端名分支」应已收敛。

    review 指出注册表自己就是硬编码后端名的重灾区（``known_names`` 的 order 字典、
    ``default_name`` 的 ``return "workbuddy"``、``spec_for`` 的 ``if name in ("trae",)``）。
    现在 ``_REGISTRY`` 是唯一允许出现后端名的地方；剩下**一处**按名字判断是
    **有意保留**的，这条测试把它记下来，多出来的就是新的渗透点：

    - ``resolve_backend`` —— 某个后端的模型名映射（``map_model``）是它私有的
      归一规则，形状各后端差异大，不适合塞进声明。

    上限从 2 收到 1（2026-10-16）：原先的第二处是 ``is_enabled`` 里的
    ``if name == "trae"``（后端总闸上线时顺带改成了读
    ``BackendSpec.kill_switch_env`` 声明），所以现在**一处都不剩余地**。
    """
    import re
    src = (registry.__file__ and Path(registry.__file__).read_text(encoding="utf-8")) or ""
    # 只看代码行（剥注释与 docstring）。
    import ast
    tree = ast.parse(src)
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in doc_lines:
            continue
        code = line.split("#", 1)[0]
        if re.search(r'==\s*["\'](?:workbuddy|trae)["\']|in\s*\(\s*["\'](?:workbuddy|trae)["\']', code):
            hits.append(f"registry.py:{i}: {code.strip()}")
    # 只允许 resolve_backend 一处（见 docstring）。
    assert len(hits) <= 1, (
        "注册表出现新的按后端名分支（应改为从 _REGISTRY / 声明派生）：\n  " + "\n  ".join(hits)
    )


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
        for key in ("name", "label", "short", "icon", "color", "order", "login_kind", "enabled", "default"):
            assert key in m, f"{m['name']} 缺字段 {key}"
        assert m["label"] and m["short"] and m["icon"], f"{m['name']} 展示字段为空"
    # 顺序：order 升序，默认后端在最前。
    orders = [m["order"] for m in meta]
    assert orders == sorted(orders), f"元数据未按 order 排序：{orders}"
    assert meta[0]["name"] == registry.default_name()


def test_backend_meta_trae_carries_kind_not_prefix():
    """Trae 的登录方式来自声明，前端据此渲染。

    `prefix` **不再出现在元数据里**（v16 去前缀；前端也不再读它）——
    这条顺带守住「有人把 prefix 加回元数据」的回归。
    """
    trae = next(m for m in registry.backend_meta() if m["name"] == "trae")
    assert "prefix" not in trae, "去前缀后元数据不该再下发 prefix"
    assert trae["login_kind"] == "trae"
    assert trae["default"] is False


# --------------------------------------------------------------------------
# 2. 派生量由声明算出
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    # v16 去前缀后 `backend_name_for` 只剩两条规则：pinned → 声明的后端；其余 → 默认后端。
    # 它给的是**静态兜底**（同名多后端时不知道此刻该走谁——那是 pool 的裸名策略）。
    ("glm-5.2", "workbuddy"),
    ("auto", "workbuddy"),
    ("", "workbuddy"),
    (None, "workbuddy"),
    (123, "workbuddy"),
    ("trae/glm-5.2", "workbuddy"),   # 前缀已无路由含义 → 归默认后端
    ("mytrae/x", "workbuddy"),
    ("fast-model", "workbuddy"),     # pinned：声明归默认后端
])
def test_backend_name_for_is_pinned_else_default(model, expected):
    assert registry.backend_name_for(model) == expected


def test_prefix_helpers_are_gone():
    """前缀辅助函数已删除（v16 去前缀）。

    守住「有人把它们加回来」：这几个函数的存在本身会吸引新代码写
    `strip_prefix(model)` 这种运行期剥前缀，而它在新世界里只会把
    `openrouter/free` 这类真实模型名误伤。
    """
    for name in ("prefix_for", "strip_prefix", "backend_name_for_prefix"):
        assert not hasattr(registry, name), f"{name} 应已删除"


def test_model_prefix_declaration_still_exists_for_migrations():
    """`model_prefix` **不能删** —— `admin/db.py` 的两个迁移要靠它认历史前缀。

    删了就只能写死 `"trae/"` 字面量，那正是「加后端要改多处」。
    """
    assert registry.spec_for("trae").model_prefix == "trae/"
    assert registry.spec_for("workbuddy").model_prefix == ""


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
    # 历史前缀（仅供迁移读取）——本测试用它验证「声明仍被保留」。
    model_prefix = "fake/"
    # 声明式路由：这些裸名永远归本后端（v16 后这是「加后端不用改调用方」的机制）。
    pinned_models = ("fake-pinned",)
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


class _LazyFakeBackend(BackendSpec):
    """一个**尚未被构造**的假后端：模拟 trae 那样「用到才 import/构造」。

    必须与 _FakeBackend 分开：注册装置把它放进注册表的**名字表**，但不预先
    构造实例 —— 这正是 make_credential 分派最容易出错的情形。
    """

    name = "lazyfake"
    model_prefix = "lazy/"

    def chat_url(self) -> str:
        return "https://lazy.invalid/v1/chat"

    def make_credential(self, path):
        return None


@pytest.fixture()
def lazy_backend(monkeypatch):
    """注册一个**懒构造**的假后端（模拟 trae 的按需 import）。

    关键：只让 ``_load`` 能构造它、并让 ``known_names()`` 认这个名字，
    **不**预先实例化 —— 调用方必须能只凭名字正确分派。

    这里顺带暴露了「注册一个新后端要动哪几处」：``_load`` + ``known_names``
    的已知名集合。这正是 `test_backend_arch_guard.py` 关注的「加后端零改动」
    边界 —— 若将来注册面缩小成一处，这个 fixture 会先失败。
    """
    real_load = registry._load

    def load(name):
        if name == "lazyfake":
            return _LazyFakeBackend()
        return real_load(name)

    monkeypatch.setattr(registry, "_load", load)
    # 让 known_names() 认这个名字（生产里它是个封闭集合，见该函数的注释）。
    monkeypatch.setattr(registry, "known_names",
                        lambda: tuple(sorted(set(registry._BACKENDS) | {"workbuddy", "trae", "lazyfake"},
                                             key=lambda n: ({"workbuddy": 0, "trae": 1}.get(n, 50), n))))
    # spec_for 也必须能**按需构造**它 —— 这正是「懒后端」的定义：
    # 名字可见、实例按需创建。生产里这一步由 spec_for 的 _load 分支承担。
    real_spec_for = registry.spec_for

    def spec_for(name):
        if str(name) == "lazyfake":
            return load("lazyfake")
        return real_spec_for(name)

    monkeypatch.setattr(registry, "spec_for", spec_for)


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


def test_new_backend_is_recognized_without_touching_callers(fake_backend):
    """新后端**无需改调用方**就被认出来：known_names / meta / spec_for / 账号分派。

    v16 去前缀后不再有「按前缀路由」这条自动接线，所以这里改验**声明驱动的
    三件事**仍然自动跟上：白名单、前端元数据、凭据分派。它们正是「加后端只改
    一个文件」的实质 —— 前缀只是过去用来做路由的一种手段。
    """
    assert "fake" in registry.known_names()
    assert registry.spec_for("fake").label == "Fake"
    meta = {m["name"]: m for m in registry.backend_meta()}
    assert meta["fake"]["owned_by"] if "owned_by" in meta["fake"] else True
    assert meta["fake"]["icon"] == "★"
    # `resolve_backend(fake/...)` 不再是「认前缀」而是「默认后端 + 原样名」：
    # 想真的把请求路由到新后端，必须走 pinned 或 pool 的裸名策略。
    spec, upstream = registry.resolve_backend("fake/whatever")
    assert spec.name == registry.default_name()


def test_new_backend_pinned_models_route_by_declaration(fake_backend):
    """新后端可以用 `pinned_models` 声明「这些裸名永远归我」——不用改任何调用方。"""
    assert registry.pinned_model_owner("fake-pinned") == "fake"
    assert registry.backend_name_for("fake-pinned") == "fake"


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


def test_make_credential_dispatches_lazy_backend_by_name(lazy_backend, tmp_path):
    """回归：**懒构造**的后端也要能被凭据分派认出来。

    曾经的判据是 ``name in _BACKENDS or name == "trae"`` —— 它漏掉「和 trae 一样
    按需构造、但还没被触碰过」的后端：那份凭据会被**静默交给默认后端**解析，
    类型不对却不报错。这正是「加后端零改动」最容易悄悄破掉的地方，而行为测试
    全绿（当前只有两个后端时完全正常）。
    """
    import json

    f = tmp_path / "a.info"
    f.write_text(json.dumps({"backend": "lazyfake", "auth": {}, "account": {}}), encoding="utf-8")
    # 分派到 _LazyFakeBackend.make_credential（返回 None），而不是默认后端的管理器。
    assert registry.make_credential(f) is None
    # 尚未构造也能被凭据分派认出来（不是靠「已经构造过」）。
    # 注意：v16 去前缀后 `backend_name_for("lazy/x")` 不再按前缀路由（恒为默认后端）——
    # 「认不认得出这个后端」改由 **make_credential 的分派**验证（上面那一行）。
    assert registry.spec_for("lazyfake").name == "lazyfake"


def test_make_credential_does_not_eagerly_construct_lazy_backend(lazy_backend, tmp_path):
    """分派**不得**顺带把懒后端构造出来（trae 的私有协议只有真用到才该 import）。"""
    import json

    f = tmp_path / "a.info"
    f.write_text(json.dumps({"backend": "nope", "auth": {}, "account": {}}), encoding="utf-8")
    registry.make_credential(f)   # 未知后端 → 走默认
    assert registry._BACKENDS.get("lazyfake") is None, "未知后端的分派不该顺手构造懒后端"


def test_unknown_prefix_is_not_mistaken_for_a_backend():
    """只有**声明过的**名字才路由；带斜杠的名字照常走默认后端（v16 后无前缀语义）。"""
    assert registry.backend_name_for("notfake/x") == "workbuddy"


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


def test_upstream_usage_support_is_declared():
    """上游用量对账只默认后端可用：Trae 的 token 打 WorkBuddy 计费域必然 401，
    不声明就意味着不进同步队列（曾因无差别入队每天留一条永远清不掉的留痕）。"""
    assert registry.spec_for("workbuddy").supports_upstream_usage is True
    assert registry.spec_for("trae").supports_upstream_usage is False


# --------------------------------------------------------------------------
# 6. 上游错误的解释权归后端声明（pool 不得 import 后端模块去挑异常类）
# --------------------------------------------------------------------------

def test_upstream_error_class_resolves_from_declaration():
    """声明的是**类名**，运行时从后端自己的模块取类。

    给类名而不是类对象，是为了让 base/registry 不在顶部 import 各后端模块 ——
    那会把 Trae 的整套私有协议拖进所有部署形态。这个测试同时守住两件事：
    声明确实能被解析，且解析出的确实是该后端自己的异常类。
    """
    from core.backends.base import BackendHTTPError

    # 默认实现：基类既有的通用异常。
    assert BackendSpec().upstream_error_class() is BackendHTTPError
    # Trae 声明了自己的 ug 接口异常（401/403 会被 pool 判为凭据失效）。
    trae = registry.spec_for("trae")
    assert trae.error_class_name == "TraeAPIError"
    from core.backends.trae import TraeAPIError

    assert trae.upstream_error_class() is TraeAPIError


def test_only_account_ops_backend_implements_checkin_claim():
    """签到报文的解释规则由**声明了 account_ops_backend 的后端**实现。

    各家签到报文的形状是私有协议，没有通用默认值 —— 所以默认实现必须显式
    拒绝（否则第三个后端会静默套用别人的规则，把失败读成成功）。
    """
    with pytest.raises(NotImplementedError):
        BackendSpec().interpret_checkin_claim({"code": 0})

    trae = registry.spec_for("trae")
    ok, msg, retryable = trae.interpret_checkin_claim({"code": 0})
    assert ok is True and retryable is False

    # 可重试的上游拒绝码（9074/9004）→ 判为可重试。
    ok, msg, retryable = trae.interpret_checkin_claim({"code": 9074, "message": "高峰限流"})
    assert ok is False and retryable is True and "高峰" in msg


def test_upstream_error_class_rejects_misdeclared_name():
    """``error_class_name`` 指向非异常类时必须显式报错。

    这段代码跑在**异常处理路径**上（pool 的 except 匹配）：若名字写错、或指向
    别的模块的名字，裸 ``AttributeError`` 会在这里炸成二次异常，极难定位。
    所以约定「必须与后端同模块」，并在违反时给一句能读懂的话。
    """

    class _Misdeclared(BackendSpec):
        name = "misdeclared"
        error_class_name = "Nope"           # 同模块里没有这个名字

    with pytest.raises(TypeError) as ei:
        _Misdeclared().upstream_error_class()
    assert "Nope" in str(ei.value) and "不同于" not in str(ei.value)

    class _NotAnException(BackendSpec):
        name = "notexc"
        error_class_name = "not_an_exception_here"

    # 模块里有个同名的非异常对象（下面这行）→ 也要被拒。
    globals()["not_an_exception_here"] = "我是字符串"
    with pytest.raises(TypeError):
        _NotAnException().upstream_error_class()


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

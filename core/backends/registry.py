"""registry — 模型名 → 后端分发 + 凭据工厂。

路由规则：模型名带 `trae/` 前缀 → trae 后端（剥前缀后 map_model 归一为
上游 config_name）；其余（含 `auto`）→ workbuddy（默认）。两后端模型 id
大量重叠（glm-5.2 / deepseek / kimi…），前缀是唯一无歧义、对客户端零改动的
信号（Codex CLI / Claude Code 改不了 header/路径）。
"""
from __future__ import annotations

import json
import os

from fastapi import HTTPException

from .base import BackendHTTPError, BackendSpec  # noqa: F401（re-export 便利）
from .workbuddy import WorkBuddyBackend

_BACKENDS: dict[str, BackendSpec] = {}

#: 后端注册表 —— **唯一**允许出现后端名的地方（`core/backends/registry.py`）。
#:
#: 「加一个后端」在这里加一行就够了：注册表按 `lazy` 决定是立即 import 还是
#: 用到才 import（后者是为了不让 Trae 的整套私有协议与登录态进入所有部署形态）。
#: 其余一切（顺序、默认后端、前缀、元数据、白名单、凭据分派）都从
#: `BackendSpec` 的**声明**派生 —— 见 `known_names` / `default_name` / `spec_for`。
#:
#: `order` 显式写在这里而不是读 `spec.order`：读声明就得先构造 spec，而构造
#: 正是我们要推迟的事（懒后端）。它决定管理页展示顺序，必须稳定。
#:
#: `module` / `attr` 用字符串描述构造入口，避免在模块顶部 import 后端模块。
_REGISTRY: tuple[dict, ...] = (
    {"name": "workbuddy", "order": 0, "module": "core.backends.workbuddy",
     "attr": "WorkBuddyBackend", "lazy": False},
    {"name": "trae", "order": 1, "module": "core.backends.trae",
     "attr": "TraeBackend", "lazy": True},
)


def backend_module(name: str):
    """懒后端自己的模块对象（如 ``core.backends.trae``）。

    给「必须用到那个模块里、不适合搬到声明上的东西」用：模型名映射
    （``map_model``）、未知模型的异常类、动态目录的模块级读取。这些东西的形状
    各后端差异大，硬塞进 ``BackendSpec`` 只会让声明变成一个什么都装的袋子。

    只在**真正要走该后端**的运行时路径上调用 —— 它会 import 那个模块。
    """
    from importlib import import_module

    for entry in _REGISTRY:
        if entry["name"] == str(name or ""):
            return import_module(entry["module"])
    raise KeyError(name)


def _construct(entry: dict) -> BackendSpec:
    """按注册表条目构造后端实例。

    懒后端在这一步才 import 它的模块 —— 这正是 ``lazy`` 声明的意义。
    非懒后端（workbuddy）的模块在文件顶部就 import 了，这里的 ``backend_module``
    只是一次命中缓存的查找；统一走同一条路，就不必在这里认后端名字。
    """
    cls = getattr(backend_module(entry["name"]), entry["attr"])
    return cls()


def _load(name: str) -> BackendSpec:
    """按名字懒构造后端单例。

    懒构造是为了让 trae 保持「按需 import」——模块顶部强依赖 trae 会把它的
    一整套私有协议（以及登录态）拖进所有部署形态。
    """
    for entry in _REGISTRY:
        if entry["name"] == name:
            return _construct(entry)
    raise KeyError(name)


def backends() -> dict[str, BackendSpec]:
    """已构造的后端表。非懒后端在这一步补齐，懒后端要等 `spec_for` 触碰。"""
    for entry in _REGISTRY:
        if not entry["lazy"] and entry["name"] not in _BACKENDS:
            _BACKENDS[entry["name"]] = _construct(entry)
    return _BACKENDS


def known_names() -> tuple[str, ...]:
    """全部已注册后端的名字，按管理页展示顺序。

    取代写死的 ``KNOWN_BACKENDS`` 元组：新增后端后，WAF 分桶、backend 白名单、
    管理页后端列表都自动跟上，不必再改三处。

    顺序稳定（先 workbuddy 后 trae，其余按 order/名字），因为前端与
    ``/v1/models`` 都依赖稳定顺序。trae 被 kill switch 关掉时也仍然列出——
    它是**已知**后端，只是当前禁用；桶时有时无会让前端要处理两种形状。
    """
    order = {e["name"]: e["order"] for e in _REGISTRY}
    names = set(_BACKENDS) | {e["name"] for e in _REGISTRY}
    return tuple(sorted(names, key=lambda n: (order.get(n, 50), n)))


def default_name() -> str:
    """默认后端名：账号 doc 缺 backend 字段、模型名无前缀时都归到它。

    由 ``default_for_missing_backend`` 声明 —— 注册表里**只有一个**后端能声明
    它是默认后端（``test_at_most_one_default_backend`` 守着）。
    """
    for name in known_names():
        if spec_for(name).default_for_missing_backend:
            return name
    raise RuntimeError("注册表里没有后端声明 default_for_missing_backend=True")


def spec_for(name) -> BackendSpec:
    """按后端名取 spec（未知名字 → 默认后端）。

    已知但**尚未构造**的后端在这里按需构造 —— 判据是注册表里的名字，
    不是 ``_BACKENDS`` 里有没有实例（那正是 P1 修过的那类漏判）。
    """
    name = str(name or "")
    if name in _BACKENDS:
        return _BACKENDS[name]
    for entry in _REGISTRY:
        if entry["name"] == name:
            return _BACKENDS.setdefault(name, _construct(entry))
    return spec_for(default_name())


def prefix_for(model) -> str:
    """模型名命中的后端前缀（无前缀 → 空串）。"""
    for name in known_names():
        prefix = spec_for(name).model_prefix
        if prefix and isinstance(model, str) and model.startswith(prefix):
            return prefix
    return ""


def strip_prefix(model) -> str:
    """剥掉后端前缀，得到上游模型名（无前缀时原样返回）。"""
    prefix = prefix_for(model)
    return model[len(prefix):].strip() if prefix else (model or "")


def canonical_name(model) -> str:
    """模型名（对外 id 或上游 id）→ **规范名** —— 跨后端的同一个模型。

    规则（specs/model-routing-by-policy.md §4，用户已拍板）：
    1. 先看所属后端的 ``canonical_for`` 声明里有没有**非恒等**映射 ——
       有就用它（``kimi-k3-1 → kimi-k3``）；
    2. 否则默认 = ``model_id`` **剥掉后端前缀**。

    只声明非恒等映射：恒等映射没有信息量，却让每个新后端都要为自己的每个
    模型抄一遍自己的 id，改上游 id 时反而多一处要同步的地方。

    这是**分组与自动选后端共用的唯一判定处**。渲染与路由若各算一遍，
    就会出现「显示 2 条、实际只路由到 1 条」且界面不报错。
    """
    mid = str(model or "")
    backend = backend_name_for(mid)
    upstream = strip_prefix(mid)
    mapping = spec_for(backend).canonical_for or {}
    # 映射键优先按**上游 id**（剥前缀后）匹配：`canonical_for` 是「上游叫什么 →
    # 规范名叫什么」，写声明时不用关心前缀（前缀是本后端的属性，不该抄进映射）。
    if upstream in mapping:
        return str(mapping[upstream]).strip() or upstream
    if mid in mapping:
        return str(mapping[mid]).strip() or upstream
    return upstream


#: 设置页总闸的数据源（``{后端名: 是否启用}``，缺键即启用）。
#:
#: **为什么要注入而不是直接读 DB**：本模块是 core 层，不能 import admin
#: （admin 依赖 registry，反向 import 成环）。admin 在启动时把一个「读内存
#: 字典」的可调用对象塞进来——与 ``trae.set_catalog_provider`` 同一模式
#: （core 侧对象持有 admin 启动时注入的可调用对象）。
#:
#: 为什么必须是**内存**字典而不是每次查库：``is_enabled`` 在请求路径上
#: （``resolve_backend`` 每次请求都问一次），不能带一次 SQLite 查询。
_enabled_provider = None


def set_enabled_provider(provider) -> None:
    """admin 启动时注入总闸数据源（返回 ``{后端名: bool}`` 或 None）。

    传 None 退回「全启用」——测试与脚本直接构造 registry 时的默认行为，
    与总闸功能上线前完全一致。
    """
    global _enabled_provider
    _enabled_provider = provider


def toggle_disabled(name: str) -> bool:
    """设置页总闸是否把该后端关掉了（读注入的数据源，缺键/无数据源=启用）。"""
    if _enabled_provider is None:
        return False
    try:
        flags = _enabled_provider() or {}
    except Exception:
        # 数据源异常时**按启用处理**：总闸是「下线功能」，因读取失败就把
        # 线上后端下线会造成大面积 400，比不禁用坏得多。
        return False
    return not bool(flags.get(name, True))


def is_enabled(name: str) -> bool:
    """该后端是否启用：环境逃生门 **且** 设置页总闸，两者都为「开」。

    两层的分工（specs/backend-toggle.md §3）：
    - **环境逃生门**（``BackendSpec.kill_switch_env``）：运维手段，不经过 UI，
      优先级最高。关掉它 UI 也救不回来——这是「出事了最快的止血方式」。
    - **设置页总闸**（``settings.backend_enabled``）：用户功能，缺键即启用。

    两层都读**声明**，不认后端名：加一个带逃生门的后端不用改这里。
    """
    return not spec_for(name).env_disabled() and not toggle_disabled(name)


def backend_name_for(model) -> str:
    """中间件/池用：模型名 → 后端名（不 raise）。无前缀 → 默认后端。

    判定顺序（两处都读**声明**，不认后端名）：

    1. **私有模型**（``BackendSpec.pinned_models``）：无前缀的虚拟模型（三档
       智能路由）由声明指定归属，不走「无前缀 ⇒ 默认后端」的兜底；
    2. **前缀**：带前缀的 id 归声明了该前缀的后端。

    两处都没有 ⇒ 默认后端（``auto`` 及一切裸名走这里）。
    """
    if isinstance(model, str):
        for name in known_names():
            if model in spec_for(name).pinned_models:
                return name
        for name in known_names():
            prefix = spec_for(name).model_prefix
            if prefix and model.startswith(prefix):
                return name
    return default_name()


def trae_enabled() -> bool:
    """环境逃生门：CODEBUDDY_ENABLE_TRAE=0 时全链路禁用 Trae（模型列表/路由/目录）。

    保留这个函数名给既有调用方/测试用；它只看环境变量，**不含**设置页总闸
    ——要问「整体启不启用」用 :func:`is_enabled`。
    """
    return os.environ.get("CODEBUDDY_ENABLE_TRAE", "1") != "0"


def resolve_backend(model) -> tuple[BackendSpec, str]:
    """返回 (spec, upstream_model)。

    `trae/<名>` → TraeBackend + map_model 归一后的 config_name；
    未知 Trae 模型 → HTTPException(400)（CODEBUDDY_TRAE_ALLOW_UNKNOWN_MODELS=1 时放行透传）。
    其余 → WorkBuddyBackend + 原模型名（空/auto 由各端点自行 setdefault）。
    """
    prefix = prefix_for(model)
    if prefix:
        name = backend_name_for(model)
        if not is_enabled(name):
            # 文案不区分「谁关的」：环境逃生门关掉时提示「管理员禁用」也不算错
            # （管理员就是运维），统一一句比让用户对着两种措辞猜要省事。
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"{spec_for(name).label} 后端已被管理员禁用",
                        "type": "invalid_request_error",
                    }
                },
            )
        raw = strip_prefix(model)
        if name == "trae":
            trae = backend_module("trae")
            if os.environ.get("CODEBUDDY_TRAE_ALLOW_UNKNOWN_MODELS", "0") == "1":
                return spec_for("trae"), raw
            try:
                return spec_for("trae"), trae.map_model(raw)
            except trae.UnknownTraeModel as e:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "message": f"未知的 Trae 模型：{raw}",
                            "type": "invalid_request_error",
                        }
                    },
                ) from e
        return spec_for(name), raw
    return spec_for(default_name()), (model if isinstance(model, str) else "")


def make_credential(path):
    """按 .info 文档的 backend 字段分派凭据管理器。

    - {"backend": "trae", ...}  → TraeCredentialManager（秒级 expiresAt、ExchangeToken 刷新）
    - 其余（含缺省/旧格式）      → converter.CredentialManager（毫秒级，行为不变）
    读文件失败时回落默认后端（保持旧行为，错误在 get_headers 时暴露）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    if isinstance(doc, dict):
        name = doc.get("backend")
        # 判据是「这个名字是不是已知后端」，**不是**「它是否已被构造」。
        # 用 _BACKENDS 判断会漏掉懒构造的后端（还没被触碰过就不在表里），
        # 于是那份凭据被静默交给默认后端解析 —— 类型不对但不报错，
        # 正是「加后端零改动」最容易悄悄破掉的地方。
        if name in known_names():
            return spec_for(name).make_credential(path)
    return spec_for(default_name()).make_credential(path)


def backend_meta() -> list[dict]:
    """管理页用的后端元数据（前端不再写死后端名，见 GET /admin/api/backends）。

    ``enabled`` 反映 kill switch；``accounts`` 留给调用方填（server 侧知道账号数）。
    """
    out = []
    for name in known_names():
        spec = spec_for(name)
        out.append({
            "name": spec.name,
            "label": spec.label,
            "short": spec.short,
            "icon": spec.icon,
            "icon_svg": spec.icon_svg,
            "color": spec.color,
            "order": spec.order,
            "prefix": spec.model_prefix,
            "login_kind": spec.login_kind,
            # 两个字段分开给：enabled = 总闸 ∧ 逃生门（整体启不启用），
            # env_disabled = 单看逃生门。前端靠后者把这类框置灰并说明原因——
            # 否则用户勾上一个被环境变量禁用的后端，保存成功但毫无效果（静默 no-op）。
            "enabled": is_enabled(spec.name),
            "env_disabled": spec.env_disabled(),
            "default": spec.default_for_missing_backend,
        })
    return sorted(out, key=lambda m: (m["order"], m["name"]))


def catalog_ids(backend) -> list[str]:
    """某后端当前生效目录的 id 列表（动态表优先，静态表兜底）。

    ``BackendSpec.catalog_entries()`` 是第一来源；但目录提供者是**实例级**的，
    而 ``backend_module`` 上的模块级入口（如 trae 的 ``trae_model_catalog``）
    在没有实例时会回落到静态表。测试要验「提供者装了没有」时必须看**实例**，
    所以这里优先走 spec，取不到才回落模块级函数。
    """
    spec = spec_for(backend)
    entries = spec.catalog_entries()
    if entries:
        return [m["id"] for m in entries]
    module = backend_module(backend)
    fallback = getattr(module, f"{backend}_model_catalog", None)
    return [m["id"] for m in fallback()] if callable(fallback) else []


def trae_model_ids() -> list[str]:
    """Trae 模型表的 id 列表（保留旧名；实现已泛化为 catalog_ids）。"""
    return catalog_ids("trae")

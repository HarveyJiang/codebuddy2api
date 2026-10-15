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


def _trae_module():
    from . import trae  # 延迟 import：phase 隔离 + 防循环

    return trae


def _load(name: str) -> BackendSpec:
    """按名字懒构造后端单例。

    懒构造是为了让 trae 保持「按需 import」——模块顶部强依赖 trae 会把它的
    一整套私有协议（以及登录态）拖进所有部署形态。
    """
    if name == "trae":
        return _trae_module().TraeBackend()
    if name == "workbuddy":
        return WorkBuddyBackend()
    raise KeyError(name)


def trae_backend() -> BackendSpec:
    """Trae 后端单例（按需构造）。"""
    if "trae" not in _BACKENDS:
        _BACKENDS["trae"] = _trae_module().TraeBackend()
    return _BACKENDS["trae"]


def backends() -> dict[str, BackendSpec]:
    """后端表（workbuddy 恒可用；trae 按需懒构造）。"""
    if "workbuddy" not in _BACKENDS:
        _BACKENDS["workbuddy"] = WorkBuddyBackend()
    return _BACKENDS


def known_names() -> tuple[str, ...]:
    """全部已注册后端的名字，按管理页展示顺序。

    取代写死的 ``KNOWN_BACKENDS`` 元组：新增后端后，WAF 分桶、backend 白名单、
    管理页后端列表都自动跟上，不必再改三处。

    顺序稳定（先 workbuddy 后 trae，其余按 order/名字），因为前端与
    ``/v1/models`` 都依赖稳定顺序。trae 被 kill switch 关掉时也仍然列出——
    它是**已知**后端，只是当前禁用；桶时有时无会让前端要处理两种形状。
    """
    order = {"workbuddy": 0, "trae": 1}
    names = set(_BACKENDS) | {"workbuddy", "trae"}
    return tuple(sorted(names, key=lambda n: (order.get(n, 50), n)))


def default_name() -> str:
    """默认后端名：账号 doc 缺 backend 字段、模型名无前缀时都归到它。

    由 ``default_for_missing_backend`` 声明，不再硬编码 "workbuddy"。
    """
    for spec in backends().values():
        if spec.default_for_missing_backend:
            return spec.name
    return "workbuddy"


def spec_for(name) -> BackendSpec:
    """按后端名取 spec（未知名字 → 默认后端）。"""
    name = str(name or "")
    if name in ("trae",):
        return trae_backend()
    return backends().get(name) or backends()["workbuddy"]


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


def is_enabled(name: str) -> bool:
    """该后端是否在全局 kill switch 下启用。

    kill switch 目前只有 Trae 有（``CODEBUDDY_ENABLE_TRAE``）。这是**环境开关**，
    与后端能力无关，所以按名字查是合适的 —— 它是「这个名字确实有开关」的白名单，
    而不是「按名字分支」的逻辑。加一个没有 kill switch 的后端时这里不用改。
    """
    if name == "trae":
        return trae_enabled()
    return True


def backend_name_for(model) -> str:
    """中间件/池用：模型名 → 后端名（不 raise）。无前缀 → 默认后端。"""
    for name in known_names():
        prefix = spec_for(name).model_prefix
        if prefix and isinstance(model, str) and model.startswith(prefix):
            return name
    return default_name()


def trae_enabled() -> bool:
    """kill switch：CODEBUDDY_ENABLE_TRAE=0 时全链路禁用 Trae（模型列表/路由/目录）。"""
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
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"{spec_for(name).label} 后端已禁用（CODEBUDDY_ENABLE_TRAE=0）",
                        "type": "invalid_request_error",
                    }
                },
            )
        raw = strip_prefix(model)
        if name == "trae":
            trae = _trae_module()
            if os.environ.get("CODEBUDDY_TRAE_ALLOW_UNKNOWN_MODELS", "0") == "1":
                return trae_backend(), raw
            try:
                return trae_backend(), trae.map_model(raw)
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
            "color": spec.color,
            "order": spec.order,
            "prefix": spec.model_prefix,
            "login_kind": spec.login_kind,
            "enabled": is_enabled(spec.name),
            "default": spec.default_for_missing_backend,
        })
    return sorted(out, key=lambda m: (m["order"], m["name"]))


def trae_model_ids() -> list[str]:
    """Trae 模型表的 id 列表（动态表优先，静态表兜底）。"""
    return [m["id"] for m in _trae_module().trae_model_catalog()]

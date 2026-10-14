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

TRAE_PREFIX = "trae/"

_BACKENDS: dict[str, BackendSpec] = {}


def _trae_module():
    from . import trae  # 延迟 import：phase 隔离 + 防循环

    return trae


def backends() -> dict[str, BackendSpec]:
    """后端表（workbuddy 恒可用；trae 按需懒构造，避免模块顶部强依赖 trae）。"""
    if "workbuddy" not in _BACKENDS:
        _BACKENDS["workbuddy"] = WorkBuddyBackend()
    return _BACKENDS


def trae_backend() -> BackendSpec:
    """Trae 后端单例（按需构造）。"""
    if "trae" not in _BACKENDS:
        _BACKENDS["trae"] = _trae_module().TraeBackend()
    return _BACKENDS["trae"]


def backend_name_for(model) -> str:
    """中间件/池用：模型名 → 后端名（不 raise）。无前缀 → workbuddy。"""
    if isinstance(model, str) and model.startswith(TRAE_PREFIX):
        return "trae"
    return "workbuddy"


def resolve_backend(model) -> tuple[BackendSpec, str]:
    """返回 (spec, upstream_model)。

    trae/<名> → TraeBackend + map_model 归一后的 config_name；
    未知 Trae 模型 → HTTPException(400)（CODEBUDDY_TRAE_ALLOW_UNKNOWN_MODELS=1 时放行透传）。
    其余 → WorkBuddyBackend + 原模型名（空/auto 由各端点自行 setdefault）。
    """
    if isinstance(model, str) and model.startswith(TRAE_PREFIX):
        if os.environ.get("CODEBUDDY_ENABLE_TRAE", "1") == "0":
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "Trae 后端已禁用（CODEBUDDY_ENABLE_TRAE=0）",
                        "type": "invalid_request_error",
                    }
                },
            )
        trae = _trae_module()
        raw = model[len(TRAE_PREFIX):].strip()
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
    return backends()["workbuddy"], (model if isinstance(model, str) else "")


def make_credential(path):
    """按 .info 文档的 backend 字段分派凭据管理器。

    - {"backend": "trae", ...}  → TraeCredentialManager（秒级 expiresAt、ExchangeToken 刷新）
    - 其余（含缺省/旧格式）      → converter.CredentialManager（毫秒级，行为不变）
    读文件失败时回落 workbuddy（保持旧行为，错误在 get_headers 时暴露）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    if isinstance(doc, dict) and doc.get("backend") == "trae":
        return _trae_module().TraeCredentialManager(path)
    return backends()["workbuddy"].make_credential(path)


def trae_model_ids() -> list[str]:
    """Trae 静态模型表的 id 列表（32 条 config_name）。"""
    return [m["id"] for m in _trae_module().TRAE_STATIC_MODELS]

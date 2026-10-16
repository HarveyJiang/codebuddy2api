"""WorkBuddyBackend — 腾讯 CodeBuddy/WorkBuddy 后端（原有生产路径的封装）。

行为保持约束（字节级）：
- 200 → `aiter_bytes()` 原样直通（含工具调用 SSE，一个字节都不改）；
- 非 2xx → raise BackendHTTPError(status, body)（消费方补 `_err_event`，
  与旧 `_stream_upstream` 的非 200 分支输出完全一致）；
- httpx.HTTPError → raise BackendHTTPError(502, transport=True)；
- timeout 语义逐调用点透传（chat 流式 None / responses 流式 300 /
  非流式 120 / count_tokens 30），与旧代码各 call-site 一致。
"""
from __future__ import annotations

import httpx

from .base import BackendHTTPError, BackendSpec


class WorkBuddyBackend(BackendSpec):
    name = "workbuddy"
    owned_by = "codebuddy"
    tencent_policies = True

    # 默认后端：接管所有不带前缀的模型名（含 auto）。
    model_prefix = ""
    default_for_missing_backend = True

    # 管理页元数据（需求 2/5/7）
    label = "WorkBuddy"
    short = "WB"
    icon = "◈"
    # 图形场景用 SVG：外菱形描边 + 内实心小菱形（对应字符 ◈ 的「菱形套小菱形」）。
    # viewBox 24×24、fill=currentColor，尺寸全交给 CSS（见 icon_svg 的约定）。
    icon_svg = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M12 2.6 21.4 12 12 21.4 2.6 12z"/>'
        '<path d="M12 9.2 14.8 12 12 14.8 9.2 12z" fill="currentColor" stroke="none"/>'
        "</svg>"
    )
    color = "#2f6fed"
    order = 0
    login_kind = "browser"

    # 凭据：毫秒级 expiresAt，本身的 CredentialManager 就带 backend_name="workbuddy"。
    credential_backend = "workbuddy"
    expires_at_to_ms = 1

    # 积分/签到走 pool 的默认实现（billing 接口），故不声明 account_ops_backend。
    #
    # 模型目录来自 /v3/config（见 admin/models.py:fetch）。它只认**本后端**账号的
    # 凭据，所以拉目录时必须挑一个默认后端的账号 —— 由本声明告诉 admin，
    # 而不是在 admin 里写死「用 workbuddy 账号」。
    needs_own_credential_for_catalog = True

    # 三档智能路由（fast / balanced / deep）是**本后端私有**的虚拟模型：
    # 无前缀，且不参与裸名自动选择（那种请求只会命中默认后端＝本后端）。
    # 声明出来是防将来路由策略改动把它们选到别的后端去 —— 这不是「无前缀
    # 所以恰好归我」，而是「本来就只可能归我」。
    pinned_models = ("fast-model", "balanced-model", "deep-model")

    def chat_url(self) -> str:
        from .. import converter  # 延迟 import 防循环

        return f"{converter.BACKEND}/v2/chat/completions"

    def make_credential(self, path):
        from .. import converter  # 延迟 import 防循环

        return converter.CredentialManager(path)

    async def astream(self, headers, body, *, timeout=None, raise_on_error=False):
        url = self.chat_url()
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        raw = await r.aread()
                        raise BackendHTTPError(r.status_code, raw)
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as e:
            raise BackendHTTPError(502, str(e), transport=True) from e

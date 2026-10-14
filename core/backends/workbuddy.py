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

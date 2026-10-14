"""BackendSpec / BackendHTTPError — 上游后端的唯一抽象。

astream 契约（所有后端必须遵守）：
- 正常路径：yield 的字节**已是 OpenAI Chat SSE**（`data: {...}\\n\\n` 行序列，
  以 `data: [DONE]` 结尾；Trae 归一化器保证无 done 时也补 `[DONE]`）。
- 非 2xx / 传输层错误：raise BackendHTTPError(emitted=False)。
- Trae 流内业务错误（event:error）：
  - raise_on_error=False（流式端点）：astream 先 yield
    `data: {"error":{"code":<映射码>}}` + `data: [DONE]`，再 raise
    BackendHTTPError(emitted=True) —— 让 PoolMiddleware 的 SSE 扫描命中
    `error.code ∈ {401,402,403,429}` → 冷却账号，中间件与适配器零改动。
  - raise_on_error=True（非流式 / count_tokens）：直接 raise，不 emit。
"""
from __future__ import annotations

from typing import AsyncIterator


class BackendHTTPError(Exception):
    """上游错误的统一异常。

    status:    HTTP 语义状态码。Trae 流内业务错误按 classify 映射
              （1005→402 额度耗尽、401→401、429→429、404→404、5xx→5xx）。
    body:     原始错误体字节（供 _safe_err_raw 解析 / _err_event 透传）。
    emitted:  True 表示错误已以 OpenAI error chunk + [DONE] 发给客户端，
              消费方（chat/responses/anthropic 流式）不应再补发错误事件。
    transport: True 表示传输层错误（httpx.HTTPError），status 恒为 502。
    """

    def __init__(self, status: int, body, *, emitted: bool = False, transport: bool = False):
        super().__init__(f"backend error status={status} emitted={emitted} transport={transport}")
        self.status = int(status)
        if isinstance(body, (bytes, bytearray)):
            self.body = bytes(body)
        else:
            self.body = str(body).encode("utf-8", "replace")
        self.emitted = emitted
        self.transport = transport


class BackendSpec:
    """上游后端规格。子类覆盖 name/owned_by/tencent_policies 与 astream。"""

    #: 后端标识（"workbuddy" / "trae"），与账号 doc 的 backend 字段对应
    name: str = "base"
    #: /v1/models 里该后端模型的 owned_by
    owned_by: str = "base"
    #: 是否套用腾讯内容策略（developer→system、filter_system_identity、
    #: desensitize、project_responses_chat_body、stream_options、过滤重试）。
    #: WorkBuddy=True（全保留）；Trae=False（上游是私有协议，全跳过）。
    tencent_policies: bool = False

    def chat_url(self) -> str:
        raise NotImplementedError

    async def astream(
        self,
        headers: dict,
        body: dict,
        *,
        timeout: float | None = None,
        raise_on_error: bool = False,
    ) -> AsyncIterator[bytes]:
        """POST 上游并产出 OpenAI Chat SSE 字节。见模块 docstring 契约。"""
        raise NotImplementedError
        yield b""  # pragma: no cover — 让它成为 async generator

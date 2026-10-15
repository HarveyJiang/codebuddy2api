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

    def __init__(self, status: int, body, *, emitted: bool = False, transport: bool = False,
                 upstream_code: int | None = None):
        super().__init__(f"backend error status={status} emitted={emitted} transport={transport}")
        self.status = int(status)
        if isinstance(body, (bytes, bytearray)):
            self.body = bytes(body)
        else:
            self.body = str(body).encode("utf-8", "replace")
        self.emitted = emitted
        self.transport = transport
        #: 上游业务错误码（如 Trae 流内 event:error 的 1005/4001），供重试判定
        self.upstream_code = upstream_code


class BackendSpec:
    """上游后端规格。子类覆盖下面的能力声明与 chat_url/astream。

    设计约束：**调用方不得再写 `backend == "trae"` 这类分支**。凡是一个后端
    与另一个后端不同的地方，都在这里声明成属性/方法，调用方读声明、不读名字。
    这样「新增一个后端」= 新增一个子类 + 注册，pool/server/models/converter 零改动。

    下面的默认值对应「一个最朴素的后端」。每个子类只需覆盖与自己不同的项。
    """

    # --- 身份 ------------------------------------------------------------

    #: 后端标识（"workbuddy" / "trae"），与账号 doc 的 backend 字段对应
    name: str = "base"
    #: /v1/models 里该后端模型的 owned_by
    owned_by: str = "base"
    #: 模型 id 前缀（"trae/"）。空串表示「无前缀」——该后端是**默认后端**，
    #: 接管所有不带前缀的模型名。全局只能有一个后端声明空前缀。
    model_prefix: str = ""
    #: 管理页展示元数据（需求 2/5/7）。前端经 GET /admin/api/backends 读取，
    #: 不再写死后端名。order 决定展示顺序（默认后端在最前）。
    label: str = "base"
    short: str = "base"
    icon: str = "◈"
    color: str = "#888888"
    order: int = 100
    #: 是否需要（且有）交互式授权登录流程（前端据此决定是否渲染「XX 授权」按钮）。
    #: WorkBuddy 走文件导入 + 浏览器授权，Trae 走 PKCE 授权，都按自己的面板渲染。
    login_kind: str = ""  # "" | "browser" | "trae" | "import"

    # --- 内容策略 --------------------------------------------------------

    #: 是否套用腾讯内容策略（developer→system、filter_system_identity、
    #: desensitize、project_responses_chat_body、stream_options、过滤重试）。
    #: WorkBuddy=True（全保留）；Trae=False（上游是私有协议，全跳过）。
    tencent_policies: bool = False
    #: 非流式聚合时是否保留 delta.reasoning_content（Trae 保留）。
    keep_reasoning_delta: bool = False
    #: count_tokens 是否走上游。Trae 默认本地估算（上游大概率忽略 max_tokens=1，
    #: 走真实请求会白烧生成额度）。
    count_tokens_upstream: bool = True

    # --- 凭据 ------------------------------------------------------------

    #: 凭据管理器的「所属后端」标识。池选号后用它与请求后端比对做一致性闸门
    #: （防独立部署下 A 后端账号收到 B 后端请求）。None = 用本 spec 的 name。
    credential_backend: str | None = None
    #: 账号 doc 里 expiresAt 的单位换算到毫秒的倍数。
    #: WorkBuddy=1（本来就是毫秒）；Trae=1000（Unix 秒）。
    expires_at_to_ms: int = 1
    #: 账号 doc 无 backend 字段时的默认归属（旧格式凭据）。None = 用本 spec 的 name。
    default_for_missing_backend: bool = False

    # --- 凭据 ------------------------------------------------------------

    #: 凭据管理器的「所属后端」标识。池选号后用它与请求后端比对做一致性闸门
    #: （防独立部署下 A 后端账号收到 B 后端请求）。None = 用本 spec 的 name。
    credential_backend: str | None = None
    #: 账号 doc 里 expiresAt 的单位换算到毫秒的倍数。
    #: WorkBuddy=1（本来就是毫秒）；Trae=1000（Unix 秒）。
    expires_at_to_ms: int = 1
    #: 账号 doc 无 backend 字段时的默认归属（旧格式凭据）。None = 用本 spec 的 name。
    default_for_missing_backend: bool = False

    def credential_name(self) -> str:
        """凭据一致性闸门用的名字（见 core/converter.py 的 _selected_backend）。"""
        return self.credential_backend or self.name

    def make_credential(self, path):
        """按本后端格式构造凭据管理器。子类必须实现。"""
        raise NotImplementedError

    # --- 模型目录 ---------------------------------------------------------

    #: 是否参与「服务启动时拉取上游模型目录」的后台刷新。默认 True。
    #: 一个后端的目录可能是纯静态的（无上游可拉），那就声明 False。
    refreshable_catalog: bool = True
    #: 该后端是否需要**专属凭据**去拉目录。为 True 时 admin 会用
    #: `any_manager(<本后端名>)` 拿一个本后端的账号；为 False 时用默认后端的账号。
    #: 理由：默认后端的目录接口只认它自己的凭据（拿别的后端的 headers 打过去必然 4xx）。
    needs_own_credential_for_catalog: bool = False
    #: 从未拉到过动态目录时的静态兜底条目：[{id, name, context_length, ...}]。
    #: 空列表表示该后端没有静态兜底（目录只能来自上游）。
    static_catalog: list = []

    #: 凭据管理器上「拉取本后端模型目录」的方法名。
    #: 目录的传输细节属于凭据（登录态在它手里），所以约定它是
    #: ``manager.<catalog_method>()-> list[{id, name, context_length, ...}] | None``。
    #: 为空串表示该后端不支持动态目录（只吃 static_catalog）。
    catalog_method: str = ""

    def fetch_catalog(self, manager):
        """用给定凭据管理器拉一次本后端模型目录；返回 None 表示这次没拉到。

        默认实现转发到凭据管理器的 ``catalog_method``。子类可在需要时覆盖
        （例如目录要从别处取、或要多打几个接口）。
        """
        if not self.catalog_method:
            return None
        return getattr(manager, self.catalog_method)()

    def catalog_entries(self):
        """本后端**当前生效**的目录：[{id, name, context_length}]（bare id）。

        注册表注入的提供者会转发到这里，所以子类读的是自己模块里的目录状态
        （动态表优先、静态兜底）。默认返回声明的静态表 —— 没有动态目录的后端
        不必实现这个。
        """
        return [{"id": m["id"], "name": m.get("name") or m["id"],
                 "context_length": m.get("context_length")} for m in self.static_catalog]

    def set_catalog_provider(self, provider):
        """注册一个无参 callable，返回上面那种列表（或 None/空 → 回落静态表）。

        由 admin 在启动时注入：它知道「动态目录存在哪、什么时候刷过」，
        后端模块不必依赖 admin。默认实现只记住它，供 ``catalog_entries`` 覆盖者使用。
        """
        self._catalog_provider = provider

    # --- 账号操作（积分 / 签到） -------------------------------------------

    #: 该后端是否参与自动签到（pool.tick）。默认 True。
    supports_auto_checkin: bool = True

    #: 账号操作（积分/签到）由谁实现：None = 用 pool 里的默认实现（默认后端走这条路，
    #: 因为是 billing 接口）；否则是后端名，表示「交给该后端自己的实现」。
    #:
    #: 为什么是这么个形状：默认后端的积分/签到是唯一一套「写死在 pool 里」的实现，
    #: 另建一个后端时不该去改 pool，所以让它在此声明自己接管。pool 只做分派。
    account_ops_backend: str | None = None

    # --- 失败归因 --------------------------------------------------------

    #: 该后端「软冷却」的上游状态码 → 冷却秒数。命中的状态码不进连续失败计数
    #: （账号本身通常是好的，只是该模型/资源不可用）。
    #: Trae: {404: 600}（config_name 不存在）；WorkBuddy: {}（404 按硬失败处理）。
    soft_cooldown: dict = {}

    def soft_cooldown_for(self, status: int) -> float | None:
        """该状态码是否触发软冷却；是则返回冷却秒数。"""
        return self.soft_cooldown.get(int(status))

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

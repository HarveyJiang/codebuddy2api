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

import os
import sys
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
    #: 该后端的**环境逃生门**环境变量名（None = 没有）。设成 "0" 即全链路禁用。
    #:
    #: 为什么是声明而不是在 ``is_enabled`` 里写 ``if name == "trae"``：
    #: 「这个名字有没有开关」是**每个后端自己的属性**，认名字等于把注册表
    #: 变成名单。声明化后加后端不用改 registry，也让 arch guard 能守住
    #: 「禁止按后端名分支」这条既有铁律（原先那个 if 是它的唯一例外）。
    #:
    #: 与设置页总闸（``settings.backend_enabled``）的关系：环境门优先级更高
    #: （运维逃生用，不经过 UI），两者都为「开」才启用。见 registry.is_enabled。
    kill_switch_env: str | None = None

    def env_disabled(self) -> bool:
        """环境逃生门是否关闭。读声明，不认名字。"""
        return bool(self.kill_switch_env) and os.environ.get(self.kill_switch_env, "1") == "0"

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

    #: 本后端**私有的虚拟模型**（bare id）：不走上游目录、永远归属本后端、
    #: 不参与裸名自动选择候选（见 specs/model-routing-by-policy.md）。
    #:
    #: 为什么是声明而不是在 backend_name_for 里写死名字：同名模型在多个后端
    #: 都可能有，但「它属于谁」是后端自己的属性。声明化后加后端不用改判定处
    #: （``backend_name_for`` 是**前缀**判定，读的是各后端自己的 ``model_prefix``），
    #: 也让「不得出现 ``== "fast-model"`` 这类字面量分支」能被 arch guard 顺带守住。
    #:
    #: 与 `model_prefix` 的关系：前缀只能表达「本后端接管哪些**带前缀**的 id」，
    #: 表达不了「一个无前缀 id 固定属于我这个后端」（否则就要抢默认后端的名额）。
    #: 今天这些无前缀 id 恰好落到默认后端（``backend_name_for`` 的兜底分支），
    #: 加这个声明后它从「巧合」变成「保证」。
    pinned_models: tuple = ()

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

    # --- 上游错误的解释权 --------------------------------------------------

    #: 本后端的账号操作会抛出的「上游调用失败」异常类型。pool 用它做类型化兜底
    #: （401/403 → 凭据失效；其余原样透出）。
    #:
    #: 这里给的是**属性名**而不是异常类本身，也不是一个转发判定的方法 —— 因为
    #: 那两个写法都必须在本模块顶部 import 各后端的模块才能拿到那个类，而
    #: registry 的懒构造设计（见 registry._load）要求后端模块只在真用到时才
    #: import。写成「类名 → 运行时按子类模块取」就既不急切 import，也不必为
    #: 每个后端在基类里写一个转发方法（那正是 requirements 2 要消灭的形状）。
    error_class_name: str = "BackendHTTPError"

    def upstream_error_class(self):
        """本后端上游错误的异常类（用于 pool 的 except 类型化兜底）。

        约定：``error_class_name`` 指向的类必须与 BackendSpec 子类**同模块**
        （即 ``type(self).__module__``）。写别的模块的名字会解析失败 ——
        这里显式报错而不是让 ``getattr`` 抛裸 ``AttributeError``：这段代码跑在
        **异常处理路径**上，最不该在这种地方出现难懂的二次异常。
        """
        if self.error_class_name == "BackendHTTPError":
            return BackendHTTPError
        module_name = type(self).__module__
        module = sys.modules.get(module_name)
        cls = getattr(module, self.error_class_name, None) if module else None
        if not isinstance(cls, type) or not issubclass(cls, BaseException):
            raise TypeError(
                f"{type(self).__name__}.error_class_name={self.error_class_name!r} "
                f"在模块 {module_name} 里不是异常类；该名字必须指向与后端同模块的异常类型"
            )
        return cls

    def interpret_checkin_claim(self, data: dict):
        """签到 claim 响应的业务码 → ``(成功?, 消息, 可重试?)``。

        默认实现覆盖不了任何后端：签到报文的形状纯粹是各家的私有协议，
        所以约定由**声明了 ``account_ops_backend`` 的后端**自己实现
        （pool 只调 ``spec_for(backend).interpret_checkin_claim(...)``）。
        之所以是 spec 上的方法而不是 pool 里的一个函数 + 类型判断，见
        ``error_class_name`` 的说明：调用方不该知道是哪个后端在签到。
        """
        raise NotImplementedError(f"{type(self).__name__} 未声明签到报文解释规则")

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

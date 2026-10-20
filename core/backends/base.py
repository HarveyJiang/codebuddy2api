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


class BackendUsageError(Exception):
    """拉上游用量明细失败（各后端共用）。

    放在 core 而不是 admin：实现方在后端模块里（core 不能反向依赖 admin），
    而 admin 侧（pool）要把它翻成自己的 BillingError —— 对用户可见的报错
    文案与状态码由 admin 决定，后端只负责说「哪一步坏了、什么状态码」。
    """

    def __init__(self, message, status=502, code=None):
        super().__init__(message)
        self.status = int(status)
        self.code = code


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
    #: **仅供历史迁移读取，运行期禁用**（v16 起对外 id 就是上游裸名）：老库
    #: `trae/glm-5.2` 这类行靠它认出并剥掉（`admin/db.py` 的 `_v11_to_v12` 回填
    #: `backend`、`_v15_to_v16` 剥前缀），所以不能删。新代码请读 `models.backend`
    #: 列或 `admin/pool.py` 的裸名策略，arch guard 会拦住。
    model_prefix: str = ""
    #: 管理页展示元数据（需求 2/5/7）。前端经 GET /admin/api/backends 读取，
    #: 不再写死后端名。order 决定展示顺序（默认后端在最前）。
    label: str = "base"
    short: str = "base"
    #: 字符图标。**纯文本场景**用它（账号详情的「账号类型：◈ WorkBuddy」、title
    #: 提示等）；图形场景见下面的 ``icon_svg``。
    icon: str = "◈"
    #: 内联 SVG 图标（**图形场景**用：行前归属图标、账号类型图标）。
    #: 不能用字符：各字符 East Asian Width 不同（``◈`` 窄、``⚡`` 宽且属 emoji 区），
    #: 同字号下字形盒差近一倍，``font-size`` 调不平；SVG 尺寸只由 viewBox/CSS 决定。
    #: 约定：``viewBox="0 0 24 24"``、``fill="currentColor"``、不写 width/height。
    icon_svg: str = ""
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
    #: 声明而非 ``is_enabled`` 里写 ``if name == "trae"``：「有没有开关」是后端
    #: 自己的属性，认名字会把注册表变成名单，arch guard 也守不住。
    #: 环境门优先级高于设置页总闸（运维逃生用，不经 UI），两者都为「开」才启用。
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
    #: 声明而非在 ``backend_name_for`` 写死名字：「它属于谁」是后端自己的属性，
    #: 这样加后端不用改判定处，arch guard 也能守住「不得按后端名分支」。
    #: 与 `model_prefix` 的关系：前缀只剩历史迁移用途，表达不了「一个无前缀 id
    #: 固定属于本后端」——pinned 是**当下**的判定，这些 id 不走默认后端兜底。
    pinned_models: tuple = ()

    #: 跨后端**规范名**映射：``{上游 id: 规范名}``，只声明**非恒等**的。
    #: 用途（specs/model-routing-by-policy.md §4）：`model_id` 在两边天然不等
    #: （腾讯 `kimi-k3-1` vs 字节 `trae/kimi-k3`），`name` 是展示文本不可靠，
    #: 两者都当不了「同一个模型」的判据，故要一层显式映射，它同时是管理页
    #: **分组键**与**自动选后端的键**。默认空字典 = 「剥前缀即规范名」，
    #: 恒等映射不写（没有信息量，却让每个新后端抄一遍自己的 id）。
    canonical_for: dict = {}

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

    #: 该后端是否有「上游用量明细」可对账（pool 的每日/手动同步）。默认 False。
    #:
    #: 用量对账接口**不是各后端通用**的：域名、鉴权头、分页参数、返回结构
    #: 全都不同（WorkBuddy 打在计费域、用驼峰分页参数、返回 code==0 的包裹；
    #: Trae 打在 api.trae.cn、用下划线分页参数、按 session 聚合且无 code 字段）。
    #: 新后端若无差别地进同步队列，就是每天拿它的 token 去打别人的网关——
    #: 必然 401，只会在 usage_sync_error 里堆一条永远清不掉的留痕。
    supports_upstream_usage: bool = False

    #: 本后端模块里「拉上游用量明细」的**模块级函数名**。
    #: 约定签名 ``f(manager, client, start, end) -> list[dict]``：
    #: - ``manager``：该后端的凭据管理器（用它取该后端该用的头）；
    #: - ``client``：调用方给的 httpx.Client（测试用 MockTransport 注入）；
    #: - ``start``/``end``：datetime（北京时间，带时区）；
    #: - 返回**归一化行**列表，每行 ``{request_id, credit, model, client,
    #:   request_time, agent_purpose, input_trunc}``，字段语义与
    #:   upstream_request_usage 表一一对应。
    #:
    #: 写模块级函数名（而不是 import 或方法转发）是为了不破坏懒构造：
    #: 后端模块在 registry 里是按需 import 的，基类顶层 import 它就等于
    #: 每次都把整个后端拖进来。取函数时用 ``sys.modules.get`` 现取
    #: （与 ``upstream_error_class`` 同一手法）。
    usage_fetcher: str = ""

    def fetch_usage_rows(self, manager, client, start, end) -> list[dict]:
        """拉本后端的上游用量明细，返回归一化行列表。

        默认实现转发到本后端模块里的 ``usage_fetcher``。子类一般不覆盖，
        覆盖的理由只有「拉取需要的不止一次请求」这种。
        """
        if not self.usage_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明上游用量明细拉取方式")
        return self._module_fn(self.usage_fetcher, "上游用量明细")(manager, client, start, end)

    #: 账号操作（积分/签到）由谁实现：None = 用 pool 里的默认实现（默认后端走这条路，
    #: 因为是 billing 接口）；否则是后端名，表示「交给该后端自己的实现」。
    #: 这么个形状的理由：默认后端的积分/签到是 pool 里唯一写死的一套，另建后端
    #: 时不该去改 pool，让它在此声明自己接管，pool 只做分派。
    account_ops_backend: str | None = None

    # --- 账号操作（积分 / 签到）的实现位置 ---------------------------------
    #
    # 与 ``usage_fetcher`` 完全同款：**本后端模块里的模块级函数名**，运行时用
    # ``sys.modules.get(type(self).__module__)`` 现取，不顶层 import（保 registry
    # 懒构造）。约定签名：
    #
    # - ``credits_fetcher``    → ``f(manager, client, headers) -> dict``
    #   （剩余积分 + 过期信息，字段名见 workbuddy.summarize_packages / trae.user_ent_summary）
    # - ``checkin_status_fetcher`` → ``f(manager, client, headers) -> dict``
    #   （``{checked, active|enable, credits}``，字段名两边不同，pool 只取自己那两个键）
    # - ``checkin_claim_fetcher`` → ``f(manager, client, headers) -> dict``
    #   （上游原始签到响应；HTTP 恒 200，成败由 interpret_checkin_claim 判）
    # - ``billing_fetcher``    → ``f(manager, client, path, body) -> dict``
    #   （WorkBuddy 的 billing 直连接口；Trae 没有这个概念，声明为空即可）
    #
    # 用四个独立声明而不是一个 ``account_ops()`` 入口：签到失败不该阻断积分拉取、
    # 积分拉取又要在签到成功后才做，pool 需要**分步**调用，一个大入口反而要
    # 在基类里替各后端决定流程。
    credits_fetcher: str = ""
    checkin_status_fetcher: str = ""
    checkin_claim_fetcher: str = ""
    billing_fetcher: str = ""

    def _module_fn(self, name, what):
        """从本后端模块里取名为 ``name`` 的模块级函数（取不到即报错）。

        三个调用点（fetch_usage_rows / fetch_credits / fetch_checkin_status …）
        共用这套懒取逻辑：后端模块在 registry 里按需 import，基类顶层 import
        会破坏懒构造（每次把整个后端拖进内存）。
        """
        module_name = type(self).__module__
        module = sys.modules.get(module_name)
        fn = getattr(module, name, None) if module else None
        if not callable(fn):
            raise NotImplementedError(
                f"{type(self).__name__} 未提供{what}实现（模块 {module_name} 里没有可调用的 {name!r}）"
            )
        return fn

    def fetch_credits(self, manager, client, headers) -> dict:
        """拉本后端的积分余额与过期信息。字段名与后端无关，前端同一套渲染。"""
        if not self.credits_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明积分拉取方式")
        return self._module_fn(self.credits_fetcher, "积分")(manager, client, headers)

    def fetch_checkin_status(self, manager, client, headers) -> dict:
        """拉本后端的签到状态。"""
        if not self.checkin_status_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明签到状态查询方式")
        return self._module_fn(self.checkin_status_fetcher, "签到状态")(manager, client, headers)

    def fetch_checkin_claim(self, manager, client, headers) -> dict:
        """执行本后端的签到，返回上游原始响应（成败由 interpret_checkin_claim 判）。"""
        if not self.checkin_claim_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明签到执行方式")
        return self._module_fn(self.checkin_claim_fetcher, "签到")(manager, client, headers)

    def fetch_billing(self, manager, client, path, body=None):
        """WorkBuddy 的 billing 直连接口（Trae 无此概念，声明为空 → 不可调用）。"""
        if not self.billing_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明计费接口")
        return self._module_fn(self.billing_fetcher, "计费")(manager, client, path, body or {})

    # --- 成长任务（任务 / 旅行 / 活跃） -----------------------------------
    #
    # 与上面四个 fetcher 完全同款：**本后端模块里的模块级函数名**，运行时用
    # ``sys.modules.get(type(self).__module__)`` 现取，不顶层 import（保 registry
    # 懒构造）。四个动作各自独立声明，而不是一个 ``growth()`` 大入口：
    #
    # - ``growth_tasks_fetcher``   → ``f(manager, client, headers) -> list[dict]``
    #   （成长任务列表；每行已归一化，字段语义与后端无关）
    # - ``growth_task_action_fetcher`` → ``f(manager, client, headers, action, **kw) -> dict``
    #   （``action`` ∈ ``accept``/``claim``：接单与领奖）
    # - ``travel_fetcher``         → ``f(manager, client, headers, action, **kw) -> dict``
    #   （``action`` ∈ ``status``/``depart``/``claim``：猫猫旅行）
    # - ``activity_report_fetcher`` → ``f(manager, client, headers) -> dict``
    #   （活跃上报：一条事件点亮连登并解锁领养任务）
    #
    # 签到与保活不在这里声明：签到走上面已有的 checkin_* fetcher，
    # 保活是「低峰刷新凭据」，由 pool 的定时任务做，两个后端都适用。
    growth_tasks_fetcher: str = ""
    growth_task_action_fetcher: str = ""
    travel_fetcher: str = ""
    activity_report_fetcher: str = ""

    #: 该后端是否参与**成长任务的自动执行**（定时出发/领奖/上报活跃）。
    #: 默认 False：与 supports_upstream_usage 同理——成长中心只认自家登录态，
    #: 别的后端没有这套接口，自动任务打过去只会白跑。
    supports_growth: bool = False

    def fetch_growth_tasks(self, manager, client, headers) -> list[dict]:
        """拉本后端的成长任务列表（已归一化的行）。"""
        if not self.growth_tasks_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明成长任务拉取方式")
        return self._module_fn(self.growth_tasks_fetcher, "成长任务")(manager, client, headers)

    def fetch_growth_task_action(self, manager, client, headers, action, **kwargs) -> dict:
        """执行成长任务动作：``accept``（接单）/ ``claim``（领奖）。"""
        if not self.growth_task_action_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明成长任务动作")
        return self._module_fn(self.growth_task_action_fetcher, "成长任务动作")(
            manager, client, headers, action, **kwargs)

    def fetch_travel(self, manager, client, headers, action, **kwargs) -> dict:
        """旅行动作：``status``/``depart``/``claim``。"""
        if not self.travel_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明旅行接口")
        return self._module_fn(self.travel_fetcher, "旅行")(manager, client, headers, action, **kwargs)

    def fetch_activity_report(self, manager, client, headers) -> dict:
        """活跃上报（点亮连登、解锁领养任务的前置）。"""
        if not self.activity_report_fetcher:
            raise NotImplementedError(f"{type(self).__name__} 未声明活跃上报接口")
        return self._module_fn(self.activity_report_fetcher, "活跃上报")(manager, client, headers)

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
    #: 给**属性名**而非异常类/转发方法：那两个写法都必须在模块顶部 import
    #: 各后端模块，破坏 registry 的懒构造。类名 → 运行时按子类模块取，
    #: 既不必急切 import，也不必给每个后端在基类写转发方法。
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
        所以约定由**有签到能力的后端**自己实现（pool 只调
        ``spec.interpret_checkin_claim(...)``）。
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

# 成长任务（任务 / 旅行 / 活跃 / 保活）

上游 WorkBuddy 有一整套「积分养成」：每日成长任务、猫咪旅行、连签抽奖、活跃上报。
参考实现有三个来源，本项目的接口契约是从它们逆向出来的：

- `linguo2625469/workbuddy2api-panel`（Go，接口与错误码最全）
- `88lin/workbuddy-auto-signin`（Python，`run_growth` 的完整编排顺序最可信）
- `ithtelab/workbuddy-manager`（控制台，**没有**成长任务实现，仅作 UI 参考）

## 一、接口契约（最容易记错的部分）

| 用途 | 方法与路径 | 域 | 要点 |
|---|---|---|---|
| 任务列表 | `GET /activity/growth/tasks` | copilot | **无 code 信封**，成功就是 200 + JSON |
| 接单 | `POST /activity/growth/tasks/accept` | copilot | body `{"task_codes": [...]}` —— **复数数组**，单数一律 400 |
| 领奖 | `POST /activity/growth/tasks/{code}/claim` | **www.workbuddy.cn** | **路径带 code、body 空**；缺 `x-client-platform: web` 与 growth-center 的 Referer → `task not found` |
| 旅行状态 | `GET /activity/growth/buddy/travel/status` | copilot | `state` ∈ idle / traveling / arrived；`daily_limit_reached` 服务端明示 |
| 旅行目的地 | `GET /activity/growth/buddy/travel/config` | copilot | `locations[]` |
| 出发 | `POST /activity/growth/buddy/travel/depart` | copilot | body `{"location_id": N}` |
| 领旅行奖励 | `POST /activity/growth/buddy/travel/claim` | copilot | body `{"record_id": N}` → `reward_credit` |
| 活跃上报 | `POST /v2/report` | **www.codebuddy.cn** | body 是**数组**；`userId` 必填（缺了上游 200 静默丢弃） |

**前缀不一致**：两家参考项目对 travel 路径的 `/v2` 前缀不一致。本项目先试不带 `/v2` 的，
**仅在 404 时**再试带 `/v2` 的。

**任务状态机**：`accept_status` ∈ not_accepted / accepted / in_progress / completed / claimed。
接单与领奖是两步：先接单才能推进到可领奖。

**实发值优先**：任务列表里的 `reward_credit` 只是活动配置值，改版会与实发对不上 ——
前端优先用领奖响应里的 `credit`。

**风控口径**：活跃上报每号每天 1 次即可（频控按密度判罚），一条上报同时点亮连登
并解锁领养（first_buddy）任务。

## 二、后端抽象

成长任务沿用既有的「基类声明 + 后端自己实现」范式（`core/backends/base.py`）：

| 声明 | 签名 | 作用 |
|---|---|---|
| `growth_tasks_fetcher` | `f(manager, client, headers) -> list[dict]` | 任务列表（行已归一化） |
| `growth_task_action_fetcher` | `f(manager, client, headers, action, **kw) -> dict` | `accept` / `claim` |
| `travel_fetcher` | `f(manager, client, headers, action, **kw) -> dict` | `status` / `depart` / `claim` |
| `activity_report_fetcher` | `f(manager, client, headers) -> dict` | 活跃上报 |
| `supports_growth` | bool | 该后端是否参与**自动**执行（定时任务） |

声明是**模块级函数名**，运行时用 `sys.modules.get(type(self).__module__)` 现取，
不顶层 import 后端模块（保 registry 的懒构造）。

只有 WorkBuddy 实现了这套（`core/backends/workbuddy.py`）；Trae 没有，
调用时抛 `NotImplementedError`，pool 把它翻成「该后端暂不支持成长任务」。

## 三、admin 层

`AccountPool.growth(aid, action, **params)` 是统一入口，action ∈
tasks / accept / claim / travel_status / travel_depart / travel_claim / activity。
异常翻法与 `operate` 一致（`BackendUsageError` → `BillingError`，401/403 → 冷却 300s）。

接口：

- `GET /admin/api/growth/tasks?account_id=`
- `GET /admin/api/growth/travel?account_id=`
- `POST /admin/api/growth/actions` body `{account_id, action, task_codes?, task_code?, location_id?, record_id?}`

## 四、前端

侧边栏新增「成长任务」页（`#page-growth`）：每账号一行，显示**签到 / 保活 / 活跃**
三项本地状态 + 操作按钮（签到 / 保活 / 报活跃 / 看任务）；点账号行打开
`#growth-dialog` 看该账号的上游任务列表（接单 / 领奖）与旅行状态（出发 / 领奖）。

## 五、自动执行

默认**关闭**（`growth_travel_hours` / `growth_activity_hours` 为空串）——这些动作会真打
上游并领积分，替用户自动做主不合适。开启后：

- 旅行：按配置小时跑一次「状态 → 已到达就领奖 / 闲着就出发」循环，每号每天一次
  （`travel_date` 去重）。领奖优先于出发：未领的奖励不该被下一次出发覆盖。
- 活跃：按配置小时上报一次，每号每天一次（`activity_date` 去重）。排在旅行之前 ——
  领养任务依赖它。

时点配置形如 `9,21`（小时列表，逗号分隔，北京时间），存在 `store.data["pool"]`。

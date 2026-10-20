# 用量统计与展示 Spec：限额文案、token 历史、上游用量报表

配套 `specs/db-redesign.md`（v2 库结构）。本文只描述**在其之上新增**的部分。

## 背景

1. 密钥「限额」弹窗复用通用确认弹窗，输入框 label 在 HTML 里写死为「账号备注」
   （`index.html` `#rename-label`），导致设置请求上限时显示"账号备注"，文案错位。
2. `keys.usage` 换天即 `clear()` 归零（`server.py:345-347`），**每日用量没有历史**，
   无法回答"这个密钥上周用了多少"。
3. `upstream_request_usage` 表已存在且后台每账号每天自动同步一次，但**没有任何界面**：
   用户看不到积分消耗，也无从按日/按模型统计。
4. token 数字直接裸显示（`12345 tokens`），长数字不可读。

## 目标

1. 限额弹窗文案准确，说明「次数」口径。
2. token 显示人类友好格式，并按日留存历史（可查近 30 天趋势）。
3. 新增「用量明细」页：默认只读本地库，**点击才打上游**；支持时间过滤、分页（默认 100）、
   防抖；提供按日 / 按账号 / 按模型 / 按客户端的报表。

## 全局约定

沿用 `db-redesign.md`：

- 金额落库 `*_micro`（×1e6 整数），内存与接口输出保持 float。
- 时间一律 `INTEGER` 秒级 unix。
- **日历分桶按北京时间**（与 `request_log_stats` 的 `date(time,'unixepoch','+8 hours')` 一致）。
- 外键约束不建；删 key 后历史靠 `key_name` 快照列仍可读。
- 不存请求/响应正文。

## 一、限额弹窗文案

`confirmAction(title, desc, callback, rename)` 的输入框 label 写死在 HTML。改为可传参：

```js
confirmAction(title, desc, callback, { value, label, inputType = "text", placeholder } = null)
```

`field === null` 时隐藏输入框（现有 `rename === null` 语义不变）。

| 调用点 | 标题 | label | 类型 |
|---|---|---|---|
| 账号备注 | 编辑账号备注 | 账号备注 | text |
| 调度权重 | 设置调度权重 | 调度权重（0–100） | number |
| **密钥限额** | **设置每日请求上限** | **每日请求上限（次）** | number, min=0 |

限额说明文案：

> 该密钥每天最多可发起的请求数。留空或填 0 表示不限制。
> 超限后新请求返回 429，次日北京时间 00:00 自动重置。
> 只统计进入转发的对话接口（`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、
> `/v1/messages/count_tokens`），成功与失败都计入；被限流、被模型白名单拒绝的请求不计入。

## 二、token 友好格式

单一函数，密钥表格与历史表格共用：

| 区间 | 显示 | 示例 |
|---|---|---|
| 0–999 | 原样 | `842` |
| 1_000–999_999 | 一位小数 K | `12.3K` |
| ≥1_000_000 | 一位小数 M | `3.4M` |

精度一律 `toFixed(1)` 并去掉尾随 `.0`。

> **口径提示（必须在界面标注）**：`usage_tokens` 取自流式响应里的 `usage.total_tokens`
> （`pool.py:748-753`，多块取最大值）。上游未回 usage 块、或流中途断开时会少记。
> 它是**估算值**，可看趋势，不可用于对账。

## 三、key_usage_daily（新增表）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| key_id | TEXT | 否 | — | 联合主键 |
| day | TEXT | 否 | — | 北京时间 `YYYY-MM-DD`，联合主键 |
| count | INTEGER | 否 | 0 | 当日累计请求数 |
| tokens | INTEGER | 否 | 0 | 当日累计 tokens（估算） |
| key_name | TEXT | 是 | NULL | 密钥名快照，删 key 后历史仍可读 |

索引：`INDEX(day)`（按日汇总要扫全 key）。

### 落库时机：不新增写入点

`keys.usage` 本身就是**当日累计值**，`flush()` 每 5 秒把整份 keys 写库
（`save_keys`）。在同一事务里顺带 UPSERT 当日快照即可，**语义是覆盖而非累加**：

```sql
INSERT INTO key_usage_daily (key_id, day, count, tokens, key_name) VALUES (?,?,?,?,?)
ON CONFLICT(key_id, day) DO UPDATE SET
  count=MAX(count, excluded.count),
  tokens=MAX(tokens, excluded.tokens),
  key_name=excluded.key_name
```

写的是**当日累计快照**而不是增量，所以冲突时是覆盖而非累加。但覆盖必须用
`MAX` 而不是 `excluded.count` 直接赋值：

`usage` 只在**单进程内存**里单调递增，**进程重启后内存清零**，当日计数从 0
重新累加，此后每次 flush 写回的都是比库里**更小**的值。直接覆盖会把当天历史
改小（实测：库中 5000 → 重启后写 1 → 变成 1，历史永久丢失）。这与一开始
"`usage` 单调递增，后写的值一定 ≥ 先写的值"的假设相反——那个假设只在进程
不重启时成立。

`MAX` 保证只增不减：重启只丢「最后 5 秒未 flush」的增量，不会让历史倒退。
若要跨重启精确，正确做法是从 `request_logs` 回填当天计数，不在本设计范围内。

行的 `day` 取 `usage["date"]`（key 侧换天的那一天），不取「现在」：跨天后首个
请求到来前，旧日期快照会被再写一次，`MAX` 语义下无副作用。

只在 `usage_date` 非空时写，避免为每个 key 每天造空行。

### 裁剪

保留 93 天（与 `request_logs` 一致），启动时按 `day < cutoff` 删除。

### 接口

`GET /admin/api/keys/usage?days=30` →

```json
{"days": ["2026-10-14", "2026-10-13"],
 "keys": [{"id": "abc", "name": "opencode",
           "series": [{"day": "2026-10-14", "count": 42, "tokens": 123456}]}],
 "total": {"count": 328, "tokens": 987654}}
```

界面入口：点击密钥表格的「今日用量」格子 → 弹窗显示该密钥近 30 天明细
（日期 / 次数 / token + 区间合计）。

## 四、用量明细页（新增导航项 usage）

### 页面区块

1. 过滤条：开始时间、结束时间、账号下拉（全部 / 单账号）
2. 操作：`拉取上游`（点击才打上游）、`刷新`（只读本地）
3. 报表卡：区间积分合计、条数、免费/付费拆分
4. 明细表：每页 100 条，列 = 时间 / 账号 / 模型 / 客户端 / 积分 / 用途
5. 分页器：`‹ 上一页 · 第 X/Y 页 · 共 N 条 · 下一页 ›`，每页可选 50/100/200
6. 报表切换：按日 / 按账号 / 按模型 / 按客户端

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/usage` | **只读本地库，不打上游**。参数 `start/end/account_id/limit/offset`，返回 `{rows, total, limit, offset}` |
| POST | `/admin/api/usage/sync` | **点击才打上游**。body `{start, end, account_id?}`，拉完写库，返回 `{synced, failed, message?}` |
| GET | `/admin/api/usage/report` | 报表聚合，参数 `start/end/group_by`，返回分组小计 |

分页默认 `limit=100`（可选 50/100/200，上限 500）。参数全部绑定，不拼 SQL。

### 默认行为

- 打开页面 **不自动打上游**，只读本地库。
- 默认时间范围：**近 30 天**，结束时间 = 当前时刻。
- 默认排序：`request_time DESC`。

### 防抖（三层，缺一不可）

1. **前端**：点击后按钮立即 `disabled` 且文案改「拉取中…」，响应返回后再加 3 秒冷却。
2. **服务端并发锁**：复用 `pool.jobs`（`asyncio.Lock`）。已有任务时直接返回
   `{"synced": 0, "skipped": true, "message": "已有任务进行中"}`，不排队、不报错。
3. **每账号节流**：同一账号 60 秒内不重复打上游（键 = `aid`，值 = 上次时间，进程内字典）。

### 上游拉取改造

现有 `sync_upstream_usage(aid)` 写死 `USAGE_SYNC_DAYS=30` 窗口，且成功后写
`account_status.usage_synced_at`。手动拉取必须**支持任意范围**且**不能写 `usage_synced_at`**：

> 若手动拉一个很短的区间（比如今天上午）就标记「今日已同步」，后台每日自动同步会
> 据此跳过当天，其余时段永远拉不到，**留下永久空洞**（`db-redesign.md` 已把这个坑
> 列为窗口必须 ≥ 同步周期的理由）。

因此拆成两个方法，共用已有的用量拉取（各后端自己实现，见下节）：

- `sync_upstream_usage(aid)`：**后台每日同步**，30 天窗口，写 `usage_synced_at`
  / `usage_sync_error`。行为不变。
- `sync_upstream_usage_range(aid, start, end)`：**手动拉取**，任意范围，只写库 +
  写 `usage_manual_synced_at` 留痕，**不动 `usage_synced_at`**。

  节流时间戳 `_usage_manual_at[aid]` 必须在 `_pull_upstream_usage` **成功之后**
  才写。上游 5xx / 凭据失效恰恰是最需要重试的场景，失败时把 60 秒窗口消耗掉，
  用户只会看到「刚拉取过请稍后重试」而实际上什么都没拉到。防连点由 `jobs` 锁
  与前端冷却承担，节流只负责跨请求去重。

  `usage_manual_synced_at` 必须同时进 `ACCOUNT_STATUS_COLUMNS` 与 `accounts`
  表 DDL（走 v2→v3 迁移补列）。只写内存字典的话它重启即丢，「留痕」名存实亡，
  且**不报错**、只有主动查库才会发现。

### 上游拉取：各后端自己实现（不再只有默认后端）

用量对账接口**不是各后端通用**的：域名、鉴权头、分页参数、返回结构全都不同。
所以实现放在**后端自己的模块**里，由 `BackendSpec` 声明（pool 只读声明、
不认后端名 —— 与 `catalog_method` / `account_ops_backend` 同一手法）：

| 声明 | 含义 |
|---|---|
| `supports_upstream_usage: bool` | 该后端有没有上游用量对账接口（决定是否进同步队列） |
| `usage_fetcher: str` | 本后端模块里的**模块级函数名**（`f(manager, client, start, end) -> list[dict]`） |

`BackendSpec.fetch_usage_rows(manager, client, start, end)` 转发到该函数：
用 `sys.modules.get(type(self).__module__)` 现取，不在基类顶层 import
后端模块（那会破坏 registry 的懒构造）。

归一化行字段与 `upstream_request_usage` 表一一对应：
`request_id / credit / model / client / request_time / agent_purpose / input_trunc`。

| 后端 | 接口 | 分页 | 时间口径 | 行粒度 |
|---|---|---|---|---|
| WorkBuddy | `www.workbuddy.cn/billing/meter/get-user-request-usage` | 驼峰 `pageNum`/`pageSize`，响应 `code==0` 包裹 | `requestTime` 是北京时间无时区字符串，按 CN 解析 | 单次请求 |
| Trae | `api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session` | 下划线 `page_num`/`page_size`，响应**无 code 字段** | `usage_time` 是 unix 秒 | **会话**（会话内再按 `usage_group_details` 分组） |

WorkBuddy 侧的请求参数沿用驼峰（与同域 `get-user-resource` 的
`PageNumber/PageSize` 不同）：

```json
{"startTime": "2026-10-07 00:00:00", "endTime": "2026-10-14 23:59:59",
 "pageNum": 1, "pageSize": 100}
```

按 `data.total` 翻页，上限 200 页。失败一律抛
`core.backends.base.BackendUsageError`（带 status）；pool 把它翻成自己的
`BillingError` —— 对用户可见的文案由 admin 决定，后端只负责说「哪一步坏了、
什么状态码」。

落库仍是 `ON CONFLICT(request_id) DO UPDATE`（credit 可能不是终值，不能
`OR IGNORE`）。**UPSERT 同时更新 `request_time`**：WorkBuddy 侧同一个
requestId 的时间不变（无副作用），Trae 侧同一会话的行要随最新 `usage_time`
落到正确的那一天。

**时区**：WorkBuddy 的 `requestTime` 是 `YYYY-MM-DD HH:MM:SS` 无时区标记，
必须按 CN 解析（漏掉差 8 小时，跨天统计全错）；Trae 的 `usage_time` 是 unix
秒，直接用。

### 报表口径

- 积分展示前 `_from_micro(credit_micro)`。
- 按日分桶：`date(request_time, 'unixepoch', '+8 hours')`。
- **免费（`credit_micro = 0`）与付费分开计**：限免期模型照样记账，混入会低估单价。
- 分组维度限定白名单 `day / account / model / client`，不接受任意列名。

## 五、测试

`tests/admin/test_server.py` 补：

- `/admin/api/usage` 分页（`limit/offset` 与 `total` 一致）、时间过滤、账号过滤、非法参数 400。
- `/admin/api/usage/report` 四种 `group_by` 各自返回分组且积分合计与明细一致；非法维度 400。
- `/admin/api/usage/sync` 并发时被 `jobs` 锁挡下返回 `skipped`，不重复打上游。
- 手动拉取**不写** `usage_synced_at`（后台同步仍会执行）。
- `key_usage_daily` 换天写新行、同天覆盖。

`tests/persistence/test_database.py` 补：

- `key_usage_daily` 往返、裁剪、覆盖语义（后写的更大值生效）。

## 六、验收

- 密钥限额弹窗 label 显示「每日请求上限（次）」，不再是「账号备注」。
- token 以 `12.3K` / `3.4M` 显示；密钥「今日用量」可点开看近 30 天趋势。
- 用量明细页：默认只读本地库且不打上游；点「拉取上游」才请求，重复点击被防抖拦下。
- 分页默认 100 条/页，翻页与总数一致。
- 手动拉取任意短区间后，后台每日同步仍会执行（不被误标记为已同步）。
- 跨天统计按北京时间，与 `request_logs` 的日界线一致。

### 时间参数的时区口径（易错，务必照做）

时间区间有三处解释，必须统一为北京时间，否则报表按日分桶会整体偏一个时区：

1. **服务端 `_query_time`**：字符串（如 `2026-10-14 00:00`）按北京时间 `.replace(tzinfo=CN)`
   解析；整数视为**绝对 unix 秒**。两种输入的语义不同，接口注释里要写死。
2. **前端 `usageUnix`**：`<input type="datetime-local">` 的 value 是**无时区的墙上
   时间**，直接 `new Date(v)` 会按**浏览器本地时区**解析。非 +08:00 的用户会把
   整个区间错开（纽约实测偏 12 小时）。必须显式补 `+08:00` 再解析：
   `new Date(v.length === 16 ? v + ":00+08:00" : v + "+08:00")`。
3. **上游 `requestTime`**：无时区标记，按北京时间解析后再转时间戳
   （`strptime(...).replace(tzinfo=CN).timestamp()`）。注意这不是 `.timestamp()`
   之后再减 8 小时——`.replace(tzinfo=CN)` 已经是「把这个挂钟时间当作北京时间」
   的正确写法，再减就成了双重偏移。

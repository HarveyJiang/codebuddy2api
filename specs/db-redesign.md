# DB 重构 Spec：去掉 kv 表，全量结构化 + 模型表

> **状态：已完成（2026-10）。** 线上库已是 v3，无 `kv` 表。
> 文中提到的 v1/`state.json` 迁移代码（`_v1_to_v2`、`_upgrade_keys`、`_absorb*`、
> `Database(legacy_json=...)`）在确认没有旧库需要升级后**已全部移除**，本文作为
> 设计记录保留。当前迁移表只剩 `(3, _v2_to_v3)`。

## 背景

- 当前 `state.db` 共 4 张表：`schema_meta`、`accounts`、`keys`、`kv`（`admin/db.py:34`）。
- `kv` 存 5 个大 JSON：`active`、`pool`、`model_registry`、`account_status`、`session_bindings`（`admin/db.py:21` `KV_KEYS`）。
- `keys` 表另有 3 个 JSON 列：`usage`、`models`、`accounts`（`admin/db.py:39`）。
- 读放大：读一条要全量解析，统计、关联、过期清理都做不了。
- 写放大：`keys`/`accounts` 由 `Store.save()` 全量 DELETE+INSERT；且 `record_key_usage()` **每个 API 请求**都调一次 `save()`（`admin/server.py:310-323`，`PoolMiddleware` 在 `finally` 里无条件调用，`admin/pool.py:628`），等于每请求全量重写这两张表（`kv` 有脏标记可跳过）。本次要把这个每请求写并入防抖。

## 目标

1. 删除 `kv` 表，`SCHEMA_VERSION` 1 → 2。
2. JSON 只留功能单一、无需统计关联的；其余全部拆成表。
3. 新增模型表、请求日志表、上游用量表。
4. 迁移走版本化 `MIGRATIONS` 列表，后续加列可增量追加。

## 全局约定

- **金额**：落库一律整数最小单位（`*_micro`）。**内存值保持现在的 float** 不变——`store.data` 里的 `remaining` 是 float，`state()` 的 `remaining == 0` 判断依赖它；只在落库层 ×1e6 取整、读库时 ÷1e6 还原。不要顺手把内存也改成 micro，否则比较与除法会静默出错。
- **瞬时值**：一律 `INTEGER` 秒级 unix 时间戳，不用 REAL。
- **日历值**：`TEXT` `YYYY-MM-DD` / `HH:MM`。
- **外键**：不建外键约束，表结构里也不写 `REFERENCES`；关联行删除由业务代码显式清理。理由：`replace_accounts` 是 DELETE+INSERT 全量重写，外键约束与之冲突。`request_logs` / `upstream_request_usage` 的 `account_id`、`key_id` 只是引用，删号后靠快照列仍可读。
- **删除语义**：账号是**硬删**（删行 + 显式清理 `session_bindings` 与各 key 白名单里的 id）；密钥是**软删**（`revoked_at` 置位、保留行不删，审计链不断）。

## 表结构

可空列说明：`是`=允许 NULL，`否`=NOT NULL。

### schema_meta

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| key | TEXT PK | 否 | — | 目前只有 `version` |
| value | TEXT | 否 | — | 版本号，`2`；比较时转 int |

### accounts（账号实体 + 全部运行态字段）

按写频拆表在当前持久化模型下无收益：`Store.save()` 在同一事务里全量重写所有状态表，单独立表省不了 IO，只增加重写次数。故原 `account_status` 全部并回本表。

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | TEXT PK | 否 | — | `token_hex(8)`，16 位 hex |
| name | TEXT | 否 | — | 备注名，最长 60 字符 |
| file | TEXT | 否 | — | `auth/` 下凭据文件名，如 `aid.info`，凭据 JSON 本体不进库 |
| enabled | INTEGER | 否 | 1 | 0/1，禁用后不参与路由 |
| created | INTEGER | 否 | 0 | 秒级时间戳 |
| weight | INTEGER | 否 | 0 | 0-100，权重排序；由 `PATCH /admin/api/accounts/{id}` 的 `weight` 写入（现写 `account_status.weight`，`server.py:700`） |
| remaining_micro | INTEGER | 是 | NULL | 当前剩余额度 |
| packages | INTEGER | 是 | NULL | 积分包个数；`state()` 判 exhausted 用 |
| earliest_expiry | INTEGER | 是 | NULL | 最早到期 unix 秒（原 REAL 改整） |
| earliest_remaining_micro | INTEGER | 是 | NULL | 该到期包剩余额度。**旧字段名 `earliest_expiry_remaining`**，`pool.rows()` 读取侧同步改名（`pool.py:197`） |
| expiring_soon_micro | INTEGER | 是 | NULL | 3 天内到期额度（含当天） |
| expiring_soon_updated | INTEGER | 是 | NULL | 统计时间 |
| credits_updated | INTEGER | 是 | NULL | 余额查询时间 |
| checkin_date | TEXT | 是 | NULL | YYYY-MM-DD，最近一次签到日期 |
| checkin_fails | INTEGER | 否 | 0 | 连续签到失败次数 |
| auto_attempt | INTEGER | 是 | NULL | 上次自动签到尝试时间 |
| checkin_slot | TEXT | 是 | NULL | 上午/中午/下午/晚间 |
| checkin_when | TEXT | 是 | NULL | HH:MM，当天签到时刻 |
| checkin_offset_date | TEXT | 是 | NULL | 槽位归属日期 |
| token_refreshed | INTEGER | 是 | NULL | 凭据刷新时间 |
| auth_invalid | INTEGER | 否 | 0 | 0/1，认证失效标记 |
| refresh_slot | TEXT | 是 | NULL | HH:MM，凭据预刷新时刻 |
| refresh_slot_date | TEXT | 是 | NULL | 槽位归属日期 |
| refresh_window_date | TEXT | 是 | NULL | 当天已处理标记 |
| consecutive_fails | INTEGER | 否 | 0 | 连续请求失败次数，成功清零 |
| cooldown_until | INTEGER | 是 | NULL | 冷却到期时间，指数退避上限 2 小时 |
| request_count | INTEGER | 否 | 0 | 累计请求数 |
| last_request | INTEGER | 是 | NULL | 上次请求时间 |
| last_error | TEXT | 是 | NULL | 上次错误信息 |
| usage_synced_at | INTEGER | 是 | NULL | 上游用量表最后同步成功时间 |
| usage_sync_error | TEXT | 是 | NULL | 上游用量同步错误，同步失败需留痕 |

**不建 `usage_history`，消耗速率功能一并删除**（定稿）：余额变化采样只服务于 `daily_rate`/`days_to_empty`/`expire_unused` 三个展示，本次不要这些功能，故既不建 JSON 列也不建采样表。随之需要从代码删除：

- `AccountPool.consumption()`（`admin/pool.py:163`）整体删除；
- `pool.update()` 里的 `usage_history` 滚动采样（`admin/pool.py:130-139`）；
- `pool.rows()` 的 `daily_rate`/`days_to_empty`/`expire_unused` 三个输出（`admin/pool.py:187, 190-194, 201-203`）；
- 前端 `admin/static/app.js:127` 对应的三段展示文案；
- `tests/test_account_pool.py:290-296` 附近构造 `usage_history` 的用例。

`state()` 的 exhausted 判断只依赖 `packages`/`remaining`/`credits_updated`，不受影响。

**本表同时不出现的字段**：`capacity_micro`、`used_micro`（现在只被 `summarize_packages()` 写、从未被读）、`expiring_today_micro`（全库零引用）。`summarize_packages()` 的返回值相应去掉 `capacity`/`used`。

每请求热字段与冷字段同表，因为它们本来就在同一次全量重写里；真正的高频写入只有 `request_count`/`cooldown_until` 两列的内存更新，5 秒防抖一次落库，不构成瓶颈。

### keys

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | TEXT PK | 否 | — | `token_hex(8)` |
| name | TEXT | 否 | — | 备注名 |
| hash | TEXT | 否 | — | `sha256(明文)`，明文不落盘 |
| hint | TEXT | 否 | — | `前5…后4`，如 `sk-wb-…abcd` |
| created | INTEGER | 否 | 0 | 秒级时间戳 |
| enabled | INTEGER | 否 | 1 | 0/1，临时停用；停用后直接 401，可随时恢复 |
| revoked_at | INTEGER | 是 | NULL | 撤销时间；非空=已撤销，**保留行不删** |
| expires_at | INTEGER | 是 | NULL | NULL=永不过期；到期后按撤销处理 |
| daily_limit | INTEGER | 是 | NULL | NULL=不限 |
| usage_date | TEXT | 是 | NULL | 当天 YYYY-MM-DD，原 `usage.date` 铺平 |
| usage_count | INTEGER | 否 | 0 | 当天请求数 |
| usage_tokens | INTEGER | 否 | 0 | 当天 token 累计 |
| models | TEXT | 否 | '[]' | 允许的模型 JSON 数组，无元素=不限 |
| accounts | TEXT | 否 | '[]' | 允许的账号 JSON 数组，无元素=不限 |
| routing | TEXT | 是 | NULL | 轮转策略 round_robin / random / expiry_first；NULL=**动态跟随** `settings.default_routing`（不是建 key 时的快照） |

**状态是派生值，不存单一 status 列**：`enabled=0` → 停用；`revoked_at` 非空 → 已撤销；`expires_at` 非空且已过 → 已过期。三者语义不同：**停用可恢复，撤销不可逆**（保留 `revoked_at` 作审计）。`check_api`（`admin/server.py:290`）依次判撤销、停用、过期、限额。

原 `usage={date,count,tokens}` 跨天清零（`admin/server.py:297`）改为三列。

`usage_count` 是限额判定的权威来源；`request_logs` 是审计，理论上可能因异常丢行而与之不一致，不做自动对账。**注意**：现在 `record_key_usage()` 每请求 `save()`（`admin/server.py:323`），改造后三列只改内存，随 5 秒防抖落库，删掉那次 `save()`，否则"攒批写日志"的前提不成立。

`accounts` 数组内可能残留已删除的账号 id，删账号时业务代码需遍历所有 key 反序列化剔除并重写；代价可接受（key ≤100），换取白名单不拆关联表。

### settings（单行 id=1）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | INTEGER PK | 否 | 1 | CHECK(id=1)，单行 |
| auto_checkin | INTEGER | 否 | 1 | 0/1 |
| checkin_time | TEXT | 否 | '09:00' | HH:MM |
| affinity_expiry_days | INTEGER | 否 | 3 | 亲和改绑阈值天数 |
| credit_min_interval | INTEGER | 否 | 60 | 秒，积分拉取限流 |
| sync_start | TEXT | 否 | '10:00' | 原 `sync_window[0]` |
| sync_end | TEXT | 否 | '20:00' | 原 `sync_window[1]` |
| batch_spread | INTEGER | 否 | 90 | 秒，批量错峰 |
| default_routing | TEXT | 否 | 'round_robin' | 新建 key 的默认轮转策略；`keys.routing=NULL` 的 key 动态跟随此值 |
| queue_failover | INTEGER | 否 | 0 | v4 加。容量队列自动切换总开关，**默认关闭**（唯一会重放请求的功能） |
| queue_retry | INTEGER | 否 | 3 | v4 加。单次请求最多尝试几次（含首次） |
| queue_cooldown | INTEGER | 否 | 120 | v4 加。命中排队的账号被优先跳过的秒数 |
| queue_hold_ms | INTEGER | 否 | 3000 | v4 加。扣留响应头等首个分片的上限（毫秒） |
| queue_model_fallback | INTEGER | 否 | 1 | v4 加。是否允许降级到同价位模型 |
| queue_fallback_model | TEXT | 否 | 'fast-model' | v4 加。最终兜底的智能路由模型 |
| backup_enabled | INTEGER | 否 | 0 | v5 加。自动备份总开关，**默认关闭**（定时备份持续占磁盘） |
| backup_interval_hours | INTEGER | 否 | 24 | v5 加。自动备份间隔（1-720） |
| backup_keep | INTEGER | 否 | 10 | v5 加。保留份数，超出部分在每次备份后清理（1-100） |
| backup_last_at | INTEGER | 否 | 0 | v5 加。上次成功备份的 unix 秒；定时器判断「该不该跑」的唯一依据 |
| backup_last_name | TEXT | 否 | '' | v5 加。上次成功备份的文件名，用于在列表里标出「最近一次」 |
| models_updated | INTEGER | 否 | 0 | 模型目录更新时间戳，原 `model_registry.updated` |

原 `kv.active` 随 `pool.routing` 一起下沉：全局 `pool.routing` 落成 `default_routing`，每 key 的策略进 `keys.routing`。其余默认值沿用 `AccountPool.POOL_DEFAULTS`（`admin/pool.py:87`）——改造后 `POOL_DEFAULTS` 里**删掉 `routing` 键**（默认值改由本表 `default_routing` 提供），并同步 `Store._load` 的合并逻辑（`server.py:128`）。

前端要跟着改：`admin/static/index.html` 的"请求分配"下拉（现有 `round_robin`/`manual` 两项）改为"新建密钥默认分配"并补 `random`/`expiry_first`；`admin/static/app.js:47,59,98` 读的 `overview.pool.routing` 与 `pool.sync_window` 分别改读 `default_routing` 与 `sync_start`/`sync_end`。

### session_bindings

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| affinity_key | TEXT PK | 否 | — | `request_affinity()` 哈希（`admin/pool.py:506`） |
| account_id | TEXT | 否 | — | 绑定的账号 id |
| expires | INTEGER | 否 | — | 到期时间戳 `now+86400`；加 INDEX(expires) |

过期清理 `DELETE WHERE expires<=now`；4096 上限淘汰 `ORDER BY expires LIMIT 1`。删账号时 `DELETE FROM session_bindings WHERE account_id=?`。

`affinity_key` 由 `request_affinity()` 对 `[authorization, model, 会话标识]` 取 sha256（`pool.py:531`），**已天然按调用方隔离**，不同 key 不会共用同一条绑定，无需再按 key 分表或加列。

### models

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| model_id | TEXT PK | 否 | — | 如 glm-5.2 |
| name | TEXT | 否 | — | 展示名 |
| description | TEXT | 是 | NULL | 只存中文 descriptionZh |
| credits_micro | INTEGER | 是 | NULL | 单价倍率 ×1e6；NULL=标准计费，排序视为 0 |
| max_input_tokens | INTEGER | 是 | NULL | 上下文上限 |
| source | TEXT | 否 | 'builtin' | upstream / builtin |
| available | INTEGER | 否 | 1 | 是否在上游 cli 列表 |
| enabled | INTEGER | 否 | 1 | 管理页是否启用 |
| sort_order | INTEGER | 否 | 0 | 上游顺序，排序 tiebreak |
| promo | TEXT | 是 | NULL | 促销展示文本；见下"促销取舍" |

刷新策略：**UPSERT 不删行**。上游消失的模型标 `available=0` 保留，保证 `request_logs.model_id` / `upstream_request_usage.model` 长期可 join。`enabled` 仅由用户操作改写，刷新时不动。

**`available=0` 的行不进 registry**：`ModelRegistry` 只加载 `available=1` 的行填 `_catalog`/`_available`，`available=0` 只留在表里供日志 join。这样 `snapshot_ids()`（`models.py:254`）自然只含上游当前的模型，`PoolMiddleware`（`pool.py:565`）不会把"上游已下线"误报成"管理页已禁用"。管理页目录与现状一致：消失的模型从列表消失。

展示排序 `ORDER BY enabled DESC, credits_micro, sort_order`，替代 `ModelRegistry.snapshot()` 内排序（`admin/models.py:242`）。

**可用列表查询必须显式带 `available=1 AND enabled=1`**：`enabled_models()`（`models.py:249`）现语义是"available 减 disabled"，改查表后若只写 `enabled=1`，会漏掉 available 条件。注意 `/v1/models`（`converter.py:530`）读的是 product.json/`DEFAULT_MODELS`，不查本表——**保持现状**，可用性的实际拦截以 `PoolMiddleware` + `enabled_models()` 为准，spec 不改这一处。

**促销取舍（定稿，单列 TEXT）**：

- `promo` 只做**展示**：上游 `modelPromotions` 中启用且 `kind=discount` 的活动拍平后的 badge label，多个用 `／` 连接，无则 NULL。
- **折后价与 factor 由内置 `PROMOTIONS` 静态表在读取时计算**，沿用 `active_promo()`（`models.py:358`）的"当前时刻 + valid_from/valid_until + daily 时段 + 取最小 factor"逻辑，不依赖 `promo` 列。
- `/admin/api/overview` 的 `models[].promo` 字段**仍返回对象** `{label, factor, credits}`，由 `snapshot()` 在读取时组装，前端契约不变。
- 代价（已知取舍）：上游新推出的动态促销不会即时反映到折后价，只影响展示文案；面板可能同时看到"促销文案"与"按内置表算出的价格"，**以内置表为准**。要精确跟随就单开 `model_promotions` 表，本次不做。

### request_logs（新增；替代内存 recent deque）

现 `RequestMetrics.recent` 是内存 `deque(maxlen=100)`（`admin/metrics.py:17`），重启清空。

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | INTEGER PK | 否 | — | 自增，不用 AUTOINCREMENT |
| time | INTEGER | 否 | — | 秒级时间戳 |
| source | TEXT | 否 | 'api' | api / test |
| path | TEXT | 否 | — | /v1/chat/completions 等 |
| status | INTEGER | 是 | NULL | **生效**状态码：HTTP 非 2xx 取 HTTP 码，SSE 内带的 error.code（401/402/403/429）优先 |
| outcome | TEXT | 否 | — | success / stream_error / interrupted / http_error；已蕴含成功与否，不再单存 ok 布尔 |
| duration_ms | INTEGER | 否 | — | 耗时毫秒 |
| key_id | TEXT | 是 | NULL | 调用密钥，无关联时 NULL；加 INDEX(key_id, time) |
| account_id | TEXT | 是 | NULL | 路由到的账号，无关联时 NULL；加 INDEX(account_id, time) |
| account_name | TEXT | 是 | NULL | 账号名快照，删号后日志仍可读 |
| model_id | TEXT | 是 | NULL | 请求模型 id；加 INDEX(model_id, time) |
| model_name | TEXT | 是 | NULL | 模型展示名冗余，免 join |
| upstream_request_id | TEXT | 是 | NULL | 上游聊天响应里的 requestId，与 upstream_request_usage 对账键。**是否可用尚未验证**，验证通过前不建索引；不可用则删列 |

只记元数据，不存 prompt/回复/key 明文。

**单一观察点（必须按此实现）**：一行日志只由一个地方写。观察点定在 `MetricsMiddleware`（最外层，`server.py:466`），因为只有它有 `path/source/status/outcome/duration_ms`：

- `PoolMiddleware` 通过 ContextVar 把 `key_id`、`account_id`、`account_name`、`model_id`、`model_name`、SSE 解出的 `error.code` 传给内层请求；`MetricsMiddleware` 在 `finish` 时合并这些字段 + 自己解出的 HTTP/流状态，组装成一行。
- `status` 口径在观察点内统一：先取 HTTP 码，SSE 内 `error.code`（401/402/403/429）优先覆盖。
- 管理页测试请求（`server.py:809`）同时经过两个中间件，但 `PoolMiddleware` **不再单独写日志**，只有带 `source="test"` 的 `MetricsMiddleware` 落一行，避免双写。
- **覆盖范围**：`/v1/messages/count_tokens` 也在 `PoolMiddleware` 拦截路径内（会选账号、调上游，`pool.py:542`），必须补进 `MetricsMiddleware.PATHS`（`metrics.py:7`），否则该接口不产生日志也不参与 status 口径。

**写入策略**：不每请求写库。观察点把行追加到内存批量缓冲，随现有 5 秒防抖（`pool.flush()`，`pool.py:143`）一次性批量 INSERT，避免与单写连接的 RLock（`admin/db.py:52`）和全量状态重写抢锁。**缓冲归 `Store` 持有**（`pool.flush()` 现在只 `save()` store，日志缓冲一并在这里落库，不要另起一套定时器）。裁剪放启动时执行，不放进 `save()` 的 BEGIN IMMEDIATE 事务。

**容量兜底**：保留 3 个月，并设硬上限 200 万行；启动裁剪时 `DELETE FROM request_logs WHERE time < now-93*86400`，若仍超上限再按 `id` 从旧到新删到上限，避免一次全表 DELETE 与请求抢锁。

**接口契约变化**：`/admin/api/overview`（`server.py:566`）现在返回 `metrics.snapshot()["recent"]`，元素含 `ok` 布尔。新结构不再存 `ok`，`recent` 由 `request_logs` 最近 100 行组装，`ok` 以 `outcome == "success"` 回填，保持前端字段不变。

分类统计直接 SQL，不建汇总表：支持按天 / 来源 / 接口 / 状态码 / 结果 / 账号 / 密钥 / 模型分组计数、成功率、`AVG(duration_ms)`。

### upstream_request_usage（新增；上游 get-user-request-usage 对账）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| request_id | TEXT PK | 否 | — | 上游 `requestId`，如 `crb-xxx` |
| account_id | TEXT | 是 | NULL | 拉取账号；不设外键、不级联，删号后保留账单 |
| account_name | TEXT | 是 | NULL | 账号名快照 |
| credit_micro | INTEGER | 否 | 0 | 单次消耗积分 ×1e6，0=免费照样计数 |
| model | TEXT | 是 | NULL | 上游 `model`；加 INDEX(model, request_time) |
| client | TEXT | 是 | NULL | 上游 `client` |
| request_time | INTEGER | 否 | — | `requestTime` 转 unix 秒；加 INDEX(account_id, request_time) |
| agent_purpose | TEXT | 是 | NULL | 上游 `agentPurpose`，自由文本，视为可能含提示词片段，只做展示不参与检索 |

不存 `input / inputTrunc` 全文（隐私，现有 metrics 也不存正文）。

**接口实测格式**（2026-10 验证）：

请求 `POST https://www.workbuddy.cn/billing/meter/get-user-request-usage`，body 驼峰参数——注意与同域 `get-user-resource` 的 `PageNumber/PageSize` 不同，本接口用 `pageNum/pageSize`：

```json
{"startTime": "2026-10-07 00:00:00", "endTime": "2026-10-14 23:59:59", "pageNum": 1, "pageSize": 100}
```

响应外层 `requestId`（如 `05a2eef0-...`）是本次查询的 trace id，**不是**单条用量记录，别与明细里的 `requestId` 混淆：

```json
{"code": 0, "msg": "OK", "requestId": "05a2eef0-...",
 "data": {"total": 827, "data": [{"requestId": "crb-5cbde20a...", "credit": 0,
   "model": "step-5-preview", "client": "WorkBuddy", "requestTime": "2026-10-14 13:59:00",
   "inputTrunc": "...", "input": "...", "agentPurpose": ""}]}}
```

实测观测到的三点，落到设计上：

1. **同一次对话在上游可能对应多条记录。** 同一 `requestTime`、同一 `model`、同一 `input` 有两条不同 `requestId`，其中一条 `client: "WorkBuddy"`、另一条 `client: ""`——即桌面端与经本网关的调用各记一条。`request_id` 主键去重是对的，但不能假设上游条数 = 本地 `request_logs` 条数。
2. **`client: ""` 与官方 UA 的差异（待验证假设）**：初步观察 `client: ""` 对应非官方 UA 的调用，但该字段的生成方式没有证据链（与下面的对账键同类）。修好 converter UA（`core/converter.py:74`）后若该字段变为 `WorkBuddy`，可作为改动生效的信号；**不作为验收依据**。
3. **`credit: 0` 确认存在**，限免期模型（step-5-preview）照样记账入库计数。

同步：**每账号每天拉一次近 30 天增量**（窗口必须 ≥ 同步周期，否则连续断档会留永久空洞；实测 8 天 827 条 ≈ 9 页，单次上限 pageSize 100），按 `data.total` 翻页，**`INSERT … ON CONFLICT(request_id) DO UPDATE`**（credit 可能不是终值，用 `OR IGNORE` 会把首条 pending/0 永久钉死）；保留 3 个月，启动时裁剪。统计按 `account_id / model / date(request_time)` 分组，免费（credit_micro=0）与付费分开计。

**时区**：`requestTime` 是 `YYYY-MM-DD HH:MM:SS` 无时区标记，必须按北京时间 `.replace(tzinfo=CN)` 再转时间戳，照 `pool.py:41` `package_expiry` 的写法。漏掉差 8 小时，跨天统计全错。

**同步留痕**：每次同步写 `accounts.usage_synced_at` / `usage_sync_error`，否则同步挂了、过时几天都没人知道。

**对账键（待验证）**：`request_logs.upstream_request_id` 需从上游聊天响应取 `crb-` 形式的 requestId。已确认的只有用量查询响应的明细里有该 id，聊天响应侧**尚未验证**，两个待查位置：

1. 上游 SSE chunk 的 `id` 字段（`_stream_upstream` 的 `_feed()`，`converter.py:828` 只读了 usage/choices，id 被丢弃）；
2. 上游响应头（`converter.py` 目前完全不读 response headers）。

上线实测前先打一条真实请求探测这两处。若两处都没有，**删除该列**，本地表只做本地统计不做对账；不要用 `account_id+model+±60s` 时间窗猜匹配，同分钟多次同模型请求会错配，对账价值为负。

即使拿到该 id，仍不能假设上游条数 = 本地日志条数（上游还含本网关之外的调用），对账应按 id 关联并单独统计未匹配项。

## 迁移

采用版本化迁移列表，后续加列只追加不重写。**编号约定：每个元素的第一个值是"目标版本"**，即 `(2, _v1_to_v2)` 表示把库升到版本 2：

```python
SCHEMA_VERSION = 5
MIGRATIONS = ((3, _v2_to_v3), (4, _v3_to_v4), (5, _v4_to_v5))   # 追加 (6, _v5_to_v6)
```

启动顺序（顺序不可颠倒）：

1. 读 `schema_meta.version`（**只读，不写**）；读不到 = **全新库**，建表后直接写 `SCHEMA_VERSION`，**不跑任何数据迁移**，跳到第 4 步。
2. 按 `MIGRATIONS` 顺序执行 `目标版本 > 当前版本` 的迁移——**所有待执行迁移放进同一个事务**，全部成功才随第 3 步一起提交。任一失败整体回滚，版本号不变，下次从头重跑。
3. 成功后才 `INSERT OR REPLACE` 写入 `SCHEMA_VERSION`。
4. 若 `accounts` 表为空且存在旧 `state.json`，执行 legacy 导入（`db.py:175`），导入后改名 `.bak`。

**两个必须注意的点**：

- **加 `settings` 列的硬性要求**：这是单行表，新列必须 `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT <值>`。
原因是 `Store._load` 的默认值合并是 `{**POOL_DEFAULTS, **settings}`——只补**字典里缺的键**，
列存在但值为 NULL 时不算缺键，`None` 会一路传到 `int()` 上。所以 `ALTER` 必须带默认值
（`_v3_to_v4` / `_v4_to_v5` 都照此写）。

**不能沿用现有的无条件覆盖写法**：当前 `db.py:63` 每次启动都 `INSERT OR REPLACE ... 'version', SCHEMA_VERSION`，若照搬会把刚执行的迁移立刻标记为已完成，迁移等于没做。
- **不要写"每步单独事务 + 失败只重跑失败的步"**：step1 已提交、step2 失败时版本号仍是旧值，下次启动是从 step1 重跑，不是从 step2。用单事务整体回滚规避，并额外要求每步 DDL 带 `IF NOT EXISTS`、导入用 UPSERT 作双保险。

### v1 → v2 的映射（逐个 key 写死）

| 旧 kv key | 新去处 | 处理 |
|---|---|---|
| `pool` | `settings` 单行 | `sync_window[0]→sync_start`、`sync_window[1]→sync_end`；`auto_checkin`/`checkin_time`/`affinity_expiry_days`/`credit_min_interval`/`batch_spread` 原样；`routing` **丢弃**（`manual` 不再支持，语义由 `settings.default_routing` + `keys.routing` 取代） |
| `model_registry` | `models` + `settings.models_updated` | `catalog{}` 与 `available[]` 合并成行（`name`/`description`/`credits`/`max_input_tokens`/`source`，`sort_order` 取 `available` 里的下标）；`disabled[]` → 对应行 `enabled=0`；`catalog` 里有、`available` 里没有的行 `available=0` 保留；`updated` → `settings.models_updated`；`promotions` 拍平成 `promo` 文本 |
| `account_status` | 并进 `accounts` 行 | 逐 id 写入对应列；`remaining`/`expiring_soon`/`earliest_expiry_remaining` 乘 1e6 取整（后者改名 `earliest_remaining_micro`）；`capacity`/`used`/`usage_history` 丢弃 |
| `active` | 丢弃 | 见"接口与调用改动"的 active 删除清单 |
| `session_bindings` | `session_bindings` 表 | 逐条插入 `affinity_key`/`account_id`/`expires` |

其它：

- `keys` 表：`usage` JSON 拆成 `usage_date`/`usage_count`/`usage_tokens` 三列（`date` 缺失写 NULL，`count`/`tokens` 缺失写 0）；`models`/`accounts` 列原样保留；新增 `enabled=1`、`expires_at=NULL`、`revoked_at=NULL`、`routing=NULL`。
- `keys.routing` 旧库不存在；旧 `pool.routing == "manual"` 的记录按 `default_routing` 处理。
- 字段改名：`earliest_expiry_remaining` → `earliest_remaining_micro`，读取侧 `pool.rows()`（`pool.py:197`）同步改。
- `Database.set_kv/get_kv` 删除，换具名方法：`get_settings/save_settings、load_accounts/save_accounts、load_keys/save_keys、load_bindings/save_bindings、load_models/save_models、append_request_logs、load_upstream_usage/save_upstream_usage`。

## 接口与调用改动

- `Store._load`（`admin/server.py:111`）：从新表重建内存状态，浮点字段从 micro ÷1e6 还原。
- `Store.save`（`admin/server.py:168`）：单事务内分块写 accounts/keys/settings/bindings/models + 批量 `request_logs`，脏标记按块保留。
- **`active` 彻底移除**，需逐处改：`select()` 的 manual 分支（`pool.py:222-223, 232`）、`sync_active()`（`server.py:228`）、`account_rows()` 的 active 徽标（`server.py:250`）、`PATCH /admin/api/accounts/{id}` 的 `{"active": true}` 分支（`server.py:705-712`）、删除/停用时的清理（`server.py:714-715, 734-735`）、`save_browser_account`（`server.py:216-217`）、`_load`（`server.py:122`）、`Store.__init__` 的 `data` 初始值（`server.py:90`）与"全新部署从凭据目录导入"分支（`server.py:104-105`）。管理页"设为当前账号"按钮与徽标一并删除。
- `CONFIG["cred"]` 与 `/health`：去掉全局 active 后，`_cred()` 优先取 `REQUEST_CREDENTIAL`（`converter.py:494`）保证按 key 路由正确，无需改；`sync_active()` 改为把 `CONFIG["cred"]` 设为"任一启用账号"（复用 `any_enabled_manager()`，`server.py:239`），供 `/health`（`converter.py:514`）与管理页测试（`server.py:800`）判断"是否有可用账号"。后台任务沿用 `any_enabled_manager()`。
- **路由按 key**：`pool.select()`（`admin/pool.py:219`）加 `kid` 参数，候选集 = 该 key 的 `accounts` 白名单 ∩ 可用账号（`kid=None` 时=全部可用账号，供测试请求与无 key 路径）。**重试分支同样要传 kid**——`PoolMiddleware` 在凭据失败后重选（`pool.py:584`）调的是 `select(affinity_key)`，漏传会选到不在白名单里的账号。
- **三档策略语义（定死）**：
  - `round_robin`：按 `order_candidates()`（`pool.py:210`，权重降序 + 最早过期升序）排序，用 `self.cursor` 轮转。
  - `random`：在候选集内随机。
  - `expiry_first`：只按最早过期升序（不看权重），轮转。
  - `routing` 为 NULL 取 `settings.default_routing`；1 个账号即指定，多账号按上述策略。
  - `PATCH /admin/api/keys` 加 `routing / accounts / enabled / expires_at` 参数（`enabled` 对应停用，与撤销区分；`expires_at` 允许设 NULL 表示永不过期）。
- 金额读写在落库层统一 ×1e6 / ÷1e6（内存保持 float），`summarize_packages()`（`admin/pool.py:46`）返回值不变，改造在 `save_accounts/load_accounts`。
- `ModelRegistry.export_state/import_state`（`admin/models.py:71`）输入输出形状不变，底层走 `models` 表，只加载 `available=1`；`fetch()` 改为 UPSERT 语义，消失的模型标 `available=0` 保留行；`enabled_models()` 查询条件写全 `WHERE available=1 AND enabled=1`。
- 上游用量同步：每账号每天一次近 30 天，走 `billing()`（`pool.py:280`）同一鉴权头，参数用驼峰 `pageNum/pageSize`；成功后写 `usage_synced_at`、清 `usage_sync_error`，异常写 `usage_sync_error`，不因单账号失败中断其它账号。

## 测试

- `tests/test_db.py`：删除 `set_kv/get_kv` 用例，改为：settings 读写、accounts 全字段往返（金额 ×1e6 精度往返）、keys 三列 + 白名单数组 + 软删/停用/过期往返、binding 过期清理、models UPSERT 不删行、`available=0` 的模型不出现在 `enabled_models()` 且不进 registry、registry（catalog+禁用+promo）往返、v1→v2 迁移（造旧 kv + 旧 keys.usage JSON，断言新表存在且 `kv` 已删除、`account_status` 值全等、`manual` 路由被替换）、**迁移幂等且版本号不被无条件覆盖**（跑两次断言数据不重复、版本号正确）、上游 `requestTime` 无时区按 CN 解析。
- 其余用例（原子性、并发、脏标记、坏库报错、明文不落盘）保留，改调新 API。
- `test_account_pool / test_admin_server / test_model_registry`：行为断言不变，另补：`select(kid)` 白名单与重试分支、三档 routing、`request_logs` 一行一请求（test 请求不双写）、"最后一个有效密钥不可撤销"（已撤销的不计入计数）；删除 `usage_history`/`consumption` 相关用例。
- 前端：`admin/static/*` 的 routing/sync_window/active 字段同步改。

## 验收

- `state.db` 无 `kv` 表。
- 旧库 + 旧 `state.json` 均能一次迁移并重启可读，重复启动幂等，版本号不会被启动流程覆盖；全新库不跑数据迁移。
- 金额字段无浮点，时间字段无 REAL；内存值仍为 float。
- 上游已消失的模型不会对外可用，也不会被误报"已禁用"；软删密钥后最后一个有效密钥仍受保护。
- 每个 API 请求只产生一行 `request_logs`。
- 全量 `pytest` 通过。

# DB 重构 Spec：去掉 kv 表，全量结构化 + 模型表

## 背景

- 当前 `state.db` 共 4 张表：`schema_meta`、`accounts`、`keys`、`kv`（`admin/db.py:34`）。
- `kv` 存 5 个大 JSON：`active`、`pool`、`model_registry`、`account_status`、`session_bindings`（`admin/db.py:21` `KV_KEYS`）。
- `keys` 表另有 3 个 JSON 列：`usage`、`models`、`accounts`（`admin/db.py:39`）。
- 问题：读一条要全量解析，统计、关联、过期清理都做不了；`Store.save()` 全量重写大 blob（`admin/server.py:179`）。

## 目标

1. 删除 `kv` 表，`SCHEMA_VERSION` 1 → 2。
2. JSON 只留功能单一、无需统计关联的；其余全部拆成表。
3. 新增模型表，替代 `model_registry` JSON。
4. 内存 `Store.data` 形状不变，只改 `Database` 落库层 + `Store._load/save`（`admin/server.py:111`）。

## 表结构

可空列说明：`是`=允许 NULL，`否`=NOT NULL。

### schema_meta（保留）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| key | TEXT PK | 否 | — | 目前只有 `version` |
| value | TEXT | 否 | — | 版本号，`2` |

### accounts（合并原 account_status 冷字段：账单/签到/刷新归账号，冷却独立成表）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | TEXT PK | 否 | — | `token_hex(8)`，16 位 hex |
| name | TEXT | 否 | — | 备注名，最长 60 字符 |
| file | TEXT | 否 | — | `auth/` 下凭据文件名，如 `aid.info`，凭据 JSON 本体不进库 |
| enabled | INTEGER | 否 | 1 | 0/1，禁用后不参与路由 |
| created | INTEGER | 否 | 0 | 秒级时间戳 |
| weight | INTEGER | 否 | 0 | 0-100，权重排序 |
| remaining | REAL | 是 | NULL | 当前剩余额度 |
| capacity | REAL | 是 | NULL | 总量 |
| used | REAL | 是 | NULL | 已用 |
| packages | INTEGER | 是 | NULL | 积分包个数 |
| earliest_expiry | REAL | 是 | NULL | 最早到期 unix 时间戳 |
| earliest_remaining | REAL | 是 | NULL | 该到期包剩余额度 |
| expiring_soon | REAL | 是 | NULL | 3 天内到期额度（含当天） |
| expiring_today | REAL | 是 | NULL | 当天到期额度 |
| expiring_soon_updated | INTEGER | 是 | NULL | 统计时间 |
| credits_updated | INTEGER | 是 | NULL | 余额查询时间 |
| checkin_date | TEXT | 是 | NULL | YYYY-MM-DD，今日是否签到 |
| checkin_fails | INTEGER | 否 | 0 | 连续签到失败次数 |
| auto_attempt | REAL | 是 | NULL | 上次自动签到尝试时间 |
| checkin_slot | TEXT | 是 | NULL | 上午/中午/下午/晚间 |
| checkin_when | TEXT | 是 | NULL | HH:MM，当天签到时刻 |
| checkin_offset_date | TEXT | 是 | NULL | 槽位归属日期 |
| token_refreshed | INTEGER | 是 | NULL | 凭据刷新时间 |
| auth_invalid | INTEGER | 是 | NULL | 0/1，认证失效标记 |
| refresh_slot | TEXT | 是 | NULL | HH:MM，凭据预刷新时刻 |
| refresh_slot_date | TEXT | 是 | NULL | 槽位归属日期 |
| refresh_window_date | TEXT | 是 | NULL | 当天已处理标记 |

### keys（3 列还原：models / accounts 保留 JSON 数组，usage 铺平）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | TEXT PK | 否 | — | `token_hex(8)` |
| name | TEXT | 否 | — | 备注名 |
| hash | TEXT | 否 | — | `sha256(明文)`，明文不落盘 |
| hint | TEXT | 否 | — | `前5…后4`，如 `sk-wb-…abcd` |
| created | INTEGER | 否 | 0 | 秒级时间戳 |
| daily_limit | INTEGER | 是 | NULL | NULL=不限；按天计数超限 429 |
| enabled | INTEGER | 否 | 1 | 0/1，禁用后直接 401，不参与鉴权 |
| expires_at | INTEGER | 是 | NULL | NULL=永不过期；秒级时间戳，到期后按撤销处理 |
| models | TEXT | 否 | '[]' | 允许的模型 JSON 数组，无元素=不限；随 key 同生命周期，不独立成表 |
| accounts | TEXT | 否 | '[]' | 允许的账号 JSON 数组，无元素=不限；同上 |
| usage_date | TEXT | 是 | NULL | 当天 YYYY-MM-DD，原 `usage.date` 铺平 |
| usage_count | INTEGER | 否 | 0 | 当天请求数，原 `usage.count` 铺平 |
| usage_tokens | INTEGER | 否 | 0 | 当天 token 累计，原 `usage.tokens` 铺平 |
| routing | TEXT | 否 | 'round_robin' | 轮转策略：round_robin / random / expiry_first；新建时取 settings.default_routing；accounts 为空=全账号轮转，1 个=指定，多余 1 个按策略轮转 |

原 `usage={date,count,tokens}` 跨天清零（`admin/server.py:297`）改为三列读写，不用 JSON。

### settings（单行 id=1，原 pool_settings 改名；原 kv.pool 去掉 routing；active 下沉到 keys）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | INTEGER PK | 否 | 1 | CHECK(id=1)，单行 |
| auto_checkin | INTEGER | 否 | 1 | 0/1 |
| checkin_time | TEXT | 否 | '09:00' | HH:MM |
| affinity_expiry_days | INTEGER | 否 | 3 | 亲和改绑阈值天数 |
| credit_min_interval | INTEGER | 否 | 60 | 秒，积分拉取限流 |
| sync_start | TEXT | 否 | '10:00' | 原 sync_window[0] |
| sync_end | TEXT | 否 | '20:00' | 原 sync_window[1] |
| batch_spread | INTEGER | 否 | 90 | 秒，批量错峰 |
| default_routing | TEXT | 否 | 'round_robin' | 新建 key 的默认轮转策略：round_robin / random / expiry_first |
| models_updated | INTEGER | 否 | 0 | 模型目录更新时间戳，原 registry_meta |

全局不再存路由目标：原 `kv.active` + `pool.routing` 下沉到 `keys` 表按 key 设置（见下）。默认值沿用 `AccountPool.POOL_DEFAULTS`（`admin/pool.py:87`）其余项。

### account_cooldown（冷却功能独立表：每请求写的热字段，原 account_status 热字段）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| account_id | TEXT PK | 否 | — | REFERENCES accounts(id) ON DELETE CASCADE |
| consecutive_fails | INTEGER | 否 | 0 | 连续请求失败次数，成功清零 |
| cooldown_until | REAL | 是 | NULL | 冷却到期时间，指数退避上限 2 小时 |
| request_count | INTEGER | 否 | 0 | 累计请求数 |
| last_request | INTEGER | 是 | NULL | 上次请求时间 |
| last_error | TEXT | 是 | NULL | 上次错误信息 |

写入点：`pool.record()`（`admin/pool.py:259`）只写本表，不碰 accounts；`pool.update()` 写 accounts 冷字段（不再记录 usage_history）。请求路径热表行小，5 秒防抖刷盘。`summarize_packages()`（`admin/pool.py:46`）同步算出 `expiring_today / expiring_soon` 一起入库，展示层直接读两列。

### session_bindings（原 kv.session_bindings）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| affinity_key | TEXT PK | 否 | — | `request_affinity()` 哈希（`admin/pool.py:506`） |
| account_id | TEXT | 否 | — | 绑定的账号 id |
| expires | INTEGER | 否 | — | 到期时间戳，`now+86400`；加 INDEX(expires) |

过期清理改 `DELETE WHERE expires<=now`；4096 上限淘汰改 `ORDER BY expires LIMIT 1`（现 `admin/pool.py:227` 全量遍历）。

### request_logs（新增；替代内存 recent deque）

现 `RequestMetrics.recent` 是内存 `deque(maxlen=100)`（`admin/metrics.py:17`），重启清空。

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| id | INTEGER PK AUTOINCREMENT | 否 | — | 自增 |
| time | INTEGER | 否 | — | 秒级时间戳 |
| source | TEXT | 否 | 'api' | api / test |
| path | TEXT | 否 | — | /v1/chat/completions 等 |
| status | INTEGER | 是 | NULL | HTTP 状态码，未完成为 NULL |
| ok | INTEGER | 否 | 0 | 0/1，完成成功标记 |
| duration_ms | INTEGER | 否 | — | 耗时毫秒 |
| outcome | TEXT | 否 | — | success / stream_error / interrupted / http_error |
| key_id | TEXT | 是 | NULL | 调用密钥，无关联时 NULL |
| account_id | TEXT | 是 | NULL | 路由到的账号，无关联时 NULL |
| model_id | TEXT | 是 | NULL | 请求模型 id，如 glm-5.2；加 INDEX(model_id, time) |
| model_name | TEXT | 是 | NULL | 模型展示名冗余，免 join |

只记元数据，不存 prompt/回复/key 明文。保留 3 个月，按 `time` 裁剪（`DELETE WHERE time < now-93*86400`，每天一次），展示取 `ORDER BY id DESC LIMIT 100`。
分类统计直接 SQL，不建汇总表：`INDEX(time)` + `INDEX(source, path)` + `INDEX(outcome)` + `INDEX(account_id, time)`，支持按天 / 来源 / 接口 / 状态码 / 结果 / 账号分组计数、成功率、`AVG(duration_ms)`。

### models（模型表，新增；合并 catalog + available + disabled）

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| model_id | TEXT PK | 否 | — | 如 glm-5.2 |
| name | TEXT | 否 | — | 展示名 |
| description | TEXT | 是 | NULL | 只存中文 descriptionZh |
| credits | REAL | 是 | NULL | NULL=标准计费，排序视为 0 |
| max_input_tokens | INTEGER | 是 | NULL | 上下文上限 |
| source | TEXT | 否 | 'builtin' | upstream / builtin |
| available | INTEGER | 否 | 1 | 是否在上游 cli 列表 |
| enabled | INTEGER | 否 | 1 | 管理页是否启用（= NOT disabled） |
| sort_order | INTEGER | 否 | 0 | 上游顺序，排序 tiebreak |
| promo | TEXT | 是 | NULL | 促销展示文本，如 夜间折扣；上游 modelPromotions 拍平取 label，有效性在导入时判定，过期置空 |

展示排序 `ORDER BY enabled DESC, credits, sort_order`，替代 `ModelRegistry.snapshot()` 内排序（`admin/models.py:242`）。折后价展示时按内置 `PROMOTIONS` 系数算，`promo` 只做文本展示。

### upstream_request_usage（新增；上游 get-user-request-usage 对账，免费也存）

接口：`POST billing/meter/get-user-request-usage`，参数 `{startTime, endTime, pageNum, pageSize}`，分页取 `data.data[]`，按 `requestId` 去重。`credit=0` 的免费请求同样入库并计入统计/限额。

| 字段名 | 类型 | 可空 | 默认 | 说明 |
|---|---|---|---|---|
| request_id | TEXT PK | 否 | — | 上游 `requestId`，如 `crb-xxx` |
| account_id | TEXT | 否 | — | 拉取账号；REFERENCES accounts(id) ON DELETE CASCADE；加 INDEX(account_id, request_time) |
| credit | REAL | 否 | 0 | 单次消耗积分，0=免费，照样计数 |
| model | TEXT | 是 | NULL | 上游 `model` |
| client | TEXT | 是 | NULL | 上游 `client` |
| request_time | INTEGER | 否 | — | `requestTime` 转 unix 时间戳；保留 3 个月裁剪 |
| agent_purpose | TEXT | 是 | NULL | 上游 `agentPurpose` |

不存 `input / inputTrunc` 全文（隐私，现有 metrics 也不存正文）。同步：每账号每天拉一次近 7 天增量，`INSERT OR IGNORE`；统计按 `account_id / model / date(request_time)` 分组，免费与付费分开计（`credit=0` 单列）。

## 迁移

- 启动建新表；存在旧 `kv` 表时按上表导入 5 个 key 后 `DROP TABLE kv`。
- 旧 `keys.usage` JSON 拆成 `usage_date/usage_count/usage_tokens` 三列，`models/accounts` 列原样保留。
- 旧 `state.json` 导入（`admin/db.py:175`）复用同一导入路径，导入后改名 `.bak` 不变。
- `Database.set_kv/get_kv` 删除，换具名方法：`get_settings/save_settings、load_accounts/save_accounts、load_cooldown/save_cooldown、load_bindings/save_bindings、load_registry/save_registry` 等。

## 接口与调用改动

- `Store._load`（`admin/server.py:111`）：从新表重建 `data.pool/active/account_status/session_bindings/model_registry`，内存形状不变，`pool.py` 直接读写 `store.data` 的代码零改。
- `Store.save`（`admin/server.py:168`）：单事务内分块写新表，脏标记按块保留。
- keys 状态为派生值不另存列：`enabled=0`→已禁用；`expires_at` 非空且已过→已过期；展示层按此判定。`check_api`（`admin/server.py:290`）先判启用再判过期，`PATCH /admin/api/keys` 加 `enabled / expires_at` 参数。
- 路由按 key：`pool.select()`（`admin/pool.py:219`）加 `kid` 参数，候选集=该 key 的 `accounts` 白名单 ∩ 可用账号；`accounts` 1 个即指定，多余 1 个按 `routing` 策略轮转（round_robin 按权重+过期排序轮转，random 随机，expiry_first 优先最早过期）。`PoolMiddleware` 把 `check_api` 返回的 kid 透传给 `select`。`PATCH /admin/api/keys` 加 `routing / accounts` 参数。
- `ModelRegistry.export_state/import_state`（`admin/models.py:71`）输入输出形状不变，底层走 `models` 系列表。

## 测试

- `tests/test_db.py`：删除 `set_kv/get_kv` 用例，改为：settings 读写、accounts 冷字段往返、cooldown 热字段往返、binding 过期清理、registry（catalog+禁用+promo 文本）往返、keys usage 三列往返 + 白名单数组读写、v1→v2 迁移（造旧 kv + 旧 keys.usage JSON，打开后断言新表且 `kv` 已删除）。
- 其余用例（原子性、并发、脏标记、坏库报错、明文不落盘）保留，改调新 API。
- `test_account_pool / test_admin_server / test_model_registry`：行为断言不变。

## 验收

- `state.db` 无 `kv` 表、无 JSON 大 blob 列（`daily_json` 例外）。
- 旧库 + 旧 `state.json` 均能一次迁移并重启可读。
- 全量 `pytest` 通过。

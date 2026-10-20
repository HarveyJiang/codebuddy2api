# 请求日志的错误码分类（status 与 error_code 分列）

用户 2026-10-20 提出的「错误码分类细化」。**注意与
[模型级冷却](model-cooldown.md) 是两件事**：那一篇改的是**冷却判定的粒度**
（模型级 vs 账号级），这一篇改的是**日志的维度**（列与分类）。两篇合起来
才覆盖「单个模型限流不会冷掉整个账号，且日志里能看出来」。

## 一、为什么改

改动前 `request_logs.status` 一列身兼两职：HTTP 状态码与上游业务码挤在一起。
`admin/metrics.py` 会用上游 SSE 里的 `error.code` **覆盖** HTTP 状态码 ——
上游用 200 裹着错误回（`{"error":{"code":11128}}`）时，日志里记的就是 11128，
于是：

- 客户端实际拿到的是 **HTTP 200**，日志里却是 11128 —— 看不���「客户端收到了什么」；
- 本地回的 503（模型在冷却）和上游回的 503 混作一团，处理方式完全不同；
- `outcome` 同时只有四类（success / stream_error / interrupted / http_error），
  「限流 / 鉴权失效 / 额度耗尽 / 上游挂了 / 根本没打上游」全归成「流式错误」
  或「请求失败」。

还有一个更隐蔽的坑：**本地拒绝的请求压根不进 `request_logs`**。
`log_context` 原来是在 `select()` **之后**才建的，池子空 / 模型全冷时它还不
存在，异常直接抛出去 —— 也就是说「今天有多少请求因为模型冷却被拒」这件事，
日志里一个字都查不到。

## 二、列的划分

| 列 | 是什么 | 例子 |
|---|---|---|
| `status` | **HTTP 状态码**（客户端实际收到的那个） | 200 / 401 / 503 |
| `error_code` | **上游业务码原样**（字符串） | `"11128"` / `"USER_LIMIT"` / `"WAITING"` |
| `outcome` | **互斥的分类** | `rate_limited` / `auth_error` / `model_cooling` / … |

`error_code` 用 TEXT 而不是 INTEGER：上游码有数字（429）与文本（`USER_LIMIT`）
两种形态，硬塞进 INTEGER 只能丢掉一半。存量行一律 NULL —— 历史行的业务码
无从得知，**不拿旧的 status 反推**（那会把「HTTP 200」写成「业务码 200」）。

## 三、分类表

`admin/metrics.py` 的 `classify_outcome()` 是唯一权威，前端只做翻译。优先级
从高到低：

| 优先级 | 条件 | outcome |
|---|---|---|
| 1 | 本地拒绝（根本没打上游） | `model_cooling` / `no_account` / `client_error` |
| 2 | 上游排队 | `queue` |
| 3 | WAF / 通道拦截（11128 之类） | `waf_blocked` |
| 4 | 限流 | `rate_limited` |
| 4 | 鉴权 / 权限被拒 | `auth_error` |
| 4 | 额度耗尽 | `quota_exhausted` |
| 4 | 上游 5xx | `upstream_error` |
| 5 | 成功 | `success` |
| 6 | 其他非 2xx | `http_error` |
| 7 | 流内报错 / 未完整结束 | `stream_error` / `interrupted` |

两条容易记反的规则：

- **业务码优先于 HTTP 状态**：上游在 SSE 里报 429 时 HTTP 还是 200，但那**就是**
  限流；归成「流式错误」会让人误判成流断了。
- **业务码也优先于「成功」**：有业务错误码就说明这次请求出错了，哪怕 HTTP 是
  200（上游用 200 裹错误是常态）。

WAF 单列一类的原因：11128 这类拦截换遍全池都会被拦，问题在**出口 IP** 不在
账号；归成「鉴权失败」会让人去查账号凭据，查半天发现凭据没问题。

文本业务码（如 `USER_LIMIT`）**不硬映射**到任何类别 —— 上游的文本码含义各不
相同，硬映射会把「额度超了」误判成「限流」。转不成数字时退回看 HTTP 状态。

## 四、实现要点

- `admin/pool.py` 新增两个结构化异常：`LocalReject`（本地拒绝，携带
  `reason`）与 `ModelCoolingError`（模型全冷，带 `model`）。选号失败不再靠
  **报错文案**区分，改成按类型判断。
- `log_context` 提前到取密钥之前建立，**任何**返回路径都会带上它 ——
  本地拒绝的请求也进日志。
- `log_context` 新增三个标记：`local_reject` / `queued` / `waf`。
- `admin/metrics.py` 的 `_record_request_log()` 是**唯一**写一行日志的地方：
  `status` 只放 HTTP 码，`error_code` 放业务码，`outcome` 由
  `classify_outcome()` 定。
- 迁移：v23 → v24（`request_logs` 加 `error_code` 列）。

## 五、前端

- **不新增列**：表格已经横向溢出（用户 2026-10-17 报过）。业务码挂在状态徽标
  **下面的小字**（`码 11128`），鼠标悬停可看全。
- 悬停提示带全三样：**业务码 + 结果分类 + 来源 IP/UA**。
- 三处日志表（请求日志页 / 账号日志弹框 / 密钥日志弹框）共用同一个
  `statusCell()`，避免三处各写一份、慢慢漂移。

## 六、兼容性

- 老行的 `outcome` 取值（success / stream_error / interrupted / http_error）
  仍在新集合里，统计口径 `outcome='success'` 不变。
- 前端翻译表盖住后端会返回的每一个取值 —— 少一个，那一类在界面上就退化成
  「请求失败」，等于白分类。

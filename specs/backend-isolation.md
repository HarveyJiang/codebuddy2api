# 后端隔离：腾讯（workbuddy）与字节（trae）

两个后端共用**一个账号池**，靠 `accounts.backend` 字段区分。共用本身没问题——
凭据、冷却、排队、限额护栏、成本账本全部按**账号**记，天然隔离。

但共用有个前提：**任何一处漏传 backend，一个后端的请求就会拿到另一个后端的账号。**
凭据格式（毫秒 vs 秒级 `expiresAt`）、上游域名、模型目录全都不兼容，所以这种
「串」不会表现为「用错账号」，而是表现为一个**莫名其妙的 4xx**。

本文档记录必须成立的隔离不变量，以及每条不变量对应的代码位置与回归测试
（`tests/pool/test_backend_isolation.py`）。

---

## 路由：模型名即后端

```
trae/<名>  → TraeBackend
其余（含 auto）→ WorkBuddyBackend
```

`trae/` 前缀是唯一无歧义、对客户端零改动的信号（Codex CLI / Claude Code 改不了
header 或路径）。见 `core/backends/registry.py:backend_name_for`。

模型目录同表存放，Trae 项的 `model_id` 带 `trae/` 前缀，命名空间天然隔离。

---

## 五条必须成立的隔离不变量

### 1. 选号必须带后端过滤

`AccountPool.select(..., backend=...)`。**`backend=None` 表示不过滤**，只留给老调用方
与测试；生产路径的 5 个调用点全部显式传 `backend`：

| 位置 | 调用 |
|---|---|
| `pool.py:1253` | 首次选号 |
| `pool.py:1258` | 凭据准备失败后重试 |
| `pool.py:1296` | 排队切号重试 |
| `pool.py:1312` | 排队切号 + 降级模型后重试 |

### 2. `try_select` 必须原样透传 backend

**最隐蔽的一条**：`select()` 一直有后端过滤，但 `try_select()` 曾经没有透传。
后果是**只在排队重试时串**——首次请求完全正常，所以从表面症状根本看不出来；
一旦命中排队，腾讯请求就会改用字节凭据。

`try_select` 现在强制透传，且本侧候选耗尽时返回 `None`（交由降级模型 / 原样返回
上游排队响应），**不借用对侧账号**。

### 3. WAF gate 按后端分桶

`WafIpGate` 的判定口径是「同一出口 IP 被**不同账号**接连拦」。两个后端用的是完全
不同的凭据体系与上游域名，混在一个桶里会怎样：

> 字节的账号被拦到 IP 级 → gate 判定激活 → **腾讯的轮转也被一起停掉**。

这正是「加了字节不该影响腾讯」的反面。分桶后互不干扰，阈值语义（≥2 个不同账号、
60 秒窗、激活期不续期、解除后清空窗口）完全不变。

`snapshot()` 不指定 backend 时恒定列出 `workbuddy` 与 `trae` 两个桶，好让管理页
拿到**稳定**的结构，而不是「桶时有时无」。

### 4. tick 必须逐账号兜住异常

`AccountPool.tick()` 里每个账号的 `operate()` 调用原先只 `except HTTPException`。
而 `operate` 对 Trae 账号走 `_operate_trae`，会抛 `TraeAPIError` / JSON 解析 / 网络
错误等**任意**异常。

后果：**一个坏掉的字节账号会打断整轮 tick**，排在其后的腾讯账号这轮既不签到也不
刷新。异常还被外层 `pool_worker` 的 `except Exception` 静默吞掉，连日志都没有。

现在逐账号兜住并留痕（`_logger.exception`），其余账号照常推进。同样的问题也存在于
凭据预刷新循环，已一并修掉。

### 4b. 收养凭据失败不得阻断 tick

`pool_worker` 先 `adopt_auth_files()` 再 `tick()`。收养读的是**所有**后端的凭据文件，
一个坏文件抛异常若不单独接住，**tick 根本不会跑**——两个后端的签到与刷新一起停摆。
现在收养单独兜底并记日志，失败不影响本轮 tick。

### 5. 排队降级换模型不得跨后端

`AccountPool.fallback_models()` 从 `registry.snapshot()["models"]` 里挑降级候选，
而这个快照**把两个后端的模型混在一个列表**（trae 条目带 `trae/` 前缀）。
换模型之后 `PoolMiddleware` 仍然用**原 backend** 去 `try_select`：

```python
next_model = self._next_model(pool_cfg, attempt_model, computed_models, backend)
...
picked = self.pool.try_select(affinity_key, kid, tried_accounts,
                              model=attempt_model, backend=backend)   # backend 没变
```

所以一旦不过滤，workbuddy 请求会降级成 `trae/glm-5.3-flash`，然后**拿腾讯账号去要
字节的模型**——上游必然 4xx。这与「选号必须带后端过滤」是同一类风险的另一条出口：
那条不变量管的是**账号**别串，这条管的是**模型名**别串。

`fallback_models` 现在接受 `backend` 并按 `entry["backend"]` 过滤候选，用户配置的
`queue_fallback_model` 也走同一检查（它同样可能被写成另一个后端的模型）。不传
`backend` 时不过滤，保持老调用方的行为。

回归测试：`test_fallback_models_never_crosses_backend`、
`test_fallback_model_setting_is_also_backend_checked`（均验证过「去掉过滤即失败」）。

---

## 已经天然隔离、不需要改的部分

| 机制 | 为什么不受影响 |
|---|---|
| 账号冷却 / `consecutive_fails` | 按 `account_id` 记，两侧各记各的 |
| 排队让位 `queued_until` | 同上 |
| 限额护栏（`admin/guards.py`） | 按账号统计，两侧各算各的额度 |
| 成本账本（`admin/cost_ledger.py`） | 以 `(account, model)` 为键，模型名已含 `trae/` 前缀 |
| 会话亲和绑定 | `affinity_key` 含模型名，Trae 模型都带前缀，实际不会撞 |
| 模型目录 | 同表但 `model_id` 带前缀，命名空间隔离 |

> 亲和绑定目前是**靠模型名巧合隔离**，不是显式按后端。若将来出现不带前缀的字节模型，
> 这条就不再成立。加固时应在 `request_affinity` 里把 backend 一并计入哈希。

---

## 一条不变量之外的环境开关

`CODEBUDDY_ENABLE_TRAE=0` 是 Trae 的 kill switch（模型列表 / 路由 / 目录全链路禁用）。
需要**完全**断开字节时用它，而不是去停用/删除账号——停用账号不影响已经排好的轮转与
亲和绑定。
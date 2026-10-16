# 去掉模型前缀（施工方案，v5 —— 迁移版，定稿）

> 状态：**待确认后开工**。
> 用户 2026-10-16 拍板（最终）：
> 「**那就不清空，修改数据去除前缀，使用联合主键**」

**取代** v1（`api-models-dedup-plan.md`）、v2/v3/v4（`remove-model-prefix-plan.md` 旧版）。

---

## 一、事实

### 1.1 前缀是我们自己造的，上游返回裸名

`core/backends/trae.py` 的 `_TRAE_STATIC_SPECS`：

```python
("kimi-k3",          "Kimi-K3",   200000, 1000000),
("glm-5.3",          "GLM-5.3",   200000, 1000000),
```

`trae/kimi-k3` 是 `admin/models.py:735` 拼出来的：

```python
prefix = spec_for(backend).model_prefix          # "trae/"
out += [prefix + mid for mid in state["available"]]   # ← 凭空造出 trae/kimi-k3
```

### 1.2 已写进库（dev 实测）

```
models 表 model_id 带前缀的行: 25 / 总行数 48
例: ('trae/Doubao-Seed-Code', 'trae', 1)
裸名 'kimi-k3' 在库里: []      ← 不存在
```

### 1.3 一个有利的事实：`backend` 列**早就回填好了**

`_v11_to_v12`（`admin/db.py:214`）已经按前缀把归属写进了 `backend` 列：

```python
db._write.execute("UPDATE models SET backend=? WHERE model_id LIKE ? ...",
                  (name, f"{prefix}%", default))
```

**所以归属数据是现成的、正确的。** 本迁移只需：把 `model_id` 去掉前缀 + 换主键。

---

## 二、迁移设计（v15 → v16）

### 2.0 前置事实：生产还在 v9，会连续跑 v10→v16

**实测**（生产 `workbuddy-oneclick` 容器）：

```
models 列: ['model_id','name','description','credits_micro','max_input_tokens',
            'source','available','enabled','sort_order','promo','synced_at']
schema_meta: [('version', '9')]
```

生产**没有** `backend` / `deleted` / `canonical` 三列（分别由 v12 / v13 / v14 加）。
所以生产发布时会**连续跑 v10 → v16**，本迁移（v16）运行时**前置列一定已存在**
（v12 加了 `backend`，v13 加了 `deleted`）——顺序天然正确，无需特判。

**但要在测试里覆盖这条长链**：用**当前生产库的副本**（v9）跑一遍全链迁移到 v16，
确认不报错、不丢数据。只测 v15→v16 是不够的。

### 2.1 为什么必须换主键

`models.model_id TEXT PRIMARY KEY`。去前缀后两边同名会撞：

| 后端 | 裸名 |
|---|---|
| workbuddy | `glm-5.3` |
| trae | `glm-5.3` ← 同一 key |

**实测两边同名 8 个**：`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`kimi-k3`、
`kimi-k2.6`、`minimax-m3`、`deepseek-v4.1-flash`、`step-5-preview`。

→ 主键改 **`PRIMARY KEY (model_id, backend)`**。

### 2.2 SQLite 改主键 = 重建表

```sql
CREATE TABLE models_new (..., PRIMARY KEY (model_id, backend));
INSERT OR REPLACE INTO models_new SELECT
    CASE WHEN model_id LIKE ? THEN substr(model_id, ?) ELSE model_id END, ...
    FROM models;
DROP TABLE models; ALTER TABLE models_new RENAME TO models;
```

**用户拍板**：「不会有冲突，有就舍弃保留一个」→ 用 `INSERT OR REPLACE`，
**冲突时保留后一条，不报错**。

> 我仍会在迁移里**统计并打日志**（迁移前后行数、被 replace 的条数）。
> 不是为了报错，是为了万一量级异常时能在日志里看见 —— 静默丢几十行数据
> 而没有任何痕迹，是排查不出来的。

**关键点**：

1. **剥前缀读后端声明**（`spec_for(name).model_prefix`），**不写死 `'trae/'`**。
2. **`deleted=1` 的行也一起迁移**（它们为 `request_logs` 的 join 而留）。
3. **幂等**：`LIKE 'trae/%'` 只匹配还带前缀的行，重复跑不会重复剥。

### 2.3 `request_logs.model_id` **一起去前缀**（用户拍板）

用户改定：「日志也一起改」。

**实测生产库有 12 条带前缀记录**（真实客户端曾传 `trae/xxx`，是历史事实）：

```
request_logs.model_id: 总 6419, 带前缀 12
  trae/DeepSeek-V4-Flash-Official
  trae/Doubao-Seed-Code
  trae/glm-5.3-flash
  ...
```

迁移里一并：

```sql
UPDATE request_logs SET model_id = <剥前缀> WHERE model_id LIKE 'trae/%';
```

**一并改的理由**：`idx_logs_model_time` 那条按模型查日志的索引
（用量页「这个模型用过哪些请求」）要对得上 `models` 表。
不去前缀 → 这 12 条成为永久失联的孤儿行。

> 与之相关：`upstream_request_usage.model` 与 `account_usage_daily.model`
> **实测 0 条带前缀**（生产/ dev 都是），但**迁移里仍要一起处理** ——
> 它们与 `request_logs.model_id` 是同一件事（模型名的历史记录），
> 只改一张表会让「同一个模型名在不同表里写法不一致」，以后更乱。
> 同理 `upstream_request_usage` 的 `model` 也一起剥。

### 2.4 日志写入点**不用改**（自然就是裸名）

`admin/pool.py:1398` 的 `model_name = body["model"]` —— 记的是**客户端传来的
原始模型名**。去前缀后客户端传的就是裸名，日志自然记裸名。
`pool.py:1487` 的 `attempt_model` 同理。**无需改写入代码。**

---

## 三、代码改动清单（8 项）

| # | 位置 | 改动 |
|---|---|---|
| 1 | `admin/db.py` | 新增 `_v15_to_v16`（重建表 + 去前缀）；`SCHEMA_VERSION = 16`；建表语句改联合主键 |
| 2 | `admin/db.py:937` `save_models` | `ON CONFLICT(model_id)` → `ON CONFLICT(model_id, backend)` |
| 3 | `admin/models.py:353` | 写库 key 用**裸名**（归属仍写 `backend` 列） |
| 4 | `admin/models.py:735/745` | `enabled_models` / `snapshot_ids` 不拼前缀（**同进同退**，§四坑 1） |
| 5 | `admin/models.py:659/664/682/697` | `snapshot()` 的 `id = bare` |
| 6 | `core/backends/registry.py:238` | `backend_name_for` 归属改读 `models.backend`，不看前缀 |
| 7 | 删前缀基础设施 | `prefix_for` / `strip_prefix` / `backend_name_for_prefix` / `BackendSpec.model_prefix` / `trae.py:1137` / `workbuddy.py:24` / `backend_meta().prefix` / `app.js` 的 `backends[].prefix` 读法 |
| 8 | 删 v1/v2 遗留 | `core/models_dedup.py` + 测试、`converter._dedup_models` / `_bare_is_routable`、`bare_routing_provider.bare_is_routable` / `set_bare_routable` |

> 注意 `admin/db.py:180` 那处 `WHERE model_id NOT LIKE '%/%'`（v8→v9 的
> `synced_at` 回填）是**历史迁移**，**不去修改**（迁移不可重写，见既有约定）。
> 新建库不会走它。

---

## 四、两个必须知道的坑

### 坑 1：`snapshot_ids` 与 `enabled_models` 必须同进同退

`admin/pool.py:1378`：

```python
model not in enabled_models() and model in snapshot_ids()  → 400「已在管理页禁用」
```

只改一个 → **禁用静默失效**（禁用了还能调，且不报错）。同一提交改完。

### 坑 2：`backend_name_for` 的归属判据要换掉

去掉前缀后裸名归谁？三处对齐（都已存在，只需接上）：

- **请求路径**：中间件按策略选（§5），写 `scope["codebuddy.routed_backend"]`；
- **展示路径**：`_owner_backend_for` 走策略第一名（§9.6）；
- **静态兜底**：`pinned_models` → 默认后端。

---

## 五、管理页：按上游分页签（用户拍板）

- 每个可见后端一个页签，**无「全部」页签，不做合并**
- 页签**不写死后端名**（读 `/admin/api/backends`）
- **关闭的上游，页签直接隐藏**（判据 `backends[].enabled`，不是「有没有模型行」）
- 当前页签被关闭时**自动切到可见页签**
- 单后端页签里**不画归属 icon**、**不调 `groupModelsByName`**

---

## 六、不变量（改完必须成立）

1. **`/v1/models` 无 `/`**；同名模型按后端各一条（`glm-5.3` 两条，`backend` 不同）。
2. **禁用仍然有效**（§四坑 1）。
3. **裸名路由正确**：`glm-5.3` 按策略落 `workbuddy`；调序后落 `trae`。
4. **只有一个后端时完整可用**（§5.4）。
5. **Trae 独有模型能用裸名调**（`Doubao-Seed-Code`）。
6. **归属由 `models.backend` 决定**。
7. **8 个同名模型不互相覆盖**（联合主键 + `INSERT OR REPLACE` 语义已定义）。
8. **迁移保数据**：`deleted=1` 的行、自定义模型、历史日志都还在（只剥前缀，不删行）。
9. **迁移幂等**：重复跑不重复剥前缀。
10. **生产长链可迁移**：**v9 的库副本能一路跑到 v16**（§2.0），不报错、不丢数据。
11. **日志与模型表对得上**：`request_logs.model_id` / `upstream_request_usage.model` /
    `account_usage_daily.model` 的模型名与 `models.model_id` 同一套写法（都无前缀）。

---

## 七、施工顺序（每步跑全量测试）

1. **回滚 v1/v2 遗留** → 回到 846 passed / 1 skipped 基线。
2. **迁移 + 表结构**：`_v15_to_v16`（重建表 + 去前缀 + 日志表去前缀）、
   联合主键、`ON CONFLICT(model_id, backend)`。
   **验证两条链**：(a) v15 库副本 → v16；(b) **生产 v9 库副本 → v16**（§2.0）。
   对比迁移前后行数、抽样检查 8 个同名模型的 `backend` 是否正确。
3. **上游去前缀**：写库 key / `enabled_models` / `snapshot_ids` / `snapshot`
   （**一个提交**，§四坑 1）。此步会红一批断言带前缀 id 的老测试 —— **逐条查清**。
4. **registry 去前缀**：归属改读 `backend` 列；删前缀基础设施。
5. **管理页页签**：HTML + CSS + JS + 前端测试。
6. **文档 + arch guard**。

---

## 八、决策记录（用户已拍板）

| # | 问题 | 决定 |
|---|---|---|
| ① | 清空还是迁移？ | **迁移**（保数据） |
| ② | 主键撞了怎么办？ | **保留一个**（`INSERT OR REPLACE`，不报错） |
| ③ | `request_logs.model_id` 去前缀？ | **去**（用户：「日志也一起改」） |
| ④ | `5445701`（去重提交）+ WIP 怎么清？ | revert + 丢弃 WIP |
| ⑤ | 迁移前备份？ | 是，按约定 `sqlite3 .backup` |

**无待确认项，可开工。**

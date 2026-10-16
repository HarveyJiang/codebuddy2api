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

1. **`/v1/models` 无本项目自造的前缀**（准确说法见 §9.3：上游自带的 `/`
   如 `openrouter/free` 是真实模型名，保留）；同名模型按后端各一条
   （`glm-5.3` 两条，`backend` 不同，各报自己的 `owned_by`）。
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

> 六步均已完成，落地记录见 §九（含施工中发现的**两个真 bug** 与已改写的契约）。

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

---

## 九、落地记录（2026-10-16 完成）

六步全部完成，每步一次提交，全量测试每步跑过。最终：**后端 869 passed /
1 skipped，前端 5/5**。

| 步 | 提交 | 内容 |
|---|---|---|
| 1 | `3775544` | 回滚 v1/v2 遗留（revert `5445701`、丢弃 `wip-dedup`、删 `core/models_dedup.py` 与相关 spec/测试） |
| 2 | `739e7cb` | `_v15_to_v16` 迁移 + 联合主键 `(model_id, backend)` |
| 3 | `7557741` | 上游去前缀（写库 key / `enabled_models` / `snapshot_ids` / `snapshot`，一个提交） |
| 4 | `0ef0110` | registry 去前缀（删 `prefix_for` / `strip_prefix` / `backend_name_for_prefix`） |
| 5 | `5290b9c` | 管理页按上游分页签 |
| 6 | （本次） | 文档 + arch guard |

### 9.1 与 §六不变量的对照

| # | 不变量 | 结果 |
|---|---|---|
| 1 | `/v1/models` 无自造 `/` | ✅ 生产库实测 0 条 `trae/`；上游自带的 `/`（`openrouter/free`）保留 —— **见 §9.3 的措辞修正** |
| 2 | 禁用仍然有效 | ✅ 逐行读 `models.backend`；e2e 钉住 |
| 3 | 裸名路由正确 | ✅ `glm-5.3` 按策略落 WB；调序后落 Trae（`test_bare_name_routing_e2e.py`） |
| 4 | 只有一个后端时完整可用 | ✅ 单后端部署测试 + 前端单页签测试 |
| 5 | Trae 独有模型能用裸名调 | ✅ 用 `Doubao-Seed-Code` 做判据（`glm-5.2` 两边都有，会走 WB） |
| 6 | 归属由 `models.backend` 决定 | ✅ `ModelRegistry.load` 读列不猜前缀 |
| 7 | 8 个同名模型不互相覆盖 | ✅ `load_model_rows()` 返回行列表（旧 `load_models()` 按 id 索引**会静默丢行**，已标注） |
| 8 | 迁移保数据 | ✅ 生产 v9 副本 47 → 47 行 |
| 9 | 迁移幂等 | ✅ 重复开库不重复剥 |
| 10 | 生产长链可迁移 | ✅ v9 → v16 无报错 |
| 11 | 日志与模型表同一套写法 | ✅ `request_logs.model_id` 等三表 `UPDATE` 剥前缀 |

### 9.2 施工中发现并修掉的两个真 bug

这两条都不在计划里，是改造过程中暴露出来的。**都被新测试钉住了**。

1. **`load_models()` 按 `model_id` 索引会静默丢行**（`admin/db.py`）。
   联合主键落地后，同名两行互相覆盖 —— 表现只是「模型少了一条」，不报错。
   修法：新增 `load_model_rows()` 返回行列表，`ModelRegistry.load` 改用它；
   老 `load_models()` 保留给先前的调用点并在 docstring 注明会丢行。

2. **后端总闸的提示被模型门盖掉**（`admin/pool.py`，严重）。
   原顺序是先按 `backend_name_for(模型名)` 判「该后端是否被禁用」，再跑模型门。
   去前缀后 `backend_name_for` 对**所有裸名**都返回默认后端（同名模型两边都有，
   静态判不出），于是关掉 Trae 后请求一个 Trae 独有模型会报
   **「模型 X 已在管理页禁用」** —— 把「后端下线」说成「你禁用了某个模型」，
   排障时会跑去模型页找一个从没动过的开关。
   修法：**把归属解析提到模型门之前**。`_resolve_backend_for` 逐后端看候选，
   能以 `NoCandidate` 给出逐条原因（「trae：后端已被管理员禁用」），这才是唯一
   真相源；模型门降级为兜底（裸名路由被总闸关掉 / 无注册表时它仍是必须的）。

   附带一个**行为变更**（正确行为，非回归）：关掉 Trae 后请求 `glm-5.2` 不再 400，
   而是落到 WorkBuddy —— 因为「带前缀 = 用户点名后端」这个语义已随去前缀消失，
   它现在只是一个普通裸名，策略会从候选里剔掉被禁用的 Trae 后选中 WB。

### 9.3 已改写的契约

- **`/v1/models` 同名行的 `owned_by`**（原 §六不变量 1 与 `model-routing-by-policy.md`
  §9.6，用户 2026-10-16 拍板）：原来是「裸名 → 策略第一名的**单一**答案」。
  现在**每条按自己的后端报** —— `glm-5.3` 列出两条，一条 `codebuddy`、一条
  `trae-solo`。理由：`listed_models()` 逐行给出 `(id, backend)`，每条行本来就是
  一个具体后端的模型，报别的后端的名字没有意义。
  旧的单一答案路径保留为**兜底**（`converter._owner_backend_for`），只在拿不到
  行清单时用（admin 未加载 / 独立跑 converter / 单测）。
- **`/v1/models` 里「无 `/`」的措辞**：准确说法是「**无本项目自造的前缀**」。
  上游自带的 `/`（如 `openrouter/free`）是**真实模型名**，必须保留 —— 已实测确认。
- **§8 决策 ① 的 `canonical_for` 默认规则**：原文「不声明则默认 `model_id`
  **剥前缀**即规范名」。去前缀后 `model_id` 本身就是裸名，默认规则退化为
  「不声明即恒等」，不再需要剥前缀这一步。
- **`BackendSpec.model_prefix` 的定位**：**保留但降级**。它被 `admin/db.py` 的
  两个历史迁移正当使用（`_v11_to_v12` 按前缀回填 `backend` 列、`_v15_to_v16`
  剥前缀）。删了迁移就无从知道 `trae/` 这个历史前缀 —— 只能写死字面量，而那正是
  「加后端要改多处」。docstring 已改写为「仅供历史迁移读取，运行期禁止使用」，
  并由 arch guard 拦截。

### 9.4 管理页的新形态

- 页签 = **可见后端**（`backends[].enabled`，即总闸 ∧ 环境逃生门），**不写死
  后端名**（读 `/admin/api/backends`，顺序来自 `order`）；**无「全部」页签**。
- 关掉一个上游 → 它的页签与它的行**一起**消失（而不是显示成空页签）；
  当前页签被关闭时**自动切到**可见页签。
- 单页里整列同一归属 → **不画归属 icon**；状态列是**逐行**开关（不再是
  「一次作用到同名模型全部后端」的批量开关）。
- **删除** `groupModelsByName` / `renderModelGroup` / `modelsInGroup` /
  `modelBaseName` / `backendOrder`。合并展示的理由（同名两行看着像重复）随分页
  一起消失 —— 它们本来就不会同屏了。

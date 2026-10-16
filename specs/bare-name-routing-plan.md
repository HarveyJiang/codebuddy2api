# 裸名路由实现计划（§5 候选集 / 排序 / 降级）

> ⚠️ **2026-10-16 后续：前缀已被彻底去掉**（见 `specs/remove-model-prefix-plan.md`）。
> 本文里的 `owned_by` 契约（§三 之 9、§9.6：「裸名 → 策略第一名的**单一**答案」）
> **已被改写**：同名模型在两上游各有一条，`/v1/models` 里**每条按自己的后端报**
> `owned_by`。旧的单一答案路径降级为兜底（拿不到行清单时用）。
> 本文的候选集/排序/降级设计本身仍然有效。

> 对应 `specs/model-routing-by-policy.md` 的 §5 与 §8 阶段二/三。
> 阶段一（`canonical` 列 + 归并展示）已落地（`732ba98`）。本文是把 §5 落成代码的施工图。
>
> 状态：**施工中**。范围与关键取舍见「二、决策」；不确定项见「六、待确认」。

---

## 一、现状：裸名请求今天怎么走

一次请求的后端是在**中间件里、账号选择之前**定下来的。只有一处：

```
admin/pool.py:1298
    backend = backend_name_for(body.get("model") if isinstance(body, dict) else None)
```

`backend_name_for`（`registry.py:219`）的判定顺序：

1. `pinned_models` 命中 → 该后端（三档 `fast/balanced/deep`）；
2. 命中某后端前缀（`trae/`）→ 该后端；
3. 否则 → **默认后端**（WorkBuddy）。

所以 `glm-5.3`（裸名）今天恒定走 WorkBuddy，即使 Trae 也有同名模型。

**这就是要改的那一行。** 改法：把第 3 步从「无条件默认后端」换成「按策略在候选后端里选第一名」。

## 二、决策（照 §5 与 §9 抄，不重新讨论）

| # | 项 | 定论 | 出处 |
|---|---|---|---|
| 1 | 候选集 | 规范名 == 请求的裸名，且：后端 `is_enabled`、有启用账号、该模型行 `enabled` | §5.1 |
| 2 | 默认策略 | **静态优先级** = 注册表顺序（WB 在前）。用户可用 `settings.model_backend_order` 覆盖 | §5.2 / §9.2 |
| 3 | 单 key 覆盖 | `keys.model_backend_order`（NULL = 跟随全局） | §9.4 |
| 4 | 价格红线 | 候选 `credits_micro` **> 基准** → 出局；`NULL` 视为 +∞；基准 = 排序后第一个可用候选 | §9.3 |
| 5 | 价格红线豁免 | ① 请求带显式前缀；② 用户**显式配过**顺序（全局或单 key 非默认） | §9.3 |
| 6 | 降级 | **恒为 1，不跨后端重试**。第一名走不通 → 走既有同后端降级（换号 → 同价同类换模型） | §5.3 / §9.5 |
| 7 | 单后端前提 | 只有默认后端时，候选集自然只剩它 → 退化为「直接走」，**不写特例分支** | §5.4 |
| 8 | 落点记录 | `request_logs.backend`（v8 已有），不加 `route_trace` | §5.3 |
| 9 | `/v1/models` 的 `owned_by` | 写当前策略第一名 | §9.6 |

**价格红线为什么仍然要做**（§9.3 已诚实说明它今天多为兜底）：静态优先级下第一名恒为
默认后端，便宜候选本来就赢不了。但 (a) 将来上动态策略、(b) 用户手动调序、两种场景
它都会立刻生效。零成本，且能拦住「免费变收费」。

**前置依赖**：`credits_micro`（整数）要一路保留到内存目录（§9.3 末尾）。
今天 `db.load_models` 读出来就 `_from_micro` 成 float `credits` 了。

## 三、实现分解（4 个提交，每个独立可回滚）

### 提交 1：保留 `credits_micro` 到内存目录（无行为变更）

- `admin/db.py: load_models` 额外保留 `credits_micro` 整数（**不 pop**，或另存
  `credits_micro` 键）。`credits`（float）继续存在 —— 显示层不动。
- `admin/models.py: snapshot()` 把 `credits_micro` 带进每条 entry。
- `admin/models.py: persist()` 的 rows 里带上 `credits_micro`（写回精确值，避免
  float → micro 的二次换算漂移）。
- 测试：DB 有的行 → 内存目录的 `credits_micro` == DB 原值；`credits` 仍是 float。

**为什么单独一个提交**：它是纯数据管道改动，行为零变化，但为价格红线提供精确整数。
混在路由提交里会让「路由改动导致的行为差异」与「数据精度修复」纠缠。

### 提交 2：候选集与排序（纯函数 + 声明式，不接线）

在 `core/backends/registry.py`（唯一允许出现「按后端名」逻辑的地方之外）新增：

- `admin/routing.py`（新文件）：
  - `candidates(registry, model, key, ...)` → 有序候选列表 `[(backend, reason)]`；
  - `resolve_bare_backend(model, ...)` → 选中的后端名 + 出局原因（供 400 报错）；
  - 排序：注册表顺序 / `settings.model_backend_order` / `keys.model_backend_order`；
  - 价格红线：比较 `credits_micro`，`NULL` 视为 +∞，带豁免判定。
- **不接线**：本提交只加函数 + 单测，`pool.py` 一行不改。这样能先把规则测透。

测试覆盖（逐条对 §5.1/§5.2/§9.3）：
- 单候选直接选；零候选 → 报错列出每个后端的出局原因；
- 多候选按注册表顺序选第一个；用户调序生效；单 key 覆盖全局；
- 价格红线：贵的一方出局；`NULL` 出局；带前缀豁免；显式配序豁免；
- 单后端前提：只有 WB 时（Trae 总闸关）候选只有 WB，无特例分支。

### 提交 3：接线路由（阶段三，含 kill switch）

- `admin/pool.py:1298` 改为：有前缀 → 原逻辑；裸名 → `resolve_bare_backend(...)`。
- **kill switch**：`settings.bare_name_routing`（默认**关**）。关掉 = 今天的
  「裸名走默认后端」，即整个方案可一键回滚。§8 阶段三的要求。
- `resolve_backend`（`registry.py:250`）同样要认这个选择结果，否则
  `_resolve_backend_or_http` 的凭据闸门会用错后端。
- 一致性：中间件选定的 backend 与 converter 解析的 backend **必须是同一个**。
  做法：中间件把选定后端写进 `scope`（已有 `REQUEST_LOG_CONTEXT` 同款机制），
  converter 读它，不重复算（避免两处算法漂移）。

**风险点**：中间件选后端 → 池选账号 → converter 再解析后端。三处必须一致。
用「scope 传递选定后端」把三处收敛成一处决策。

### 提交 4：`/v1/models` 的 `owned_by` 与文档

- `list_models` 的 `owned_by` 写策略第一名（§9.6）。裸名条目：`owned_by` = 策略
  第一名；带前缀条目：各自后端。
- 更新 `specs/model-routing-by-policy.md`：标注 §5 已实现、补「实现记录」。

## 四、测试与验证策略

- 每提交跑全量（当前 786 passed / 1 skipped）+ 前端 5 条。
- **变异验证**（§8 明确要求）：「选择逻辑被改回硬编码默认后端时测试必须变红」。
  行为测试很可能抓不住，所以：
  - 结构断言：`pool.py` 的裸名分支必须调用路由函数（源码扫描）；
  - 行为断言：构造两个后端都有 `glm-5.3`、Trae 在前 → 必须选 Trae。
    改回「无条件默认后端」→ 这条红。
- 端到端：dev 上两个后端都有 `glm-5.3`，实测裸名请求落点与
  `request_logs.backend` 一致。

## 四之二、接线时发现的两处「文档没写、必须显式处理」的情形

1. **`auto` / 空不得走策略**（§4 表格末行「不变：走默认后端」）。它们**不是目录里的
   模型**，拿去查候选集必然零候选 → 会从「走默认后端」变成 **400**，破坏一切既有客户端
   （`converter` 各端点都 `payload.get("model") or "auto"`）。已在
   `_resolve_backend_for` 里显式短路。这是**唯一**一处为「非候选」写的分支，且它不是
   §5.4 禁止的「只有默认后端就特判」——它特判的是**名字本身不指向模型**。
2. **`has_enabled_account` 在 `Store` 上、不在 `AccountPool` 上**。第一版接线写成了
   `self.pool.has_enabled_account(...)`，测试直接 `AttributeError`。这类错误只会在
   **真实 app 路径**上暴露（纯函数测试碰不到 pool/store 的边界），正是 e2e 那一层
   存在的理由。

## 四之三、为什么 `Decision.candidates` 只放「存活候选」

第一版把**排序后的全部**候选放进 `candidates`，同时把价格的出局原因放进 `rejected`——
于是「贵的出局」在返回值里**看不见**（它既在 candidates 里，又在 rejected 里，自相矛盾）。
改成「`candidates` = 价格红线通过后的存活者」后，`candidates` 与 `rejected` 互为补集，
调用方读一个就够。§5.3 的「降级恒为 1」也据此表达：失败就换账号（同后端），不在这里
预留 fallback 列表。

## 四之四、dev 实测结果（2026-10-16）

两个后端都有 `glm-5.3`（WorkBuddy `790000` micro、Trae `780000` micro），是天然的
验证场景。实测三步：

| 步骤 | `/v1/models` 里裸名 `glm-5.3` 的 `owned_by` | 真实请求的 `request_logs.backend` |
|---|---|---|
| 默认（注册表顺序） | `codebuddy` | `workbuddy` ✅ |
| 设 `model_backend_order=[trae, workbuddy]` | `trae-solo` | `trae` ✅ |
| 清除（回到默认） | `codebuddy` | `workbuddy` ✅ |

展示层与请求落点**始终一致**，且随调序一起变 —— §5 与 §9.6 都按预期工作。

**实测踩到的两个坑**（都不是产品问题，但会浪费下次验证的时间）：

1. **算日志要用 id 递增，不能按「最新一条同模型的行」取**：request_logs 是 5 秒
   防抖落库，按「最新一行」判断会与 flush 时序赛跑、读到**上一次**请求的结果。
   我第一版就这样误报过一次（显示「调序后仍落 workbuddy」，其实那条是调序前的行）。
   正确做法：先记 `MAX(id)`，再取 `id > 该值` 的行。
2. **管理 API 只能从 HTTPS 域名调**：管理 cookie 带 `Secure`（`create_app` 默认），
   从容器内 `http://127.0.0.1:8787` 调会被 401（浏览器语义不回传 Secure cookie）；
   而从容器内走 https 域名又会被 WAF 403（**按 User-Agent 拦** `Python-urllib`）。
   结论：验证脚本从**宿主机**跑、走 https 域名、带一个普通 UA。

## 五、回滚

- 提交 1/2/4：无行为变更，`git revert` 即可。
- 提交 3：`settings.bare_name_routing=0` 一键回到今天行为（不必改代码）。

---

## 六、已确认（用户拍板，2026-10-16）

### 6.1 实现状态

全部四个提交已落地并推送 `dev`：

| 提交 | 内容 |
|---|---|
| `13c5fcf` | 内存目录保留精确 `credits_micro` |
| `f05432a` | 候选集 / 排序 / 价格红线（纯函数 + 22 条单测，未接线） |
| `2ee4f0c` | 接线 + kill switch + 凭据闸门一致性 |
| `658e7ca` | `/v1/models` 裸名 `owned_by` 报策略第一名（§9.6） |
| `f7a8863` | 设置入口（调序 + kill switch）+ v15 迁移 |

**dev 已部署并实测通过**（见 §四之四）。**尚未上生产**。

### 6.2 已知遗留

**（a）单 key 调序的前端 UI 与 DB 列未做**
（`keys.model_backend_order` 列尚未加）：后端解析已支持单 key 覆盖
（`build_order` 的 `key_configured`），但 DB 列与前端 `routing-chip` 入口留待
下一步。这是**用户拍板**的范围（「先做后端 + 全局设置项」）。

**（b）§6.2 的「API 层去重」未做** —— 归并目前**只在管理页前端**做。
实测 dev `/v1/models` 仍返回 42 条（22 带前缀 + 20 裸名），`kimi-k3-1` 与
`trae/kimi-k3` **两条都在**。已做的部分：裸名条目的 `owned_by` 报策略第一名
（§9.6，`658e7ca`）。未做的部分：同一规范名的多后端条目**没有合并成一条**。

  这意味着「用户只看到一个模型」目前只在**管理页**成立，在 **API 客户端侧不成立**
  —— 客户端仍会看到 `kimi-k3-1` 与 `trae/kimi-k3` 两个 id。
  §6.2 的设计是「只返回一条、id 为裸名」，这与保留带前缀条目是两个规则，
  需要一起落地（否则「合并成裸名一条」会给「传裸名」之外的老客户端造成歧义）。

**（c）生产发布未做**：生产仍在 v9 回滚态，缺 v10–v15 迁移。发布前按约定用
  `sqlite3 .backup` 备份。

### 6.3 arch guard 的遗留（文档 §6.2 附注）

`tests/protocol/test_backend_arch_guard.py` 目前只扫**相等/包含比较**与
**带后端名的标识符**，**不扫 `startswith("trae")`**。所以「有人新写一句
`if mid.startswith("trae/")`」不会被守卫拦住（本轮已把现存的三处清掉，但没有
让守卫覆盖这个形态）。补一条 `startswith` 规则是独立的小任务。

### 6.4 已确认的决策（用户拍板，2026-10-16）

1. **跳过阶段二（影子选择）**：直接上阶段三。理由：单测 + dev 实测已足够，
   阶段三自带 kill switch，回滚成本同样低（一个设置项）。
   **代价与替代**：不写 `events` 影子记录；改为在 dev 实测「裸名请求落点 ==
   `request_logs.backend`」，并把 §5.1 的「出局原因」做进 400 报错里。
2. **kill switch 默认开**：照 §8 原文。`settings.bare_name_routing` 默认 `True`；
   关掉即回落到「裸名走默认后端」（今天的行为）。
   > 与 `queue_failover`（默认关）的差异是**有意**的：queue_failover 会**重放整个
   > 请求**，失败代价是重复计费；裸名路由只是「选哪个后端」，选错的最大后果是
   > 走到另一个同样能服务该模型的后端。影响面性质不同，默认值也就不同。
3. **调序 UI 范围**：本轮做**后端解析（已支持全局 + 单 key 的形状）+ 全局设置项**，
   单 key 的前端 `routing-chip` 入口后置。
   - **更正**（2026-10-16 实查）：本节原写「`keys.model_backend_order` 列本轮就加」，
     实际**没加**。`build_order` 的 `key_configured` 参数已就位（解析逻辑齐备），
     但 `keys` 表没有这一列，`_resolve_backend_for` 读它时恒为 None → 当前总是
     跟随全局。这是**已知遗留**（见 §6.2(a)），不是「已完成的中间态」。
     写错的原因：施工时把「接口已留好」当成了「列已加」。

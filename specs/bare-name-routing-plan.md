# 裸名路由实现计划（§5 候选集 / 排序 / 降级）

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

## 五、回滚

- 提交 1/2/4：无行为变更，`git revert` 即可。
- 提交 3：`settings.bare_name_routing=0` 一键回到今天行为（不必改代码）。

---

## 六、已确认（用户拍板，2026-10-16）

1. **跳过阶段二（影子选择）**：直接上阶段三。理由：单测 + dev 实测已足够，
   阶段三自带 kill switch，回滚成本同样低（一个设置项）。
   **代价与替代**：不写 `events` 影子记录；改为在 dev 实测「裸名请求落点 ==
   `request_logs.backend`」，并把 §5.1 的「出局原因」做进 400 报错里。
2. **kill switch 默认开**：照 §8 原文。`settings.bare_name_routing` 默认 `True`；
   关掉即回落到「裸名走默认后端」（今天的行为）。
   > 与 `queue_failover`（默认关）的差异是**有意**的：queue_failover 会**重放整个
   > 请求**，失败代价是重复计费；裸名路由只是「选哪个后端」，选错的最大后果是
   > 走到另一个同样能服务该模型的后端。影响面性质不同，默认值也就不同。
3. **调序 UI 范围**：本轮做**后端解析（全局 + 单 key 都支持）+ 全局设置项**，
   单 key 的前端 `routing-chip` 入口后置。
   - `keys.model_backend_order` 列**本轮就加**（§9.4 的加列迁移），解析逻辑也实现
     （照抄 `_routing_for` 形状），只是前端暂不给编辑入口 —— 留空 = 跟随全局，
     正是默认行为，所以「后端支持但 UI 未做」不是半成品，是一个可用的中间态。

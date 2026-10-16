# 模型归属显式化：行前 icon、三档路由归类、自定义模型多选归属（设计方案，未实现）

> 状态：**方案讨论中**（2026-10-16）。用户需求三条：
> 1. 模型管理页，模型名前面加 icon 区分归属；
> 2. `fast-model` / `balanced-model` / `deep-model` 归类到腾讯；
> 3. 添加自定义模型时**选归属，可多选**（Trae / WorkBuddy）。
> 本文档只做方案设计，**不实现**，待用户确认后再排期。

---

## 1. 先说结论：三件事其实是一件事

今天 `models` 表**没有 `backend` 列**。归属完全由 `model_id` 的前缀推导
（`backend_for_model_id` → `backend_name_for` → 读各后端声明的 `model_prefix`）：

| `model_id` | 归属 | 怎么来的 |
|---|---|---|
| `glm-5.2`、`fast-model` | WorkBuddy（腾讯） | 无前缀 → 默认后端 |
| `trae/kimi-k3` | Trae（字节） | `trae/` 前缀 → Trae |

所以用户提的三条，本质都是**「归属」从隐式（前缀推导）变为显式（看得见、选得到）**：

1. 行前 icon = 把推导出的归属**画出来**（纯展示）；
2. 三档路由归类腾讯 = 把「无前缀即腾讯」这条隐式规则**说出来、钉死**（防未来误判）；
3. 自定义多选归属 = 把「归属」从「ID 写法里藏着」变成「表单里明选」（交互）。

好消息：**都不需要动 `models` 表结构**。前缀推导机制本身是对的（第 3 阶段
刚把前缀收进 `BackendSpec.model_prefix` 声明），缺的只是展示层和输入层。

---

## 2. 现状与约束（动手前必须知道的三条）

### 2.1 模型表已有后端标记，但不在模型名前面

`renderModelGroup`（`app.js`）已经给每条打 `mini-backend` 标记（icon + label），
但位置在**倍率/上下文单元格**里，模型名（`idCell`）前面没有。用户要的
「模型前面加 icon」是把标记挪到/复制到 `idCell` 的 `<strong>` 前面。
纯前端改动，约 10 行，无后端影响。

### 2.2 三档路由模型今天已经是腾讯的，但靠的是「巧合」

`fast-model` / `balanced-model` / `deep-model` 是 WorkBuddy 上游 `/v3/config` 的
智能路由档位（`models.py:410` 注释：「官方固定提供，始终附加」），裸 id 无前缀，
所以 `resolve_backend` 把它们送给默认后端（腾讯）。行为正确，但**没有任何声明
说「这三个属于腾讯」**——靠的是「无前缀 → 默认后端 → 恰好是腾讯」这条三跳巧合。

风险：如果将来默认后端换了（或裸名自动选后端方案落地，见
`specs/model-routing-by-policy.md`），这三个会被当成「无归属裸名」参与自动选择，
可能落到 Trae——而 Trae 上游**根本没有这三个档位**，调过去必 400。

### 2.3 往 Trae 加自定义模型，上游会拒绝（最重要的约束）

`TraeBackend.map_model` 对目录里没有的 id 抛 `UnknownTraeModel` → 请求变 400
（除非开 `CODEBUDDY_TRAE_ALLOW_UNKNOWN_MODELS=1` 放行透传）。
而 `ModelRegistry.add` 的 docstring 也写明：「自定义模型只对默认后端有意义：
其它后端的目录完全由上游决定（用户不该往 Trae 目录里塞一个上游没有的模型——
那个模型调不通）」。

所以「自定义模型归属多选 Trae」**不是免费的**：选了 Trae， mild 情况下用户加的是
「上游菜单没列出但实际可调用」的 id（这正是添加模型的原始用途，见
`model_add` 的 docstring），能调通；选了个上游真没有的 id，就是加一个永远 400
的行。这条必须在 UI 上讲清楚，否则用户会认为是系统 bug。

---

## 3. 目标与非目标

**目标**

1. 模型管理页每行模型名前有归属 icon（单后端组显示一个，多后端组按后端顺序并排）。
2. 三档路由模型显式声明归属腾讯，不依赖「无前缀巧合」；未来任何路由改动不得把它选到别的后端。
3. 添加自定义模型时，归属用多选框（WorkBuddy / Trae / …注册表驱动，自动跟随新后端），
   默认全选当前启用的后端；提交后每个选中的后端各建一行。

**非目标**

- 不给 `models` 表加 `backend` 列。归属继续由 `model_id` 前缀推导——加列是重复存储，
  两处不一致时以哪个为准会成为新的 bug 来源。
- 不做「一个逻辑模型多后端共享一行」。多选 = 多行（`foo` + `trae/foo`），
  每行的倍率/开关/描述各自独立。这与需求 8 的「合并显示 + 批量开关」正交：
  展示层合并，存储层分行。
- 不放开 Trae 自定义模型的可用性校验（2.3 的约束保留，UI 上提示）。

---

## 4. 方案

### 4.1 行前 icon（纯前端，`app.js` + `style.css`）

在 `renderModelGroup` 的 `idCell` 里，`<strong>` 前面加 icon：

- 单后端组：该后端的 `backendIcon`（颜色取 `backendMeta().color`，与账号表一致）。
- 多后端组：按 `backendOrder` 并排多个 icon（复用 `tagFor`，它已经存在，
  只是现在长在倍率单元格里——考虑把它移到 idCell，而不是两处各一份）。

数据源全部来自 `GET /admin/api/backends`（已有），不引入新接口。
图标语义与账号表统一：◈=WorkBuddy（蓝 #2f6fed）、⚡=Trae（紫 #7c4dff）。

细节：`mini-backend` 现有样式是「小 pill」，行前 icon 建议用账号表同款
`account-icon-backend`（纯 icon 无 pill，字号略大），视觉噪音更小。

### 4.2 三档路由归类腾讯（声明化，一处改动）

在 `WorkBuddyBackend` 上加声明（Tele 侧的 spec 文件，不是 Trae）：

```python
# 该后端私有的虚拟模型：不走上游目录、永远归属本后端、
# 不参与裸名自动选择（见 specs/model-routing-by-policy.md）。
pinned_models = ("fast-model", "balanced-model", "deep-model")
```

配套两处读取（都是「读声明不读名字」，延续第 3 阶段传统）：

1. `backend_for_model_id`：命中任一后端 `pinned_models` 的 id，直接返回该后端，
   不走前缀推导（今天结果一样，但从此是声明保证而非巧合）。
2. 裸名自动选择方案（`model-routing-by-policy.md` 的候选集）：`pinned_models`
   里的 id **不参与候选**——它们不是「多后端同名模型」，是单后端私有模型，
   自动选择无权染指。

为什么是声明而不是在 `resolve_backend` 里写死三个名字？
因为「私有虚拟模型」是通用概念：Trae 将来也可能有自己的私有模型
（如 `auto`/`default` 归一到 `glm-5.2` 已有类似语义），声明让每个后端自己说，
调用方零改动。arch guard 现有规则（不得出现 `== "fast-model"` 这类字面量分支）
天然覆盖这条——写错地方测试会红。

### 4.3 自定义模型归属多选（表单 + 前端展开，后端小改）

**交互**：`model-dialog` 的 ID 输入框下方加「归属后端」多选框，
选项来自 `/admin/api/backends`（注册表驱动，加后端自动多一项，不用改前端）。
默认勾选**当前启用的**后端（`enabled` 字段已有）。ID 输入框的占位提示改为
「填裸名即可（如 `deepseek-v4-flash`），归属在下方选择；如需精确控制可直接
带前缀（如 `trae/xxx`），此时归属以后缀…以前缀为准」。

**提交语义**（推荐：前端展开，后端零改动）：

- 选了 N 个后端 → 前端调 N 次现有 `POST /admin/api/models`，
  id 分别为 `foo`（WorkBuddy，无前缀）与 `trae/foo`（Trae，加前缀）。
  前缀拼接规则来自 `backends[].prefix`（已有字段），不是手写 `trae/`。
- 用户只填了带前缀的 id（如 `trae/foo`）→ 视为显式指定，归属多选框自动收敛
  为仅 Trae（禁用其它选项并提示「带前缀的 ID 已指定归属」）。
- 部分成功要如实报告：「WorkBuddy 已保存，Trae 失败：xxx」（已有 toast 机制，
  逐个调天然支持）。

为什么推荐前端展开而不是后端一次建多行？

1. 后端 `ModelRegistry.add(mid)` 的语义已经是「按前缀定后端」，单个 id 单行，
   测试全覆盖；加 `backends=[...]` 参数是新语义、新测试、新文档，不值。
2. 两行本来就是独立条目（倍率/开关各管各），分两次调与一次调效果相同。
3. 失败 Granularity 更细：Trae 侧 400（上游没这个模型）不影响 WorkBuddy 侧落盘。

**Trae 侧不做任何校验**（用户决策，2026-10-16）：和之前完全一样——
不校验、不提示。`ModelRegistry.add` 原本就允许往 Trae 目录加 id，
调不通时上游自然报 400，用户自己负责。加提示是多余噪音。

**与 `model-routing-by-policy.md` 的关系**：多选建出来的多行（如 `foo` +
`trae/foo`），正好就是那个方案里的「同规范名多行」。两个方案互补：
本方案解决「怎么建出来」，那个方案解决「裸名请求时怎么选」。
`canonical` 列（那个方案 6.1 节）将来可以直接从这种多行结构派生，
不需要返工。

---

## 5. 改动清单（预估）

| 文件 | 改动 | 规模 |
|---|---|---|
| `admin/static/app.js` | `idCell` 加行前 icon（复用 `tagFor`/`backendIcon`）；`model-dialog` 提交时按选中后端展开多调 | 小（~40 行） |
| `admin/static/index.html` | `model-dialog` 加归属多选框 + Trae 提示行 | 小（~15 行） |
| `admin/static/style.css` | 行前 icon 样式（复用 `account-icon-backend` 语义） | 极小 |
| `core/backends/workbuddy.py` | `WorkBuddyBackend` 加 `pinned_models` 声明 | 3 行 |
| `core/backends/base.py` | 基类加 `pinned_models = ()` 默认 + 注释约定 | ~10 行 |
| `admin/models.py` 或 `registry.py` | `backend_for_model_id` 先查 `pinned_models` 再走前缀 | ~10 行 |
| `tests/` | pin 声明测试（换后端/改默认后端不影响三档归属）+ arch guard 天然覆盖字面量分支；前端 `pooltable`-风格的结构测试 | 中 |
| `scripts/check_admin_ui.py` | 新增 id（归属多选框）自动纳入检查，无需改脚本 | 0 |

**不动**：`models` 表结构、`POST /admin/api/models` 接口形状、
`ModelRegistry.add` 语义、队列/亲和/账本。

---

## 6. 决策记录（用户已拍板，2026-10-16）

1. **多选的实现形状**：✅ **前端展开调 N 次**。选 N 个后端 = 调 N 次现有
   `POST /admin/api/models`（`foo` + `trae/foo`），后端零改动，部分失败逐个报告。
2. **Trae 侧校验**：✅ **和之前一样，不做任何校验**（不校验、不提示）。
   `ModelRegistry.add` 原本就允许往 Trae 目录加 id，调不通上游自然报 400，
   用户自己负责。
3. **行前 icon 与现有标记的关系**：✅ **搬到模型名前**。`tagFor` 标记从倍率/
   上下文单元格移到 `idCell`，一处标记，行更干净。
4. **排期**：待定。建议本方案先行——它是裸名自动选后端
  （`model-routing-by-policy.md`）的输入（多行结构），先有稳定的多行，
   自动选择才有东西可选。

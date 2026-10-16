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

**非目标（已修订，2026-10-16）**

- ~~不给 `models` 表加 `backend` 列~~ → **加 `backend` 列**（见 §3.1）。
  原「重复存储」的判断是错的：`persist()` 里 backend 是循环变量（写入时已知），
  今天的做法是拼进字符串再反解回来——列是把已知事实存下来，不是重复。
- 不做「一个逻辑模型多后端共享一行」。多选 = 多行（`foo` + `trae/foo`），
  每行的倍率/开关/描述各自独立。这与需求 8 的「合并显示 + 批量开关」正交：
  展示层合并，存储层分行。
- 不放开 Trae 自定义模型的可用性校验（2.3 的约束保留，UI 上提示）。

---

## 3.1 `models.backend` 列设计（用户拍板加列，2026-10-16）

### 为什么加：信息在写入时已知，不该编码进字符串再解析

`ModelRegistry.persist()` 是 `for backend in list(self._state)` 循环——
backend 是循环变量，**写盘时已知**。今天把它拼成 `prefix + bare` 存进
`model_id`，`load()` 再逐行 `backend_for_model_id(mid)` 反解回来。
加列后：写盘直接存列，读盘直接读列，字符串解析只留在**请求路径**
（`resolve_backend` 解析请求里的模型名——线上格式只有字符串，无列可读，
这处解析消不掉，也不需要消）。

权威划分：**列管存下来的行，前缀管线上的请求**，各管一头。

### 列定义

- `backend TEXT NOT NULL DEFAULT 'workbuddy'`（默认后端名；与 `accounts.backend`
  同一口径——那张表也是这么干的，有先例）。
- 写入点恰好两处：
  1. `persist()`：循环变量直接写；
  2. `add(mid)`：用户输入的 id 无归属信息，只能前缀推导（多选 UI 落地后，
     前端传显式 `backend` 参数，见 §4.3 修订）。
- 回填（迁移的一部分）：历史行按前缀推导填——这是**一次性**的，
  与「每次读都推导」有本质区别。回填后列即权威。
  线上当前 v9；本迁移与 `canonical`（路由方案）各自独立，版本号按落地顺序顺延。

### drift 防护（列与前缀不一致时怎么办）

规定：**写入时两者必须一致**（persist 从同一变量出两份；add 推导后同时定 id 与列）。
测试锁两条：

1. 回归测试：构造多后端目录 → `persist` → `load` → 断言每行 `backend` 列值
   等于行归属（不需要经过字符串）；
2. drift 断言：全表 `backend != 前缀推导(model_id)` 的行必须为零。
   这条是结构性的——将来有人改了写入路径漏设列，它先红。
   **例外**：`pinned_models` 声明的私有模型（`fast-model` 等）由 §4.2 的声明
   决定归属，断言对它们比对 `pinned_models` 声明而非前缀推导——
   两者今天结论一致（无前缀 ⇒ 默认后端），但断言必须走同一条权威路径，
   否则将来 Trae 声明了自己的私有模型时断言会误报。

### 不变的东西

- `model_id` **保留前缀**：它是对外路由键（客户端传的、账本 join 的），
  动它等于动客户端与历史账本。列是加法，不是替换。
- `backend_for_model_id` **保留**：请求路径与 `add(mid)` 裸调还要用它。
  但 `load()` / `snapshot()` / `enabled_models()` 这类「读存量行」的路径
  改读列，不再逐行解析。
- `pinned_models` 声明仍要：列解决「行属于谁」，声明解决「路由策略不得碰谁」
  （三档不参与自动选择候选）——两回事，都要。

### 与 `canonical` 列的关系

互补不重叠：`backend` 答「这行属于谁」（每行一个值，写入即定）；
`canonical` 答「这几行是同一个逻辑模型」（跨行关系，路由方案用）。
将来裸名自动选择的候选查询就是
`WHERE canonical = ?`，然后按每行的 `backend` 分组——两个列各司其职。

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
| `admin/models.py` 或 `registry.py` | `backend_for_model_id` 先查 `pinned_models` 再走前缀；`persist`/`load`/`snapshot`/`enabled_models` 改读 `backend` 列不再逐行解析 | ~30 行 |
| `admin/db.py` | `models` 加 `backend` 列 + `MODEL_COLUMNS` + 迁移（回填按前缀推导，一次性） | ~25 行 |
| `tests/` | pin 声明测试（换后端/改默认后端不影响三档归属）+ arch guard 天然覆盖字面量分支；前端 `pooltable`-风格的结构测试 | 中 |
| `scripts/check_admin_ui.py` | 新增 id（归属多选框）自动纳入检查，无需改脚本 | 0 |

**要动**（2026-10-16 修订，原写「不动 `models` 表结构」与 §3.1 加列矛盾）：
`models` 表**加 `backend` 列**（§3.1），迁移只加列 + 按前缀回填。

**不动**：`POST /admin/api/models` 接口形状、`ModelRegistry.add` 语义、
队列/亲和/账本、`/v1/models` 输出顺序。

---

## 6. 模型列表排序规则（用户已拍板，2026-10-16）

管理页模型列表（`ModelRegistry.snapshot()`）的排序键，从今天的
「归属 → 启用 → 倍率」改为四层：

```
1. 来源：custom(0) → builtin(1) → upstream(2)
   自定义永远在最前，内置其次，动态拉取的最后。
2. 归属：注册表顺序（今天是 workbuddy 在前，trae 在后）。
   注意用户原话写的是（trae, workbuddy），经确认为笔误，**保持现状不调换**。
3. 启用：启用的在前，禁用的沉底（与今天一致）。
4. 倍率：`credits` 升序；`None`（按标准计费）视为 0，与免费并列最前（与今天一致）。
   同分用目录原顺序（`available` 下标）做稳定决胜，保持今天的行为。
```

配套决策：

- **适用范围只管理页**：只改 `snapshot()`；`/v1/models`（`enabled_models()`，
  客户端可见顺序）**不动**——不改变客户端行为。
- 前端 `groupModelsByName` 的分组排序（按组内首条的后端顺序 + 组名）不受影响：
  它读的是分组后的首条，分组内顺序变了只影响「同名组拿哪条当代表」，
  而代表取的是 `items[0]`——快照排完序后每组首条即该组最高优先级条目，
  语义自洽，无需改前端。

改动点：`admin/models.py::snapshot` 的内层 `group.sort(key=...)` 与外层
`for backend in known_backends()` 循环——外层循环要改成「按来源分三段」，
或保持外层循环、改成全列表统一 sort（key 含来源与后端顺序）。
回归测试：构造三来源 × 两后端 × 两种启用状态 × 不同倍率的目录，
断言输出顺序；变异验证（把来源键删掉应变红）。

## 7. 决策记录（用户已拍板，2026-10-16）


1. **多选的实现形状**：✅ **前端展开调 N 次**。选 N 个后端 = 调 N 次现有
   `POST /admin/api/models`（`foo` + `trae/foo`），后端零改动，部分失败逐个报告。
2. **Trae 侧校验**：✅ **和之前一样，不做任何校验**（不校验、不提示）。
   `ModelRegistry.add` 原本就允许往 Trae 目录加 id，调不通上游自然报 400，
   用户自己负责。
3. **行前 icon 与现有标记的关系**：✅ **搬到模型名前**。`tagFor` 标记从倍率/
   上下文单元格移到 `idCell`，一处标记，行更干净。
4. **`models.backend` 列**：✅ **加**（用户拍板，2026-10-16）。原「不加列」结论
   已推翻——`persist()` 里 backend 是循环变量、写入时已知，今天的做法是拼进
   字符串再解析回来（stringly typed round-trip）。详见 §3.1。
   `model_id` **保留前缀**（对外路由键 + 账本 join 键，不动）；
   `backend_for_model_id` **保留**（请求路径与 `add(mid)` 还要用）。
5. **与总闸的关系**：账号行/模型行是否按后端启用的隐藏或置灰，
   由 `backend-toggle.md` 统一定，本方案不单独决定（避免两处各定一套）。
6. **排期**：✅ **第二位**（用户拍板顺序，2026-10-16：总闸 → 归属 → 路由）。
   前置依赖 `backend-toggle.md`（总闸先落，本方案沿用它的「行按 `is_enabled` 隐藏」
   惯例，不另定一套）；本方案又是裸名自动选后端（`model-routing-by-policy.md`）
   的输入（`backend` 列 + 多行结构）。

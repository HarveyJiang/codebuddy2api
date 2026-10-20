# 会话聚合头 + 出站指纹剥离（施工方案 v1 —— 已实施）

> 状态：**已实施**（用户 2026-10-19 拍板后按本方案落地）。
> 用户 2026-10-19 拍板：「1,2,3,4 先做，完成后，会话头族和出站指纹剥离收益也高但会碰到请求链路核心（要从入站取 conversation id、要改 system prompt 脱敏策略）先写方案确认后在做。」
> 清单 1（WAF IP 熔断）与 4（退避抖动）本项目早已有（`admin/rotation.py`，commit `7c226f7`），本方案只写清单 5、6。
> **用户对本文第五节四问的答复**：① A 默认开；② B 现在就加；③ sticky 一起做（三档都实现，sticky 行为要生效）；④ 模板句改写 **user 正文也要改写**（规则覆盖所有 role，不只 system）。下面正文已按此修订。

---

## 一、事实

### 1.1 上游认这些头（linguo2625469/workbuddy2api-panel 实测在用）

`internal/upstream/headers.go`：

| 头 | 规则 | 作用 |
|---|---|---|
| `X-Conversation-ID` | 入站透传，空则**不发、不伪造** | 会话标识 |
| `X-Conversation-Request-ID` | **必发**，对话轮级聚合主键 | 一次 user send 聚合成一条 |
| `X-Conversation-Message-ID` | = `X-Request-ID`（消息级，每消息新） | 消息级 |
| `X-Root-Request-ID` | = convReqID | 根请求 |
| `X-Trace-ID` | 入站透传或回落 convReqID | 链路 |
| `X-B3-TraceId` / `X-B3-SpanId` / `X-B3-Sampled` | TraceId 16/32 hex（非法回落 messageID）、SpanId = messageID[:16]、Sampled=1 | 链路 |
| `X-Agent-Purpose` | 默认 `conversation` | **上游用量归因不再出现「网关特征」** |

背景（linguo issue #35）：官方客户端**一次 user send 内的所有 tool call / 重试 / 换号复用同一个 ID**，上游后台按它聚合成一条。我们现在是每次转发都新生成 `X-Request-ID`（uuid7），一条多轮对话在上游那边被拆成 N 条独立请求。

### 1.2 上游对**请求内容**做逐字精确匹配（不是语义审核）

linguo `internal/upstream/sanitize.go`：上游看到下列字面就整单拦截，**改一个字符即可绕过**：

| 特征 | 处理 |
|---|---|
| 裸数字 `11128` | 整单拦截（与上下文无关；相邻 11148 / 11101 / 11115 / 99999 均放行）→ 改成 `11-128`（连字符有效，零宽空格无效——上游会归一化） |
| `x-anthropic-billing-header` | 键值段 + 裸键名两形态，整段剥离 |
| 尾随裸 kv `cc_[a-z0-9_]+=...`（如 `cc_entrypoint=`） | 整段剥离 |
| `You are Claude Code` | 最小改写一词 |
| `Main branch (` | 最小改写（Main → Default） |
| `You are a coding agent running in the Codex CLI` | 最小改写（CLI 后插 tool） |
| `github.com/anthropics/` | 最小改写 |

覆盖盲区（linguo 特别标注）：`tool_calls[].function.arguments`（字符串化 JSON）、`reasoning_content`、镜像字段 `reasoning`。

### 1.3 本项目现状

**上游请求头**（`core/converter.py:284 _build_headers_from`）：

```
Content-Type / Accept / Authorization / X-User-Id / X-Enterprise-Id / X-Tenant-Id /
X-Domain / X-IDE-Type / X-IDE-Name / X-IDE-Version / X-Product / X-Product-Version /
X-Request-ID(uuid7) / User-Agent
```

→ **没有任何会话头被转发**；`X-Agent-Purpose` 也没有。全仓 grep `x-conversation-id` 只有 `admin/pool.py:1400/1405` 两处（账号亲和用，不转发）。

**入站会话标识已有现成来源**（`admin/pool.py:1395 request_affinity`）：

1. 入站头 `x-session-id` / `session_id` / `x-conversation-id`
2. 体字段 `prompt_cache_key` / `conversation_id` / `session_id`
3. 兜底：首条 user 消息
   → 算 hash，用于**同会话固定同账号**（4 个 POST 路径生效）。

**出站改写层**（已有两个，都只管一部分）：

| 模块 | 覆盖 | 开关 |
|---|---|---|
| `core/system_identity.py` `filter_system_identity` | **只** system/developer 消息；行首「you are …」+ 品牌词 → 整行删除；`Main branch (…)` → `Main branch:`；`# Claude Code` 标题 → `# ` | 无条件（tencent_policies） |
| `core/desensitize.py` `desensitize_body` | 敏感词插零宽空格、Codex 运行时块压缩、tool metadata 剥离 | **默认关**（`WORKBUDDY_DESENSITIZE`） |

→ **`Main branch (`、`You are Claude Code`、`You are a coding agent running in the Codex CLI` 在 system 侧已被覆盖**（第一行删除 + 品牌词命中；后者 `_IDENTITY` 含 codex）。
→ **裸数字 `11128` 全仓无处理**（只命中 `admin/rotation.py` 的**响应侧**判定）。
→ `x-anthropic-billing-header`、`cc_entrypoint=`、`github.com/anthropics/` 全仓无处理。
→ `desensitize_body` 用零宽空格 → linguo 实测**对逐字匹配无效**（上游会归一化），与本方案要做的不是一回事。

### 1.4 11128 在本项目是真实故障，不是理论风险

- `core/converter.py:834-836` 注释：「11128 是上游 WAF 对请求内容的指纹识别，换账号无效——线上实测连续换遍全池 8 个账号全部 11128，能救的只有改请求内容」。
- `tests/pool/test_account_pool.py:1073` 注释：「11128 是『通道未授权』类拦截，实测换遍全池账号都被拦——它拦的是（内容）」。
- **可复现的死循环**：用户把 11128 报错贴回对话 → 下一次请求体里出现 `11128` → 整单被拦 → 用户再贴一次报错 → …… 换号无用（这是内容问题），只有改写出站内容能解。

### 1.5 链路拓扑（决定会话 ID 放哪、能不能跨重试复用）

```
AdminMiddleware → PoolMiddleware → MetricsMiddleware → mount("/") → converter.app
```

`admin/pool.py:1865` 每次 attempt 重新调 `self.app(scope, receive, observe)`，**传的是同一个 scope dict 对象** → scope 上放的值跨重试 / 换号稳定。
`admin/server.py:2262/2308` 管理端测试请求走同一条链。
→ **会话 ID 组可以放 `scope["codebuddy.session"]`，converter 端 `request.scope` 读得到，不需要改 pool**；纯 standalone 部署也天然可用（每请求一个 scope）。

---

## 二、设计

### 2.1 分成三件独立的事（可分别开关、分别回滚）

| 编号 | 内容 | 风险 | 成本 |
|---|---|---|---|
| **A** | 出站指纹剥离（清单 6） | 低 | 中 |
| **B** | `X-Agent-Purpose: conversation` | 极低 | 极小 |
| **C** | 会话聚合头族（清单 5） | 中 | 中 |

### 2.2 A：出站指纹剥离

**新文件** `core/outbound_sanitize.py`（core 层，不依赖 admin），导出：

```python
def sanitize_outbound_body(body: dict) -> dict
```

- **纯函数**：同样的入站 body 永远得到同样的出站 body（**保 prompt 缓存前缀稳定**；linguo 的零宽空格方案不满足这条且对上游无效）。
- **深度遍历**这些文本位置：所有 role 的 `content`（string 与 content block list 的 `text`）、`tools[].description`、`tool_calls[].function.arguments`、`reasoning_content`、镜像字段 `reasoning`、tool result 的 content。
- **规则集**（模板句改写 + 三条剥离，用户拍板：user 正文也改写）：
  1. 模板句最小改写（每句只改一词，匹配串**不带结尾标点**，CLI 版以句号收尾、桌面版以逗号接后继内容，两种形态一并覆盖）：
     - `You are Claude Code, Anthropic's official CLI for Claude` → `...official CLI tool for Claude`
     - `Main branch (you will usually use this for PRs)` → `Default branch (you will usually use this for PRs)`
     - `You are a coding agent running in the Codex CLI, a terminal-based coding assistant.` → `...Codex CLI tool, ...`
     - `To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues` → `To provide feedback, ...`
  2. 精确数字串 `11128` → `11-128`（**子串匹配**，不动相邻的 11148 / 11101 / 11115 / 99999）
  3. `x-anthropic-billing-header` 的键值段整段删除；裸键名缩写为 `x-anthropic-billing-hdr`
  4. 尾随裸 kv `cc_[a-z0-9_]+=\S*` → 删除
  5. `github.com/anthropics/` → `github.com/anthropic/`（最小改写，保留可读性）
- **零宽空格穿透**：上游会归一化零宽空格（`core/desensitize.py` 插的 U+200B 对上游逐字匹配无效），所以改写用**允许字符间夹零宽空格**的正则匹配，替换串是干净文本 → 命中片段的零宽空格被清掉。
- **开关**：`WORKBUDDY_OUTBOUND_SANITIZE`，走 `core/runtime_config.py` 的 `raw()`，**默认 true**（用户拍板）。
- **接线点**：4 个端点里 `filter_system_identity(body)` 与脱敏**之后**：
  - `core/converter.py`（chat / responses / anthropic / count_tokens 四处）
  - 与 `filter_system_identity` 一样**只对 `backend.tencent_policies` 生效**（Trae 不碰）。
- **不动的地方**：入站原 body 原样保留（客户端看到的内容不变）；`request_logs` 不存正文，无影响；管理端「用量明细」看到的仍是上游原文。

### 2.3 B：`X-Agent-Purpose`

- 在 `_build_headers_from` 里加 `"X-Agent-Purpose": "conversation"`。
- **只加在 chat 路径**？—— 注意 `_build_headers_from` 的产物被 chat / billing（`www.codebuddy.cn` + `www.workbuddy.cn` 两个 host）/ config 三类共用。`X-Agent-Purpose` 与 host 无关、语义是「这条请求是什么用途」，billing 请求发它也无害但没必要。
  → **决策：加在共享 builder**（billing 请求带上它不影响上游，因为 billing 接口不读这个头），改动最小；若将来发现 billing 接口对多余头敏感再拆。
- **收益**：上游账单的 `agentPurpose` 从空变成 `conversation` → 我们 `upstream_request_usage.agent_purpose` 会有值 → 生产「用量明细」页的「用途」列不再空白，且和上游对账口径更清晰。linguo 注释原话：「上游用量归因不再出现 client/agentPurpose 为空的『网关特征』」。
- **顺带**：`X-IDE-Name/Type/Product` 已经是 `WorkBuddy`（`IDE_TYPE = _PRODUCT_NAME`），与 linguo 一致，**不动**（动了会改上游用量接口的 `client` 字段口径）。

### 2.4 C：会话聚合头族

**新文件** `core/session_headers.py`（core 层）：

```python
def resolve_session_identity(headers, body) -> dict:
    """从入站头/体取会话标识：conversation_id / conversation_request_id（可能为空）。"""

def build_session_headers(*, conversation_id, conversation_request_id, message_id) -> dict:
    """按官方语义派生 X-Conversation-* / X-Root-Request-ID / X-Trace-ID / X-B3-*。"""
```

**取值优先级**（与 `request_affinity` 同源，避免两套身份口径）：

1. `x-conversation-request-id`（入站头）→ `conversation_request_id` ← 最准
2. `x-conversation-id`（入站头）→ `conversation_id`
3. `x-session-id` / `session_id`（入站头）→ `conversation_id`
4. 体字段 `conversation_id` / `session_id` / `prompt_cache_key` → `conversation_id`

**派生规则**（照 linguo）：

| 头 | 值 |
|---|---|
| `X-Conversation-ID` | 有才发（**不伪造**） |
| `X-Conversation-Request-ID` | 入站有就用；否则回落 = 本次 `X-Request-ID` |
| `X-Conversation-Message-ID` | = `X-Request-ID`（消息级，每消息新） |
| `X-Root-Request-ID` | = convReqID |
| `X-Trace-ID` | 入站 `x-trace-id` 优先，否则 convReqID |
| `X-B3-TraceId` | convReqID[:16]（入站 `x-b3-traceid` 优先） |
| `X-B3-SpanId` | messageID[:16] |
| `X-B3-Sampled` | `1` |

**接线点**：4 个端点拿到 `headers = cred.get_headers()` 之后 merge（`core/converter.py:880` / `:1421` / `:1595` / `:1796`），**只对 `backend.tencent_policies` 生效**（Trae 走自己的 `_solo_headers`，不受影响）。
入站头从端点的 `request.headers` 取，体从 `payload` 取。

**关于「一次 user send 聚合」能做到什么程度**（诚实说明）：

linguo 的三个场景里，我们在代理层能独立做到的只有两个：

| 场景 | 我们能否做到 |
|---|---|
| 客户端多轮 tool call 聚合成一条 | ❌ 做不到——HTTP 层是多个独立请求，边界只有客户端知道 → **靠客户端自带 `x-conversation-request-id`**，我们透传 |
| 重试 / 换号聚合成一条 | ✅ 可做——`sticky` 档（用户拍板要做），缓存在 scope 里，池每次 attempt 传同一个 dict |
| 上游侧不再碎成 N 条 | ⚠️ 依赖上一行；只在客户端自带 convReqID 时完全成立 |

### 2.5 三档总开关

`WORKBUDDY_SESSION_HEADERS`：`off`（完全不发会话头）/ `pass`（发会话头，但 `X-Request-ID` 每次尝试都新——保守，不动上游按 UUIDv7 还原时刻的语义）/ `sticky`（**默认**：额外让同一客户端请求内的重试/换号复用同一 `X-Request-ID`）。

用户拍板「sticky 一起做，sticky 行为要生效」→ **默认档位 = `sticky`**。

---

## 三、风险与取舍

### 3.1 `X-Request-ID` 复用（sticky 档）会改现有语义

现状：`X-Request-ID` 每次转发新生成 uuid7（上游按 UUIDv7 时间位还原请求时刻）。
sticky：同一客户端请求内换号/重试复用同一个 → 上游把多次尝试聚合成一条。

- **收益**：上游侧一次客户端请求只留一条记录；我们的 `upstream_request_usage` requestId 与上游对齐。
- **风险**：① 改动已有行为（`tests/protocol/test_uuid7_request_id.py` 有断言要跟着改）；② 上游按 ID 聚合的规则我们只能靠 linguo 的实测推断，没有自己的实测数据；③ 失败尝试与成功尝试同 ID，上游若按「首次出现时间」记账，时间戳会偏早几秒。
- **结论**：**默认开**（用户拍板「sticky 一起做，sticky 行为要生效」）。要退回保守行为把 `WORKBUDDY_SESSION_HEADERS` 设成 `pass` 即可。

### 3.2 模板句改写：做，且覆盖 user 正文

原方案打算不做模板句改写（只改 system 侧），用户拍板改为**做，并覆盖 user 正文**。

- 改写是**最小改写一词**，语义不变（`official CLI` → `official CLI tool`、`Main` → `Default`、`CLI` → `CLI tool`、`give` → `provide`）。
- 与 system 侧已有的 `filter_system_identity` 是**两层**：前者删「你是谁」的身份声明行，后者改「官方客户端固定模板句」——两者拦的东西不同。
- 代价：user 正文里若真的讨论这些句子，模型看到的是改写后的版本。可接受——这些句子出现在请求里本身就是上游的拦截条件。
- **零宽空格穿透**：改写正则允许字符间夹 U+200B，替换串是干净文本 → 命中片段的零宽空格被清掉（上游会归一化零宽空格，靠插零宽绕过无效）。

### 3.3 11128 改写的误伤

用户正常聊到 `11128`（比如贴自己的日志）也会被改成 `11-128` → 语义轻微损失。取舍理由：留着就是整单被拦（死循环），改错只是模型少看懂一个数字。开关可关。

### 3.4 cc_* kv 剥离的误伤

`cc_entrypoint=cli` 这类尾随 kv 只出现在 Claude Code 注入的 system 提示里，正常对话不会写。若用户自己写了 `cc_xxx=yyy`，会被删。风险极低。

### 3.5 缓存前缀

A 的改写是纯函数 → 同一入站永远同一出站 → 上游缓存前缀稳定（首次会 miss 一次，之后恢复）。B/C 只加头不改 body → 不影响缓存。

### 3.6 归因口径变化

B 会让上游账单 `agentPurpose` 从空变 `conversation` → 生产「用量明细」的「用途」列内容变化（是改进，但用户会看到差异）。A 不改上游账单字段。

---

## 四、分步实施（已全部完成，合并为一个 commit）

| 步 | 内容 | 验收 | 状态 |
|---|---|---|---|
| 1 | **A**：`core/outbound_sanitize.py` + 4 端点接线 + `WORKBUDDY_OUTBOUND_SANITIZE` 开关 | 新测试 `tests/protocol/test_outbound_sanitize.py`：模板句改写（身份句 CLI 版/桌面版逗号版、Main branch、Codex CLI、feedback give→provide）/ 11128 精确改写与相邻数字不动 / 指纹剥离（hdr 键值段、裸键名、尾随 kv、链接）/ 零宽空格穿透 / 纯函数幂等 / 不改入参 / 深度遍历盲区（content block、tool_calls.arguments、reasoning、tools.description） | ✅ 28 项 |
| 2 | **B**：`X-Agent-Purpose`（加在 `_build_headers_from` 共享 builder，chat/billing/config 都带） | 测试：builder 产出该头；Trae 不受影响；billing 路径共用 builder 不报错 | ✅ |
| 3 | **C**：`core/session_headers.py` + 4 端点接线 + `WORKBUDDY_SESSION_HEADERS` 三档开关 | 测试 `tests/protocol/test_session_headers.py`：取值优先级 / 无标识不伪造 / 派生关系正确 / B3 长度 16 / 三档行为 / Trae 不受影响 / sticky 跨 attempt 复用 X-Request-ID；另加 `tests/pool/test_account_pool.py::test_scope_identity_survives_rotation`（池换号重试必须传同一个 scope dict —— sticky 的前提） | ✅ 24 项 + 1 项 |
| 4 | 全量回归 + 文档 | 后端 pytest 全绿（978 passed / 1 skipped）、前端 node --test 全绿（10 文件）、`check_admin_ui.py` 通过（258 ids） | ✅ |

**未部署**：改完 + 测试全绿 + commit + push dev 即停，等用户说「部署」。

---

## 五、已确认（用户 2026-10-19 拍板）

1. **A 默认开**（`WORKBUDDY_OUTBOUND_SANITIZE` 默认 true）：只动精确数字串、模板句与几个不该出现的 kv；关掉则 11128 死循环仍在。
2. **B（`X-Agent-Purpose`）现在就加**：生产「用量明细」的「用途」列会从空变 `conversation`。
3. **C 的 `sticky` 档一起做**（三档都实现，sticky 行为要生效）→ 默认档位 = `sticky`。
4. **模板句改写 user 正文也要改写**：A 的规则覆盖所有 role，不只 system。

# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

适用场景：

- 用 **Codex CLI** 走 `/v1/responses`
- 用 **Claude Code / CC Switch** 走 `/v1/messages`
- 用 **Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI** 走 `/v1/chat/completions`

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

`workbuddy2api` 是一个本地协议转换器。它会读取你已经登录好的 WorkBuddy / CodeBuddy 桌面端凭据，转发到腾讯后端 `copilot.tencent.com`，然后在本地暴露这些接口：

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

它不负责登录，不模拟桌面端，也不替你执行工具。它只做三件事：

1. 读取本机登录态并注入鉴权头
2. 在 OpenAI / Anthropic 协议和腾讯后端协议之间转换
3. 对 `Codex CLI` 这类长上下文 agent 请求做后端友好的压缩投影

> 命名说明：项目对外名称现在叫 `workbuddy2api`。代码里仍保留部分历史命名，比如 `codebuddy2openai`、`CODEBUDDY2OPENAI_*`，目的是兼容旧配置和环境变量。
> 另外，GitHub 仓库路径当前也可能仍沿用 `codebuddy2openai`，这是仓库路径与项目展示名尚未完全统一，不影响使用。

### 你能用它做什么

- 把 WorkBuddy 订阅复用到 OpenAI 兼容客户端
- 让 Codex CLI 直接接腾讯后端，而不是只接 OpenAI 官方
- 让 Claude Code 通过 CC Switch 复用 WorkBuddy 支持的模型
- 保留原生 `tools` / `tool_calls` / 流式 SSE / 多轮工具调用

### 当前支持

| 客户端 / 协议 | 接口 | 当前状态 |
|------|------|------|
| OpenAI Chat Completions | `/v1/chat/completions` | 已支持 |
| OpenAI Responses | `/v1/responses` | 已支持，适配 Codex CLI |
| Anthropic Messages | `/v1/messages` | 已支持，适配 Claude Code / CC Switch |
| OpenAI Models | `/v1/models` | 已支持 |
| Health Check | `/health` | 已支持 |

---

## 3 分钟上手

### 1. 前置条件

你需要先满足这 3 个条件：

1. 本机已经安装并登录 **WorkBuddy / CodeBuddy** 桌面端
2. 本机有 **Python 3.14**（与 Docker 一致；3.14 才内置 `uuid.uuid7()`，见下）
3. 已安装依赖 `fastapi`、`uvicorn`、`httpx`、`cryptography`

默认会在这些位置寻找登录态：

- macOS: `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
- Windows: `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
- Linux: `~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info`

### 2. 安装依赖

推荐用 `uv`：

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.lock.txt
```

也可以用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock.txt
```

`requirements.lock.txt` 锁定直接依赖和间接依赖的版本，推荐用于稳定安装；Docker 构建也使用它。文件保留 Python 版本及平台条件，例如 Windows 不安装 `uvloop`。`requirements.txt` 保留未锁定的直接依赖，主动尝试新版时可运行 `uv pip install --upgrade -r requirements.txt`。

维护者更新锁文件后需重新安装并运行测试：

```bash
uv pip compile --universal --python-version 3.10 --no-strip-extras --no-annotate --upgrade requirements.txt -o requirements.lock.txt
uv pip sync requirements.lock.txt
uv pip install -c requirements.lock.txt pytest
uv run python -m pytest
```

> 注意：无论是启动服务，还是执行 `python3 -m core.converter --help`，都必须先装依赖。

### 3. 启动

最常用的启动方式：

```bash
uv run python -m core.converter --desensitize --log converter.log
```

或：

```bash
python3 -m core.converter --desensitize --log converter.log
```

看到监听 `http://127.0.0.1:8787` 就说明已经起来了。

### 4. 快速自检

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

如果这两条能通，说明本地服务、登录态、基本路由都没问题。

> `/health` 的深度判据（`ok` / `degraded` / `down`）只在管理后台 `python3 -m admin.server` 下生效；独立的 `core.converter` 返回的是带平台、Python 版本和 auth 文件路径的进程信息。

---

## 客户端接入

### Codex CLI

这是当前最推荐的接法。Codex CLI 走的是 `/v1/responses`，而不是 `/v1/chat/completions`。

推荐启动命令：

```bash
uv run python -m core.converter --desensitize --log converter.log
```

把下面配置合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

设置一个占位环境变量：

```bash
export CODEBUDDY2OPENAI_KEY=any-value
```

启动：

```bash
codex --profile workbuddy "你的任务描述"
```

补充说明：

- 推荐保留 `--desensitize`
- 当前 `/v1/responses` 默认已经会做投影压缩
- 如果你想尽量保留原始 system prompt，可试 `--desensitize --no-compact`
- `--desensitize --no-compact` 下若仍命中审核，当前实现会自动退回紧凑模式重试一次

### Claude Code / CC Switch

Claude Code 不走 OpenAI 协议，而是走 Anthropic Messages。

推荐启动命令：

```bash
uv run python -m core.converter --desensitize --log converter.log
```

在 CC Switch 里配置：

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

注意：

- 模型名必须填写腾讯后端支持的真实模型名
- 不做 Anthropic 模型名到腾讯模型名的自动映射
- Claude Code 场景强烈建议开启 `--desensitize`

### 其他 OpenAI 兼容客户端

适用于：

- Cherry Studio
- ZCode
- LobeChat
- NextChat
- Open WebUI
- 自己写的 OpenAI SDK 客户端

配置方式：

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: 留空，或填你启动时设置的 `--api-key`
- 模型名: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

---

## 常用命令

### 基本启动

```bash
python3 -m core.converter
python3 -m core.converter --desensitize
python3 -m core.converter --desensitize --log converter.log
python3 -m core.converter --api-key mysecret
python3 -m core.converter --port 9000
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | `WORKBUDDY_API_KEY` | 给本地客户端加一层鉴权 |
| `--log` | `WORKBUDDY_LOG` | 额外把日志写到该文件；不传则只输出到 stderr |
| `--desensitize` | 关 | 压缩运行时提示、去掉 tool description、零宽脱敏高风险关键词 |
| `--no-compact` | 关 | 配合 `--desensitize` 使用，保留更完整的原始 system prompt |
| `--skip-check` | 否 | 跳过启动预检 |

### curl 示例

```bash
curl http://127.0.0.1:8787/v1/models

curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'
```

---

## 日志与排障

### 推荐启动方式

```bash
uv run python -m core.converter --desensitize --log converter.log
```

### 日志里能看到什么

日志默认输出到 **stderr**（容器运行时直接收集），`--log` 只是额外落一份文件，不需要它也能排障；级别用 `WORKBUDDY_LOG_LEVEL` 调（默认 `INFO`，第三方库连接细节已降噪，`DEBUG` 也不会刷满 httpx）。

每次请求都会带一个唯一 ID，常见日志包括：

- `REQUEST BODY`
- `RESPONSES → CHAT BODY`
- `RESPONSES PROJECTION`
- `RESPONSE BODY`
- `RESPONSE RAW SSE`
- `⚠️内容审核拦截`

其中 `RESPONSES PROJECTION` 会告诉你：

- 投影前后消息数
- 投影前后字符数
- tool schema 压缩量
- 是否丢掉了 harness 消息
- 是否保留了 anchor user

### 最常见问题

#### 找不到登录文件

说明桌面端没登录，或者登录目录不在默认路径。先确认桌面端已经真正完成登录。

#### 401

分两种：

- 本地 401：你启用了 `--api-key`，但客户端没带同一个 key
- 后端 401：腾讯 token 失效，尝试重新打开桌面端登录

#### 响应慢

先换快一点的模型，比如 `deepseek-v4-flash`。

#### 被“敏感内容”拦截

这是腾讯后端的内容审核，不一定是用户问题本身敏感，很多时候是 agent runtime 文本触发的，比如：

- `DoS`
- `exploit`
- `credential`
- `sandbox`
- `escalation`
- 竞争品牌词
- tool description 中的安全术语

建议排查顺序：

1. 开 `--log`
2. 看同一请求 ID 下的 `REQUEST BODY` 或 `RESPONSES → CHAT BODY`
3. 如果是 Codex CLI，再看 `RESPONSES PROJECTION`
4. 开 `--desensitize`
5. 如果还不稳，再尝试 `--desensitize --no-compact`

---

## 管理后台（可选）

除原转换器外，项目附带一个中文 Web 管理后台（`admin/`），适合服务器长期运行：

- 浏览器授权添加国内 CodeBuddy / WorkBuddy 账号，无需桌面端；多账号自动轮转、冷却与积分耗尽避让
- 凭据刷新、积分余额查询、每日自动签到（默认北京时间 09:00，可配置）、积分消耗速率与过期浪费预估
- 客户端 API Key 创建与撤销，可逐枚限定可用模型 / 可用账号与分配策略
- 概览页：请求量、完成成功率、HTTP 成功率、平均耗时与最近请求（读取 SQLite，支持翻页，重启不清零）
- 用量明细页：上游账单级积分消耗报表，**默认只读本地库，点「拉取上游」才打上游**
- 模型管理：动态目录、逐个启停、手动添加上游没返回的模型

本地启动（需先安装依赖并设置至少 20 位的管理密钥）：

```bash
export WORKBUDDY_ADMIN_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(36))")
export WORKBUDDY_API_KEY=sk-wb-$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
python3 -m admin.server
```

然后访问 `http://127.0.0.1:8787/admin/`。一键 Docker 部署见下文；详细说明见 [ADMIN_README.md](ADMIN_README.md)。

> 管理后台为单进程设计，正式对外部署必须放在 HTTPS 反向代理之后（示例：`deploy/admin/nginx.conf.example`）。直接运行 `python3 -m core.converter` 的原有用法不受影响。

---

## Docker 部署

如果你更习惯用 Docker，可以直接用。

前提是把宿主机登录态目录挂进去，因为容器里拿不到桌面端 auth 文件。

### 一键部署（推荐，含管理后台）

一条命令完成密钥生成、镜像构建与启动：

```bash
bash deploy/one-click/deploy.sh
```

脚本会自动生成 `ADMIN_KEY` 与客户端 Key（保存在 `deploy/one-click/.env`，仅首次显示），随后访问 `http://127.0.0.1:8787/admin/`。改端口：编辑 `deploy/one-click/.env` 里的 `PORT` 后重跑脚本。

### docker compose（独立转换器）

先改 `deploy/standalone/docker-compose.yml` 里的 auth 挂载路径，再执行：

```bash
docker compose -f deploy/standalone/docker-compose.yml up -d --build
```

### docker run（独立转换器）

```bash
docker build -t workbuddy2api -f deploy/standalone/Dockerfile .

docker run -d --name workbuddy2api -p 8787:8787 \
  -v ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth:/data/auth:ro \
  -e WORKBUDDY_AUTH_DIR=/data/auth \
  workbuddy2api
```

### 相关环境变量

完整列表见下方「配置」一节（旧名全部继续生效）。这里只列 compose 里最常改的：

| 变量 | 说明 |
|------|------|
| `WORKBUDDY_AUTH_DIR`（旧 `CODEBUDDY_AUTH_DIR`） | 指定登录态目录 |
| `WORKBUDDY_API_KEY`（旧 `CODEBUDDY2OPENAI_KEY`） | 本地 API Key |
| `WORKBUDDY_LOG`（旧 `CODEBUDDY2OPENAI_LOG`） | 日志路径 |

---

## 模型列表

模型目录由管理后台的**模型注册表**维护（`admin/models.py`），不再以 `core/converter.py` 里的静态列表为准：

- 启动时无本地缓存、以及之后每 6 小时，拉取官方 `GET https://www.workbuddy.cn/v3/config` 动态发现，拿到模型菜单、`credits` 倍率、`maxInputTokens` 和折扣活动；失败保留上次缓存。
- 三档智能路由 `fast-model` / `balanced-model` / `deep-model` 由官方固定提供，注册表无条件附加。
- 从未成功拉取过时，回落内置官方快照（`BUILTIN_MODELS`）：`hy4-preview`、`hy3`、`hy3-x`、`step-5-preview`、`space-bunny`、`deepseek-v4.1-flash`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`minimax-m3`、`minimax-m2.7`、`kimi-k3-1`、`kimi-k2.8-preview`、`kimi-k2.7`、`kimi-k2.6`、`deepseek-v4-pro`，加三档智能路由。
- 上游 `/v3/config` 的菜单**不一定列全**：有些 id 没返回但实际仍可调用，可以在后台「模型管理」手工补上（记为 `custom`，自动拉取时**合并**而非替换，不会被冲掉），可调用性只由「启用」决定。
- 禁用某模型后：`/v1/models` 不再返回它，用它发起对话直接 400。

具体能不能用，仍取决于你的订阅与上游状态。详见 [ADMIN_README.md](ADMIN_README.md)。

---

## 能力实测

以下为对本项目上游（腾讯 CodeBuddy 后端）的实测结论，非官方文档，仅供参考。

### 图像识别（多模态）

支持。走 `image_url`（支持 `data:image/...;base64,` 内联图）传入图片，视觉模型可正确识别。

实测用例：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CODEBUDDY2OPENAI_KEY" \
  -d '{
    "model": "glm-5v-turbo",
    "messages": [{"role": "user", "content": [
      {"type": "text", "text": "这张图是什么颜色？只回答颜色词"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,<BASE64>"}}
    ]}]
  }'
```

结果：传入纯红色 PNG，模型正确回答「红色」。

**已知支持视觉的模型**：`glm-5v-turbo`（`kimi` / `deepseek` 系未逐一验证）。

### 上下文长度

实测上游可接受**超长上下文**，远超预期。用 `kimi-k3` 逐档压测（上游在响应 `usage.prompt_tokens` 中返回真实 token 数）：

| 输入规模 | 上游返回 `prompt_tokens` | 结果 |
|---|---|---|
| 256K 字符 | — | ✅ 通过 |
| 512K 字符 | — | ✅ 通过 |
| 1024K 字符 | — | ✅ 通过 |
| 2048K 字符 | 409,701 | ✅ 接受，未报超长 |
| 4096K 字符 | 819,307 | ✅ 接受，未报超长 |

结论：**上游至少接受到约 82 万 tokens 未拒绝**，实际窗口上限高于此值（未继续上探，边际成本高）。

> 说明：压测中所有响应均为 `finish_reason: "length"`（模型把 `max_tokens` 额度用在了推理输出上），**不是上下文被截断拒绝**，属于正常表现。

### 模型选型建议

| 用途 | 推荐模型 | 说明 |
|---|---|---|
| 超长文档 / 代码库 | `kimi-k3` | 已验证 800K+ tokens 上下文 |
| 图像识别 | `glm-5v-turbo` | 视觉 + 长上下文 |
| 日常对话 / 追求速度 | `glm-5.3-flash`、`deepseek-v4-flash` | 约 1.1s 级响应 |
| 重任务 / 复杂推理 | `glm-5.3` | — |

---

## 容量队列自动切换（免费模型高峰期）

免费模型（如 `Step-5-Preview`）在高峰期会被上游放进**容量队列**，不立即处理，而是返回：

```json
{"code": "WAITING", "message": "当前模型资源紧张，请等候。Step-5-Preview 前方约 7859 位用户排队…",
 "data": {"queue_position": 7859, "retry_after": 5}}
```

这段文案是**上游下发的**，不是客户端写死的。开启后，网关会在**尚未向客户端发出任何字节**前自动换号重试，并对客户端透明（不会看到半截输出或排队提示）：

1. **先换账号**：同模型换个号可能就不排队了。
2. **账号试尽后降级模型**：换到**同价位的免费/低价模型**（免费档与低价档合并，按倍率升序，越省越好）。
3. **最后兜底**：退到 `fast-model` 智能路由。
4. **全部用尽**：把上游原始排队响应原样返回，客户端可按 `data.retry_after` 自行轮询。

### 开关与配置（管理后台 → 设置 → 排队容错）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `queue_failover` | `false` | **总开关。默认关闭**，需在管理页显式打开 |
| `queue_retry` | `3` | 单次请求最多尝试几次（含首次），1 = 不重试 |
| `queue_cooldown` | `120` | 命中排队的账号让位秒数（优先跳过，**不**计入失败退避） |
| `queue_hold_ms` | `3000` | 扣留首包的**硬上限**（毫秒），也是事故防线 |
| `queue_model_fallback` | `true` | 换号用尽后是否降级模型 |
| `queue_fallback_model` | `fast-model` | 最后的兜底模型 |

### 行为边界（安全保证）

- **扣留有硬上界**：超过 `queue_hold_ms` 没有首包，就判定为「上游慢」并原样放行，**绝不无限等待**。上一版实现因为无上界扣留把 `/health` 一起拖死而被回滚，这条是那次事故的防线。
- **只在首字节前切换**：一旦响应里出现真实内容（`choices`/`delta`/`content` 等），即使命中排队关键词也立刻放行，绝不重放——否则客户端会收到重复输出。
- **形态全兼容**：非 200 的 `429`/`503`、body 里的字符串码 `WAITING`/`FULL`、上游繁忙码 `11102`/`11103`/`11001`/`50001`，以及文案兜底关键词，都识别为排队。
- **不切不该切的**：`USER_LIMIT`（个人额度到顶）与 `11101`（非流式不支持）**不**算排队——换号换模型都救不了，直接让用户看到。
- **让位不是冷却**：命中的账号只是短期优先跳过；**全部账号都在让位期内时会忽略该标记**，不会把池子自我封锁成 503。
- **默认关闭**：这是唯一会重放整个请求的功能，影响面比其它设置大，所以默认不开。

> 上游改文案不会让功能失效（主判据是结构化 code），但如果你遇到了新的排队报错形态，可以在 `admin/pool.py` 的 `QUEUE_PATTERNS` 里补关键词。

---

## 配置

配置有**唯一读取入口** `core/runtime_config.py`，优先级：

```
命令行参数 > 真实环境变量 > .env 文件 > 代码默认值
```

`.env` 用 `setdefault` 语义写入 `os.environ`，所以 docker/compose 里显式传的变量始终赢过文件。启动时若检测到你在用旧变量名（如 `CODEBUDDY_AUTH_DIR`、`ADMIN_KEY`），日志里会**汇总成一行**提示改用新名，新旧名同时有效。

### 环境变量

| 变量（旧名 → 新名） | 默认值 | 说明 |
|---|---|---|
| `WORKBUDDY_DATA_DIR`（`MANAGEMENT_DATA_DIR`） | `/data/management` | SQLite 数据目录（`state.db`） |
| `WORKBUDDY_AUTH_DIR`（`CODEBUDDY_AUTH_DIR`） | `/data/auth` | 桌面端登录态目录 |
| `WORKBUDDY_HOST` / `WORKBUDDY_PORT`（`PORT`） | `127.0.0.1` / `8787` | 监听地址与端口 |
| `WORKBUDDY_ADMIN_KEY`（`ADMIN_KEY`） | 空 | 管理后台密钥，至少 20 字符 |
| `WORKBUDDY_API_KEY`（`CODEBUDDY2OPENAI_KEY`） | 空 | 客户端 API Key |
| `WORKBUDDY_LOG`（`CODEBUDDY2OPENAI_LOG`） | 无 | 额外写日志文件 |
| `WORKBUDDY_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `WORKBUDDY_STREAM_IDLE_TIMEOUT` | `120` | 流式请求的**逐分片**空闲超时（秒） |
| `WORKBUDDY_DESENSITIZE`（`CODEBUDDY_RESPONSES_DESENSITIZE`） | 关 | 等价 `--desensitize` |
| `WORKBUDDY_NO_COMPACT` | 关 | 等价 `--no-compact` |
| `WORKBUDDY_LOSSY_PROJECTION` | 关 | 有损投影 |
| `WORKBUDDY_ENV_FILE` | `.env` | 指定 env 文件路径 |

容器部署通常用旧名最省事——旧名全部继续生效，只是启动时会给一行提示。

### 流式空闲超时与截断可观测

流式请求**不设总超时**（LLM 输出天然可能几分钟），改为逐分片判空闲：相邻两个分片间隔超过 `WORKBUDDY_STREAM_IDLE_TIMEOUT`（默认 120 秒）就中止并向客户端返回明确错误。这样上游「连接还活着但不再发数据」的卡死场景不会一直占着账号的 in-flight 名额。

若上游在**没有终止事件**的情况下断开，网关会在流末尾补一个可识别的截断错误事件，而不是让客户端把半截输出当成正常结束。

---

## 可观测性

- **默认即有日志**：日志恒输出到 stderr（由容器运行时收集），`--log` 只是额外落一份文件；重复初始化不叠加 handler。
- **`/health` 是深度检查**，不再恒返回 ok：
  - `ok` —— 有可用账号且库可读
  - `degraded` —— 库可读但**一个可用账号都没有**（凭据全失效/全暂停），仍返回 200（重启修不好凭据失效，标 unhealthy 只会触发重启循环）
  - `down` —— 状态无法判定，HTTP 503
  - `?verbose=1` 附带逐账号明细（默认不返回，避免未鉴权端点泄露账号名）
- 「可用账号」判据（启用 + 凭据可解析 + 未标认证失效）与账号池内部判据保持一致，避免漂移。

---

## 数据库

管理数据全部落 SQLite（`management/state.db`），**没有通用 kv 表**，全部结构化：

| 表 | 用途 |
|---|---|
| `accounts` / `keys` | 账号与客户端密钥（含模型/账号白名单、限额） |
| `settings` | 账号池单行配置（签到时间、积分拉取时段、亲和天数等） |
| `models` | 模型目录（来源、可用性、启用状态、倍率、上下文） |
| `request_logs` | 请求明细与概览统计，保留 93 天 |
| `upstream_request_usage` | 上游账单级积分消耗，按需拉取 |
| `key_usage_daily` | 密钥每日用量趋势 |
| `session_bindings` | 会话亲和绑定，带过期清理 |
| `schema_meta` | schema 版本号 |

约定：金额落库 `*_micro`（×1e6 整数），内存与接口输出保持 float；时间一律 `INTEGER` 秒级 unix；日历分桶按**北京时间**；不存请求/响应正文；迁移走版本化列表，所有待执行迁移合并在同一事务里，失败整体回滚且不写版本号。

详见 `specs/db-redesign.md` 与 [ADMIN_README.md](ADMIN_README.md)。

---

## 项目结构

```text
workbuddy2api/
├── core/                      # 协议转换核心（不依赖 admin，可单独成镜像）
│   ├── converter.py           # 主入口，FastAPI 服务（python3 -m core.converter）
│   ├── runtime_config.py      # 配置唯一读取入口 + 旧名兼容
│   ├── runtime_logging.py     # 日志分级与初始化
│   ├── responses_adapter.py   # OpenAI Responses ↔ Chat 适配
│   ├── responses_projection.py# Codex / agent 请求投影压缩
│   ├── anthropic_adapter.py   # Anthropic Messages ↔ Chat 适配
│   ├── desensitize.py         # 运行时文本压缩与零宽脱敏
│   ├── system_identity.py     # 剥离 system 里的身份声明
│   └── workbuddy_atrest_crypto.py # $wbEncrypted 登录态解密（WorkBuddy 5.6+）
├── admin/                     # 中文管理后台（可选，python3 -m admin.server）
│   ├── server.py              # 管理 API、会话与静态页面
│   ├── db.py                  # SQLite 持久层与版本化迁移
│   ├── models.py              # 模型注册表（/v3/config 动态发现 + 内置兜底）
│   ├── pool.py                # 账号池轮转、冷却、积分与签到
│   ├── browser_login.py       # 浏览器授权登录
│   ├── metrics.py             # 请求统计
│   └── static/                # 前端资源
├── specs/                     # 设计文档（改动先写 spec，再实现）
│   ├── db-redesign.md         # SQLite 结构定稿
│   ├── queue-failover.md      # 容量队列自动切换（换号 / 降级模型）
│   ├── config.md              # 统一配置入口
│   ├── observability.md       # 日志分级 + 深度 health
│   ├── stream-robustness.md   # 流式空闲超时与截断可观测
│   ├── usage-stats.md         # 用量明细与报表
│   └── custom-models.md       # 手动添加模型
├── deploy/                    # 部署
│   ├── standalone/            # 独立转换器（Dockerfile + compose）
│   ├── admin/                 # 管理后台（Dockerfile + compose + nginx 示例）
│   └── one-click/             # 一键式部署（deploy.sh 自动生成密钥并启动）
├── tests/                     # 单元测试（pytest），按功能域分目录（见 tests/README.md）
├── codex-codebuddy.example.toml
├── README.md                  # 本文件
├── ADMIN_README.md            # 管理后台详细说明
└── LICENSE
```

运行测试：

```bash
pytest                       # 全部（除标记为 slow 的）
pytest tests/pool             # 只跑改动涉及的那一域
pytest -m e2e                 # 只跑端到端链路
```

测试按功能域分目录（`protocol/` `pool/` `persistence/` `admin/` `security/`
`config/` `unit/`），并用 `core` / `slow` / `e2e` / `network` 标记分层——
**改哪个模块就跑哪个目录**，不必每次全量回归。约定与「写测试的几条硬规矩」
见 [`tests/README.md`](tests/README.md)。
管理页前端还有一组不依赖浏览器、也不依赖第三方包的脚本（Node）：

```bash
node tests/frontend/fmttokens.test.mjs      # 积分/token 的整数展示口径
node tests/frontend/modelgroup.test.mjs     # 同名模型跨后端的合并分组
node tests/frontend/rendermodels.test.mjs   # 用真实 overview 数据渲染模型表
```

`rendermodels.test.mjs` 会读取一份 state.db（默认 `/tmp/opencode/v7fresh`，
可用 `WB_TEST_DB` 指定目录、`WB_TEST_PYTHON` 指定解释器）；找不到就跳过，
不会报错。它是唯一能抓出「字段名写错导致渲染成 undefined」这类模板错误的检查，
改模型表时务必跑一遍。

> 这三个脚本靠从 `admin/static/app.js` 里切出纯函数来求值，因此重构 app.js 顶部
> 的 `creditsFmt` / `backend*` / `groupModelsByName` / `renderModelGroup` 时，
> 要同步确认脚本仍能切出这些片段（切不出会直接报错，不会静默跳过）。

---

## 致谢

本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。

## 免责声明

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

## 开源协议

[MIT](./LICENSE)

---

<a name="english"></a>
## English

`workbuddy2api` exposes your already logged-in **WorkBuddy / CodeBuddy** desktop session as local **OpenAI- and Anthropic-compatible APIs**.

Supported endpoints:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

Recommended use cases:

- **Codex CLI** via `/v1/responses`
- **Claude Code / CC Switch** via `/v1/messages`
- **Cherry Studio / ZCode / LobeChat / Open WebUI** via `/v1/chat/completions`

### Quick Start

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.lock.txt
uv run python -m core.converter --desensitize --log converter.log
```

Requires Python 3.10+. `requirements.lock.txt` pins runtime dependencies, including platform-specific conditions, and is also used by Docker builds. Use `requirements.txt` only when intentionally trying newer dependency versions.

Then verify:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

### Codex CLI

Use `/v1/responses` and keep `--desensitize` enabled.

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

Run:

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "your task"
```

### Claude Code / CC Switch

Use `/v1/messages`:

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### Notes

- `--desensitize` is recommended for both Codex CLI and Claude Code
- `/v1/responses` already applies backend-facing projection by default
- `--desensitize --no-compact` preserves more of the original system prompt
- if that still gets review-blocked, `/v1/responses` will retry once in compact mode
- an optional Chinese web admin console (`python3 -m admin.server`, see [ADMIN_README.md](ADMIN_README.md)) adds browser-based account authorization, multi-account rotation, client API keys, daily check-in, usage reports and a model registry; one-command Docker setup: `bash deploy/one-click/deploy.sh`

### Configuration

Single entry point: `core/runtime_config.py`. Precedence is
`CLI flags > real env vars > .env file > code defaults`. Legacy variable names
(`CODEBUDDY_AUTH_DIR`, `ADMIN_KEY`, `CODEBUDDY2OPENAI_KEY`, …) keep working — the
startup log prints one aggregated line telling you the new name.

| Variable (legacy → current) | Default | Purpose |
|---|---|---|
| `WORKBUDDY_DATA_DIR` (`MANAGEMENT_DATA_DIR`) | `/data/management` | SQLite data dir |
| `WORKBUDDY_AUTH_DIR` (`CODEBUDDY_AUTH_DIR`) | `/data/auth` | Desktop auth dir |
| `WORKBUDDY_HOST` / `WORKBUDDY_PORT` (`PORT`) | `127.0.0.1` / `8787` | Listen address |
| `WORKBUDDY_ADMIN_KEY` (`ADMIN_KEY`) | — | Admin console key (≥20 chars) |
| `WORKBUDDY_API_KEY` (`CODEBUDDY2OPENAI_KEY`) | — | Client API key |
| `WORKBUDDY_LOG` (`CODEBUDDY2OPENAI_LOG`) | — | Extra log file |
| `WORKBUDDY_LOG_LEVEL` | `INFO` | Log level |
| `WORKBUDDY_STREAM_IDLE_TIMEOUT` | `120` | Per-chunk stream idle timeout (s) |
| `WORKBUDDY_DESENSITIZE` (`CODEBUDDY_RESPONSES_DESENSITIZE`) | off | Same as `--desensitize` |
| `WORKBUDDY_NO_COMPACT` | off | Same as `--no-compact` |

### Observability

- Logging always writes to stderr (collected by the container runtime); `--log` only adds a file. Level via `WORKBUDDY_LOG_LEVEL`.
- Streaming requests have **no total timeout** (LLM output can legitimately run for minutes) but a per-chunk idle timeout, so a dead-but-open upstream connection can't hold an account slot forever. A stream that ends without a terminal event gets an explicit truncation error event instead of silently ending.
- `/health` is a deep check: `ok` / `degraded` (DB readable but zero usable accounts, still HTTP 200) / `down` (HTTP 503). `?verbose=1` adds per-account detail.

### Database

All admin state lives in SQLite (`management/state.db`), fully structured — no generic kv table: `accounts`, `keys`, `settings`, `models`, `request_logs` (93-day retention), `upstream_request_usage`, `key_usage_daily`, `account_usage_daily`, `session_bindings`, `schema_meta`. Money is stored as `*_micro` integers (×1e6) and exposed as float; timestamps are integer unix seconds; calendar bucketing is Beijing time. Migrations are versioned and applied in a single transaction, so a failure rolls back and leaves the version number untouched. See `specs/db-redesign.md`.

### Capacity Queue Failover (optional, off by default)

Free models get placed into an upstream **capacity queue** during peak hours and return
`{"code":"WAITING","data":{"queue_position":…,"retry_after":…}}`. When enabled, the gateway
switches accounts (and then downgrades to a same-tier cheap model, falling back to `fast-model`)
**before emitting any byte to the client**, so the caller never sees the queue notice. The hold
window is hard-bounded by `queue_hold_ms` (default 3000ms) — if no first chunk arrives in time it
is treated as a slow upstream and passed through, never waited on indefinitely. A response that
already contains real content is never replayed. See `specs/queue-failover.md`.

### Backend Isolation: Tencent vs. ByteDance

Tencent (CodeBuddy) and ByteDance (Trae) share **one** account pool, separated by
`accounts.backend`. Per-account state (cooldown, queue yield, quota guards, cost ledger) is
already isolated by construction, but the separation only holds if every account-selection call
passes the backend through — a single omission hands one backend's request the other backend's
account, which surfaces as a baffling 4xx rather than a wrong-account retry. Four invariants are
enforced and regression-tested: selection always filters by backend (including the queue-failover
retry, the easiest place to leak); `try_select` passes it through and never borrows an account
from the other side; the WAF fail-fast gate is bucketed per backend so a ByteDance block cannot
stall Tencent rotation; and the pool tick traps errors per account so one broken ByteDance
account cannot starve Tencent accounts of check-in and credential refresh. To detach ByteDance
entirely, set `CODEBUDDY_ENABLE_TRAE=0` rather than disabling accounts.
See `specs/backend-isolation.md`.

### Upstream Failure Handling
Three small modules on the account-pool failure path: cooldown deadlines are taken from the
upstream's **stated** reset time when it provides one (falling back to bounded local backoff),
transport errors are classified as replay-safe vs. possibly-already-billed (never replay after the
request body is sent), and logs are scrubbed of credentials by a handler-level filter so a
`log.exception` cannot leak a Bearer token. See `specs/upstream-failure-handling.md`.

### Measured Cost Ledger

Selects accounts by **observed** cost per `(account, model)`, preferring accounts measured as free —
whether a given account is billed cannot be inferred from config (free-tier windows, nightly
promotions and trials only show up in the response). Observations expire after 6 hours. Critically,
the ledger is **inert until observed**: if the upstream never returns a `credit` field, it records
nothing and selection behaves exactly as before. `observed` in `overview.metrics.cost_ledger` is
how you tell whether it is doing anything. See `specs/upstream-failure-handling.md`.

### Account Quota Guards & WAF Fail-fast (optional, off by default)

Four local guards stop requests that would burn an upstream account dry — upstream tells you
the remaining balance but will not stop you from spending it. All four default to `0`
(disabled), and when disabled nothing extra is read or written. The two load-bearing rules:
**`0` means off**, and **an unknown count never blocks** (if usage cannot be read, the guard
lets the request through rather than disabling the account — passing one request is far better
than killing the pool because a read failed). Rotation between accounts is backed off with
±25% jitter so retries do not re-cluster, and a `403`/`11128` seen on **two different accounts
within 60s** is treated as an egress-IP-level block, stopping rotation before it amplifies the
risk. See `specs/account-guards.md`.

### Capability Notes (Measured)

Not official specs — measured against this project's upstream (Tencent CodeBuddy backend).

**Vision (multimodal): supported.** Images can be passed via `image_url`, including inline `data:image/...;base64,`. Verified with a solid-color PNG on `glm-5v-turbo` — the model reported the correct color. Known vision model: `glm-5v-turbo` (kimi/deepseek families not individually verified).

**Context length: very large.** Measured with `kimi-k3`, using the real `usage.prompt_tokens` returned by upstream:

| Input size | `prompt_tokens` | Result |
|---|---|---|
| 256K chars | — | ✅ accepted |
| 512K chars | — | ✅ accepted |
| 1024K chars | — | ✅ accepted |
| 2048K chars | 409,701 | ✅ accepted |
| 4096K chars | 819,307 | ✅ accepted |

Upstream accepted roughly **820K tokens** without rejecting the request; the real limit is higher. Responses showed `finish_reason: "length"` because the token budget was spent on reasoning output, not because context was truncated.

**Model selection:**

| Use case | Model | Notes |
|---|---|---|
| Very long docs / codebases | `kimi-k3` | 800K+ token context verified |
| Image understanding | `glm-5v-turbo` | vision + long context |
| Chat / low latency | `glm-5.3-flash`, `deepseek-v4-flash` | ~1.1s response |
| Heavy tasks / reasoning | `glm-5.3` | — |

### CLI Options

```bash
python3 -m core.converter [--host HOST] [--port PORT] [--api-key KEY] [--log PATH] [--desensitize] [--no-compact] [--skip-check]
```

### Disclaimer

For personal learning and research only. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic.

---

<sub>
Keywords: codebuddy to openai · codebuddy2openai · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api
</sub>

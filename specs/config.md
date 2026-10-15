# 配置收敛 Spec：统一入口 + .env

> 状态：已实现（2026-10-15）。原设计见正文；实现见 `core/runtime_config.py` 与 `core/runtime_logging.py`。范围是**收敛已有配置的读取方式**，不引入新的配置体系（不引 pydantic-settings、
> 不建 YAML 配置文件）。

## 背景

配置入口散在三个文件、三套命名前缀、两种传参方式，且存在「文档写了但代码没读」的假配置。

### 现状全清单

**环境变量**（12 个，散在 2 个文件）：

| 变量 | 读取点 | 用途 |
|---|---|---|
| `MANAGEMENT_DATA_DIR` | `server.py:473` | 管理数据目录 |
| `CODEBUDDY_AUTH_DIR` | `server.py:473`、`converter.py:97` | 凭据目录 |
| `ADMIN_KEY` | `server.py:473` | 管理后台密码 |
| `CODEBUDDY2OPENAI_KEY` | `converter.py:1542`、`server.py:473` | 客户端 API key |
| `CODEBUDDY2OPENAI_LOG` | `converter.py:1573` | 日志文件路径 |
| `CLIENT_INFO_PRODUCT_NAME` | `converter.py:84` | 伪装的产品名 |
| `CLIENT_INFO_PRODUCT_VERSION` | `converter.py:85` | 伪装的产品版本 |
| `CLIENT_INFO_USER_AGENT_EXTENSION` | `converter.py:86` | 伪装的 UA 后缀 |
| `CODEBUDDY_LOSSY_PROJECTION` | `converter.py:1036` | 有损投影开关 |
| `CODEBUDDY_RESPONSES_DESENSITIZE` | `converter.py:974,1043` | 脱敏开关 |
| `LOCALAPPDATA` / `XDG_DATA_HOME` | `converter.py:113,115,325` | 平台标准路径（**系统变量，不属本项目配置**） |

**命令行参数**（`converter.py:1538-1565`）：`--host`、`--port`、`--api-key`、`--log`、
`--desensitize`、`--no-compact`、`--skip-check`。

### 四个具体问题

1. **`PORT` 是假配置**。`deploy/one-click/env.example` 里写了 `PORT=8787`，但
   `server.py:1099` 硬编码 `port=8787`——改 `.env` 完全无效。这是**误导用户**。
2. **`CODEBUDDY2OPENAI_LOG` 在管理模式下是死的**。`create_app` 里
   `converter.CONFIG.update({..., "log_path": None})`（`server.py:474`）**无条件覆盖**成 None，
   于是设了环境变量也不记日志。这正好是可观测性 spec 要修的。
3. **`.env` 不支持**。全项目无 `dotenv`。一键部署脚本只能把变量塞进 compose 的
   `environment:`，用户无法用一份 `.env` 管全部配置。
4. **三套前缀**（`CODEBUDDY2OPENAI_*` / `CLIENT_INFO_*` / `CODEBUDDY_*`）+ 部分无前缀
   （`ADMIN_KEY`、`MANAGEMENT_DATA_DIR`），规律性差，用户记不住。

## 目标

1. **单一读取入口**：新增 `core/runtime_config.py`，所有配置项在这里定义、读取、校验。
   其它模块不再直接 `os.environ.get`（平台变量 `LOCALAPPDATA`/`XDG_DATA_HOME` 除外）。
2. **支持 `.env`**：启动时读 `.env`（不覆盖已存在的真实环境变量——环境变量优先级更高，
   这是 docker 场景的正确语义）。
3. **不破坏现有变量名**：旧名继续生效（用户 compose 里已经写了），新增规范名并**在日志里
   提示一次旧名已弃用**。这是「收敛」而非「破坏」。
4. **修掉假配置**：`PORT` 真的生效。

## 非目标

- 不引第三方配置库（`python-dotenv` 都尽量不用，手写 40 行解析足够，且避免新依赖）。
- 不做配置热重载（改配置重启即可，与容器部署方式一致）。
- 不把配置搬进管理后台（那是运行时数据，配置属于启动时）。
- 不改 `deploy/` 的 compose 文件结构（只让其中描述的变量真的生效）。

## 设计

### 一、`core/runtime_config.py`

**放在 core 而不是 admin**：`deploy/standalone` 的 Dockerfile 只 `COPY core ./core`，
独立转换器镜像必须能只用 core 起得来，所以 core 不能反向依赖 admin。`admin/config.py`
保留为同名转发垫片，让既有的 `from admin import config` 导入继续可用。

统一命名规范：`WORKBUDDY_*` 前缀（与项目对外名 `workbuddy2api` 一致）。

```python
@dataclass(frozen=True)
class Config:
    data_dir: str            # WORKBUDDY_DATA_DIR  / MANAGEMENT_DATA_DIR
    auth_dir: str            # WORKBUDDY_AUTH_DIR  / CODEBUDDY_AUTH_DIR
    host: str                # WORKBUDDY_HOST      / --host
    port: int                # WORKBUDDY_PORT      / PORT / --port
    admin_key: str           # WORKBUDDY_ADMIN_KEY / ADMIN_KEY
    api_key: str             # WORKBUDDY_API_KEY   / CODEBUDDY2OPENAI_KEY
    log_path: str | None     # WORKBUDDY_LOG       / CODEBUDDY2OPENAI_LOG
    log_level: str           # WORKBUDDY_LOG_LEVEL    stream_idle_timeout: int # WORKBUDDY_STREAM_IDLE_TIMEOUT
    desensitize: bool        # WORKBUDDY_DESENSITIZE / CODEBUDDY_RESPONSES_DESENSITIZE
    no_compact: bool         # WORKBUDDY_NO_COMPACT
    lossy_projection: bool   # WORKBUDDY_LOSSY_PROJECTION / CODEBUDDY_LOSSY_PROJECTION
```

**优先级**（高 → 低）：命令行参数 > 真实环境变量 > `.env` > 代码默认值。

**旧名兼容**：`load_config()` 内部维护 `_ALIASES = {"WORKBUDDY_PORT": ("PORT",), ...}`，
按顺序取第一个有值的。命中旧名时 `logger.warning` 一次（不是每个请求都刷）。

**校验**：`port` 必须 1–65535，否则启动即失败并给出清晰错误（而不是 uvicorn 抛出难懂的异常）。
`log_level` 必须合法，否则回退 `INFO` 并告警。

### 二、`.env` 解析

手写最小实现，支持：`KEY=VALUE`、`# 注释`、空行、值两侧引号。
**不覆盖已存在的真实环境变量**（`os.environ.setdefault` 语义）——这样
`docker run -e` 与 compose `environment:` 始终赢过 `.env` 文件，符合直觉。

读取位置：**当前工作目录**下的 `.env`（由 `WORKBUDDY_ENV_FILE` 可覆盖）。
找不到就静默跳过（不是错误）。

### 三、收敛读取点

| 文件 | 改成 |
|---|---|
| `server.py:473` `create_app` | 参数缺省时从 `load_config()` 取（保留显式传参，测试在用） |
| `server.py:474` `log_path: None` 硬覆盖 | 删除；由 `setup_logging` 统一处理 |
| `server.py:1099` `port=8787` | `cfg.port` / `cfg.host` |
| `server.py:12/1099` `host="0.0.0.0"` | `cfg.host`，默认仍是 `0.0.0.0`（容器内必须监听全网卡） |
| `converter.py:84-86` 三个 `CLIENT_INFO_*` | 从 `config.py` 取（模块加载期取值，行为不变） |
| `converter.py:1542,1573` | 改为 `load_config()` 的结果 |
| `converter.py` 各处布尔开关 | 从 `load_config()` 取 |

**注意 `converter.py:84-86` 是模块级常量**（模块导入时求值），而 `load_config()` 读 `.env`
必须**早于**该模块导入。因此 `main()`/`create_app()` 必须在导入 converter 前调用
`config.load()`。若这个时序不好保证，就退一步：`config.py` 在**自身模块导入时**立即解析
`.env`（而不是等 `load()` 被调用），这样任何 `from admin import config` 都拿到已加载的值。
**采用后者**——更简单且无时序陷阱。

## 风险

- **`.env` 自动加载可能意外覆盖测试行为**：测试目录若有 `.env` 会串味。缓解：
  `.env` 只在文件存在时加载，且测试用 `WORKBUDDY_ENV_FILE=/dev/null` 或显式传参覆盖。
- **旧名告警刷屏**：只在首次读取时告警一次（模块级 `_warned` 集合）。

## 测试

- `.env` 解析：注释、空行、引号、`=` 两侧空格。
- 优先级：真实环境变量 **胜过** `.env`（关键语义）。
- 旧名兼容：设 `PORT=9000`，断言 `cfg.port == 9000` 且告警只出现一次。
- 校验：`PORT=abc` 与 `PORT=99999` 都在启动时报清晰错误。
- `PORT` 真的生效：`create_app` 后 `/health` 监听在配置端口（或断言传给 uvicorn 的参数）。

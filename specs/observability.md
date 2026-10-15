# 可观测性 Spec：日志分级 + 深度健康检查

> 状态：已实现（2026-10-15）。实现见 `core/runtime_logging.py` 与 `admin/server.py` 的 `/health`。
> 范围是**日志**与 **`/health`**，不含 `/metrics`（Prometheus 导出留待后续，当前用「深度 health + 结构化日志」已能覆盖自建服务的排障需求）。

## 背景

当前项目的可观测性是空白：

- **全项目只有一个 `print`**（`core/converter.py:403`），没有 `logging`。`converter.py` 里有
  一个自制的 `_log()`（`converter.py:474`），但它只在 `--log PATH` 显式开启时才写文件，
  未开启时**静默丢弃**——这是最糟的默认值：线上容器不传 `--log` 就等于没有任何输出。
- **两个入口都设了 `log_level="warning"`**（`converter.py:1605`、`server.py:1099`），
  把 uvicorn 自己的访问/错误日志压掉了。启动失败、端口占用、异常堆栈都看不到。
- **`/health` 只返回 `{"status":"ok"}`**（`server.py:564-566`），恒为 ok。它不看账号池是否
  为空、凭据是否全失效、上游是否可达，因此 Docker `healthcheck` 基本没有意义——
  容器「健康」但一个可用账号都没有时，健康检查仍然是绿的。

后果（真实发生过）：排查 Secure cookie 401、`usage_synced_at` 不落库等问题时，
`docker logs` 基本是空的，只能手工写脚本进容器查库。

## 目标

1. **默认即有日志**：不传 `--log` 也输出到 stdout/stderr，由容器运行时收集；传路径则额外落文件。
2. **分级**：`DEBUG/INFO/WARNING/ERROR`，默认 `INFO`，`--log-level` 或 `LOG_LEVEL` 覆盖。
3. **管住第三方噪音**：uvicorn/httpx 的默认级别单独控制，不被本项目的 `DEBUG` 带飞。
4. **`/health` 有深度**：区分「进程活着」与「真的能服务」。

## 非目标

- 不做 Prometheus `/metrics` 导出（内存计数器已存在，接出来是独立需求）。
- 不做日志聚合（Loki/ELK），只保证输出到标准流，交给容器/宿主机收集。
- 不改请求日志落库逻辑（`request_logs` 表已够用，属业务数据而非运行日志）。

## 设计

### 一、日志

**单一入口**：新增 `core/runtime_logging.py`，暴露 `setup_logging(level, path=None)`，
用标准库 `logging` 配置根 logger：

- 格式：`2026-10-15 07:49:27 [INFO] admin.pool: 消息`（时间用北京时间，与项目其它处一致）。
- handler：`StreamHandler(sys.stderr)` 恒有；`path` 非空时再加 `RotatingFileHandler`
  （单文件 10MB × 3 备份，防 `--log` 跑久了撑爆磁盘）。
- **幂等**：重复调用不叠加 handler（先清空自建 handler，`logging_setup` 自己持有标记）。
- 第三方降噪：`logging.getLogger("uvicorn.access").setLevel(WARNING)`、
  `httpx`/`httpcore` 设 `WARNING`；`uvicorn` 与 `uvicorn.error` 跟随本项目级别。

**改造点**：

| 位置 | 现状 | 改为 |
|---|---|---|
| `converter.py:_log()` | 只在 `log_path` 设了才写，否则丢弃 | 转发到 `logger.debug/info`，**恒输出**；`log_path` 只决定「是否额外落文件」 |
| `converter.py:403` | 裸 `print` | `logger.warning`（那是内容审核/异常分支） |
| `admin/pool.py` | 无日志 | 关键状态变迁记 `logger.info/warning`：账号选中、凭据刷新失败、故障转移、账号被暂停 |
| `converter.py:1605` | `uvicorn.run(..., log_level="warning")` | 用统一级别；`access_log` 可关闭（已有 `request_logs` 表，重复记无意义） |
| `server.py:1099` | 同上，且 host/port 硬编码 | 同上；host/port 从配置读（见 config spec） |

**打印的凭据必须脱敏**：现有的 `_log` 会打完整 SSE 原始报文，若原样进日志会泄敏。
新增的日志一律**不得**记录：API key 明文、凭据文件内容、完整 prompt/response。
`_log` 原有的「完整原始 SSE」走 `DEBUG` 级，且默认不落 stdout。

**级别约定**：

- `ERROR`：请求最终失败、上游 5xx 且无账号可退、落库失败
- `WARNING`：凭据失效、账号被暂停、节流命中、健康检查降级
- `INFO`：启动/关闭、账号选中与故障转移、配置摘要（不含敏感值）
- `DEBUG`：完整请求/响应报文、上游原始 SSE

### 二、深度 `/health`

返回结构：

```json
{
  "status": "ok",              // ok | degraded | down
  "uptime_s": 3600,
  "accounts": {"total": 3, "enabled": 3, "usable": 2, "auth_invalid": 1},
  "last_upstream_ok": 1750000000,
  "checks": {"db": "ok", "accounts": "degraded"}
}
```

**判据**（`usable` = enabled 且凭据文件存在且未被标 `auth_invalid`）：

| 条件 | status | HTTP |
|---|---|---|
| 有 ≥1 个 usable 账号，且库可读 | `ok` | 200 |
| 库可读，但 usable = 0（全失效/全暂停） | `degraded` | 200 |
| 库不可读 / 状态无法判定 | `down` | 503 |

**关键决策：`degraded` 仍返回 200。** 理由——`degraded` 表示「服务在跑，但没有可用账号」，
把 HTTP 码设成 503 会让 Docker healthcheck 把容器标记为 unhealthy 并可能触发重启，
而重启**修不好**凭据失效（要人重新授权）。重启循环只会让问题更难观察。
`down`（库坏了，进程已无法正常工作）才用 503 触发重启。

排查便利：`?verbose=1` 时附带每个账号的 `id/name/enabled/usable/auth_invalid`，
便于脚本一键看病。默认不返回（避免未鉴权端点泄露账号名）。

**非目标**：不做「主动探测上游」——health 必须廉价且快，不能每次调用都打上游接口。

## 兼容性

- `--log` 语义**扩展而非破坏**：以前「不传=不记，传=记到文件」；现在「不传=输出到 stderr，
  传=额外落文件」。这会让原本安静的部署开始有输出——这是**期望的修复**，不是回归。
- `/health` 响应**加字段不改字段**：`status` 仍在，原有读 `status == "ok"` 的调用方
  （含 `deploy/` 里的探测脚本）行为不变。

## 测试

- `setup_logging` 幂等：调用两次不产生重复行。
- 级别过滤：`WARNING` 级下 `logger.info` 不输出。
- `/health`：注入造好的账号状态，断言 `ok`/`degraded`/`down` 三态与 HTTP 码。
- `/health` 不泄露：默认响应不含账号名。
- 凭据不落日志：用一个含敏感串的凭据跑一次失败路径，断言日志内容里没有该串。

## 实现后修正（评审发现）

- **认证失效判据必须与账号池一致**。初版查的是 `status == "auth_invalid"` 字符串，
  但生产代码标记认证失效用的是布尔字段 `auth_invalid`（`admin/pool.py:183`），
  该字符串从不被写入。后果是单个账号认证失效时 `usable` 仍算 1、健康检查报 `ok`——
  恰恰是最该被发现的故障反而不报。现与 `pool.py` 用完全相同的判据。
- **`degraded` 用 200 已按现网核实**。`deploy/*/docker-compose.yml` 的 healthcheck
  只看 HTTP 码，且 `restart: unless-stopped`——503 会触发重启循环，而重启修不好
  凭据失效（要人重新授权）。决策成立。
- **访问日志用 `uvicorn.run(..., access_log=False)` 关**，不是改 `uvicorn.access`
  的 level：`uvicorn.run()` 内部会 `dictConfig` 把它重置回去。
- 完整报文（请求体/响应体/原始 SSE/上游错误正文）一律走 `_log_debug`，默认
  `INFO` 级不输出——初版只降级了流式路径，非流式路径的 5 处仍在 `INFO`。

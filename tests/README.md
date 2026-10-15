# 测试目录约定

按**功能域**分目录，改动只跑对应目录；标记（marker）用于分层。目标是让日常自检
更快、更对症，而不是每次都全量回归。

## 目录

| 目录 | 对应源码 | 什么时候跑 |
|---|---|---|
| `tests/protocol/` | `core/`（三个 API 适配层、流式、后端路由） | 改 `core/` 下任何东西 |
| `tests/pool/` | `admin/pool.py`、`admin/guards.py`、`admin/rotation.py`、`admin/cost_ledger.py` | 改账号池 / 排队 / 护栏 / 退避 / 账本 |
| `tests/persistence/` | `admin/db.py`、`admin/backup.py` | 改 schema、迁移、备份恢复 |
| `tests/admin/` | `admin/server.py`、`admin/models.py`、`admin/metrics.py`、`admin/observability.py`、`admin/browser_login.py`、`admin/trae_login.py` | 改管理接口 / 模型目录 / 观测 |
| `tests/security/` | `core/workbuddy_atrest_crypto.py`、日志脱敏 | 改脱敏、加密登录态 |
| `tests/config/` | `admin/config.py`、版本标识 | 改 .env 解析、入口、部署布局 |
| `tests/unit/` | 纯函数（如 `core/system_identity.py`） | 改对应的纯函数 |
| `tests/frontend/` | `admin/static/app.js` | 改管理页前端（Node，不属于 pytest） |

## 分层标记

标记定义在 `pytest.ini`，登记在 `tests/conftest.py`：

- **`core`** — 必须守住的不变量（数据正确性、安全、资金/积分、迁移不丢数据、
  协议兼容、后端隔离）。当前**全部用例都是 core**。
- **`slow`** — 内部有固定等待/超时。**默认不跑**（`pytest.ini` 里
  `addopts = -m "not slow"`）。**不要**用它掩盖「本来不该慢」的测试：先尝试把等待
  压掉（见「写测试的几条硬规矩」），确实压不掉才标。当前没有用例使用这条标记——
  原本几处固定等待都已经被改成事件驱动或按需触发。
- **`e2e`** — 起整套 `create_app` + ASGI 客户端，走真实请求链路。
- **`network`** — 经由 mock 传输层模拟上游交互。
- **`real_catalog_check`** — 需要真实的 `admin/server.py::_stale_catalog`
  （不被 `conftest.py` 的 autouse 装置桩掉）。

标记写在**文件末尾**（`pytestmark = [...]`），因为同一文件的用例保护的是同一块
行为；某条实在不属于本文件默认域时，再单独给它加 `@pytest.mark.<域>`。

> **默认策略是「跑全部、只排除 slow」而不是「只跑 core」。** 后者会让任何**忘打
> 标记**的新测试被静默 deselect，而输出仍然是「N passed」全绿——一条永不执行的
> 测试比一条慢测试危险得多。反过来漏标记的代价只是多跑几秒。

### 常用命令

```bash
pytest                       # 全部（除 slow）
pytest tests/pool             # 只跑账号池这一域
pytest tests/pool/test_account_pool.py::PoolTests::test_queue_hit_switches_account_and_is_transparent
pytest -m e2e                 # 只跑端到端
pytest -m ""                  # 连 slow 一起跑
pytest --durations=10         # 看谁最慢
```

## 写测试的几条硬规矩

这些规矩是从实际踩坑里来的，不是风格偏好。

1. **不要拼 `parent.parent` 找项目根。** 项目根由 `tests/conftest.py` 统一放进
   `sys.path`，用 `from conftest import ROOT`。子目录里数 `parent` 会在目录一动时
   算错（本项目搬目录时就断过一片）。
2. **不要 import 兄弟测试模块里的类。** 跨文件共用的装置（`credential`、替身
   sleep）放 `tests/conftest.py`。
   **尤其注意**：`pytest.ini` 的 `python_classes = *Tests` 是拿类的 `__name__` 去
   匹配的，所以只要 `AdminTests` 出现在某模块的命名空间里（不管叫 `_base` 还是
   `admin_tests`），它那批用例就会在该模块里再被收集一遍、报告里跑两遍
   （本项目踩过：`tests/admin/test_browser_login.py` 被收集成 45 条，真实只有 25 条）。
   要复用别的测试文件里的基类，请 `import test_server` 然后写
   `test_server.AdminTests.asyncSetUp = ...`，不要把类绑到本模块的全局名上。
3. **别让测试真等。** 生产路径里的固定等待（换号退避 `backoff_after`、签到错峰
   `batch_spread`）在测试里要用 `tests/conftest.py` 的 `recorded_no_sleep` 替掉，
   它立即返回但**记录**请求的秒数，所以「等了多久」仍可断言。数值本身去测纯函数
   （如 `rotation.backoff_after`），不要端到端等。
4. **别让测试真连网。** `create_app()` 启动时会判断模型目录是否过期、过期就
   **同步**拉上游（`_stale_catalog` → `ModelRegistry.fetch`），内置目录恒被判为过期。
   这件事已由 `tests/conftest.py` 的 autouse 装置统一挡住（桩 `_stale_catalog`），
   所以用 `create_app` 的用例不需要各自处理。要测那个判断本身的用例加
   `@pytest.mark.real_catalog_check`。
   另外 `admin/pool.py` 的积分刷新/签到会真的打上游：写这类测试时用
   `client_factory` 注入 `httpx.MockTransport`（见 `tests/pool/test_account_pool.py`）。
5. **同步等待要有明确的上界与放行点。** 测「A 必须等 B」时，先让 B 挂住、再启
   A，并断言「A 在 B 放行前**没有**返回」——只断言最终结果的话，A 在 B 开始前就
   返回（测试空跑）也照样通过。范例见
   `tests/persistence/test_database.py::test_close_waits_for_inflight_transaction`。
   反过来，别写成「另起线程做事、主线程 `wait(5)`」——一旦真卡住，主线程会先把
   那 5 秒耗光，测试才失败。要在**子线程里 join(timeout)**，主线程立刻拿到结论。
6. **回归测试一条都不要删。** docstring 里写了「回归：」「issue #NN」的用例对应
   真实出过的事故，是这套测试里价值最高的部分。

## 前端脚本

```bash
node tests/frontend/fmttokens.test.mjs      # 积分/token 的整数展示口径
node tests/frontend/modelgroup.test.mjs     # 同名模型跨后端的合并分组
node tests/frontend/rendermodels.test.mjs   # 用真实 overview 数据渲染模型表
```

`rendermodels.test.mjs` 读一份 state.db（默认 `/tmp/opencode/v7fresh`，可用
`WB_TEST_DB` 指定路径/目录、`WB_TEST_PYTHON` 指定解释器）；找不到就跳过。
这三个脚本靠从 `admin/static/app.js` 里**切出源码片段**来求值，所以重构 app.js
顶部的 `creditsFmt` / `backend*` / `groupModelsByName` / `renderModelGroup` 时，
要确认脚本仍能切出这些片段（切不出会报错，不会静默跳过）。

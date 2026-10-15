# 数据库备份与恢复

管理页「设置」页提供 `state.db` 的备份、下载、删除与恢复。默认**关闭自动备份**，
需要时手动点「立即备份」，或打开自动备份并设间隔。

## 为什么不是 `cp state.db`

库是 WAL 模式（`db.py` 里 `PRAGMA journal_mode=WAL`）：已提交的事务可能还在
`state.db-wal` 里，没合并回主库。直接拷主库会得到「旧到某次 checkpoint 为止」的
快照；单独丢掉的 `-wal` 里那部分已提交数据就永久找不回来了。

所以备份统一走 sqlite3 的在线备份 API（`Connection.backup`，`admin/backup.py` 的
`_snapshot`）：逐页复制并跟随写入者进度，**不阻塞业务读写**，产出的单文件本身就是
一致快照，不需要配套的 `-wal`。

`tests/persistence/test_backup.py::test_backup_captures_wal_data` 同时验证了两件事：备份里有
WAL 中未 checkpoint 的数据，而同一时刻「直接拷主库」确实没有——这条路径是被真的
测到的，不是靠推断。

## 文件与命名

- 目录：`<数据目录>/backups/`（与 `state.db` 同级，随数据卷一起持久化）。
- 文件名：`state-<YYYYMMDD-HHMMSS>.db`，**北京时间**。`backup.py` 里显式用
  `timezone(timedelta(hours=8))` 而不是依赖容器 `TZ`——脚本和测试里 `TZ` 未必设，
  靠环境变量会出现「界面按北京时间、文件名按 UTC」的错位。
- 同一秒内重复备份：追加毫秒后缀 `state-<ts>-<mmm>.db`。
  后缀必须加在扩展名**之前**，因为 `NAME_RE` 是 `.db$` 锚定的；加在末尾会让名字
  不再被 `list_backups()` 认出来，备份生成了却看不见（等于静默丢失）。

## 名白名单（防路径穿越）

`safe_path()` 只放行 `NAME_RE` 命中的名字，不做「过滤 `..` 和 `/`」的黑名单——
`state-20260101-000000.db/../../etc/passwd` 这种带合法前缀的拼接，黑名单很容易漏。
所有对外接口（下载/删除/恢复）都先过 `resolve()`，非法或不存在一律 404。

## 完整性校验

- `verify()`：能打开 + `PRAGMA integrity_check` 通过 + 核心表（`accounts`/`keys`/`settings`）在。
- `create()` 在改名落盘**之前**就调用 `verify()`：产出空库比不备份更危险，恢复时会
  得到「成功但全空」的结果。
- 注意「比字节数」不够：SQLite 建空库也会被撑到整页大小（≥4096 字节），正好卡在
  下界上会被当成合格。所以大小检查只用来挡「明显不是数据库」的输入，内容判定交给 `verify()`。
- `MAX_BYTES`（512 MB）挡体积异常。

## 保留策略

每次成功备份后 `prune()`，只留最近 `backup_keep` 份（默认 10，可设 1-100）。
中途失败留下的 `.tmp` 不会被列表计入、不占保留位，也不会被误认为备份。

## 定时备份

- 由 `lifespan` 里的 `backup_worker` 驱动，每轮只判断「该不该跑」，间隔由
  `backup_interval_hours`（1-720）决定。
- 检查粒度 `TICK_SECONDS = 300`：改设置后最多等 5 分钟生效，又不必每秒重算。
- `backup_last_at` **必须落库**：进程重启不能重置计时，否则会变成「每次重启都备份」。
- 单轮失败不退出循环（磁盘一时写满不该让自动备份永久停摆），记异常后按 tick 重试。

## 恢复

破坏性操作，分四步，每步都刻意留着可续跑：

1. **先验证**：备份不健康直接 400，不碰线上库（`test_restore_rejects_corrupt_backup`）。
2. **先自保**：给当前库打一份 `pre-restore` 备份；这步失败就中止——否则等于把唯一
   一份好数据换掉。
3. **原子替换**：`store.db.close()`（先 checkpoint 合并 WAL）→ 文件写成
   `restore-pending.db` → `os.replace` 替换 `state.db` → 清掉旧库残留的 `-wal`/`-shm`。
   不清理 `-wal`/`-shm` 的话，新库会被当成「WAL 里还有未合并帧」，把上一份数据读回来。
4. **重启进程**：`os._exit(0)`，由容器 `restart: unless-stopped` 拉起。

**中断续跑**：进程若在第 3、4 步之间挂掉，`state.db` 可能已被替换、也可能还是旧的，
而连接已关闭。所以替换前先把文件放到固定中转名 `restore-pending.db`；启动时
（`create_app` 里，在建 `Store` **之前**）发现它就说明上次没走完，直接补上。
宁可多恢复一次，也不能让半截状态留在线上跑。

**恢复窗口内的请求**：`store.restoring` 置位后，`AdminMiddleware` 对所有 `/admin`
请求返回 503「数据库正在恢复…」。没有这道闸时，请求会撞在已关闭的 sqlite 连接上抛
`ProgrammingError`（用户看到一屏堆栈），而正确行为是让他稍后刷新。
站位刻意放在关库**之后**——放前面会让恢复请求本身撞上 503。

恢复会丢掉内存里的管理会话（`os._exit` 清空 `sessions`），页面需要重新登录，
前端 `awaitRestart()` 会自动轮询等新进程接管。

## 不包含的东西

备份**只有 `state.db`**，不含 `data/auth/` 下的 `.info` 凭据文件。换机器重建时，
凭据需要另行迁移（或用「添加账号 → 导入文件」重新导入）。
这样选是因为凭据文件是明文 JSON、体积小且可重新获取，而账号/密钥/用量统计这些
「攒出来的」数据才是真正需要防丢的部分。

## 相关文件

| 文件 | 职责 |
|---|---|
| `admin/backup.py` | 模块级实现：在线备份、命名/白名单、校验、保留、定时判定、恢复中转 |
| `admin/db.py` | `settings` 表结构（v5 加 `backup_*` 五列）与迁移 |
| `admin/server.py` | `Store.backup_now` / `backup_list`、四个备份接口、`backup_worker`、启动续跑 |
| `admin/static/index.html` / `app.js` / `style.css` | 设置页的备份卡片与文件列表 |
| `tests/persistence/test_backup.py` | 28 个用例：模块正确性 + 接口 + 恢复守卫 |
| `scripts/e2e_backup.py` | 手动端到端脚本：起真进程走一遍备份 → 改数据 → 恢复 |

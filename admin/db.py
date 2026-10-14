"""SQLite 持久层。

替代早期的 state.json 单文件存储：管理数据（账号、密钥、账号池设置、模型注册表、
账号状态、会话绑定）全部落在 SQLite。

设计要点：
- `store.data`（内存 dict）保持原有结构不变，写操作后由 Store.save() 全量落库；
  数据量很小（账号 ≤100、密钥 ≤100），全量重写足够快。
- 首次启动时若存在 state.json 而数据库为空，自动导入旧数据并保留 .bak 备份。
- 所有写操作走同一个连接（单进程多线程），用显式事务保证整份状态原子生效。
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# 需要持久化的顶层键 -> 说明。accounts/keys 有独立表，其余走 kv 表。
KV_KEYS = ("active", "pool", "model_registry", "account_status", "session_bindings")


def _json_default(value):
    raise TypeError(f"不可序列化的状态值：{type(value).__name__}")


class Database:
    """SQLite 封装：单写连接 + 每线程读连接，自动建表与旧数据迁移。"""

    # 逐条执行而不用 executescript()：后者会先隐式 COMMIT，
    # 把外层显式事务直接结束掉，导致随后 COMMIT 报 no transaction is active。
    SCHEMA = (
        """CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, file TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS keys (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL,
            hint TEXT NOT NULL, created INTEGER NOT NULL DEFAULT 0,
            daily_limit INTEGER, usage TEXT NOT NULL DEFAULT '{}',
            models TEXT NOT NULL DEFAULT '[]', accounts TEXT NOT NULL DEFAULT '[]')""",
        """CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    )

    def __init__(self, path, legacy_json=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._local = threading.local()
        self._lock = threading.RLock()  # 串行化写事务，防多线程同时 BEGIN
        self._depth = 0  # 当前事务嵌套深度：区分「我开的嵌套」与「残留的野事务」
        self._closed = False
        self._write = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                      check_same_thread=False)
        self._write.execute("PRAGMA journal_mode=WAL")
        self._write.execute("PRAGMA synchronous=NORMAL")
        self._write.execute("PRAGMA busy_timeout=30000")
        with self.transaction():
            for statement in self.SCHEMA:
                self._write.execute(statement)
            self._write.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
                                (str(SCHEMA_VERSION),))
        # 空库 + 存在旧 state.json：导入后把旧文件备份，避免误当新数据处理。
        if legacy_json and Path(legacy_json).exists() and not self.query("SELECT 1 FROM accounts LIMIT 1"):
            self._import_legacy(legacy_json)

    # -- connections --------------------------------------------------------
    def _reader(self):
        conn = getattr(self._local, "reader", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
            self._local.reader = conn
        return conn

    @contextmanager
    def transaction(self):
        """真正的多语句事务。

        连接是 isolation_level=None（autocommit），此时 ``with conn:`` 不会开事务——
        每条语句各自提交，块内抛异常也回滚不了任何东西。所以多语句写入必须显式
        BEGIN IMMEDIATE，否则 save() 里 DELETE 与 INSERT 之间崩溃会把整表清空。

        可重入：嵌套调用时直接让出，由最外层负责提交/回滚。
        这样 Store.save() 能把整份状态包进一个事务，而内部的
        replace_accounts / replace_keys / set_kv 无需关心自己被嵌套调用。

        但「嵌套」必须由 _depth 记账，不能只看 in_transaction：后者为 True 时
        也可能是上一轮回滚失败留下的未决事务。那种情况下直接让出会让之后每次
        save() 都变成 no-op——调用方一切正常、数据全部静默丢失。所以记录在案的
        嵌套才让出，对不上的未决事务一律先回滚再报错。
        """
        with self._lock:
            if self._depth > 0:
                # 记录在案的嵌套：加入外层事务，提交/回滚交给最外层。
                self._depth += 1
                try:
                    yield self._write
                finally:
                    self._depth -= 1
                return
            if self._closed:
                # 已关闭：后续写入一律明确失败，不能静默丢数据。
                raise RuntimeError("数据库已关闭，忽略本次写入")
            if self._write.in_transaction:
                # 没有嵌套记录却有未决事务：说明上次回滚失败留下了残局。
                # 清掉它并把问题暴露出来，绝不能静默继续写。
                self._rollback()
                raise RuntimeError("数据库连接上残留未决事务，已强制回滚；请检查磁盘与日志")
            self._write.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._write
                self._write.execute("COMMIT")
            except BaseException:
                # yield 块内抛异常、COMMIT 失败（磁盘满/IO 错误）都走这里：
                # 不回滚的话事务会留在连接上，之后每次 BEGIN 都是
                # cannot start a transaction within a transaction。
                self._rollback()
                raise
            finally:
                self._depth = 0

    def _rollback(self):
        """回滚当前事务；事务已结束时 SQLite 报 no transaction active，忽略即可。"""
        try:
            self._write.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def query(self, sql, params=()):
        """读操作。"""
        return self._reader().execute(sql, params).fetchall()

    def close(self):
        if self._closed:
            return
        self._closed = True
        # 取写锁：lifespan 关闭时 cancel() 只取消 await 点，正在 to_thread 里
        # save() 的池线程不会被中断。若不等待它，_write.close() 会当场打断那个
        # 事务，那一次保存直接回滚丢失。加锁保证 close() 排在其后。
        #
        # _closed 已在取锁前置位，等待期间到达的写入会在 transaction() 里
        # 直接失败，不会跟着堆积；真正在跑的那一个事务跑完即释放锁。
        with self._lock:
            # 关闭前把 WAL 合并进主库，保证 -wal 里未落盘的帧不丢。
            self.checkpoint()
            try:
                self._write.close()
            except sqlite3.Error:
                pass
        reader = getattr(self._local, "reader", None)
        if reader is not None:
            try:
                reader.close()
            except sqlite3.Error:
                pass

    def checkpoint(self):
        """把 WAL 合并回主库并截断（关闭时调用）。

        必须在事务外执行：PRAGMA wal_checkpoint 在 BEGIN IMMEDIATE 里会因
        「database table is locked」直接失败，WAL 一点都截不掉。
        取写锁保证不会插在别人事务的中间。
        """
        with self._lock:
            try:
                self._write.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    # -- legacy import ------------------------------------------------------
    def _import_legacy(self, legacy_json):
        try:
            legacy = json.loads(Path(legacy_json).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if not isinstance(legacy, dict):
            return
        with self.transaction():
            self.replace_accounts(legacy.get("accounts") or {})
            self.replace_keys(legacy.get("keys") or {})
            for key in KV_KEYS:
                if key in legacy:
                    self._write.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                                        (key, json.dumps(legacy[key], ensure_ascii=False,
                                                         default=_json_default)))
        try:
            backup = Path(legacy_json).with_suffix(".json.bak")
            Path(legacy_json).replace(backup)
        except OSError:
            pass

    # -- accounts / keys ----------------------------------------------------
    def replace_accounts(self, accounts):
        """全量同步内存中的账号表（增删改都以内存为准）。数据量 ≤100，全量重写最简单可靠。"""
        with self.transaction():
            self._write.execute("DELETE FROM accounts")
            self._write.executemany("INSERT INTO accounts (id, name, file, enabled, created) VALUES (?,?,?,?,?)",
                                    [(aid, str(item.get("name", ""))[:60], str(item.get("file", "")),
                                      int(bool(item.get("enabled", True))), int(item.get("created", 0) or 0))
                                     for aid, item in accounts.items()])

    def replace_keys(self, keys):
        with self.transaction():
            self._write.execute("DELETE FROM keys")
            self._write.executemany("""INSERT INTO keys (id, name, hash, hint, created, daily_limit, usage, models, accounts)
                                       VALUES (?,?,?,?,?,?,?,?,?)""",
                                    [(kid, str(item.get("name", ""))[:60], item.get("hash", ""), item.get("hint", ""),
                                      int(item.get("created", 0) or 0), item.get("daily_limit"),
                                      json.dumps(item.get("usage") or {}, ensure_ascii=False),
                                      json.dumps(item.get("models") or [], ensure_ascii=False),
                                      json.dumps(item.get("accounts") or [], ensure_ascii=False))
                                     for kid, item in keys.items()])

    # -- kv -----------------------------------------------------------------
    def set_kv(self, key, value):
        with self.transaction():
            self._write.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                                (key, json.dumps(value, ensure_ascii=False, default=_json_default)))

    def get_kv(self, key, default=None):
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0][0])
        except (ValueError, IndexError):
            return default

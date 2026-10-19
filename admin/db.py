"""SQLite 持久层。

管理数据（账号、密钥、池设置、模型目录、请求日志、上游用量）全部落在 SQLite，
没有通用 kv 表。

设计要点：
- `store.data`（内存 dict）保持原有结构不变，写操作后由 Store.save() 全量落库；
  数据量很小（账号 ≤100、密钥 ≤100），全量重写足够快。
- 金额只在这层换算：落库 ×1e6 取整成 `*_micro`，读库 ÷1e6 还原成内存里的 float。
  内存里始终是 float，`state()` 的比较和速率计算都依赖它。
- 迁移走版本化 MIGRATIONS 列表（元素是 (目标版本, 迁移函数)），所有待执行迁移合并在
  同一个事务里：任一步失败整体回滚且不写版本号，下次启动从头重跑。分步提交会出现
  「step1 已提交、step2 失败」，届时版本号还是旧值，下次会重跑 step1 而非续跑。
- 所有写操作走同一个连接（单进程多线程），用显式事务保证整份状态原子生效。
"""
import json
import math
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .models import CN


# -- schema migrations -------------------------------------------------------


def _v2_to_v3(db):
    """v2 → v3：给 accounts 补 usage_manual_synced_at 列，并补 key_usage_daily 索引。

    只做「补结构」，不动数据。补列必须按 PRAGMA table_info 判断后再 ALTER——
    SCHEMA 里的 CREATE TABLE IF NOT EXISTS 对已存在的表不会补列，老库升上来
    会缺列，而 save_accounts 只写白名单列，缺列时该字段静默丢失。
    """
    # 必须用写连接查 PRAGMA：db.query 是只读连接，看不到本事务内未提交的
    # DDL，会把新列判成缺失，接着 ALTER 报 duplicate column。
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(accounts)")}
    if "usage_manual_synced_at" not in columns:
        db._write.execute("ALTER TABLE accounts ADD COLUMN usage_manual_synced_at INTEGER")
    # key_usage_daily 在 SCHEMA 里用 IF NOT EXISTS，老库也会被建出来，这里只补索引。
    db._write.execute(
        "CREATE INDEX IF NOT EXISTS idx_key_usage_day ON key_usage_daily(day)")


def _v3_to_v4(db):
    """v3 → v4：加容量队列字段（accounts.queued_until、settings 的 queue_* 六项）。

    同样只补结构不动数据。settings 是单行表，加列必须 ALTER 且带默认值，
    否则已存在的行会读出 NULL，而 POOL_DEFAULTS 的合并发生在 Python 侧、
    只补字典里**缺的键**——列存在但为 NULL 时它不认为是缺键。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(accounts)")}
    if "queued_until" not in columns:
        db._write.execute("ALTER TABLE accounts ADD COLUMN queued_until INTEGER")
    settings_columns = {row[1] for row in db._write.execute("PRAGMA table_info(settings)")}
    for name, ddl in (("queue_failover", "INTEGER NOT NULL DEFAULT 0"),
                      ("queue_retry", "INTEGER NOT NULL DEFAULT 3"),
                      ("queue_cooldown", "INTEGER NOT NULL DEFAULT 120"),
                      ("queue_hold_ms", "INTEGER NOT NULL DEFAULT 3000"),
                      ("queue_model_fallback", "INTEGER NOT NULL DEFAULT 1"),
                      ("queue_fallback_model", "TEXT NOT NULL DEFAULT 'fast-model'")):
        if name not in settings_columns:
            db._write.execute(f"ALTER TABLE settings ADD COLUMN {name} {ddl}")


def _v4_to_v5(db):
    """v4 → v5：加数据库备份设置（settings 的 backup_* 五项）。

    和 v3→v4 同理：settings 是单行表，加列必须 ALTER 且带默认值，否则已存在
    的那行读出 NULL，而 POOL_DEFAULTS 的合并只补「字典里缺的键」——列在但值为
    NULL 时不算缺键，None 会一路传到 int() 上。

    默认**关闭自动备份**：定时备份会持续占用磁盘，什么时候开、开多久由用户定，
    不能因为升级就默认替人做决定。``backup_last_at`` 记录上次成功时间，
    是定时器判断「该不该跑」的唯一依据，必须落库（进程重启不能重置计时）。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(settings)")}
    for name, ddl in (("backup_enabled", "INTEGER NOT NULL DEFAULT 0"),
                      ("backup_interval_hours", "INTEGER NOT NULL DEFAULT 24"),
                      ("backup_keep", "INTEGER NOT NULL DEFAULT 10"),
                      ("backup_last_at", "INTEGER NOT NULL DEFAULT 0"),
                      ("backup_last_name", "TEXT NOT NULL DEFAULT ''")):
        if name not in columns:
            db._write.execute(f"ALTER TABLE settings ADD COLUMN {name} {ddl}")


def _v5_to_v6(db):
    """v5 → v6：账号级限额护栏（settings 的 guard_* 四项）+ 账号日用量表。

    护栏阈值默认 **0 = 关闭**。这是刻意选择：护栏是「替用户做决定」的功能，
    默认值必须是无为，否则升级就会悄悄改变线上行为、甚至把账号池停掉。
    和 v3→v4、v4→v5 同样的理由，加列必须带 NOT NULL DEFAULT，否则已存在
    的那行读出 NULL——而 POOL_DEFAULTS 的合并只补「字典里缺的键」，列在但
    值为 NULL 时不算缺键，None 会一路传到 int() 上。

    同时新建 ``account_usage_daily``：护栏要问「这个账号今天用了多少」，
    而 request_logs 只有状态与耗时、没有 token/credit 列，聚合不出来。
    按 (账号, 天, 模型) 存，正好三种查询都能一次覆盖。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(settings)")}
    for name, ddl in (("guard_reserve_credits", "INTEGER NOT NULL DEFAULT 0"),
                      ("guard_daily_token_limit", "INTEGER NOT NULL DEFAULT 0"),
                      ("guard_daily_credit_limit", "INTEGER NOT NULL DEFAULT 0"),
                      ("guard_model_daily_token_limit", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            db._write.execute(f"ALTER TABLE settings ADD COLUMN {name} {ddl}")
    db._write.execute(
        """CREATE TABLE IF NOT EXISTS account_usage_daily (
            account_id TEXT NOT NULL, day TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
            credits_micro INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_id, day, model))""")
    db._write.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_usage_day ON account_usage_daily(day)")


def _v6_to_v7(db):
    """v6 → v7：给 accounts 补 backend 列（Trae 第二后端合并引入）。

    与 _v2_to_v3 同样的理由：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
    必须 PRAGMA 判断后 ALTER。默认值 'workbuddy' 让历史账号全部落到 WorkBuddy 后端。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(accounts)")}
    if "backend" not in columns:
        db._write.execute(
            "ALTER TABLE accounts ADD COLUMN backend TEXT NOT NULL DEFAULT 'workbuddy'")


def _v7_to_v8(db):
    """v7 → v8：给 request_logs 补 backend 列（最近请求要能区分 trae / workbuddy）。

    与 _v6_to_v7 同样的理由：CREATE TABLE IF NOT EXISTS 不给已存在的表补列，
    必须 PRAGMA 判断后 ALTER。

    历史行回填而非留空：**已有记录**理应能看出走的是哪条链路，而账号的 backend
    是稳定属性（一个 aid 不会在不同后端间迁移），所以按 account_id 关联 accounts
    回填是准确的。关联不到的（账号已删除）保持 NULL，前端显示「—」。

    默认值给 NULL 而不是 'workbuddy'：writes 时一律显式写 backend，默认值只影响
    回填不到的旧行；把它们一律标成 workbuddy 会造成**假信息**。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(request_logs)")}
    if "backend" not in columns:
        db._write.execute("ALTER TABLE request_logs ADD COLUMN backend TEXT")
        db._write.execute(
            """UPDATE request_logs SET backend = (
                   SELECT accounts.backend FROM accounts
                   WHERE accounts.id = request_logs.account_id)
               WHERE account_id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM accounts WHERE accounts.id = request_logs.account_id)""")


def _v8_to_v9(db):
    """v8 → v9：给 models 补 synced_at（每个后端的「上次拉取时刻」）。

    为什么要单独一列而不是继续用 settings.models_updated：settings 是一行固定列
    的表（SETTINGS_COLUMNS），后端数量是可变的，没法为每个后端各留一列。把
    「这份目录什么时候同步的」放在**目录行自己**身上，加后端时不用动 schema。

    回填：用 settings.models_updated 填已存在的行 —— 对默认后端那是**准确**的
    （历史上只有它一份目录）；其它后端的行填 0（「从没记过」），下次拉取就会写上
    真实时刻，语义是「未知」而不是「很久以前」。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(models)")}
    if "synced_at" not in columns:
        db._write.execute("ALTER TABLE models ADD COLUMN synced_at INTEGER NOT NULL DEFAULT 0")
        # settings 是**一行宽表**（id=1 + 各字段一列），不是 key/value 表。
        rows = db._write.execute("SELECT models_updated FROM settings WHERE id=1").fetchone()
        updated = 0
        if rows and rows[0] is not None:
            try:
                updated = int(rows[0])
            except (TypeError, ValueError):
                updated = 0
        if updated:
            # 带前缀的行（其它后端）历史上没有独立的同步时刻，保持 0（语义为「未知」）。
            db._write.execute("UPDATE models SET synced_at=? WHERE model_id NOT LIKE '%/%'",
                              (updated,))


def _v9_to_v10(db):
    """v9 → v10：给 settings 加 backend_enabled（设置页的后端总闸）。

    **JSON 列而不是新表**（specs/backend-toggle.md §3）：settings 本来就是
    「一行固定列 + 整行重写」，JSON 读改写零额外成本；后端数量是可变的，
    按后端伸缩的东西装不进固定列结构。加新后端**零迁移**（缺键即启用）。

    与 v9 的教训不冲突：v9 踩的是「运行时动态发明列名」（models_updated_<backend>），
    这里列名固定、内容按后端伸缩，从头到尾不会触发那类错误。
    无回填：列默认 '{}' 即「全部启用」，语义与升级前完全一致。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(settings)")}
    if "backend_enabled" not in columns:
        db._write.execute("ALTER TABLE settings ADD COLUMN backend_enabled TEXT NOT NULL DEFAULT '{}'")


def _v10_to_v11(db):
    """v10 → v11：给 keys 补 last_used_at（specs/keys-last-used-at.md）。

    可空、无回填：历史调用没有逐 key 留时刻（``key_usage_daily`` 只有按天聚合，
    精度到天，答不了「现在还在用吗」），回填不出来。NULL 即「从未使用」。

    写入零额外开销——``keys_dirty`` 本来就因 ``usage.count`` 每次请求都置位，
    新增这个 int 走的是同一条 5 秒防抖 flush，不增加任何落库次数。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(keys)")}
    if "last_used_at" not in columns:
        db._write.execute("ALTER TABLE keys ADD COLUMN last_used_at INTEGER")


def _v11_to_v12(db):
    """v11 → v12：给 models 补 backend（模型归属显式化）。

    specs/model-backend-attribution.md §3.1：`persist()` 里 backend 是循环变量
    （写盘时已知），今天却拼进 `model_id` 再反解回来。列把已知事实直接存下来。

    **回填按前缀推导**，且只做这一次 —— 之后列即权威，读路径不再解析字符串。
    回填用 `db._write`（迁移运行在事务里）而不是 `query()`。

    为什么默认值是 'workbuddy' 而不是空串：与 `accounts.backend` 同一口径
    （那张表也这么干，有先例）。前缀推导对无前缀行给出的正是默认后端，两者一致。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(models)")}
    if "backend" not in columns:
        db._write.execute("ALTER TABLE models ADD COLUMN backend TEXT NOT NULL DEFAULT 'workbuddy'")
    # 历史行按前缀回填：带 trae/ 前缀的归 trae，其余归默认后端。
    # 只回填**前缀能表达**的行；pinned_models（三档）本来就在默认后端名下，无需特判。
    from core.backends.registry import default_name, known_names, spec_for

    default = default_name()
    for name in known_names():
        prefix = spec_for(name).model_prefix
        if prefix and name != default:
            db._write.execute(
                "UPDATE models SET backend=? WHERE model_id LIKE ? AND backend=?",
                (name, f"{prefix}%", "workbuddy" if default == "workbuddy" else default),
            )


def _v12_to_v13(db):
    """v12 → v13：给 models 补 deleted（删除的持久化标记）。

    **为什么是单独一个迁移**：它原本被写进 `_v11_to_v12` 里（那次提交还没发布），
    但任何已经跑过 v12 的库（本地测试库、dev）版本号已是 12，迁移不会再执行，
    于是 `load_models` 查一个不存在的列而报 `no such column: deleted`。
    编辑**已应用过**的迁移是本项目明令避免的事 —— 这里就是它咬人的样子。
    拆成独立版本号后，无论 v12 是哪个版本的 `_v11_to_v12` 写的，这条都会跑到。

    语义：`deleted=1` 的行只为 request_logs 的 join 留在表里，`load_models`
    不再返回。**不能**用 `available=0` 代替 —— 那个语义归「上游没返回的自定义
    模型要保留」所有（见 `test_custom_model_loads_when_available_zero`），
    混用会让「删除」与「保留」互相打架（实测：删除重启后复活）。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(models)")}
    if "deleted" not in columns:
        db._write.execute("ALTER TABLE models ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")


def _v13_to_v14(db):
    """v13 → v14：给 models 补 canonical（跨后端规范名，见 specs/model-routing-by-policy.md §6.1）。

    **为什么需要独立一列**：`model_id` 在两边天然不等（腾讯 `kimi-k3-1` vs 字节
    `trae/kimi-k3`），`name` 是展示文本（用户可改名、两边写法可能不同），两者都
    不能当「同一个模型」的判据。没有这一列，跨后端同名永远合不起来，
    「用户只看到一个模型、系统按策略选后端」也就无从谈起。

    **可空是设计**：空 = 没有规范名可选（正常写入路径不会产生这种行，
    见下）。

    **迁移不回填、由下次目录同步写入**：规范名要靠后端声明现算，而迁移代码不该
    依赖 registry（迁移是「历史数据修复」，不该因将来加了后端而改变结果）。
    回填的代价只是「升级后到第一次同步之间，跨后端的同名行暂时显示成两行」——
    可接受，且比在迁移里粘业务规则安全。

    注意写入点（`save_models`）**不会**写空：它对每一行都调 `canonical_name()`
    现算，所以空值只可能出现在迁移后、同步前的窗口里。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(models)")}
    if "canonical" not in columns:
        db._write.execute("ALTER TABLE models ADD COLUMN canonical TEXT")


def _v14_to_v15(db):
    """v14 → v15：settings 补裸名路由的两列（specs/model-routing-by-policy.md §5/§9.4）。

    - ``model_backend_order``：用户调序（JSON 列表）。**NULL = 未配置** = 用注册表
      顺序。空列表与 NULL 同义（`_validate_backend_order` 把 [] 归一成 None）。
    - ``bare_name_routing``：裸名自动选后端的 kill switch（§8 阶段三）。
      **默认 1 = 开**（用户拍板，照 §8 原文）。

    **为什么默认开**：与 `queue_failover`（默认关）的差异是**有意**的——那个会
    **重放整个请求**，失败代价是重复计费；裸名路由只是「选哪个后端」，选错的最大
    后果是走到另一个同样能服务该模型的后端。影响面性质不同，默认值也就不同。

    加的是**列**而不是 JSON 里的一个键：这两项有固定形状（一个列表、一个布尔），
    不像 `backend_enabled` 那样随后端数量变化，塞进 JSON 只会丢掉「NULL = 未配置」
    这个有用的语义（JSON 里区分「缺键」与「显式空」要额外约定）。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(settings)")}
    if "model_backend_order" not in columns:
        # 可空是设计：NULL = 跟随注册表顺序（用户从没调过序）。
        db._write.execute("ALTER TABLE settings ADD COLUMN model_backend_order TEXT")
    if "bare_name_routing" not in columns:
        db._write.execute(
            "ALTER TABLE settings ADD COLUMN bare_name_routing INTEGER NOT NULL DEFAULT 1")


def _v15_to_v16(db):
    """v15 → v16：模型表去前缀 + 联合主键（specs/remove-model-prefix-plan.md）。

    ## 为什么

    `trae/` 前缀是**我们自己拼的**：Trae 上游返回的是裸名（`kimi-k3`、
    `Doubao-Seed-Code`），`admin/models.py` 在写库与对外时拼上 `spec.model_prefix`
    造出了 `trae/kimi-k3`。用户明确：「永远都没有这个模型」。它同时还是主键、
    路由判据（`backend_name_for` 看字符串前缀）与账本键，所以要整条链移除。

    ## 两张表要改

    1. **`models`**：`model_id` 剥前缀 + 主键改 `(model_id, backend)`。
       SQLite 不能直接改主键，只能**重建表**（建新表 → 搬数据 → 删旧表 → 改名）。
    2. **`request_logs` / `upstream_request_usage` / `account_usage_daily`**：
       模型名一并剥前缀。它们是模型名的历史记录，只改 `models` 会让同一个名字
       在不同表里写法不一致，且用量页按模型查日志（`idx_logs_model_time`）会对不上。

    ## 冲突处理

    用户拍板「不会有冲突，有就舍弃保留一个」→ `INSERT OR REPLACE`，冲突留后一条。
    仍**统计并打日志**：静默丢几十行而没有任何痕迹，事后排查不出来。

    ## 剥前缀读声明，不写死 'trae/'

    遍历各后端声明的 `model_prefix`（`spec_for(name).model_prefix`），
    与全项目 arch guard 的口径一致 —— 加后端时这里不用改。

    ## 幂等

    `LIKE 'trae/%'` 只匹配**还带前缀**的行；重复跑不会重复剥
    （剥过的行不再匹配任何前缀）。
    """
    from core.backends.registry import known_names, spec_for

    #: 各后端声明的前缀（去掉空串）。**不写死 'trae/'**。
    prefixes = [p for p in (spec_for(n).model_prefix for n in known_names()) if p]

    def strip(column):
        """把一列里的前缀剥掉的 SQL 片段（多个前缀 → 嵌套 CASE）。"""
        expr = column
        for p in prefixes:
            expr = f"CASE WHEN {expr} LIKE ? THEN substr({expr}, ?) ELSE {expr} END"
        return expr

    # --- 1. models：剥前缀 + 联合主键（重建表）---------------------------
    cols = [r[1] for r in db._write.execute("PRAGMA table_info(models)")]
    if "backend" not in cols or "deleted" not in cols or "canonical" not in cols:
        # 前置列由 v12/v13/v14 加。跑到这里说明迁移链被跳过了 —— 显式报错
        # 好过重建出一张缺列的表（那会让后面所有读写路径炸在莫名的地方）。
        raise RuntimeError(
            f"v16 迁移要求 models 已有 backend/deleted/canonical 列，实际只有 {cols}")

    before = db._write.execute("SELECT COUNT(*) FROM models").fetchone()[0]

    db._write.execute("""CREATE TABLE models_v16 (
        model_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
        credits_micro INTEGER, max_input_tokens INTEGER,
        source TEXT NOT NULL DEFAULT 'builtin',
        available INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0, promo TEXT,
        synced_at INTEGER NOT NULL DEFAULT 0,
        backend TEXT NOT NULL DEFAULT 'workbuddy',
        deleted INTEGER NOT NULL DEFAULT 0,
        canonical TEXT,
        PRIMARY KEY (model_id, backend))""")

    # CASE 链的参数顺序：每层两个（LIKE 的模式、substr 的起点）。
    # **起点是 len(p) + 1**：SQLite 的 substr 是 1-indexed，
    # 用 len(p) 会少剥一个字符（'trae/kimi-k3' → '/kimi-k3'）。
    args = []
    for p in prefixes:
        args.extend([f"{p}%", len(p) + 1])
    db._write.execute(
        f"""INSERT OR REPLACE INTO models_v16
            SELECT {strip('model_id')}, name, description, credits_micro,
                   max_input_tokens, source, available, enabled, sort_order,
                   promo, synced_at, backend, deleted, canonical
            FROM models""",
        args)

    after = db._write.execute("SELECT COUNT(*) FROM models_v16").fetchone()[0]
    db._write.execute("DROP TABLE models")
    db._write.execute("ALTER TABLE models_v16 RENAME TO models")

    # --- 2. 日志/用量表：模型名一并剥前缀 --------------------------------
    # 列名不同（request_logs.model_id / 其余 model），分开处理。
    for table, column in (("request_logs", "model_id"),
                          ("upstream_request_usage", "model"),
                          ("account_usage_daily", "model")):
        if not db._write.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        db._write.execute(
            f"UPDATE {table} SET {column} = {strip(column)} WHERE {column} LIKE '%/%'",
            args)

    # 迁移痕迹：行数变化与冲突情况写进日志（不阻断迁移）。
    print(f"[migrate v16] models 去前缀：{before} 行 → {after} 行"
          f"{'（有主键冲突，已按「保留一个」处理）' if after < before else ''}",
          file=sys.stderr)


def _v16_to_v17(db):
    """v16 → v17：accounts 补 checkin_fails_date（签到失败熔断按天重置）。

    ## 为什么

    原设计是「连续失败 5 次即熔断」，而 `tick()` 的 due 过滤是
    `checkin_fails < 5`——触顶后**没有任何路径**把计数清零（只有签到成功
    才清零，但熔断的账号永远等不到下一次尝试）：账号一旦失败 5 次就被调度器
    永久跳过，自动签到事实上失效，只能手工改库。生产上真实发生过
    （Trae 账号连续撞上上游 9074 限流后卡死）。

    修法：`checkin_fails` 只按天计数。加一列 `checkin_fails_date` 记录计数
    归属的日期，`_checkin_fails_today()` 发现日期不是今天就当 0 —— 跨天自动
    解除熔断，当天内仍保留 5 次上限（避免一个坏账号把每小时都打满）。

    ## 不回填

    日期留空 = 历史计数作废（视为跨天）。这正是想要的效果：升级后所有已熔断
    的账号立刻恢复尝试，不需要人工逐个重置。
    """
    columns = {row[1] for row in db._write.execute("PRAGMA table_info(accounts)")}
    if "checkin_fails_date" not in columns:
        db._write.execute("ALTER TABLE accounts ADD COLUMN checkin_fails_date TEXT")


def _v19_to_v20(db):
    """v19 → v20：upstream_request_usage 补 input_trunc（上游请求内容摘要）。

    ## 为什么

    用户 2026-10-19 要求用量明细能看每次调用「请求了什么」。上游
    get-user-request-usage 每行本来就回 `inputTrunc`（请求内容摘要），
    只是 db-redesign 时出于隐私考虑没落库——用户明确要看，那就存。

    ## 取舍

    - **只存 `inputTrunc`，不存 `input`**：inputTrunc 是上游自己截过的摘要
      （长度有界），input 是完整正文，隐私面差一个量级。
    - 可空、存量 NULL：93 天窗口内的历史行没有这个字段，只有下次同步
      到的行才有。UPSERT 的 DO UPDATE 不动 input_trunc——避免一条 pending/0
      的旧记录把已有内容覆盖成空。
    - TEXT 列不设长度上限：截多少是上游的事，本地原样存，展示层再截断。

    与 agent_purpose 同属「可能含提示词片段」的自由文本，只做展示。
    """
    existing = {row[1] for row in db._write.execute("PRAGMA table_info(upstream_request_usage)")}
    if "input_trunc" not in existing:
        db._write.execute("ALTER TABLE upstream_request_usage ADD COLUMN input_trunc TEXT")


def _v18_to_v19(db):
    """v18 → v19：request_logs 补四项 token 用量（输入/输出/缓存读/缓存写）。

    ## 为什么

    用户 2026-10-19 要求请求日志能看每次请求的 token 明细。上游每次响应都带
    usage 对象（OpenAI 风格 prompt_tokens/completion_tokens + cache_*；Anthropic
    风格 input_tokens/output_tokens + 同 cache 字段），系统已在转发时抓到
    （pool.py `_track_usage`），只是没落库。

    ## 做法

    四个 INTEGER 列，缺省 NULL（上游不返回 usage 或请求失败后拿不到时就是空）。
    sqlite 动态类型：列声明 INTEGER，写 NULL 即可；绝不放字符串（参考 error_code
    被写成字符串的教训）。列名直接进 LOG_COLUMNS，append/recent/page 三个读函数
    都按 LOG_COLUMNS 取，自动生效。
    """
    existing = {row[1] for row in db._write.execute("PRAGMA table_info(request_logs)")}
    for column in ("tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_creation"):
        if column not in existing:
            db._write.execute(f"ALTER TABLE request_logs ADD COLUMN {column} INTEGER")


def _v17_to_v18(db):
    """v17 → v18：request_logs.account_name 按 account_id 对齐 accounts.name。

    ## 为什么

    `account_name` 是**写时快照**：请求发生时把当时的备注名一起写进日志
    （pool.py 切号处现取 `data["accounts"][aid]["name"]`）。改备注名只动
    accounts 表，不回溯历史行 —— 于是同一账号在请求日志里会出现新旧两个
    名字（用户 2026-10-17 实测：「和外面的 name 没有对齐了」）。

    ## 做法

    一次性 UPDATE 存量行：account_id 还能在 accounts 表里找到的，全部刷成
    当前备注名。已删除账号的行**不动**（account_id 已不在 accounts 里），
    保留它们最后已知的名字——账本记事实，那些请求确实是用那个号打的。

    ## 幂等

    `SET account_name=(SELECT name ...)` 每次都从 accounts 现取，重复执行
    结果相同。新写入的日志由 PATCH /admin/api/accounts/{aid} 的
    rename_account_logs() 实时同步，不需要再刷。
    """
    db._write.execute(
        "UPDATE request_logs SET account_name="
        "(SELECT name FROM accounts WHERE accounts.id=request_logs.account_id) "
        "WHERE account_id IN (SELECT id FROM accounts)")


SCHEMA_VERSION = 20


MICRO = 1_000_000

# 内存字段名 -> DB 列名（改名与换算只发生在这层，上层只见内存字段名）
ACCOUNT_MONEY_COLUMNS = {
    "remaining": "remaining_micro",
    "earliest_expiry_remaining": "earliest_remaining_micro",
    "expiring_soon": "expiring_soon_micro",
}

# accounts 表里由 account_status 提供的列（其余属于账号实体）。
# 金额列不在此列中——它们由 ACCOUNT_MONEY_COLUMNS 单独处理，避免重复建列。
ACCOUNT_STATUS_COLUMNS = (
    "weight", "packages", "earliest_expiry", "expiring_soon_updated",
    "credits_updated", "checkin_date", "checkin_fails", "checkin_fails_date", "auto_attempt",
    "checkin_slot",
    "checkin_when", "checkin_offset_date", "token_refreshed", "auth_invalid", "refresh_slot",
    "refresh_slot_date", "refresh_window_date", "consecutive_fails", "cooldown_until",
    "request_count", "last_request", "last_error", "usage_synced_at", "usage_sync_error",
    "usage_manual_synced_at", "queued_until",
)

ACCOUNT_STATUS_DEFAULTS = {
    "weight": 0, "checkin_fails": 0, "auth_invalid": 0, "consecutive_fails": 0, "request_count": 0,
}

# 这些列必须是整数，避免把 float/str 写进 INTEGER 列
ACCOUNT_INT_COLUMNS = frozenset(ACCOUNT_STATUS_COLUMNS) - {
    "checkin_date", "checkin_slot", "checkin_when", "checkin_offset_date",
    "refresh_slot", "refresh_slot_date", "last_error", "usage_sync_error",
}

KEY_COLUMNS = (
    "name", "hash", "hint", "created", "enabled", "revoked_at", "expires_at", "daily_limit",
    "usage_date", "usage_count", "usage_tokens", "models", "accounts", "routing",
    # 最后一次通过鉴权且未超配额的时刻（specs/keys-last-used-at.md §2）。
    # 可空：历史调用没有逐 key 留时刻，NULL 即「从未使用」。
    "last_used_at",
)

MODEL_COLUMNS = (
    "name", "description", "credits_micro", "max_input_tokens", "source", "available",
    "enabled", "sort_order", "promo", "synced_at", "backend", "deleted", "canonical",
)

LOG_COLUMNS = (
    "time", "source", "path", "status", "outcome", "duration_ms", "key_id", "account_id",
    "account_name", "model_id", "model_name", "upstream_request_id", "backend",
    "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_creation",
)

REQUEST_LOG_KEEP_DAYS = 93   # 3 个月
REQUEST_LOG_MAX_ROWS = 2_000_000
KEY_USAGE_KEEP_DAYS = 93     # 与 request_logs 一致

# 上游用量报表的分组维度 -> SQL 表达式。key 来自白名单，不接受用户输入，可安全拼接。
UPSTREAM_GROUPS = {
    "day": "date(request_time, 'unixepoch', '+8 hours')",
    "account": "COALESCE(account_id, '未知')",
    "model": "COALESCE(model, '未知')",
    "client": "COALESCE(NULLIF(client, ''), '未知')",
}


def _json_default(value):
    raise TypeError(f"不可序列化的状态值：{type(value).__name__}")


def _dumps(value, default=_json_default):
    return json.dumps(value, ensure_ascii=False, default=default)


def _to_micro(value):
    """积分 → 最小单位整数；None/非法值返回 None，避免把 0 和「未知」混为一谈。"""
    number = _to_float(value)
    return None if number is None else int(round(number * MICRO))


def _from_micro(value):
    return None if value is None else value / MICRO


def _require_backend(value, model_id):
    """模型归属列的值：缺值即报错，不兜默认。

    ``models.backend`` 是「写入时已知的事实」（见 specs/model-backend-attribution.md
    §3.1）。写模型的路径只有两处（``ModelRegistry.persist`` 与 ``add`` 后的 persist），
    两处都能给出该值。这里**故意**不做 `or "workbuddy"` 这类兜底：一旦兜了，
    「新增写入路径忘了带 backend」就会静默写成默认后端，而 drift 断言之外的
    任何测试都不会红。宁可让它在写入点当场炸。
    """
    if not value:
        raise ValueError(f"模型 {model_id} 缺少 backend（归属列必须显式给出）")
    return str(value)


def _canonical_column(model_id):
    """`models.canonical` 的写入值 —— 按后端声明现算（派生列，不接受调用方直给）。

    局部 import 的理由与 `_v11_to_v12` 同：`admin.db` 在模块级 import registry
    会让「registry → admin」的反向依赖成环。这条路径在写盘时才走，不在 import 时。
    """
    from core.backends.registry import canonical_name

    return canonical_name(model_id)


def _to_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value, default=0):
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_list(raw):
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


class Database:
    """SQLite 封装：单写连接 + 每线程读连接，自动建表与旧数据迁移。"""

    # accounts / keys 单独列出来，便于 SCHEMA 复用这两张表的 DDL。
    ACCOUNTS_DDL = """CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, file TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL DEFAULT 0,
        backend TEXT NOT NULL DEFAULT 'workbuddy',
        weight INTEGER NOT NULL DEFAULT 0,
        remaining_micro INTEGER, packages INTEGER, earliest_expiry INTEGER,
        earliest_remaining_micro INTEGER, expiring_soon_micro INTEGER,
        expiring_soon_updated INTEGER, credits_updated INTEGER,
        checkin_date TEXT, checkin_fails INTEGER NOT NULL DEFAULT 0, checkin_fails_date TEXT,
        auto_attempt INTEGER,
        checkin_slot TEXT, checkin_when TEXT, checkin_offset_date TEXT,
        token_refreshed INTEGER, auth_invalid INTEGER NOT NULL DEFAULT 0,
        refresh_slot TEXT, refresh_slot_date TEXT, refresh_window_date TEXT,
        consecutive_fails INTEGER NOT NULL DEFAULT 0, cooldown_until INTEGER,
        queued_until INTEGER,
        request_count INTEGER NOT NULL DEFAULT 0, last_request INTEGER, last_error TEXT,
        usage_synced_at INTEGER, usage_sync_error TEXT,
        usage_manual_synced_at INTEGER)"""

    KEYS_DDL = """CREATE TABLE IF NOT EXISTS keys (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL, hint TEXT NOT NULL,
        created INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
        revoked_at INTEGER, expires_at INTEGER, daily_limit INTEGER,
        usage_date TEXT, usage_count INTEGER NOT NULL DEFAULT 0,
        usage_tokens INTEGER NOT NULL DEFAULT 0,
        models TEXT NOT NULL DEFAULT '[]', accounts TEXT NOT NULL DEFAULT '[]', routing TEXT,
        last_used_at INTEGER)"""

    # 逐条执行而不用 executescript()：后者会先隐式 COMMIT，
    # 把外层显式事务直接结束掉，导致随后 COMMIT 报 no transaction is active。
    SCHEMA = (
        """CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
        ACCOUNTS_DDL,
        KEYS_DDL,
        """CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            auto_checkin INTEGER NOT NULL DEFAULT 1,
            checkin_time TEXT NOT NULL DEFAULT '09:00',
            affinity_expiry_days INTEGER NOT NULL DEFAULT 3,
            credit_min_interval INTEGER NOT NULL DEFAULT 60,
            sync_start TEXT NOT NULL DEFAULT '10:00',
            sync_end TEXT NOT NULL DEFAULT '20:00',
            batch_spread INTEGER NOT NULL DEFAULT 90,
            default_routing TEXT NOT NULL DEFAULT 'round_robin',
            queue_failover INTEGER NOT NULL DEFAULT 0,
            queue_retry INTEGER NOT NULL DEFAULT 3,
            queue_cooldown INTEGER NOT NULL DEFAULT 120,
            queue_hold_ms INTEGER NOT NULL DEFAULT 3000,
            queue_model_fallback INTEGER NOT NULL DEFAULT 1,
            queue_fallback_model TEXT NOT NULL DEFAULT 'fast-model',
            backend_enabled TEXT NOT NULL DEFAULT '{}',
            backup_enabled INTEGER NOT NULL DEFAULT 0,
            backup_interval_hours INTEGER NOT NULL DEFAULT 24,
            backup_keep INTEGER NOT NULL DEFAULT 10,
            backup_last_at INTEGER NOT NULL DEFAULT 0,
            backup_last_name TEXT NOT NULL DEFAULT '',
            guard_reserve_credits INTEGER NOT NULL DEFAULT 0,
            guard_daily_token_limit INTEGER NOT NULL DEFAULT 0,
            guard_daily_credit_limit INTEGER NOT NULL DEFAULT 0,
            guard_model_daily_token_limit INTEGER NOT NULL DEFAULT 0,
            models_updated INTEGER NOT NULL DEFAULT 0,
            model_backend_order TEXT,
            bare_name_routing INTEGER NOT NULL DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS session_bindings (
            affinity_key TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            expires INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS models (
            model_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
            credits_micro INTEGER, max_input_tokens INTEGER,
            source TEXT NOT NULL DEFAULT 'builtin',
            available INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0, promo TEXT,
            synced_at INTEGER NOT NULL DEFAULT 0,
            backend TEXT NOT NULL DEFAULT 'workbuddy',
            deleted INTEGER NOT NULL DEFAULT 0,
            canonical TEXT,
            -- 联合主键（v16）：`model_id` 是**裸名**（上游原名），归属由 `backend` 表达。
            -- 一对 (model_id, backend) 唯一 —— 同一个模型在两边各有一行
            -- （如 workbuddy/glm-5.3 与 trae/glm-5.3），这是「一个模型多后端」的正确形状。
            --
            -- 历史形状是 `model_id TEXT PRIMARY KEY`，其中 trae 行存的是拼出来的
            -- `trae/xxx`；那个前缀是**我们造的**（上游只返回裸名），v16 迁移把它剥掉
            -- 并换成这个联合主键，见 `_v15_to_v16`。
            PRIMARY KEY (model_id, backend))""",
        """CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY,
            time INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'api', path TEXT NOT NULL,
            status INTEGER, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL,
            key_id TEXT, account_id TEXT, account_name TEXT,
            model_id TEXT, model_name TEXT, upstream_request_id TEXT,
            backend TEXT,
            tokens_in INTEGER, tokens_out INTEGER,
            tokens_cache_read INTEGER, tokens_cache_creation INTEGER)""",
        """CREATE TABLE IF NOT EXISTS upstream_request_usage (
            request_id TEXT PRIMARY KEY, account_id TEXT, account_name TEXT,
            credit_micro INTEGER NOT NULL DEFAULT 0, model TEXT, client TEXT,
            request_time INTEGER NOT NULL, agent_purpose TEXT,
            input_trunc TEXT)""",
        """CREATE TABLE IF NOT EXISTS key_usage_daily (
            key_id TEXT NOT NULL, day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
            key_name TEXT, PRIMARY KEY (key_id, day))""",
        """CREATE TABLE IF NOT EXISTS account_usage_daily (
            account_id TEXT NOT NULL, day TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
            credits_micro INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_id, day, model))""",
        """CREATE INDEX IF NOT EXISTS idx_bindings_expires
            ON session_bindings(expires)""",
        """CREATE INDEX IF NOT EXISTS idx_logs_time
            ON request_logs(time)""",
        """CREATE INDEX IF NOT EXISTS idx_logs_key_time
            ON request_logs(key_id, time)""",
        """CREATE INDEX IF NOT EXISTS idx_logs_account_time
            ON request_logs(account_id, time)""",
        """CREATE INDEX IF NOT EXISTS idx_logs_model_time
            ON request_logs(model_id, time)""",
        """CREATE INDEX IF NOT EXISTS idx_upstream_model_time
            ON upstream_request_usage(model, request_time)""",
        """CREATE INDEX IF NOT EXISTS idx_upstream_account_time
            ON upstream_request_usage(account_id, request_time)""",
        """CREATE INDEX IF NOT EXISTS idx_key_usage_day
            ON key_usage_daily(day)""",
        """CREATE INDEX IF NOT EXISTS idx_account_usage_day
            ON account_usage_daily(day)""",
    )

    def __init__(self, path):
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
        self._migrate()
        self.prune_request_logs()
        self.prune_upstream_usage()
        self.prune_key_usage_daily()
        self.prune_account_usage_daily()

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
        save_accounts / save_keys 无需关心自己被嵌套调用。

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

    # -- migrations ---------------------------------------------------------
    # (目标版本, 迁移函数)，按目标版本升序；pending 取 target > current 的。
    MIGRATIONS = ((3, _v2_to_v3), (4, _v3_to_v4), (5, _v4_to_v5), (6, _v5_to_v6), (7, _v6_to_v7),
                  (8, _v7_to_v8), (9, _v8_to_v9), (10, _v9_to_v10), (11, _v10_to_v11),
                  (12, _v11_to_v12), (13, _v12_to_v13), (14, _v13_to_v14), (15, _v14_to_v15),
                  (16, _v15_to_v16), (17, _v16_to_v17), (18, _v17_to_v18),
                  (19, _v18_to_v19), (20, _v19_to_v20))

    def _migrate(self):
        rows = self.query("SELECT value FROM schema_meta WHERE key='version'")
        if not rows:
            # 全新库：结构已由 SCHEMA 建好，直接标版本，不跑数据迁移。
            self._write_version(SCHEMA_VERSION)
            return
        try:
            current = int(rows[0][0])
        except (TypeError, ValueError, IndexError):
            raise RuntimeError(f"state.db 的 schema 版本号不可读：{rows[0][0]!r}")
        pending = [(target, step) for target, step in self.MIGRATIONS if target > current]
        if not pending:
            return
        with self.transaction():
            for _, step in pending:
                step(self)
            self._write.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),))

    def _write_version(self, version):
        with self.transaction():
            self._write.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(version),))

    # -- settings -----------------------------------------------------------
    SETTINGS_COLUMNS = (
        "auto_checkin", "checkin_time", "affinity_expiry_days", "credit_min_interval",
        "sync_start", "sync_end", "batch_spread", "default_routing",
        "queue_failover", "queue_retry", "queue_cooldown", "queue_hold_ms",
        "queue_model_fallback", "queue_fallback_model", "models_updated",
        # 后端总闸：{后端名: 是否启用}，缺键即启用（specs/backend-toggle.md）。
        # JSON 存 TEXT 而不是加固定列 —— 后端数量可变，装不进固定列结构。
        "backend_enabled",
        "backup_enabled", "backup_interval_hours", "backup_keep",
        "backup_last_at", "backup_last_name",
        "guard_reserve_credits", "guard_daily_token_limit",
        "guard_daily_credit_limit", "guard_model_daily_token_limit",
        # 裸名路由（specs/model-routing-by-policy.md §5/§9.4）。
        # model_backend_order 在库里是 NULL 或 JSON 文本（列表），经 _load 归一成
        # 「list 或 None」；bare_name_routing 是 0/1，经 _load 归一成 bool。
        "model_backend_order", "bare_name_routing",
    )

    def get_settings(self, defaults=None):
        rows = self.query("SELECT * FROM settings WHERE id=1")
        if not rows:
            return None
        return {column: rows[0][column] for column in self.SETTINGS_COLUMNS}

    def save_settings(self, values):
        with self.transaction():
            self._write.execute(
                f"INSERT OR REPLACE INTO settings (id, {', '.join(self.SETTINGS_COLUMNS)})"
                f" VALUES (?, {', '.join('?' * len(self.SETTINGS_COLUMNS))})",
                (1, *(values.get(column) for column in self.SETTINGS_COLUMNS)))

    def update_settings(self, values):
        """局部更新设置行；**行不存在时补建**（不是静默不生效）。

        原实现是裸 UPDATE：settings 行尚未建立时它匹配 0 行、静默成功返回，
        调用方以为写进去了。任何在「首次 save_settings 之前」写设置的路径
        （如模型目录 persist）都会踩到 —— 数据看起来「偶尔丢失」，最难查。
        改 UPSERT：先保证行存在，再更新传入的列。
        """
        if not values:
            return
        with self.transaction():
            self._write.execute(
                "INSERT OR IGNORE INTO settings (id) VALUES (1)")
            self._write.execute(
                f"UPDATE settings SET {', '.join(f'{k}=?' for k in values)} WHERE id=1",
                tuple(values.values()))

    # -- accounts -----------------------------------------------------------
    def load_accounts(self):
        """{id: {实体字段…, account_status 字段…}}；金额已 ÷1e6 还原成 float。"""
        result = {}
        for row in self.query("SELECT * FROM accounts"):
            item = {key: row[key] for key in row.keys()}
            for memory, column in ACCOUNT_MONEY_COLUMNS.items():
                item[memory] = _from_micro(item.pop(column, None))
            result[row["id"]] = item
        return result

    def save_accounts(self, entities, statuses):
        """全量同步：实体字段来自 entities，account_status 的字段并进同一行。"""
        columns = ("id", "name", "file", "enabled", "created", "backend",
                   *ACCOUNT_STATUS_COLUMNS, *ACCOUNT_MONEY_COLUMNS.values())
        rows = []
        for aid, entity in entities.items():
            merged = {**entity, **(statuses.get(aid) or {})}
            values = [aid,
                      str(entity.get("name", ""))[:60],
                      str(entity.get("file", "")),
                      int(bool(entity.get("enabled", True))),
                      _to_int(entity.get("created")),
                      str(entity.get("backend") or "workbuddy")]
            for column in ACCOUNT_STATUS_COLUMNS:
                values.append(_status_value(column, merged))
            values.extend(_to_micro(merged.get(memory)) for memory in ACCOUNT_MONEY_COLUMNS)
            rows.append(tuple(values))
        marks = ", ".join(["?"] * len(columns))
        with self.transaction():
            self._write.execute("DELETE FROM accounts")
            self._write.executemany(
                f"INSERT INTO accounts ({', '.join(columns)}) VALUES ({marks})", rows)

    # -- keys ---------------------------------------------------------------
    def load_keys(self):
        result = {}
        for row in self.query("SELECT * FROM keys"):
            item = {key: row[key] for key in row.keys()}
            item["models"] = _decode_list(item.get("models"))
            item["accounts"] = _decode_list(item.get("accounts"))
            result[row["id"]] = item
        return result

    def save_keys(self, keys):
        columns = ("id", *KEY_COLUMNS)
        marks = ", ".join(["?"] * len(columns))
        rows = []
        for kid, item in keys.items():
            usage = item.get("usage") or {}
            rows.append((kid,
                         str(item.get("name", ""))[:60], item.get("hash", ""), item.get("hint", ""),
                         _to_int(item.get("created")), _to_int(item.get("enabled"), 1),
                         item.get("revoked_at"), item.get("expires_at"), item.get("daily_limit"),
                         usage.get("date") or None, _to_int(usage.get("count")),
                         _to_int(usage.get("tokens")),
                         _dumps(_decode_list(item.get("models"))),
                         _dumps(_decode_list(item.get("accounts"))),
                         item.get("routing"),
                         # 原样传，不走 _to_int：那个函数把 None 变成 0，
                         # 会把「从未使用」写成 0（1970 年）。这里 NULL 必须
                         # 保持 NULL —— 前端按真值判断显示「从未使用」，
                         # 而将来的「早于某时间」比较也依赖 0 与 NULL 可区分。
                         item.get("last_used_at")))
        with self.transaction():
            self._write.execute("DELETE FROM keys")
            self._write.executemany(
                f"INSERT INTO keys ({', '.join(columns)}) VALUES ({marks})", rows)

    # -- session bindings ---------------------------------------------------
    def load_bindings(self):
        return {row["affinity_key"]: {"account_id": row["account_id"], "expires": row["expires"]}
                for row in self.query("SELECT * FROM session_bindings")}

    def save_bindings(self, bindings):
        rows = [(str(key), str(value.get("account_id")), _to_int(value.get("expires")))
                for key, value in bindings.items()
                if isinstance(value, dict) and value.get("account_id")]
        with self.transaction():
            self._write.execute("DELETE FROM session_bindings")
            self._write.executemany(
                "INSERT OR REPLACE INTO session_bindings (affinity_key, account_id, expires)"
                " VALUES (?,?,?)", rows)

    def delete_bindings_for_account(self, account_id):
        with self.transaction():
            self._write.execute("DELETE FROM session_bindings WHERE account_id=?", (account_id,))

    # -- models -------------------------------------------------------------
    def load_model_rows(self, only_available=False, include_custom=False):
        """读模型目录，返回**行列表**（每行带 `model_id` 与 `backend`）。

        include_custom 时额外带上手工添加的行。两类条件的连接词是 OR：自定义模型
        可能 available=0（上游根本没返回它），用 AND 会把它们全过滤掉，用户保存的
        模型重启后消失。

        **已删除的行（deleted=1）一律不返回**：它们只为 request_logs 的 join 留在
        表里，不该再进任何目录。没有这个过滤，删除会在重启后复活（`available=0`
        表达不了「已删除」——那条语义已被「上游没返回的自定义模型要保留」占用）。

        **为什么返回列表而不是 id 索引的字典**：v16 起主键是 `(model_id, backend)`，
        同名模型在两个后端各有一行（生产实测 8 组）。按 `model_id` 索引的字典
        会让其中一行**静默覆盖**另一行 —— 重启后一个后端的模型凭空消失，
        且没有任何报错。需要完整目录的调用方（`ModelRegistry.load`）必须用这个。

        `load_models()` 保留给「按 id 取一行」的老调用点（它是 id 索引的，
        同名多后端时只留最后一行）——新代码请用本方法。
        """
        sql = "SELECT * FROM models WHERE deleted=0"
        where = []
        if only_available:
            where.append("available=1")
        if include_custom:
            where.append("source='custom'")
        if where:
            sql += " AND (" + " OR ".join(where) + ")"
        rows = []
        for row in self.query(sql):
            item = {key: row[key] for key in row.keys()}
            # 精确值（整数 micro）**保留**在内存里：路由层的价格红线要精确比较
            # （specs/model-routing-by-policy.md §9.3：用户明确要求「精确比较」，
            # float 比较会引入误差）。显示层继续用下面的 float `credits`——
            # 两者是同一个 DB 值，只是精度不同，不新增存储。
            item["credits_micro"] = item.get("credits_micro")
            item["credits"] = _from_micro(item.get("credits_micro"))
            rows.append(item)
        return rows

    def load_models(self, only_available=False, include_custom=False):
        """读模型目录，按 `model_id` 索引（**同名多后端时只留最后一行**）。

        需要完整目录（含每条归属后端）请用 :meth:`load_model_rows` ——
        这个字典会丢行，见那里的说明。
        """
        return {item["model_id"]: item
                for item in self.load_model_rows(only_available, include_custom)}

    def save_models(self, models):
        """UPSERT 不删行：上游消失的模型由调用方标 available=0 保留。

        入参 item 的单价是 float（`credits`），由这里统一换算成 `credits_micro`。
        不接受已换算的 micro 值——那会被再乘一次 1e6。
        """
        if not models:
            return
        columns = ("model_id", *MODEL_COLUMNS)
        marks = ", ".join(["?"] * len(columns))
        rows = []
        for mid, item in models.items():
            # 单价**只从 float `credits` 换算**，不认 `credits_micro` 入参。
            #
            # 曾经想过「有精确值就原样写回」，实测证明那是**多余的**：
            # `_to_micro(_from_micro(m)) == m` 对全部 micro 取值恒成立
            # （穷举 0..2e6 零例外），所以两种写法结果逐位相同。
            # 留一个不可测的分支只会让人以为它有用 —— 不写。
            # `load_models` 仍保留 `credits_micro` 给**路由层精确比较**用
            # （§9.3），那是读路径的事，与写入无关。
            rows.append((mid,
                         str(item.get("name") or mid), item.get("description"),
                         _to_micro(item.get("credits")),
                         _to_int(item.get("max_input_tokens")) or None,
                         item.get("source") or "upstream",
                         _to_int(item.get("available"), 1), _to_int(item.get("enabled"), 1),
                         _to_int(item.get("sort_order")), item.get("promo"),
                         _to_int(item.get("synced_at")),
                         # 归属：调用方（ModelRegistry）写入时已知，直接存列（§3.1）。
                         # **缺值即报错**，不给默认值 —— 给了的话「persist 忘了写列」
                         # 这类回归会静默写成 workbuddy 而测试全绿（变异测试实测抓到
                         # 这个盲区）。列自身的 DEFAULT 'workbuddy' 只兜迁移期的历史行，
                         # 那条路径不经过这里。
                         _require_backend(item.get("backend"), mid),
                         # 删除标记：1 = 用户删掉的保留行（供日志 join，不加载）。
                         _to_int(item.get("deleted"), 0),
                         # 跨后端规范名：**派生值，不是调用方给的**（§6.1）。
                         # 从 `mid` + 后端声明现算，所以谁都不可能写歪——若让调用方
                         # 透传，就多出一条「persist 与注册表对同一模型算出不同值」的
                         # 路径，正是这个列要消灭的那类不一致。
                         # 调用方显式给了就用它（留给测试与将来「用户手工指定别名」）。
                         item.get("canonical") or _canonical_column(mid)))
        with self.transaction():
            self._write.executemany(
                f"INSERT INTO models ({', '.join(columns)}) VALUES ({marks})"
                # 冲突键是**联合主键**（v16）：同一个裸名在两边各有一行，
                # 只有 (model_id, backend) 同时相同才是同一行。
                " ON CONFLICT(model_id, backend) DO UPDATE SET"
                f" {', '.join(f'{c}=excluded.{c}' for c in columns[1:])}", rows)

    def enabled_models(self):
        """可调用列表（DB 视角）：只看 enabled。

        available 不参与：上游没返回有两种情况——真下线了，或只是菜单没列全。
        网关分不清，只有用户知道，所以放行与否交给 enabled。

        注意：运行时决定放行的是 `ModelRegistry.enabled_models()`（它还要算上
        自定义模型与内存里的启停覆盖）。这个方法只在直接读表的场景用。
        """
        return [row["model_id"] for row in self.query(
            "SELECT model_id FROM models WHERE enabled=1 ORDER BY sort_order")]

    def known_model_ids(self):
        """全部已知 id，用于区分「已禁用」与「未知」。

        不按 available 过滤：自定义模型不在上游目录里，过滤掉会让它们被当成
        「未知」而绕过禁用检查。
        已删除（deleted=1）的不算已知：用户删掉它就是想让它不可用，
        留着反而会把它当「已知但禁用」而给出误导性的报错。
        """
        return {row["model_id"] for row in self.query(
            "SELECT model_id FROM models WHERE deleted=0")}

    # -- request logs -------------------------------------------------------
    def append_request_logs(self, rows):
        """批量写入请求日志；空批次直接返回，不开事务。"""
        if not rows:
            return
        marks = ", ".join(["?"] * len(LOG_COLUMNS))
        with self.transaction():
            self._write.executemany(
                f"INSERT INTO request_logs ({', '.join(LOG_COLUMNS)}) VALUES ({marks})",
                [tuple(row.get(column) for column in LOG_COLUMNS) for row in rows])

    def recent_request_logs(self, limit=100):
        rows = self.query(
            f"SELECT {', '.join(LOG_COLUMNS)} FROM request_logs ORDER BY id DESC LIMIT ?",
            (limit,))
        return [{column: row[column] for column in LOG_COLUMNS} for row in rows]

    def rename_account_logs(self, aid, new_name):
        """改备注名后把历史日志里的 account_name 一起刷成新名（用户 2026-10-17）。

        `account_name` 是写时快照，只改 accounts 表会让同一账号在日志里出现
        新旧两个名字。改名时同步 UPDATE，日志与账号池永远对齐；v18 迁移负责
        刷存量数据，这里负责以后的每一次改名。
        """
        with self.transaction():
            self._write.execute(
                "UPDATE request_logs SET account_name=? WHERE account_id=?",
                (new_name, aid))

    def page_request_logs(self, limit=50, offset=0, since=None, until=None,
                          source=None, outcome=None, key_id=None, account_id=None):
        """分页读请求日志（按 id 倒序），返回 {rows, total, limit, offset}。

        total 是不带 limit/offset 的同条件总数，供前端画页码。条件都用参数绑定，
        不接受 SQL 片段，避免拼接注入。

        key_id：只看某个客户端密钥的请求。命中 idx_logs_key_time 索引。
        """
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        clauses, params = [], []
        if since is not None:
            clauses.append("time >= ?")
            params.append(int(since))
        if until is not None:
            clauses.append("time < ?")
            params.append(int(until))
        if source:
            clauses.append("source = ?")
            params.append(str(source))
        if outcome:
            clauses.append("outcome = ?")
            params.append(str(outcome))
        if key_id:
            clauses.append("key_id = ?")
            params.append(str(key_id))
        # account_id：账号详情里的「最近请求」。命中 idx_logs_account_time 索引。
        if account_id:
            clauses.append("account_id = ?")
            params.append(str(account_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self.query(f"SELECT COUNT(*) AS n FROM request_logs{where}", tuple(params))[0]["n"]
        rows = self.query(
            f"SELECT id, {', '.join(LOG_COLUMNS)} FROM request_logs{where}"
            " ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        return {"rows": [{"id": row["id"], **{column: row[column] for column in LOG_COLUMNS}}
                         for row in rows],
                "total": total, "limit": limit, "offset": offset}

    def request_log_summary(self, since=None, until=None):
        """仪表盘用的累计口径（跨重启、来自 request_logs）。

        api/test 分列计数，避免把后台测试算进「对话请求」。
        """
        clauses, params = [], []
        if since is not None:
            clauses.append("time >= ?")
            params.append(int(since))
        if until is not None:
            clauses.append("time < ?")
            params.append(int(until))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self.query(
            "SELECT COUNT(*) AS n, SUM(outcome='success') AS ok,"
            " SUM(source='api') AS api, SUM(source='test') AS test,"
            " SUM(status IS NOT NULL AND status >= 200 AND status < 300) AS http_ok,"
            " AVG(duration_ms) AS avg, MIN(time) AS first_time"
            f" FROM request_logs{where}", tuple(params))[0]
        total = row["n"] or 0
        success = row["ok"] or 0
        http_ok = row["http_ok"] or 0
        return {"completed": total, "succeeded": success, "failed": total - success,
                "api_count": row["api"] or 0, "test_count": row["test"] or 0,
                "success_rate": round(100 * success / total, 1) if total else None,
                "http_success_rate": round(100 * http_ok / total, 1) if total else None,
                "avg_duration_ms": round(row["avg"]) if row["avg"] is not None else None,
                "first_time": row["first_time"]}

    # 分组维度 -> SQL 表达式。name 只是列名，不带用户输入，可安全拼接。
    LOG_GROUPS = {
        "day": "date(time, 'unixepoch', '+8 hours')",
        "source": "source",
        "path": "path",
        "status": "status",
        "outcome": "outcome",
        "account": "account_id",
        "key": "key_id",
        "model": "model_id",
    }

    def request_log_stats(self, since=None, until=None, group_by=None, limit=500):
        """请求日志统计：不分组时给总量，分组时给每组计数、成功率与平均耗时。

        成功率只认 outcome='success'（HTTP 200 但 SSE 中途失败不算成功），
        与 MetricsMiddleware 的判定保持一致。日期分桶按北京时间，避免跨天错位。
        """
        clauses, params = [], []
        if since is not None:
            clauses.append("time >= ?")
            params.append(int(since))
        if until is not None:
            clauses.append("time < ?")
            params.append(int(until))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        if group_by is None:
            rows = self.query(
                "SELECT COUNT(*) AS n, SUM(outcome='success') AS ok, AVG(duration_ms) AS avg"
                f" FROM request_logs{where}", tuple(params))
            row = rows[0]
            return {"count": row["n"] or 0, "success": row["ok"] or 0,
                    "avg_duration_ms": row["avg"], "groups": []}
        expression = self.LOG_GROUPS.get(group_by)
        if expression is None:
            raise ValueError(f"不支持的分组维度：{group_by}")
        rows = self.query(
            f"SELECT {expression} AS bucket, COUNT(*) AS n, SUM(outcome='success') AS ok,"
            f" AVG(duration_ms) AS avg FROM request_logs{where}"
            f" GROUP BY bucket ORDER BY n DESC LIMIT ?", (*params, int(limit)))
        # 分组模式下不返回总量：桶受 limit 截断，求和会小于真实总量，
        # 字段名叫 count 容易被当成全量。真实总量用不分组调用（count 无歧义）。
        return {"groups": [
            {"key": row["bucket"], "count": row["n"] or 0, "success": row["ok"] or 0,
             "success_rate": round(100 * (row["ok"] or 0) / row["n"], 1) if row["n"] else None,
             "avg_duration_ms": round(row["avg"]) if row["avg"] is not None else None}
            for row in rows]}

    def prune_upstream_usage(self, keep_days=REQUEST_LOG_KEEP_DAYS):
        """启动时裁剪上游用量（spec 要求保留 3 个月）。

        这张表每次同步会 UPSERT 进 30 天窗口内的所有行，只增不减，
        不裁剪会随账号数×时间无限膨胀。按 request_time（不是本地写入时间）裁，
        与同步窗口同基准。
        """
        cutoff = int(time.time()) - keep_days * 86400
        with self.transaction():
            self._write.execute(
                "DELETE FROM upstream_request_usage WHERE request_time < ?", (cutoff,))

    def prune_request_logs(self, keep_days=REQUEST_LOG_KEEP_DAYS, max_rows=REQUEST_LOG_MAX_ROWS):
        """启动时裁剪：先按时间，再按行数上限兜底，避免一次全表 DELETE 长时间持锁。"""
        cutoff = int(time.time()) - keep_days * 86400
        with self.transaction():
            self._write.execute("DELETE FROM request_logs WHERE time < ?", (cutoff,))
        total = self.query("SELECT COUNT(*) AS n FROM request_logs")[0]["n"]
        if total <= max_rows:
            return 0
        with self.transaction():
            self._write.execute(
                "DELETE FROM request_logs WHERE id IN"
                " (SELECT id FROM request_logs ORDER BY id LIMIT ?)", (total - max_rows,))
        return total - max_rows

    # -- account usage daily (guards) --------------------------------------
    def add_account_usage(self, account_id, model, tokens, credits=0.0, day=None):
        """累加一次账号用量（护栏的计数来源）。

        与 key_usage_daily 的「覆盖当日快照」不同，这里是**累加**：调用点是
        每笔完成的请求，粒度就是一次请求，没有快照可覆盖。重启后内存计数
        丢失也无妨——库里已经逐次累加过了，这正是把计数放库里的原因。

        ``credits`` 按 MICRO 落库（与其他金额列一致），避免浮点累加漂移。
        """
        if not account_id:
            return
        day = day or datetime.fromtimestamp(time.time(), CN).strftime("%Y-%m-%d")
        with self.transaction():
            self._write.execute(
                """INSERT INTO account_usage_daily
                       (account_id, day, model, count, tokens, credits_micro)
                   VALUES (?,?,?,1,?,?)
                   ON CONFLICT(account_id, day, model) DO UPDATE SET
                     count = count + 1,
                     tokens = tokens + excluded.tokens,
                     credits_micro = credits_micro + excluded.credits_micro""",
                (str(account_id), day, str(model or ""),
                 _to_int(tokens) or 0, _to_micro(credits) or 0))

    def account_usage_today(self, day=None):
        """当天各账号的用量汇总 → {aid: {tokens, credits, models}}。

        按 (账号, 模型) 聚合过一次再返回，上层不用再关心模型维度：
        ``tokens`` / ``credits`` 是账号当天总量，``models`` 是逐模型 token 数。
        """
        day = day or datetime.fromtimestamp(time.time(), CN).strftime("%Y-%m-%d")
        out = {}
        for row in self.query(
                "SELECT account_id, model, tokens, credits_micro FROM account_usage_daily"
                " WHERE day = ?", (day,)):
            aid = str(row["account_id"])
            bucket = out.setdefault(aid, {"tokens": 0, "credits": 0.0, "models": {}})
            tokens = int(row["tokens"] or 0)
            bucket["tokens"] += tokens
            bucket["credits"] += _from_micro(row["credits_micro"]) or 0.0
            model = str(row["model"] or "")
            if model:
                bucket["models"][model] = bucket["models"].get(model, 0) + tokens
        return out

    def prune_account_usage_daily(self, keep_days=KEY_USAGE_KEEP_DAYS):
        """启动时裁剪：day 是 TEXT 的 YYYY-MM-DD，可直接字符串比较。"""
        cutoff = (datetime.fromtimestamp(time.time(), CN) - timedelta(days=keep_days)
                  ).strftime("%Y-%m-%d")
        with self.transaction():
            self._write.execute("DELETE FROM account_usage_daily WHERE day < ?", (cutoff,))

    # -- key usage daily ---------------------------------------------------
    def save_key_usage_daily(self, keys):
        """把每个 key 的当日累计快照写进历史表（覆盖语义）。

        入参是 Store.data["keys"] 全量 dict。keys.usage 本身就是**当日累计值**
        （换天才 clear），所以这里是「写当日快照」而不是累加。

        冲突时必须用 MAX 而不是直接覆盖：usage 只在单进程内存里单调递增，
        **重启后内存清零**，当日计数会从 0 重新累加，此时每次 flush 写回的都
        是比库里更小的值。直接覆盖会把当天历史改小（实测 5000 -> 1）。
        MAX 保证只增不减，重启只丢「最后 5 秒未 flush」的增量而不会倒退。

        行的 day 取 usage["date"]（即 key 侧换天的那一天），不取「现在」——
        跨天后首个请求到来前，旧日期的快照会被再写一次，MAX 语义下无副作用。
        usage_date 为空的 key 跳过（避免造空行）。
        """
        rows = []
        for kid, item in (keys or {}).items():
            usage = item.get("usage") or {}
            if not usage.get("date"):
                continue
            rows.append((kid, usage["date"],
                         _to_int(usage.get("count")) or 0,
                         _to_int(usage.get("tokens")) or 0,
                         item.get("name")))
        if not rows:
            return 0
        with self.transaction():
            self._write.executemany(
                """INSERT INTO key_usage_daily (key_id, day, count, tokens, key_name)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(key_id, day) DO UPDATE SET
                     count=MAX(count, excluded.count),
                     tokens=MAX(tokens, excluded.tokens),
                     key_name=excluded.key_name""", rows)
        return len(rows)

    def key_usage_series(self, days=30):
        """按 key 取近 N 天的每日用量序列，供「今日用量」弹窗画趋势。

        返回 {days: [...], keys: [{id, name, series: [{day, count, tokens}]}],
        total: {count, tokens}}。缺天的日子不补零——图表按实际有记录的点画。
        """
        days = max(1, min(int(days), 365))
        cutoff = (datetime.fromtimestamp(time.time(), CN) - timedelta(days=days - 1)
                  ).strftime("%Y-%m-%d")
        rows = self.query(
            "SELECT key_id, day, count, tokens, key_name FROM key_usage_daily"
            " WHERE day >= ? ORDER BY day DESC, key_id", (cutoff,))
        by_key, seen_days = {}, []
        for row in rows:
            entry = by_key.setdefault(
                row["key_id"], {"id": row["key_id"], "name": row["key_name"] or row["key_id"],
                                "series": []})
            entry["series"].append({"day": row["day"], "count": row["count"] or 0,
                                    "tokens": row["tokens"] or 0})
            if row["day"] not in seen_days:
                seen_days.append(row["day"])
        total = self.query(
            "SELECT SUM(count) AS c, SUM(tokens) AS t FROM key_usage_daily WHERE day >= ?",
            (cutoff,))[0]
        return {"days": seen_days, "keys": list(by_key.values()),
                "total": {"count": total["c"] or 0, "tokens": total["t"] or 0}}

    def prune_key_usage_daily(self, keep_days=KEY_USAGE_KEEP_DAYS):
        """启动时裁剪：day 是 TEXT 的 YYYY-MM-DD，可直接字符串比较。"""
        cutoff = (datetime.fromtimestamp(time.time(), CN) - timedelta(days=keep_days)
                  ).strftime("%Y-%m-%d")
        with self.transaction():
            self._write.execute("DELETE FROM key_usage_daily WHERE day < ?", (cutoff,))

    # -- upstream request usage ---------------------------------------------
    def load_upstream_usage(self, account_id=None, since=None, limit=None):
        sql, params = "SELECT * FROM upstream_request_usage", []
        clauses = []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if since is not None:
            clauses.append("request_time>=?")
            params.append(int(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY request_time DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        result = []
        for row in self.query(sql, tuple(params)):
            item = {key: row[key] for key in row.keys()}
            item["credit"] = _from_micro(item.pop("credit_micro", None))
            result.append(item)
        return result

    def save_upstream_usage(self, records):
        """UPSERT：credit 可能不是终值，用 INSERT OR IGNORE 会把首条 pending/0 永久钉死。"""
        rows = [(str(r.get("request_id")), r.get("account_id"), r.get("account_name"),
                 _to_micro(r.get("credit")) or 0, r.get("model"), r.get("client"),
                 _to_int(r.get("request_time")), r.get("agent_purpose"), r.get("input_trunc"))
                for r in records if r.get("request_id")]
        if not rows:
            return 0
        with self.transaction():
            self._write.executemany(
                """INSERT INTO upstream_request_usage
                   (request_id, account_id, account_name, credit_micro, model, client,
                    request_time, agent_purpose, input_trunc)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     credit_micro=excluded.credit_micro,
                     account_name=excluded.account_name""",
                rows)
        return len(rows)

    def _upstream_where(self, since=None, until=None, account_id=None, backend=None):
        clauses, params = [], []
        if since is not None:
            clauses.append("request_time >= ?")
            params.append(int(since))
        if until is not None:
            clauses.append("request_time < ?")
            params.append(int(until))
        if account_id:
            clauses.append("account_id = ?")
            params.append(str(account_id))
        if backend:
            # 用量表本身没有 backend 列（账号才是后端的持有者），用子查询关联：
            # account_id IN (SELECT id FROM accounts WHERE backend=?)。
            # 账号已删时它的历史用量不再归属任何后端 tab——与「按账号筛不到已删
            # 账号」的既有行为一致，不是新引入的取舍。
            clauses.append("account_id IN (SELECT id FROM accounts WHERE backend=?)")
            params.append(str(backend))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)

    def page_upstream_usage(self, limit=100, offset=0, since=None, until=None,
                            account_id=None, backend=None):
        """分页读上游用量（本地库，不打上游），返回 {rows, total, limit, offset}。

        total 是不带 limit/offset 的同条件总数，供前端画页码。条件全部参数绑定。
        """
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where, params = self._upstream_where(since, until, account_id, backend)
        total = self.query(
            f"SELECT COUNT(*) AS n FROM upstream_request_usage{where}", params)[0]["n"]
        rows = self.query(
            "SELECT request_id, account_id, account_name, credit_micro, model, client,"
            " request_time, agent_purpose, input_trunc FROM upstream_request_usage"
            f"{where} ORDER BY request_time DESC LIMIT ? OFFSET ?",
            (*params, limit, offset))
        result = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["credit"] = _from_micro(item.pop("credit_micro", None))
            result.append(item)
        return {"rows": result, "total": total, "limit": limit, "offset": offset}

    def upstream_usage_report(self, since=None, until=None, account_id=None,
                              group_by="day", limit=500, backend=None):
        """上游用量报表：按白名单维度分组，给积分合计、条数与免费/付费拆分。

        免费（credit_micro=0）与付费分开计——限免期模型照样记账，混入会低估单价。
        group_by 只接受 UPSTREAM_GROUPS 的键，不接受任意列名。
        """
        if group_by not in UPSTREAM_GROUPS:
            raise ValueError(f"不支持的分组维度：{group_by}")
        expr = UPSTREAM_GROUPS[group_by]
        where, params = self._upstream_where(since, until, account_id, backend)
        rows = self.query(
            f"SELECT {expr} AS bucket, COUNT(*) AS n,"
            " SUM(credit_micro) AS credit_micro,"
            " SUM(CASE WHEN credit_micro = 0 THEN 1 ELSE 0 END) AS free,"
            " SUM(CASE WHEN credit_micro <> 0 THEN 1 ELSE 0 END) AS paid"
            f" FROM upstream_request_usage{where}"
            " GROUP BY bucket ORDER BY bucket DESC LIMIT ?",
            (*params, int(limit)))
        groups = [{"bucket": row["bucket"], "count": row["n"],
                   "credit": _from_micro(row["credit_micro"] or 0),
                   "free": row["free"] or 0, "paid": row["paid"] or 0}
                  for row in rows]
        total_row = self.query(
            "SELECT COUNT(*) AS n, SUM(credit_micro) AS credit_micro,"
            " SUM(CASE WHEN credit_micro = 0 THEN 1 ELSE 0 END) AS free,"
            " SUM(CASE WHEN credit_micro <> 0 THEN 1 ELSE 0 END) AS paid"
            f" FROM upstream_request_usage{where}", params)[0]
        # 桶总数独立于 limit：前端要能说清「显示的是前 N 组，共 M 组」。
        bucket_total = self.query(
            f"SELECT COUNT(DISTINCT {expr}) AS n FROM upstream_request_usage{where}",
            params)[0]["n"] or 0
        return {"group_by": group_by, "groups": groups,
                "bucket_total": bucket_total,
                "truncated": bucket_total > len(groups),
                "total": {"count": total_row["n"] or 0,
                          "credit": _from_micro(total_row["credit_micro"] or 0),
                          "free": total_row["free"] or 0, "paid": total_row["paid"] or 0}}

def _status_value(column, merged):
    default = ACCOUNT_STATUS_DEFAULTS.get(column)
    value = merged.get(column)
    if value is None:
        return default
    if column in ACCOUNT_INT_COLUMNS:
        return _to_int(value, default if default is not None else 0)
    return int(value) if isinstance(value, bool) else value

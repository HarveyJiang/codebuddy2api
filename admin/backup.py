"""数据库备份与恢复（单机、无外部依赖）。

为什么不用 ``cp state.db``
-------------------------
库是 WAL 模式，正在写入的页可能还在 ``state.db-wal`` 里没合并回主库。
直接拷主库会得到「旧到某个 checkpoint 为止」的快照，且 ``-wal`` 一丢，
那部分已提交的数据就再也找不回来。这里统一走 sqlite3 的在线备份 API
(``Connection.backup``)：它逐页复制并跟随写入者的进度，不阻塞业务读写，
产出的单文件本身即一致快照，拷完不需要配套的 ``-wal``。

与 ``Database`` 的边界
----------------------
本模块只认「一个 sqlite 连接 + 目标目录」，不 import ``db``/``server``——
``Database`` 的写连接是私有的且带事务记账，备份要跨模块拿它很容易把事务
语义弄乱。因此这里**自己另开一条只读连接**，用 URI ``mode=ro`` 打开，
既不会污染 ``_write``，也不会因为持有写锁而拖住正常请求。
"""
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 备份文件名：state-<北京时间戳>.db。用北京时间对齐业务侧其它时间戳口径
# （容器 TZ 是 Asia/Shanghai，但**不能**依赖它——脚本/测试里 TZ 未必设），
# 所以显式指定 +8，避免出现「界面按北京时间、文件名按 UTC」的错位。
CN = timezone(timedelta(hours=8))
_STAMP = "%Y%m%d-%H%M%S"
# 白名单同时接受带毫秒后缀的形式：同一秒内重复备份时用它避重名（见 scoped_name）。
NAME_RE = re.compile(r"^state-\d{8}-\d{6}(?:-\d{3})?\.db$")

# 单次备份的默认上限：库本身只有几 MB，真超过这个量级说明有别的东西写进来了。
MAX_BYTES = 512 * 1024 * 1024
# 定时备份的对齐粒度：检查间隔取「距下次整点还有多久」与它的较小值，
# 这样改设置后最多等 5 分钟就生效，又不会每秒都算一遍。
TICK_SECONDS = 300


def backup_dir(root):
    """备份目录：与 state.db 同级放 ``backups/``，随数据卷一起持久化。"""
    return Path(root) / "backups"


def filename(now=None):
    return f"state-{datetime.fromtimestamp(now or time.time(), CN).strftime(_STAMP)}.db"


def safe_path(root, name):
    """把外部传入的文件名解析成备份目录内的路径，越界返回 None。

    只放行 ``NAME_RE`` 命中的名字：备份目录里不该出现别的东西，白名单比
    「过滤掉 .. 和 /」更不容易漏（``state-20260101-000000.db/../../x`` 这种
    带合法前缀的拼接，靠黑名单很容易放过）。
    """
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    path = (backup_dir(root) / name).resolve()
    base = backup_dir(root).resolve()
    if path.parent != base:
        return None
    return path


def _snapshot(db_path, target):
    """把 source 库在线复制到 target（sqlite 备份 API，不阻塞写入者）。"""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(target)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create(root, db_path, now=None, keep=10, minimal_size=0):
    """生成一份备份，返回文件名（失败抛 OSError/RuntimeError）。

    ``minimal_size``：健康库的字节下限。备份出空库比不备份更危险——恢复时
    会得到「成功但全空」的结果，所以宁可在这里失败。

    注意：SQLite 即使建的是空库，文件也会被撑到整页大小（4096 字节一轮）。
    所以**只比字节数不够**——空库备份正好是 4096，卡在下界上会被判成「合格」。
    真正的判据是内容（``verify`` 会查核心表），这里的大小检查只用于阻挡
    「明显不是数据库」的输入，两者互补。
    """
    stem = filename(now)
    final = backup_dir(root) / stem
    if final.exists():
        # 同一秒重复点「立即备份」：换个名字而不是覆盖，避免两次点击互相截断。
        final = backup_dir(root) / scoped_name(stem, now)
    # 先写临时文件再改名：中途失败留下的 ``.tmp`` 不会被 list() 当备份列出，
    # 也不会被 prune 的保留计数占位。临时名以 .tmp 结尾，同样不会被认成备份。
    tmp = backup_dir(root) / (final.name + ".tmp")
    try:
        _snapshot(db_path, tmp)
        size = tmp.stat().st_size
        if size < minimal_size:
            raise RuntimeError(f"备份内容不完整（{size} 字节），已放弃这次备份")
        if size > MAX_BYTES:
            raise RuntimeError(f"备份体积异常（{size} 字节），已放弃这次备份")
        # 页数下限之后再看内容：空库/别的东西都不会有我们的核心表。
        ok, message = verify(tmp)
        if not ok:
            raise RuntimeError(f"备份内容不可用：{message}")
        # 兜底：改名后必须仍能被 list() 认出来，否则等于生成了个看不见的孤儿。
        if not NAME_RE.fullmatch(final.name):
            raise RuntimeError(f"备份文件名不合规：{final.name}")
        tmp.replace(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    prune(root, keep)
    return final.name


def list_backups(root):
    """列出备份，新的在前。只认白名单文件名，``.tmp`` 之类一律不算。"""
    base = backup_dir(root)
    if not base.is_dir():
        return []
    items = []
    for path in base.iterdir():
        if not NAME_RE.fullmatch(path.name) or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({"name": path.name, "size": stat.st_size, "created": int(stat.st_mtime)})
    items.sort(key=lambda item: (item["created"], item["name"]), reverse=True)
    return items


def resolve(root, name):
    """校验并返回份备份的路径，非法或不存在返回 None。"""
    path = safe_path(root, name)
    if path is None or not path.is_file():
        return None
    return path


def remove(root, name):
    path = resolve(root, name)
    if path is None:
        return False
    path.unlink(missing_ok=True)
    return True


def prune(root, keep):
    """只保留最近 ``keep`` 份。``keep <= 0`` 表示不自动清理。"""
    if keep is None or keep <= 0:
        return []
    items = list_backups(root)
    removed = []
    for item in items[keep:]:
        if remove(root, item["name"]):
            removed.append(item["name"])
    return removed


def scoped_name(name, now=None):
    """把 ``state-<ts>.db`` 改写成 ``state-<ts>-<毫秒>.db``，仍在白名单内。

    同一秒内重复请求备份时用它避开重名。**不能**用 pid 之类加在末尾的写法——
    ``NAME_RE`` 是 ``\\.db$`` 锚定的，追加在扩展名之后会让名字不再被 list()
    认出来，备份生成了却看不见（等于静默丢失）。
    """
    if not NAME_RE.fullmatch(name):
        return name
    millis = int(((now or time.time()) % 1) * 1000)
    return name[:-3] + f"-{millis:03d}.db"


def verify(path):
    """校验备份可用：能打开、能过完整性检查、核心表在。

    返回 ``(ok, message)``。恢复是不可逆操作，先验证再动线上库。
    """
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error as exc:
        return False, f"无法打开备份：{exc}"
    try:
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "备份内部结构损坏（integrity_check 未通过）"
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            return False, f"备份无法读取：{exc}"
        missing = {"accounts", "keys", "settings"} - tables
        if missing:
            return False, f"备份缺少数据表：{'、'.join(sorted(missing))}"
        return True, "ok"
    finally:
        conn.close()


def stale_pending(root):
    """找出上次恢复留下的中转文件，启动时续跑。

    恢复要「关闭库 → 覆盖文件 → 重开」，进程在中间任何一步挂掉，线上库不是
    空的就是旧的。所以覆盖前先把已校验的备份放到固定中转名，启动时发现中转名
    存在就说明上次没走完，**优先把它补上**——宁可多恢复一次，也好过留一个
    半截库在那跑。
    """
    pending = Path(root) / "restore-pending.db"
    if not pending.is_file():
        return None
    return pending


def begin_restore(root, source, db_path):
    """把 source 放到中转文件并原子替换 db_path，返回中转文件路径。

    调用方必须先关掉数据库连接：Windows 与部分文件系统上替换被占用的文件会
    失败，且即便成功，旧连接持有的页缓存也会在之后写回、把刚恢复的内容覆盖掉。

    旧库的 -wal/-shm 在**替换前**就改名移走：留着的风险是半截状态——
    os.replace(pending, db_path) 之后、finish_restore 之前若进程挂掉，
    db 已是新库而 wal 还是旧库的，启动续跑只认 stale_pending()（那时已返回
    None），不会有人来清这份 wal，新库会被当成「wal 里还有未合并帧」而读出
    上一份数据。改成先移走，finish_restore 就只剩删除已不存在的文件这一步，
    中途崩溃也不再留半截状态。
    """
    root = Path(root)
    pending = root / "restore-pending.db"
    tmp = root / "restore-pending.db.tmp"
    shutil.copyfile(source, tmp)
    os.replace(tmp, pending)
    if db_path is not None:
        for suffix in ("-wal", "-shm"):
            stale = Path(str(db_path) + suffix)
            if stale.exists():
                os.replace(stale, root / f"restore-stale{suffix}")
        os.replace(pending, db_path)
    return pending


def finish_restore(root):
    """确认恢复完成，清掉可能残留的中转文件与旧 WAL。"""
    root = Path(root)
    (root / "restore-pending.db").unlink(missing_ok=True)
    (root / "restore-pending.db.tmp").unlink(missing_ok=True)
    (root / "restore-stale-wal").unlink(missing_ok=True)
    (root / "restore-stale-shm").unlink(missing_ok=True)


def due(settings, now=None):
    """判断定时备份是否到期。``settings`` 是 pool 设置字典。"""
    if not settings.get("backup_enabled"):
        return False
    interval = int(settings.get("backup_interval_hours") or 0)
    if interval <= 0:
        return False
    now = now or time.time()
    last = int(settings.get("backup_last_at") or 0)
    if last <= 0:
        return True
    return now - last >= interval * 3600


def next_check_delay(settings, now=None, tick=TICK_SECONDS):
    """下一次该在多少秒后检查。"""
    if not settings.get("backup_enabled"):
        return tick
    interval = int(settings.get("backup_interval_hours") or 0)
    if interval <= 0:
        return tick
    now = now or time.time()
    last = int(settings.get("backup_last_at") or 0)
    if last <= 0:
        return 0
    remaining = last + interval * 3600 - now
    return max(1, min(tick, remaining))


def human_size(size):
    # size 在循环里已被缩放到当前单位，format 时**不能再除 1024**——
    # 曾经的 bug：判的是缩放后的值、format 又除一次，所有非 B 档都变成
    # 「0.0 KB / 0.0 MB」（用户 2026-10-17 发现的「备份怎么都是 0M」）。
    size = int(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def describe_age(created, now=None):
    delta = (now or time.time()) - created
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def retention_cutoff(days, now=None):
    return (now or time.time()) - days * 86400


def is_expired(created, days, now=None):
    return days > 0 and created < retention_cutoff(days, now)


def human_time(ts):
    return datetime.fromtimestamp(ts, CN).strftime("%Y-%m-%d %H:%M:%S")


def schedule_hint(settings):
    """给界面显示的一句人类可读的定时备份说明。"""
    if not settings.get("backup_enabled"):
        return "自动备份已关闭"
    interval = int(settings.get("backup_interval_hours") or 0)
    if interval <= 0:
        return "自动备份已开启，但间隔为 0，不会触发"
    last = int(settings.get("backup_last_at") or 0)
    if last <= 0:
        return f"每 {interval} 小时一次，尚未执行首次备份"
    nxt = human_time(last + interval * 3600)
    return f"每 {interval} 小时一次，下次约 {nxt}"


def remaining_until(settings, now=None):
    if not settings.get("backup_enabled"):
        return None
    interval = int(settings.get("backup_interval_hours") or 0)
    if interval <= 0:
        return None
    last = int(settings.get("backup_last_at") or 0)
    if last <= 0:
        return 0
    return max(0, int(last + interval * 3600 - (now or time.time())))


__all__ = [
    "MAX_BYTES", "TICK_SECONDS", "backup_dir", "begin_restore", "create",
    "describe_age", "due", "filename", "finish_restore", "human_size",
    "human_time", "is_expired", "list_backups", "next_check_delay", "prune",
    "remaining_until", "remove", "resolve", "retention_cutoff", "safe_path",
    "schedule_hint", "stale_pending", "verify",
]

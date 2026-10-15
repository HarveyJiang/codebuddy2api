"""Small, persistent management layer for workbuddy2api. Single-process deployment."""
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from .models import CN

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from core import converter
from . import config, logging_setup
from .browser_login import BrowserLogin
from .db import UPSTREAM_GROUPS, Database
from .pool import AccountPool, PoolMiddleware, ROUTING_POLICIES
from .metrics import RequestMetrics, MetricsMiddleware
from .models import CN, ModelRegistry
from . import backup as db_backup
from . import version as app_version

COOKIE = "workbuddy_admin"
MAX_BODY = 1024 * 1024
ASSETS = Path(__file__).parent / "static"
# 健康库的字节下限。文件明显小于一页（SQLite 页大小 4096）就说明对象根本不是
# 我们的库。注意这个下界只挡「明显不是数据库」，**不能**用来判断库是否为空——
# 空库经备份接口产出后正好被撑到 4096 字节，卡在下界上，内容判定须交给 verify()。
_MIN_HEALTHY_DB_BYTES = 4096


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def clean_name(value, fallback):
    if value is None:
        return fallback
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
        raise HTTPException(400, "名称需为 1–60 个字符")
    return value.strip()


def _id_list(value):
    """把客户端传来的模型/账号 id 列表清洗成去重后的字符串列表（空 = 不限制）。"""
    if not isinstance(value, list):
        return []
    result, seen = [], set()
    for item in value:
        if isinstance(item, str) and item and item not in seen and len(item) <= 128:
            seen.add(item)
            result.append(item)
    return result


def _query_int(params, name, default=None):
    """查询参数转整数；空值取默认，非法值明确 400（不让它变成 500）。"""
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, f"{name} 需为整数")


def _query_time(value, name):
    """接受秒级 unix 整数或 'YYYY-MM-DD HH:MM:SS'（北京时间），统一转 unix 秒。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=CN).timestamp())
        except ValueError:
            continue
    raise HTTPException(400, f"{name} 需为秒级时间戳或 YYYY-MM-DD HH:MM:SS")


def _log_rows(rows):
    """请求日志行 → 前端结构：补 ok 布尔（新表只存 outcome，不再存 ok）。"""
    result = []
    for row in rows:
        item = dict(row)
        item["ok"] = item.get("outcome") == "success"
        result.append(item)
    return result


# save() 里 kv 脏标记的初始值：与任何真实值都不同，保证首次一定写入。
_MISSING = object()

# accounts 表拆成「实体字段 + account_status 字段」，这里是实体的那几列。
ENTITY_FIELDS = ("name", "file", "enabled", "created")


class Store:
    def __init__(self, root, auth_dir, initial_key, admin_key):
        if len(admin_key) < 20:
            raise RuntimeError("ADMIN_KEY must contain at least 20 characters")
        self.root, self.auth_dir = Path(root), Path(auth_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.admin_digest = digest(admin_key)
        self.sessions, self.attempts = {}, {}
        self.test_lock = asyncio.Lock()
        self.test_keys = set()
        self.managers = {}
        self.started = time.time()
        self.events = deque(maxlen=40)
        self.path = self.root / "state.db"
        # 恢复期间置位：库已被关掉、进程即将退出，此时到达的请求必须快速失败，
        # 而不是撞在已关闭的连接上报 500（"Cannot operate on a closed database"）。
        self.restoring = False
        # SQLite 是唯一持久层；内存 self.data 是工作副本，写操作后统一落库。
        self.db = Database(self.path)
        self.data = {"accounts": {}, "account_status": {}, "keys": {}, "pool": {},
                     "session_bindings": {}}
        self._dirty = {}  # 脏标记：值未变则跳过落库，避免每请求重写大 blob
        self.keys_dirty = False  # key 用量/管理改动等 save() 时才会落盘的字段
        self.log_buffer = []  # request_logs 批量缓冲，随 5 秒防抖落库
        self._load()
        if not self.data["accounts"] and not self.data["keys"]:
            # 全新部署（数据库为空）：从凭据目录导入已有凭据。
            for p in sorted(self.auth_dir.glob("*.info")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    self.validate_credential(doc)
                except (ValueError, OSError, HTTPException):
                    continue
                aid = secrets.token_hex(8)
                name = str(doc.get("account", {}).get("nickname") or "已迁移账号")[:60]
                self.data["accounts"][aid] = {"name": name, "file": p.name, "enabled": not bool(doc.get("disabled")), "created": int(time.time())}
            if initial_key and not any(item["hash"] == digest(initial_key) for item in self.data["keys"].values()):
                self.add_key("原有 API Key", value=initial_key)
            self.save()
        self.sync_any_enabled()

    def _load(self):
        """从 SQLite 恢复内存状态。

        accounts 一行装的是「实体字段 + account_status 字段」，这里按内存结构拆回
        data['accounts'] / data['account_status']；金额已在 Database.load_accounts 里
        ÷1e6 还原成 float。keys 同理：usage 三列重新组装成内存里的 usage dict。
        """
        for aid, merged in self.db.load_accounts().items():
            self.data["accounts"][aid] = {k: merged.get(k) for k in ENTITY_FIELDS}
            # 只保留非 NULL 的状态字段：以前 account_status 里只有实际写过的键，
            # 下游大量用 status.get(x, 默认值)，让 NULL 值也占位会把默认值吃掉。
            self.data["account_status"][aid] = {
                k: v for k, v in merged.items() if k not in ENTITY_FIELDS and v is not None}
        for kid, row in self.db.load_keys().items():
            self.data["keys"][kid] = {
                "name": row.get("name", ""), "hash": row.get("hash", ""),
                "hint": row.get("hint", ""), "created": row.get("created", 0),
                "enabled": row.get("enabled", 1), "revoked_at": row.get("revoked_at"),
                "expires_at": row.get("expires_at"), "daily_limit": row.get("daily_limit"),
                "usage": {"date": row.get("usage_date"), "count": row.get("usage_count", 0),
                          "tokens": row.get("usage_tokens", 0)},
                "models": row.get("models") or [], "accounts": row.get("accounts") or [],
                "routing": row.get("routing")}
        settings = self.db.get_settings()
        # 池设置可能来自旧数据（缺新字段），用默认值补齐缺失项。
        self.data["pool"] = {**AccountPool.POOL_DEFAULTS, **(settings or {})}
        self.data["session_bindings"] = self.db.load_bindings()

    @staticmethod
    def validate_credential(doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict) or not isinstance(doc.get("account"), dict):
            raise HTTPException(400, "需要桌面端 .info／JSON 登录文件，包含 auth 和 account 对象")
        for key in ("accessToken", "refreshToken"):
            if not isinstance(doc["auth"].get(key), str) or not doc["auth"][key].strip():
                raise HTTPException(400, "凭据缺少 accessToken 或 refreshToken，请重新导出桌面端登录文件")
            if len(doc["auth"][key]) > 65536:
                raise HTTPException(400, "凭据字段过长")
        if not isinstance(doc["account"].get("uid"), str) or not doc["account"]["uid"]:
            raise HTTPException(400, "凭据缺少 account.uid")
        if not isinstance(doc["auth"].get("expiresAt"), (int, float)) or isinstance(doc["auth"].get("expiresAt"), bool):
            raise HTTPException(400, "凭据需要有效的 expiresAt 时间戳")
        for key in ("domain",):
            value = doc["auth"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "凭据包含无效字段")
        for key in ("uid", "enterpriseId"):
            value = doc["account"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "账号字段格式不正确")

    def save(self):
        """全量落库：每张状态表各写一次，与 request_logs 批量缓冲同处一个事务。

        整份状态收进单个事务，恢复旧 write_json「要么全新要么全旧」的原子性——
        否则账号已提交而密钥尚未提交时崩溃，重启后两边对不上。

        pool / session_bindings 带脏标记：只有内容真的变化时才写（模型目录序列化后
        约 20KB，每请求重写会明显拖慢请求路径）。脏标记存深拷贝快照——若直接存引用，
        原地修改会同时改到快照，导致永远判定为「未变化」而漏写。
        """
        rows = self.log_buffer
        self.log_buffer = []
        try:
            with self.lock, self.db.transaction():
                self.db.save_accounts(self.data["accounts"], self.data["account_status"])
                self.db.save_keys(self.data["keys"])
                # 当日用量快照随 keys 一起写（覆盖语义，见 db.save_key_usage_daily）。
                # 放同一事务：keys 落库而历史没落库会造成两边不一致。
                self.db.save_key_usage_daily(self.data["keys"])
                self.keys_dirty = False
                for key, dump, load in (("pool", self.db.save_settings, None),
                                        ("session_bindings", self.db.save_bindings, None)):
                    value = self.data.get(key)
                    if value != self._dirty.get(key, _MISSING):
                        dump(value)
                        self._dirty[key] = copy.deepcopy(value)
                if rows:
                    self.db.append_request_logs(rows)
        except BaseException:
            # 提交失败（磁盘满/IO 错误）时事务已回滚，把这批日志放回缓冲，
            # 下次 flush 重试。否则它们会永久消失且没有任何提示。
            self.log_buffer[:0] = rows
            raise

    def queue_request_log(self, row):
        """请求日志只入内存缓冲，由 5 秒防抖的 flush() 统一落库。"""
        with self.lock:
            self.log_buffer.append(row)

    def save_browser_account(self, doc, name):
        self.validate_credential(doc)
        with self.lock:
            identity = (doc["account"]["uid"], doc["account"].get("enterpriseId", ""))
            for aid, item in self.data["accounts"].items():
                try:
                    old = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if (old.get("account", {}).get("uid"), old.get("account", {}).get("enterpriseId", "")) == identity:
                    current = self.manager_for(aid, item)
                    if current:
                        with current._lock:
                            write_json(self.file_for(item), doc)
                            current._cached = None
                    else:
                        write_json(self.file_for(item), doc)
                    if name:
                        item["name"] = name
                    self.data.get("account_status", {}).pop(aid, None)
                    self.save()
                    self.sync_any_enabled()
                    return {"account_id": aid, "updated": True}
            if len(self.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            write_json(self.auth_dir / (aid + ".info"), doc)
            self.data["accounts"][aid] = {"file": aid + ".info", "name": name or str(doc["account"].get("nickname") or "浏览器授权账号")[:60], "created": int(time.time()), "enabled": True}
            self.save()
            self.sync_any_enabled()
            return {"account_id": aid, "updated": False}

    def file_for(self, item):
        path = self.auth_dir / item["file"]
        if path.parent.resolve() != self.auth_dir.resolve() or path.is_symlink():
            raise HTTPException(400, "凭据路径不合法")
        return path

    def sync_any_enabled(self):
        """把 CONFIG['cred'] 指向任一启用账号。

        去掉全局 active 后，请求路径靠 REQUEST_CREDENTIAL 拿到具体账号；
        CONFIG['cred'] 只用于 /health 与管理页测试的「有没有可用账号」判断，
        因此指向任一启用账号即可，不代表某个路由目标。
        """
        manager = self.any_enabled_manager()
        converter.CONFIG["cred"] = manager if manager is not None else None

    def manager_for(self, aid, item):
        with self.lock:
            if aid not in self.managers:
                self.managers[aid] = converter.CredentialManager(self.file_for(item))
            return self.managers[aid]

    def any_enabled_manager(self):
        """A credential manager from any enabled account, for background pulls (model
        catalog) and the /health 'is any account usable' check."""
        with self.lock:
            for aid, item in self.data["accounts"].items():
                if item["enabled"] and self.auth_dir.joinpath(item["file"]).exists():
                    return self.manager_for(aid, item)
            return None

    def account_rows(self):
        result = []
        for aid, item in self.data["accounts"].items():
            row = {"id": aid, "name": item["name"], "enabled": item["enabled"], "created": item["created"]}
            try:
                doc = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                expiry = doc["auth"].get("expiresAt", 0)
                row.update({"nickname": str(doc["account"].get("nickname") or ""), "uid": str(doc["account"].get("uid") or ""), "expires_at": expiry, "expired": expiry < time.time() * 1000, "refresh_available": bool(doc["auth"].get("refreshToken")), "status": "ready"})
            except (ValueError, OSError, KeyError):
                row.update({"status": "invalid", "expired": True, "expires_at": 0, "refresh_available": False})
            result.append(row)
        return result

    def health_detail(self):
        """深度健康检查的判据来源。

        「可用」= 启用 + 凭据文件存在且可解析 + 未被标 auth_invalid。
        注意这里**不打上游接口**：health 必须廉价，不能每次调用都产生外部请求。
        """
        rows = self.account_rows()
        by_id = {row["id"]: row for row in rows}
        total = enabled = usable = auth_invalid = 0
        for aid, item in self.data["accounts"].items():
            total += 1
            if not item["enabled"]:
                continue
            enabled += 1
            row = by_id.get(aid) or {}
            status = self.data["account_status"].get(aid) or {}
            # 必须与 pool.py 的判据一致：账号池标记认证失效用的是布尔字段
            # auth_invalid（不是 status="auth_invalid" 这个字符串，生产代码从不写它）。
            # 判据漂移会让「所有账号都失效」这种最该被发现的故障反而报 ok。
            if row.get("status") == "invalid" or status.get("auth_invalid"):
                auth_invalid += 1
                continue
            if row.get("status") == "ready":
                usable += 1
        return {"total": total, "enabled": enabled, "usable": usable,
                "auth_invalid": auth_invalid, "rows": rows}

    def add_key(self, name, daily_limit=None, models=None, accounts=None, value=None):
        """创建客户端密钥。models/accounts 为空列表表示不限制（全部可用）。

        value：指定固定密钥值（供 CODEBUDDY2OPENAI_KEY 迁移使用），缺省随机生成。
        """
        value = value or "sk-wb-" + secrets.token_urlsafe(32)
        kid = secrets.token_hex(8)
        limit = None
        if daily_limit is not None:
            try:
                limit = int(daily_limit)
            except (TypeError, ValueError):
                raise HTTPException(400, "每日上限需为整数")
            if limit < 0:
                raise HTTPException(400, "每日上限不能为负")
        self.data["keys"][kid] = {"name": name, "hash": digest(value), "hint": value[:5] + "…" + value[-4:], "created": int(time.time()), "daily_limit": limit, "usage": {},
                                  "enabled": 1, "revoked_at": None, "expires_at": None,
                                  # routing=None 表示动态跟随 settings.default_routing。
                                  "routing": None,
                                  # 空列表 = 不限制：该密钥可使用所有已启用模型 / 所有可用账号。
                                  "models": _id_list(models), "accounts": _id_list(accounts)}
        self.keys_dirty = True
        return kid, value

    def key_bindings(self, kid):
        """返回该密钥的 (允许的模型集合或 None, 允许的账号集合或 None)。"""
        item = self.data["keys"].get(kid) or {}
        models = _id_list(item.get("models"))
        accounts = _id_list(item.get("accounts"))
        return (set(models) or None, set(accounts) or None)

    def _today(self):
        return datetime.fromtimestamp(time.time(), timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    @staticmethod
    def key_unusable(item):
        """返回不可用原因；None=可用。停用可恢复，撤销不可逆。"""
        if item.get("revoked_at"):
            return "该密钥已被撤销"
        if not item.get("enabled", 1):
            return "该密钥已停用"
        expires = item.get("expires_at")
        if expires and expires <= time.time():
            return "该密钥已过期"
        return None

    def check_api(self, authorization, x_api_key):
        """Return the matching key id, or None for the legacy single admin key."""
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else x_api_key or ""
        with self.lock:
            hashed = digest(token)
            for kid, item in self.data["keys"].items():
                if hmac.compare_digest(hashed, item["hash"]):
                    reason = self.key_unusable(item)
                    if reason:
                        raise HTTPException(401, reason)
                    usage = item.setdefault("usage", {})
                    today = self._today()
                    if usage.get("date") != today:
                        usage.clear()
                        usage["date"] = today
                    limit = item.get("daily_limit")
                    if limit is not None and usage.get("count", 0) >= limit:
                        raise HTTPException(429, "该密钥今日请求数已达上限")
                    return kid
            if hashed in self.test_keys:
                return None
        raise HTTPException(401, "invalid api key")

    def record_key_usage(self, kid, tokens=None):
        """只改内存，落库交给 5 秒防抖的 flush()。

        原实现在这里直接 save()，等于每个 API 请求全量重写 keys/accounts；
        攒批之后批量写才真正省得下来。
        """
        with self.lock:
            item = self.data["keys"].get(kid)
            if not item:
                return
            usage = item.setdefault("usage", {})
            today = self._today()
            if usage.get("date") != today:
                usage.clear()
                usage["date"] = today
            usage["count"] = usage.get("count", 0) + 1
            if tokens:
                usage["tokens"] = usage.get("tokens", 0) + int(tokens)
            self.keys_dirty = True

    # -- 数据库备份 ---------------------------------------------------------

    def backup_now(self, reason="manual"):
        """生成一份备份并记录到设置里。返回备份信息；失败抛出异常由调用方转 500。

        走 ``db_backup.create``（sqlite 在线备份），不直接拷文件——WAL 模式下
        拷主库会漏掉还没 checkpoint 的已提交数据。
        """
        settings = self.data["pool"]
        self.db.checkpoint()  # 顺手合并 WAL：备份本身不依赖它，但能让库保持紧凑
        name = db_backup.create(self.root, self.path, keep=int(settings.get("backup_keep") or 10),
                                minimal_size=_MIN_HEALTHY_DB_BYTES)
        with self.lock:
            self.data["pool"]["backup_last_at"] = int(time.time())
            self.data["pool"]["backup_last_name"] = name
        self.save()
        # 只走 logging，**不**写 self.events——那个 deque 是「连接测试」页的
        # 测试记录，前端按 {model, ok, seconds} 渲染（见 app.js），塞进备份事件
        # 会让那一栏出现「undefined / 成功」的空行。
        logging.getLogger("admin.backup").info("数据库备份完成（%s）：%s", reason, name)
        return {"name": name, "reason": reason}

    def backup_list(self):
        """备份列表 + 设置里的定时状态，供设置页展示。"""
        settings = self.data["pool"]
        now = time.time()
        items = []
        for item in db_backup.list_backups(self.root):
            items.append({
                "name": item["name"], "size": item["size"], "created": item["created"],
                "size_text": db_backup.human_size(item["size"]),
                "created_text": db_backup.human_time(item["created"]),
                "age_text": db_backup.describe_age(item["created"], now),
                "is_last": item["name"] == settings.get("backup_last_name"),
            })
        return {
            "items": items,
            "count": len(items),
            "dir": str(db_backup.backup_dir(self.root)),
            "schedule": db_backup.schedule_hint(settings),
            "remaining": db_backup.remaining_until(settings, now),
            "last_at": int(settings.get("backup_last_at") or 0),
            "last_name": settings.get("backup_last_name") or "",
        }

    def require_admin(self, req):
        sid = req.cookies.get(COOKIE, "")
        with self.lock:
            record = self.sessions.get(digest(sid))
            if not record or record["expires"] < time.time():
                raise HTTPException(401, "请先登录管理后台")
        if req.method not in ("GET", "HEAD"):
            if not hmac.compare_digest(req.headers.get("X-CSRF-Token", ""), record["csrf"]):
                raise HTTPException(403, "页面已失效，请重新登录")
        return record


class AdminMiddleware:
    """管理页中间件：限体积、加安全响应头，并在数据库恢复期间守住入口。

    ``store`` 可为 None（不传即跳过恢复期检查），保持这个中间件能被单独测试。
    """

    def __init__(self, app, store=None):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            return await self.app(scope, receive, send)
        # 恢复期间库已关闭、进程即将退出。这里直接 503，避免请求撞在已关闭的
        # sqlite 连接上抛 ProgrammingError（表现为 500 + 一屏堆栈）。
        if self.store is not None and self.store.restoring:
            response = JSONResponse(
                {"detail": "数据库正在恢复，服务即将重启，请稍候刷新"}, 503)
            return await response(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                response = JSONResponse({"detail": "文件或请求超过 1 MB"}, 413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)
        await self.app(scope, replay, secure_send)


def _warn_legacy_config():
    """启动时汇总提示仍在使用的旧配置名（一次，不是每个变量一行）。

    旧名继续生效，只是提示用户改用 WORKBUDDY_* 规范名。汇总成一行是为了不让几条
    提示把启动横幅冲散——这些是「可以改但不必急着改」的信息，不该淹没真正的错误。
    """
    legacy = config.legacy_used()
    if not legacy:
        return
    pairs = "、".join(f"{old}→{new}" for old, new in legacy)
    logging.getLogger("admin.server").info(
        "以下配置使用旧名，仍生效，建议改为规范名：%s", pairs
    )


def _stale_catalog(registry):
    """缓存 catalog 是否为旧格式（描述含英文、或未按单价升序）时需重新拉取。"""
    models = registry.snapshot().get("models") or []
    if not models:
        return False
    for entry in models:
        desc = entry.get("description") or ""
        # 旧逻辑把 descriptionEn 用「 · 」拼在后面。
        if " · " in desc:
            return True
    # 旧逻辑直接沿用上游 cli 菜单顺序，未按单价升序重排。
    credits = [m.get("credits") if isinstance(m.get("credits"), (int, float)) else 0
               for m in models]
    return credits != sorted(credits)


def create_app(root=None, auth_dir=None, initial_key=None, admin_key=None, secure_cookie=True):
    cfg = config.get()
    # 只在本进程还没配过日志时初始化：让 `uvicorn admin.server:create_app` 这类
    # 导入式启动也有日志出口，同时不覆盖调用方（或测试）已装好的 handler。
    if not logging_setup.configured():
        logging_setup.setup_logging(cfg.log_level, cfg.log_path)
    _warn_legacy_config()
    data_root = Path(root or cfg.data_dir)
    # 续跑上次没走完的恢复：恢复要「关库 → 覆盖 → 重开」，进程若在中间挂掉，
    # state.db 可能已被替换成新库、或压根还是旧的。中转文件在，说明没走完，
    # 这里补上——宁可多恢复一次，也不能让半截状态留在线上跑。
    pending = db_backup.stale_pending(data_root)
    if pending is not None:
        try:
            Path(data_root / "state.db").unlink(missing_ok=True)
            os.replace(pending, data_root / "state.db")
            db_backup.finish_restore(data_root)
            logging.getLogger("admin.backup").warning(
                "检测到中断的数据库恢复，已补完并继续启动")
        except OSError:
            logging.getLogger("admin.backup").exception(
                "中断的数据库恢复未能补完，已保留中转文件待下次启动重试")
    store = Store(root or cfg.data_dir, auth_dir or cfg.auth_dir, initial_key if initial_key is not None else cfg.api_key, admin_key or cfg.admin_key)
    converter._check_auth = store.check_api
    # 以前这里把 log_path 硬覆盖成 None，导致设了 WORKBUDDY_LOG/CODEBUDDY2OPENAI_LOG
    # 也不记日志。现在日志由 setup_logging 统一管，这里不再插手。
    converter.CONFIG.update({"desensitize": cfg.desensitize, "no_compact": cfg.no_compact,
                             "log_path": cfg.log_path})
    browser_login = BrowserLogin(store.save_browser_account)
    pool = AccountPool(store)
    metrics = RequestMetrics()
    model_registry = ModelRegistry(get_credential=store.any_enabled_manager)
    # 供账号池选降级模型用（池先建、注册表后建，这里回填）。
    pool.model_registry = model_registry
    model_registry.load(store.db)
    model_registry.ensure_builtin()
    # 缓存里的描述是旧逻辑（中文+英文拼接、上游原始顺序）时重新拉取一次，
    # 让展示立即变成「只有中文 + 按单价升序」。
    if _stale_catalog(model_registry):
        model_registry.fetch()
        model_registry.ensure_builtin()
        model_registry.persist(store.db)
    @asynccontextmanager
    async def lifespan(app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                await browser_login.cleanup()
        cleanup_task = asyncio.create_task(reap())
        async def pool_worker():
            while True:
                try:
                    await pool.tick()
                except Exception:
                    pass  # Per-account errors are persisted without credential data.
                await asyncio.sleep(60)
        pool_task = asyncio.create_task(pool_worker())
        async def backup_worker():
            # 定时备份：每轮只做「醒来看一眼该不该跑」，间隔由设置决定。
            # 单轮失败不退出循环——磁盘一时写满不该让自动备份永久停摆。
            while True:
                try:
                    settings = store.data["pool"]
                    if db_backup.due(settings):
                        await asyncio.to_thread(store.backup_now, "scheduled")
                    delay = db_backup.next_check_delay(settings)
                except Exception:
                    logging.getLogger("admin.backup").exception("自动备份失败")
                    delay = db_backup.TICK_SECONDS
                await asyncio.sleep(max(1, delay))
        backup_task = asyncio.create_task(backup_worker())
        async def models_worker():
            # 每天在 04:00-05:00 之间随机选一分钟刷新模型配置（上游免鉴权接口，
            # 低峰期随机时刻，避免固定时间打点）。
            while True:
                now = datetime.now(CN)
                next_run = (now + timedelta(days=1)).replace(hour=4, minute=random.randint(0, 59), second=random.randint(0, 59), microsecond=0)
                if not (4 <= now.hour < 5):
                    today = now.replace(hour=4, minute=random.randint(0, 59), second=random.randint(0, 59), microsecond=0)
                    if today > now:
                        next_run = today
                await asyncio.sleep(max(60, (next_run - now).total_seconds()))
                model_registry.fetch()
                model_registry.persist(store.db)
        models_task = asyncio.create_task(models_worker())
        try:
            yield
        finally:
            cleanup_task.cancel()
            pool_task.cancel()
            models_task.cancel()
            backup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await browser_login.close()
            # 优雅关闭：把 WAL 里未 checkpoint 的帧合并进主库再断开。
            store.db.close()
            for task in (pool_task, models_task, backup_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.browser_login = browser_login
    app.state.pool = pool
    app.state.metrics = metrics
    app.state.model_registry = model_registry
    # Route converter's model list through the registry (dynamic + managed),
    # keeping the legacy file-based loader as last-resort fallback.
    def _managed_models():
        models = model_registry.enabled_models()
        return models or converter.get_available_models()
    converter.get_available_models = _managed_models
    app.add_middleware(AdminMiddleware, store=store)
    app.add_middleware(PoolMiddleware, pool=pool, model_registry=model_registry)
    app.add_middleware(MetricsMiddleware, metrics=metrics, store=store)

    async def payload(req):
        try:
            body = await req.json()
        except ValueError:
            raise HTTPException(400, "请求不是有效 JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "请求格式不正确")
        return body

    @app.get("/")
    async def home():
        return RedirectResponse("/admin/", 302)

    @app.get("/health")
    async def health(verbose: int = 0):
        """深度健康检查：区分「进程活着」与「真的能服务」。

        - ok：有可用账号且库可读
        - degraded：库可读但一个可用账号都没有（凭据全失效/全暂停）
        - down：状态无法判定（库坏了），HTTP 503

        degraded **仍返回 200**：重启修不好凭据失效（要人重新授权），把它标成
        unhealthy 会让容器进入重启循环，反而更难观察。只有进程已无法正常工作才用 503。
        """
        try:
            detail = store.health_detail()
        except Exception as exc:  # 库不可读等：进程已不能服务
            logging.getLogger("admin.server").error("健康检查失败：%s", exc)
            return JSONResponse({"status": "down", "checks": {"db": "error"}}, 503)

        usable = detail["usable"]
        status = "ok" if usable else "degraded"
        body = {
            "status": status,
            "version": app_version.describe(),
            "uptime_s": int(time.time() - store.started),
            "accounts": {"total": detail["total"], "enabled": detail["enabled"],
                         "usable": usable, "auth_invalid": detail["auth_invalid"]},
            "checks": {"db": "ok", "accounts": status},
        }
        if verbose:
            # 默认不返回账号名/uid，避免未鉴权端点泄露信息；排查时用 ?verbose=1。
            body["accounts"]["detail"] = [
                {"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]),
                 "status": r.get("status"), "expired": bool(r.get("expired"))}
                for r in detail["rows"]
            ]
        return body

    @app.get("/admin")
    async def redirect_admin():
        return RedirectResponse("/admin/", 302)

    @app.get("/admin/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/admin/assets/{filename}")
    async def asset(filename: str):
        if filename not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / filename)

    @app.post("/admin/api/login")
    async def login(req: Request):
        body = await payload(req)
        # Nginx overwrites X-Real-IP; the application port is loopback-only.
        ip = req.headers.get("x-real-ip") or (req.client.host if req.client else "unknown")
        now = time.time()
        with store.lock:
            store.attempts = {k: v for k, v in store.attempts.items() if v[-1] > now - 600}
            failures = [t for t in store.attempts.get(ip, []) if t > now - 600]
            if len(failures) >= 8:
                raise HTTPException(429, "尝试次数过多，请 10 分钟后重试")
            value = body.get("key")
            if not isinstance(value, str) or not hmac.compare_digest(digest(value), store.admin_digest):
                store.attempts[ip] = failures + [now]
                raise HTTPException(401, "管理密钥不正确")
            store.attempts.pop(ip, None)
            store.sessions = {k: v for k, v in store.sessions.items() if v["expires"] > now}
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            if len(store.sessions) >= 100:
                store.sessions.pop(next(iter(store.sessions)))
            store.sessions[digest(sid)] = {"csrf": csrf, "expires": now + 12 * 3600}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(COOKIE, sid, secure=secure_cookie, httponly=True, samesite="strict", path="/admin", max_age=12 * 3600)
        return response

    @app.get("/admin/api/session")
    async def session(req: Request):
        return {"csrf": store.require_admin(req)["csrf"]}

    @app.post("/admin/api/logout")
    async def logout(req: Request):
        store.require_admin(req)
        await browser_login.cancel_owner(digest(req.cookies.get(COOKIE, "")))
        with store.lock:
            store.sessions.pop(digest(req.cookies.get(COOKIE, "")), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/admin", secure=secure_cookie, httponly=True, samesite="strict")
        return response

    @app.post("/admin/api/oauth/start")
    async def oauth_start(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), None)
        try:
            return await browser_login.start(digest(req.cookies.get(COOKIE, "")), name)
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权服务连接失败，请稍后重试")

    @app.post("/admin/api/oauth/{fid}/poll")
    async def oauth_poll(fid: str, req: Request):
        store.require_admin(req)
        try:
            return await browser_login.poll(fid, digest(req.cookies.get(COOKIE, "")))
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权状态暂时无法获取，请稍后重试")

    @app.delete("/admin/api/oauth/{fid}")
    async def oauth_cancel(fid: str, req: Request):
        store.require_admin(req)
        return await browser_login.cancel(fid, digest(req.cookies.get(COOKIE, "")))

    @app.get("/admin/api/overview")
    async def overview(req: Request):
        store.require_admin(req)
        # recent 直接读 request_logs（跨重启保留、含账号/密钥/模型维度）。
        recent = _log_rows(store.db.recent_request_logs(100))
        # 仪表盘累计口径同样来自 request_logs：内存 metrics 每次重启清零，
        # 这里用库内统计覆盖计数类字段，只保留 in_flight 这种进程内的实时值。
        summary = store.db.request_log_summary()
        with store.lock:
            keys = [{"id": kid, "usage": item.get("usage") or {}, **{k: v for k, v in item.items() if k != "hash"}} for kid, item in store.data["keys"].items()]
            live = metrics.snapshot()
            merged = {**live, **summary,
                      "in_flight": live["in_flight"], "started_at": summary["first_time"] or live["started_at"],
                      "recent": recent, "affinity_hits": pool.affinity_hits, "affinity_rebinds": pool.affinity_rebinds,
                      # 实测成本账本规模。上游若从不返回 credit，这里会长期是 0——
                      # 这个数字就是判断「账本到底有没有在用」的唯一依据。
                      "cost_ledger": pool.cost_ledger.stats(),
                      # WAF IP 级拦截状态。激活期间轮转会提前收手，不放大风控。
                      "waf_gate": pool.waf_gate.snapshot()}
            return {"accounts": pool.rows(store.account_rows()), "pool": dict(store.data["pool"]), "pool_groups": {k: list(v) for k, v in AccountPool.SETTING_GROUPS.items()}, "metrics": merged, "keys": keys, "models": model_registry.snapshot(), "uptime": int(time.time() - store.started), "version": app_version.snapshot(), "events": list(store.events)}

    @app.get("/admin/api/logs")
    async def logs(req: Request):
        """分页读请求日志（sqlite，跨重启保留）。"""
        store.require_admin(req)
        params = req.query_params

        def as_int(name, default=None):
            raw = params.get(name)
            if raw in (None, ""):
                return default
            try:
                return int(raw)
            except ValueError:
                raise HTTPException(400, f"{name} 需为整数")
        result = store.db.page_request_logs(
            limit=as_int("limit", 50), offset=as_int("offset", 0),
            since=as_int("since"), until=as_int("until"),
            source=params.get("source") or None, outcome=params.get("outcome") or None)
        return {**result, "rows": _log_rows(result["rows"])}

    @app.get("/admin/api/logs/stats")
    async def log_stats(req: Request):
        store.require_admin(req)
        params = req.query_params

        def as_int(name):
            raw = params.get(name)
            if raw in (None, ""):
                return None
            try:
                return int(raw)
            except ValueError:
                raise HTTPException(400, f"{name} 需为整数时间戳")
        group_by = params.get("group_by") or None
        try:
            return store.db.request_log_stats(since=as_int("since"), until=as_int("until"),
                                              group_by=group_by, limit=500)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/admin/api/pool/refresh-credits")
    async def pool_refresh_credits(req: Request):
        store.require_admin(req)
        return await pool.refresh_credits()

    @app.get("/admin/api/keys/usage")
    async def key_usage(req: Request):
        """每个密钥按日的用量历史（供「今日用量」弹窗画趋势）。"""
        store.require_admin(req)
        params = req.query_params
        days = _query_int(params, "days", 30)
        if days is not None and not 1 <= days <= 365:
            raise HTTPException(400, "days 需在 1-365 之间")
        return store.db.key_usage_series(days=days or 30)

    @app.get("/admin/api/usage")
    async def usage(req: Request):
        """分页读上游用量明细：只查本地库，不打上游。"""
        store.require_admin(req)
        params = req.query_params
        result = store.db.page_upstream_usage(
            limit=_query_int(params, "limit", 100), offset=_query_int(params, "offset", 0),
            since=_query_time(params.get("start"), "start"),
            until=_query_time(params.get("end"), "end"),
            account_id=params.get("account_id") or None)
        return result

    @app.get("/admin/api/usage/report")
    async def usage_report(req: Request):
        """上游用量报表：按 day/account/model/client 分组聚合。"""
        store.require_admin(req)
        params = req.query_params
        group_by = params.get("group_by") or "day"
        if group_by not in UPSTREAM_GROUPS:
            raise HTTPException(400, f"不支持的分组维度：{group_by}")
        return store.db.upstream_usage_report(
            since=_query_time(params.get("start"), "start"),
            until=_query_time(params.get("end"), "end"),
            account_id=params.get("account_id") or None, group_by=group_by)

    @app.post("/admin/api/usage/sync")
    async def usage_sync(req: Request):
        """手动拉取上游用量：仅此处打上游，默认路径不碰外网。

        防抖三层：前端按钮禁用 + pool.jobs 并发锁 + 每账号 60 秒节流。
        返回 skipped 表示该账号被节流/占锁跳过，不是失败。
        """
        store.require_admin(req)
        body = await payload(req)
        now = datetime.fromtimestamp(time.time(), CN)
        end = _query_time(body.get("end"), "end") or int(now.timestamp())
        start = _query_time(body.get("start"), "start") or int(
            (now - timedelta(days=30)).timestamp())
        if start >= end:
            raise HTTPException(400, "开始时间必须早于结束时间")
        result = await pool.sync_upstream_usage_manual(
            datetime.fromtimestamp(start, CN), datetime.fromtimestamp(end, CN),
            body.get("account_id") or None)
        return result

    @app.post("/admin/api/models/refresh")
    async def models_refresh(req: Request):
        store.require_admin(req)
        ok = await asyncio.to_thread(model_registry.fetch)
        model_registry.persist(store.db)
        snapshot = model_registry.snapshot()
        if not ok:
            raise HTTPException(502, snapshot.get("error") or "拉取失败")
        return {"ok": True, "count": len(snapshot["models"])}

    @app.patch("/admin/api/models/{mid}")
    async def model_toggle(mid: str, req: Request):
        store.require_admin(req)
        body = await payload(req)
        if type(body.get("enabled")) is not bool:
            raise HTTPException(400, "enabled 需为布尔值")
        try:
            model_registry.set_enabled(mid, body["enabled"])
        except KeyError:
            raise HTTPException(404, "模型不存在")
        model_registry.persist(store.db)
        return {"ok": True}

    @app.post("/admin/api/models")
    async def model_add(req: Request):
        """手动添加模型。

        上游 /v3/config 返回的菜单不一定列全，有些 id 仍可调用。这里允许用户
        把它们补齐；新建的项 source=custom，fetch() 会合并回来而不会冲掉。
        已存在的上游模型走「只改用户可改字段」，不会变成自定义。
        """
        store.require_admin(req)
        body = await payload(req)
        mid = body.get("id")
        if not isinstance(mid, str) or not mid.strip():
            raise HTTPException(400, "模型 ID 不能为空")
        mid = mid.strip()
        if len(mid) > 128:
            raise HTTPException(400, "模型 ID 不能超过 128 个字符")
        # 这个 id 会进 URL 路径和 JSON，拒掉空格/换行/引号等易出错的字符。
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", mid):
            raise HTTPException(400, "模型 ID 只能包含字母、数字与 . _ : / -")
        name = body.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > 200):
            raise HTTPException(400, "显示名不能超过 200 个字符")
        description = body.get("description")
        if description is not None and (not isinstance(description, str)
                                        or len(description) > 1000):
            raise HTTPException(400, "描述不能超过 1000 个字符")
        credits = body.get("credits")
        if credits is not None:
            if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits < 0:
                raise HTTPException(400, "倍率需为空或非负数")
        max_in = body.get("max_input_tokens")
        if max_in is not None:
            if isinstance(max_in, bool) or not isinstance(max_in, int) or not 1 <= max_in <= 10_000_000:
                raise HTTPException(400, "上下文长度需为空或 1–10000000 的整数")
        enabled = body.get("enabled", True)
        if type(enabled) is not bool:
            raise HTTPException(400, "enabled 需为布尔值")
        source = model_registry.add(mid, enabled=enabled, name=(name or None),
                                    description=description, credits=credits,
                                    max_input_tokens=max_in)
        model_registry.persist(store.db)
        return {"ok": True, "id": mid, "source": source}

    @app.delete("/admin/api/models/{mid}")
    async def model_delete(mid: str, req: Request):
        """删除模型。只允许删自定义项。"""
        store.require_admin(req)
        try:
            model_registry.remove(mid)
        except KeyError:
            raise HTTPException(404, "模型不存在")
        except ValueError:
            # 上游模型删了没用：下一次 fetch() 会把它带回来，还会断掉日志 join。
            raise HTTPException(400, "上游模型只能禁用，不能删除")
        model_registry.persist(store.db)
        return {"ok": True}

    @app.post("/admin/api/accounts/{aid}/actions/{action}")
    async def account_action(aid: str, action: str, req: Request):
        store.require_admin(req)
        if action not in ("refresh", "status", "checkin"):
            raise HTTPException(404)
        return await asyncio.to_thread(pool.operate, aid, action)

    @app.post("/admin/api/pool/actions/{action}")
    async def pool_action(action: str, req: Request):
        store.require_admin(req)
        if action not in ("status", "checkin"):
            raise HTTPException(404)
        return await pool.batch(action)

    @app.patch("/admin/api/pool/settings")
    async def pool_settings(req: Request):
        store.require_admin(req)
        body = await payload(req)
        import re
        with store.lock:
            settings = dict(store.data["pool"])
            if "default_routing" in body:
                if body["default_routing"] not in ROUTING_POLICIES:
                    raise HTTPException(400, "调度方式无效")
                settings["default_routing"] = body["default_routing"]
            if "auto_checkin" in body:
                if type(body["auto_checkin"]) is not bool:
                    raise HTTPException(400, "签到开关无效")
                settings["auto_checkin"] = body["auto_checkin"]
            if "checkin_time" in body:
                if not isinstance(body["checkin_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["checkin_time"]):
                    raise HTTPException(400, "请选择有效的签到时间")
                settings["checkin_time"] = body["checkin_time"]
            if "credit_min_interval" in body:
                try:
                    interval = int(body["credit_min_interval"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "积分拉取间隔需为整数秒")
                if not 0 <= interval <= 3600:
                    raise HTTPException(400, "积分拉取间隔需在 0-3600 秒之间")
                settings["credit_min_interval"] = interval
            if "sync_start" in body or "sync_end" in body:
                pattern = r"(?:[01]\d|2[0-3]):[0-5]\d"
                start = body.get("sync_start", settings.get("sync_start"))
                end = body.get("sync_end", settings.get("sync_end"))
                if not (isinstance(start, str) and re.fullmatch(pattern, start)
                        and isinstance(end, str) and re.fullmatch(pattern, end)):
                    raise HTTPException(400, "预刷新时段需为两个 HH:MM 时间")
                settings["sync_start"], settings["sync_end"] = (
                    (start, end) if start <= end else (end, start))
            if "batch_spread" in body:
                try:
                    spread = int(body["batch_spread"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "批量签到错峰需为整数秒")
                if not 0 <= spread <= 600:
                    raise HTTPException(400, "批量签到错峰需在 0-600 秒之间")
                settings["batch_spread"] = spread
            if "queue_failover" in body:
                if type(body["queue_failover"]) is not bool:
                    raise HTTPException(400, "排队自动切换开关无效")
                settings["queue_failover"] = body["queue_failover"]
            if "queue_model_fallback" in body:
                if type(body["queue_model_fallback"]) is not bool:
                    raise HTTPException(400, "模型降级开关无效")
                settings["queue_model_fallback"] = body["queue_model_fallback"]
            if "queue_retry" in body:
                try:
                    retry = int(body["queue_retry"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "排队重试次数需为整数")
                if not 1 <= retry <= 10:
                    raise HTTPException(400, "排队重试次数需在 1-10 之间")
                settings["queue_retry"] = retry
            if "queue_cooldown" in body:
                try:
                    cooldown = int(body["queue_cooldown"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "排队让位时长需为整数秒")
                if not 0 <= cooldown <= 3600:
                    raise HTTPException(400, "排队让位时长需在 0-3600 秒之间")
                settings["queue_cooldown"] = cooldown
            if "queue_hold_ms" in body:
                try:
                    hold = int(body["queue_hold_ms"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "排队判定窗口需为整数毫秒")
                # 上界刻意收紧：这是「扣留响应头」的时长，越长越容易拖慢并发。
                if not 0 <= hold <= 10000:
                    raise HTTPException(400, "排队判定窗口需在 0-10000 毫秒之间")
                settings["queue_hold_ms"] = hold
            if "queue_fallback_model" in body:
                fallback = body["queue_fallback_model"]
                if not isinstance(fallback, str) or not fallback.strip() or len(fallback) > 100:
                    raise HTTPException(400, "兜底模型名无效")
                settings["queue_fallback_model"] = fallback.strip()
            # 账号级限额护栏（admin/guards.py）。上界给得宽，但要挡住明显误输入
            # （把 token 上限填成 1e9 等于没开，填成负数会绕过 0=关闭 的语义）。
            for field, label, upper in (
                    ("guard_reserve_credits", "保留余额", 100_000_000),
                    ("guard_daily_token_limit", "单账号日 token 上限", 10_000_000_000),
                    ("guard_daily_credit_limit", "单账号日积分上限", 100_000_000),
                    ("guard_model_daily_token_limit", "单账号单模型日 token 上限", 10_000_000_000)):
                if field not in body:
                    continue
                try:
                    value = int(body[field])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{label}需为整数")
                if not 0 <= value <= upper:
                    raise HTTPException(400, f"{label}需在 0-{upper} 之间（0 = 关闭）")
                settings[field] = value
            if "backup_enabled" in body:
                if type(body["backup_enabled"]) is not bool:
                    raise HTTPException(400, "自动备份开关无效")
                settings["backup_enabled"] = body["backup_enabled"]
            if "backup_interval_hours" in body:
                try:
                    hours = int(body["backup_interval_hours"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "自动备份间隔需为整数小时")
                # 下界 1 小时：更密没有意义，只会把保留份数挤爆。
                if not 1 <= hours <= 720:
                    raise HTTPException(400, "自动备份间隔需在 1-720 小时之间")
                settings["backup_interval_hours"] = hours
            if "backup_keep" in body:
                try:
                    keep = int(body["backup_keep"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "备份保留份数需为整数")
                if not 1 <= keep <= 100:
                    raise HTTPException(400, "备份保留份数需在 1-100 之间")
                settings["backup_keep"] = keep
            store.data["pool"] = settings
            store.save()
        return {"ok": True, "pool": settings}

    # -- 数据库备份 ---------------------------------------------------------

    @app.get("/admin/api/backups")
    async def backups_list(req: Request):
        store.require_admin(req)
        return await asyncio.to_thread(store.backup_list)

    @app.post("/admin/api/backups")
    async def backups_create(req: Request):
        """手动触发一次备份。文件可能到 MB 级，放到线程里做，别卡住事件循环。"""
        store.require_admin(req)
        try:
            result = await asyncio.to_thread(store.backup_now, "manual")
        except (OSError, RuntimeError) as exc:
            raise HTTPException(500, f"备份失败：{exc}")
        return {"ok": True, **result, **store.backup_list()}

    @app.get("/admin/api/backups/{name}/download")
    async def backups_download(name: str, req: Request):
        store.require_admin(req)
        path = db_backup.resolve(store.root, name)
        if path is None:
            raise HTTPException(404, "备份不存在")
        return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)

    @app.delete("/admin/api/backups/{name}")
    async def backups_delete(name: str, req: Request):
        store.require_admin(req)
        if not await asyncio.to_thread(db_backup.remove, store.root, name):
            raise HTTPException(404, "备份不存在")
        return {"ok": True, **store.backup_list()}

    @app.post("/admin/api/backups/{name}/restore")
    async def backups_restore(name: str, req: Request):
        """把指定备份恢复成线上库，然后重启进程。

        这是**破坏性操作**，分四步，每步都刻意留着可续跑：
        1. 先 verify()，备份本身不健康就直接拒绝，不碰线上库；
        2. 覆盖前先给当前库打一份 ``pre-restore`` 备份，恢复错了还能退回来；
        3. 覆盖用 os.replace 原子替换，替换前把文件放成 ``restore-pending.db``——
           进程若在「已关闭连接、还没重开」之间挂掉，启动时会靠它续跑；
        4. 重启进程（os._exit），由容器 restart 策略拉起，库才会被重新打开。
        """
        store.require_admin(req)
        path = db_backup.resolve(store.root, name)
        if path is None:
            raise HTTPException(404, "备份不存在")
        ok, message = await asyncio.to_thread(db_backup.verify, path)
        if not ok:
            raise HTTPException(400, f"备份不可用：{message}")
        # 恢复前先自保：这一步失败就不该继续（否则等于把唯一一份好数据换掉）。
        try:
            safety = await asyncio.to_thread(store.backup_now, "pre-restore")
        except (OSError, RuntimeError) as exc:
            raise HTTPException(500, f"恢复前自动备份失败，已中止：{exc}")
        with store.lock:
            # 关库：WAL 先合并，再断开连接，之后替换文件才不会丢已提交数据。
            store.db.close()
            await asyncio.to_thread(db_backup.begin_restore, store.root, path, store.path)
            await asyncio.to_thread(db_backup.finish_restore, store.root)
        # 置位必须在关库**之前**才严谨，但那样会让恢复请求本身撞上 503；这里在
        # 关库后立刻置位，把「库已关、进程未退」这个窗口缩到最小。窗口内到达的
        # 请求由 AdminMiddleware 统一转成 503，不再抛 sqlite3.ProgrammingError。
        store.restoring = True
        logging.getLogger("admin.backup").warning(
            "已从 %s 恢复数据库（恢复前已备份为 %s），即将重启进程", name, safety["name"])
        asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))
        return {"ok": True, "restored": name, "safety": safety["name"],
                "message": "恢复完成，服务正在重启"}

    @app.post("/admin/api/accounts")
    async def add_account(req: Request):
        store.require_admin(req)
        body = await payload(req)
        doc = body.get("credential")
        store.validate_credential(doc)
        name = clean_name(body.get("name"), str(doc["account"].get("nickname") or "新账号")[:60])
        with store.lock:
            if len(store.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            filename = aid + ".info"
            write_json(store.auth_dir / filename, doc)
            store.data["accounts"][aid] = {"file": filename, "name": name, "created": int(time.time()), "enabled": True}
            store.save()
            store.sync_any_enabled()
        return {"id": aid}

    @app.patch("/admin/api/accounts/{aid}")
    async def edit_account(aid: str, req: Request):
        store.require_admin(req)
        body = await payload(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            new = dict(item)
            if "name" in body:
                new["name"] = clean_name(body["name"], item["name"])
            if "weight" in body:
                try:
                    weight = int(body["weight"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "权重需为整数")
                if not 0 <= weight <= 100:
                    raise HTTPException(400, "权重需在 0-100 之间")
                store.data["account_status"].setdefault(aid, {})["weight"] = weight
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise HTTPException(400, "启用状态不正确")
                new["enabled"] = body["enabled"]
            store.data["accounts"][aid] = new
            store.save()
            store.sync_any_enabled()
        return {"ok": True}

    @app.delete("/admin/api/accounts/{aid}")
    async def delete_account(aid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            trash = store.root / "trash" / (aid + "-" + secrets.token_hex(4))
            trash.mkdir(parents=True, mode=0o700)
            source = store.file_for(item)
            if source.exists():
                write_json(trash / "credential.json", json.loads(source.read_text(encoding="utf-8")))
            write_json(trash / "metadata.json", item)
            del store.data["accounts"][aid]
            store.data["account_status"].pop(aid, None)
            # 显式清理关联行：软删/白名单里残留的已删账号 id 也要剔除，
            # 否则 key 会一直指向一个不存在的账号。
            store.data["session_bindings"] = {
                key: value for key, value in store.data["session_bindings"].items()
                if value.get("account_id") != aid}
            for key_item in store.data["keys"].values():
                if key_item.get("accounts"):
                    key_item["accounts"] = [x for x in key_item["accounts"] if x != aid]
                    store.keys_dirty = True
            store.save()
            store.sync_any_enabled()
            # Keep the file so an in-flight token refresh may still complete safely.
            # Account storage is authoritative; deleted records cannot be selected.
        return {"ok": True}

    @app.post("/admin/api/keys")
    async def add_key(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), "新客户端")
        with store.lock:
            # 软删保留行之后，已撤销的 key 会一直留在字典里；上限必须只数
            # 未撤销的，否则累计撤销到 100 枚后再也无法新建（与 revoke_key 口径一致）。
            if len([k for k, v in store.data["keys"].items() if not v.get("revoked_at")]) >= 100:
                raise HTTPException(400, "最多保存 100 枚 API Key")
            kid, key = store.add_key(name, daily_limit=body.get("daily_limit"),
                                     models=body.get("models"), accounts=body.get("accounts"))
            store.save()
        return {"id": kid, "key": key}

    @app.patch("/admin/api/keys/{kid}")
    async def edit_key(kid: str, req: Request):
        store.require_admin(req)
        body = await payload(req)
        with store.lock:
            item = store.data["keys"].get(kid)
            if not item:
                raise HTTPException(404, "密钥不存在")
            if "daily_limit" in body:
                if body["daily_limit"] in (None, ""):
                    item["daily_limit"] = None
                else:
                    try:
                        limit = int(body["daily_limit"])
                    except (TypeError, ValueError):
                        raise HTTPException(400, "每日上限需为整数")
                    if limit < 0:
                        raise HTTPException(400, "每日上限不能为负")
                    item["daily_limit"] = limit
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise HTTPException(400, "enabled 需为布尔值")
                item["enabled"] = int(body["enabled"])
            if "routing" in body:
                routing = body["routing"]
                if routing is not None and routing not in ROUTING_POLICIES:
                    raise HTTPException(400, "routing 需为 " + "／".join(sorted(ROUTING_POLICIES)) + " 或留空")
                item["routing"] = routing
            if "accounts" in body:
                item["accounts"] = _id_list(body["accounts"])
            if "models" in body:
                item["models"] = _id_list(body["models"])
            if "expires_at" in body:
                raw = body["expires_at"]
                if raw in (None, "", 0):
                    item["expires_at"] = None
                else:
                    try:
                        item["expires_at"] = int(raw)
                    except (TypeError, ValueError):
                        raise HTTPException(400, "expires_at 需为 unix 秒或留空")
            store.keys_dirty = True
            store.save()
        return {"ok": True}

    @app.delete("/admin/api/keys/{kid}")
    async def revoke_key(kid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            item = store.data["keys"].get(kid)
            if not item:
                raise HTTPException(404, "密钥不存在")
            # 软删保留行（审计链不断），但「最后一枚不可撤销」只数未撤销的：
            # 沿用 len(data["keys"]) <= 1 会把已撤销的计入，导致最后一个有效 key 也能被撤销。
            active = [k for k, v in store.data["keys"].items() if not v.get("revoked_at")]
            if len(active) <= 1:
                raise HTTPException(400, "请先创建一枚新密钥，再撤销最后一枚密钥")
            item["enabled"] = 0
            item["revoked_at"] = int(time.time())
            store.keys_dirty = True
            store.save()
        return {"ok": True}

    @app.post("/admin/api/test")
    async def test_model(req: Request):
        store.require_admin(req)
        body = await payload(req)
        model = body.get("model", "deepseek-v4-flash")
        prompt = body.get("prompt", "请只回复：连接成功")
        if not isinstance(model, str) or model not in converter.get_available_models():
            raise HTTPException(400, "请选择列表中的模型")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise HTTPException(400, "测试消息需为 1–2000 个字符")
        if store.test_lock.locked():
            raise HTTPException(409, "已有测试正在进行")
        if converter.CONFIG.get("cred") is None:
            raise HTTPException(400, "请先导入并启用一个账号")
        async with store.test_lock:
            started = time.monotonic()
            # Use a short-lived API key with the same authentication path as clients.
            with store.lock:
                temp_key = secrets.token_urlsafe(48)
                store.test_keys.add(digest(temp_key))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=MetricsMiddleware(converter.app, metrics, source="test", store=store)), base_url="http://internal") as client:
                    result = await asyncio.wait_for(client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + temp_key}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024, "stream": False}), timeout=90)
                data = result.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "") if result.status_code == 200 else ""
                output = {"ok": result.status_code == 200 and bool(answer), "status": result.status_code, "answer": answer, "seconds": round(time.monotonic() - started, 2), "usage": data.get("usage") if result.status_code == 200 else None}
                if not output["ok"]:
                    output["error"] = "上游未返回有效回复，请检查登录凭据、账号额度或模型权限。"
                    if result.status_code == 200 and data.get("choices", [{}])[0].get("finish_reason") == "length":
                        output["error"] = "已连接上游，但生成预算耗尽，未获得正文。请在客户端提高 max_tokens 后重试。"
            except (Exception, asyncio.TimeoutError):
                output = {"ok": False, "error": "调用失败或超时，请检查凭据是否有效，稍后重试。", "seconds": round(time.monotonic() - started, 2)}
            finally:
                with store.lock:
                    store.test_keys.discard(digest(temp_key))
            store.events.appendleft({"time": int(time.time()), "model": model, "ok": output["ok"], "seconds": output["seconds"]})
            return output

    app.mount("/", converter.app)
    logging.getLogger("admin.server").info(
        "管理服务已启动：账号 %d 个（启用 %d）、密钥 %d 个、模型 %d 个",
        len(store.data["accounts"]),
        sum(1 for a in store.data["accounts"].values() if a["enabled"]),
        len(store.data["keys"]), len(model_registry.snapshot_ids()),
    )
    return app


if __name__ == "__main__":
    cfg = config.get()
    logging_setup.setup_logging(cfg.log_level, cfg.log_path)
    # access_log=False：请求已经落在 request_logs 表里，uvicorn 再记一遍是重复噪声。
    # 注意必须在 uvicorn.run 这里传——run() 内部会 dictConfig，覆盖掉之前对
    # uvicorn.access 做的任何设置。
    uvicorn.run(create_app(), host=cfg.host or "0.0.0.0", port=cfg.port,
                log_level=logging_setup.uvicorn_level(), access_log=False,
                proxy_headers=True, forwarded_allow_ips="*")

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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from core import converter
from core.backends.registry import (
    backend_meta,
    backend_name_for,
    default_name as default_backend,
    is_enabled as is_backend_enabled,
    known_names as known_backends,
    make_credential,
    set_enabled_provider,
    spec_for,
)
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
# backend：账号所属后端（workbuddy / trae），凭据文件里的同名字段是权威来源。
ENTITY_FIELDS = ("name", "file", "enabled", "created", "backend")


def _decode_backend_flags(raw) -> dict:
    """总闸 JSON 串 → {后端名: bool}。脏值一律当「全启用」。

    宽容解析的理由：这是「下线功能」的开关，读坏了当成全关会让线上后端集体
    消失、大面积 400；当成全开最多是总闸暂时失效，方向上安全得多。
    只保留布尔值，键里的后端名由调用方按注册表校验（防手写错名字静默生效）。
    """
    if isinstance(raw, dict):  # 兼容已解好的形态（测试与内存直塞）
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
    else:
        return {}
    known = set(known_backends())
    return {k: bool(v) for k, v in data.items() if k in known and isinstance(v, bool)}


def _dumps_backend_flags(flags: dict) -> str:
    """{后端名: bool} → 落库用的 JSON 串。空 map 存 '{}'（= 全启用）。"""
    return json.dumps({k: bool(v) for k, v in flags.items()}, sort_keys=True)


def _validate_backend_flags(raw, store) -> str:
    """校验总闸提交，返回落库用的 JSON 串。非法即 400（不静默改写）。

    四条规则：
    1. 必须是 {后端名: 布尔}；未知后端名报错而不是忽略——静默忽略会让用户
       以为自己关掉了某个后端（前端传错名字时尤其容易发生）。
    2. **不变量：启用集合恒 ≥ 1**（用户拍板）。全关等于服务活着但没功能，
       是最难排障的状态，所以这里硬拒而不是给提示。
    3. 环境逃生门关着的后端**不计入**候选（CODEBUDDY_ENABLE_TRAE=0 时那个
       后端本来就不参与服务，不该因为「界面开着它」就允许把它算进启用集合）。
    4. **逃生门关着的后端，其键保留原值，不被本次提交覆盖**（规则 3 的必然推论）。
       前端把它渲染成未勾选（`enabled` 含逃生门），所以它提交的 false 其实是
       「逃生门造成的假象」而非用户意图。若照单写回，运维临时设一次环境变量、
       管理员随后保存任意设置，撤掉环境变量后该后端就**自动恢复启用**——
       用户的显式选择被一次无关的保存动作静默抹掉。
    """
    if not isinstance(raw, dict):
        raise HTTPException(400, "后端启用状态格式无效")
    known = list(known_backends())
    flags = {}
    for key, value in raw.items():
        if key not in known:
            raise HTTPException(400, f"未知后端：{key}")
        if not isinstance(value, bool):
            raise HTTPException(400, f"后端 {key} 的启用状态必须是布尔值")
        flags[key] = value
    live = [name for name in known if not spec_for(name).env_disabled()]
    if not live:
        # 所有后端都被环境门关着：没有候选，不施加不变量，原样回写。
        # （今天不可达——只有 Trae 声明了逃生门；但它是与规则 3 绑定的地雷。）
        return _dumps_backend_flags(store.backend_flags())
    # 缺失的后端按「启用」补齐：前端可能只提交变化过的键。
    enabled = [name for name in live if flags.get(name, True)]
    if not enabled:
        raise HTTPException(400, "至少需要保留一个后端启用")
    merged = {name: name in enabled for name in live}
    # 逃生门关着的后端：保留库里现值，不接受本次提交（规则 4）。
    current = store.backend_flags()
    for name in known:
        if name not in merged:
            merged[name] = bool(current.get(name, True))
    return _dumps_backend_flags(merged)


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
        self.trae_flows = {}
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
        # 后端总闸的内存副本 {后端名: 是否启用}，缺键=启用。_load 会填。
        # 私有属性 + backend_flags() 方法：同名会互相遮蔽（属性会盖掉方法）。
        self._backend_flags: dict[str, bool] = {}
        self._load()
        if not self.data["accounts"] and not self.data["keys"]:
            # 全新部署（数据库为空）：优先从旧版 state.json 迁移（含 API Key，
            # 只收养 .info 会把用户手里的 Key 全弄丢）；没有旧文件才收养凭据目录。
            self._migrate_state_json()
        if not self.data["accounts"] and not self.data["keys"]:
            # 从凭据目录导入已有凭据。
            for p in sorted(self.auth_dir.glob("*.info")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    self.validate_credential(doc)
                except (ValueError, OSError, HTTPException):
                    continue
                aid = secrets.token_hex(8)
                name = str(doc.get("account", {}).get("nickname") or "已迁移账号")[:60]
                self.data["accounts"][aid] = {"name": name, "file": p.name, "enabled": not bool(doc.get("disabled")), "created": int(time.time()), "backend": doc.get("backend", default_backend())}
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
        # 总闸 map 单独解一份到内存（registry 的数据源）。同时给 pool 里的
        # 原始 JSON 串兜底 —— save_settings 是整行 INSERT OR REPLACE、值从
        # data["pool"] 取，键缺失会写 NULL 撞 NOT NULL（与 models_updated 同理）。
        self._backend_flags = _decode_backend_flags(self.data["pool"].get("backend_enabled"))
        self._backend_flags = self._guard_enabled_invariant(self._backend_flags)
        self.data["pool"]["backend_enabled"] = _dumps_backend_flags(self._backend_flags)
        self.data["session_bindings"] = self.db.load_bindings()

    def _migrate_state_json(self):
        """旧版 state.json → SQLite 的一次性迁移（DB 为空且文件存在才跑）。

        迁移 accounts（含 backend）/ keys / pool 设置 / session_bindings；
        model_registry 不迁移——模型目录重启后会重新拉取，Trae 表回落静态兜底。
        迁移后把原文件改名留档，避免重复导入。
        """
        legacy = self.root / "state.json"
        if not legacy.exists():
            return
        try:
            old = json.loads(legacy.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            legacy.rename(legacy.with_suffix(".json.corrupt"))
            return
        if not isinstance(old, dict):
            legacy.rename(legacy.with_suffix(".json.corrupt"))
            return
        accounts = old.get("accounts") or {}
        keys = old.get("keys") or {}
        if not isinstance(accounts, dict) or not isinstance(keys, dict):
            return
        with self.lock:
            for aid, item in accounts.items():
                if not isinstance(item, dict) or not item.get("file"):
                    continue
                self.data["accounts"][aid] = {
                    "name": str(item.get("name") or aid)[:60],
                    "file": str(item["file"]),
                    "enabled": bool(item.get("enabled", True)),
                    "created": int(item.get("created") or time.time()),
                    "backend": item.get("backend", default_backend())}
            status = old.get("account_status")
            if isinstance(status, dict):
                for aid, fields in status.items():
                    if aid in self.data["accounts"] and isinstance(fields, dict):
                        self.data["account_status"][aid] = dict(fields)
            for kid, item in keys.items():
                if not isinstance(item, dict) or not item.get("hash"):
                    continue
                usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
                self.data["keys"][kid] = {
                    "name": str(item.get("name") or ""),
                    "hash": str(item["hash"]),
                    "hint": str(item.get("hint") or ""),
                    "created": int(item.get("created") or time.time()),
                    "enabled": item.get("enabled", 1),
                    "revoked_at": item.get("revoked_at"),
                    "expires_at": item.get("expires_at"),
                    "daily_limit": item.get("daily_limit"),
                    "usage": {"date": usage.get("date"), "count": usage.get("count", 0),
                              "tokens": usage.get("tokens", 0)},
                    "models": _id_list(item.get("models")),
                    "accounts": _id_list(item.get("accounts")),
                    "routing": item.get("routing")}
            pool = old.get("pool")
            if isinstance(pool, dict):
                # 只搬 settings 表认识的列（models_updated 等新字段用默认值）。
                known = {k: v for k, v in pool.items()
                         if k in AccountPool.POOL_DEFAULTS and v is not None}
                if known:
                    self.data["pool"] = {**AccountPool.POOL_DEFAULTS, **known}
            bindings = old.get("session_bindings")
            if isinstance(bindings, dict):
                self.data["session_bindings"] = {
                    k: v for k, v in bindings.items()
                    if isinstance(v, dict) and v.get("account_id")}
            self.save()
        legacy.rename(legacy.with_suffix(".json.migrated"))

    @staticmethod
    def validate_credential(doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict) or not isinstance(doc.get("account"), dict):
            raise HTTPException(400, "需要桌面端 .info／JSON 登录文件，包含 auth 和 account 对象")
        backend = doc.get("backend", default_backend())
        if backend not in known_backends():
            raise HTTPException(400, "backend 字段无效（仅支持 %s）" % " / ".join(known_backends()))
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
        backend = doc.get("backend", default_backend())
        with self.lock:
            identity = (backend, doc["account"]["uid"], doc["account"].get("enterpriseId", ""))
            for aid, item in self.data["accounts"].items():
                try:
                    old = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if (old.get("backend", default_backend()), old.get("account", {}).get("uid"), old.get("account", {}).get("enterpriseId", "")) == identity:
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
            self.data["accounts"][aid] = {"file": aid + ".info", "name": name or str(doc["account"].get("nickname") or "浏览器授权账号")[:60], "created": int(time.time()), "enabled": True, "backend": backend}
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
                # 按 .info 的 backend 字段分派：trae → TraeCredentialManager，其余 → WorkBuddy。
                self.managers[aid] = make_credential(self.file_for(item))
            return self.managers[aid]

    def any_manager(self, backend=None):
        """任一启用账号的凭据管理器；``backend`` 给定时只看该后端。

        取代原先的 ``any_enabled_manager`` / ``any_trae_manager`` 两个专用函数：
        它们是同一段逻辑写两遍、只差一个后端名，加第三个后端就得再抄一遍。

        调用方给 backend 的理由各不相同——后台拉模型目录只能用一个能打
        workbuddy.cn 的账号（Trae 的 headers 打过去必然 400），而动态模型表 /
        签到 / 积分又只能用 Trae 账号——所以这个参数是有意义的，不是可有可无。
        """
        with self.lock:
            for aid, item in self.data["accounts"].items():
                if not item["enabled"]:
                    continue
                if backend is not None and item.get("backend", default_backend()) != backend:
                    continue
                if self.auth_dir.joinpath(item["file"]).exists():
                    return self.manager_for(aid, item)
            return None

    def any_enabled_manager(self):
        """默认后端的一个启用账号管理器（后台拉模型目录用）。"""
        return self.any_manager(default_backend())

    def has_enabled_account(self, backend):
        """该后端是否有启用账号（供测试请求前置检查，避免拿错后端的账号）。"""
        with self.lock:
            return any(it.get("backend", default_backend()) == backend and it["enabled"]
                       for it in self.data["accounts"].values())

    def account_rows(self):
        """账号表格的数据源。**隐藏已禁用后端的账号**（用户拍板，2026-10-16）。

        隐藏而不是置灰：禁用后端是「下线」，账号随之从界面上消失；重新启用
        即刻原样回来（内存与数据库都不动它们）。总闸不变量保证至少一个后端
        启用，所以这张表永远不为空。

        只过滤**当前状态**——账本/日志/事件流不过滤，历史事实照常可查
        （与「账本记事实，不记意图」同一条原则）。
        """
        # 启用状态按**后端**算一次，而不是每个账号问一遍：is_enabled 会取 store 锁
        # 并复制 dict（O(账号数) 次加锁）。账号通常远多于后端数，先算 map 再查。
        live = {name: is_backend_enabled(name) for name in known_backends()}
        result = []
        for aid, item in self.data["accounts"].items():
            backend = item.get("backend", default_backend())
            if not live.get(backend, True):
                continue
            row = {"id": aid, "name": item["name"], "enabled": item["enabled"], "created": item["created"], "backend": backend}
            try:
                doc = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                expiry = doc["auth"].get("expiresAt", 0)
                # 各后端 expiresAt 单位不同（Trae 是 Unix 秒），统一归一为毫秒
                # （前端/池按毫秒语义处理）。换算系数由 spec 声明。
                factor = spec_for(doc.get("backend", backend)).expires_at_to_ms
                if factor != 1:
                    expiry = expiry * factor
                row.update({"nickname": str(doc["account"].get("nickname") or ""), "uid": str(doc["account"].get("uid") or ""), "expires_at": expiry, "expired": expiry < time.time() * 1000, "refresh_available": bool(doc["auth"].get("refreshToken")), "status": "ready"})
            except (ValueError, OSError, KeyError):
                row.update({"status": "invalid", "expired": True, "expires_at": 0, "refresh_available": False})
            result.append(row)
        return result

    def adopt_auth_files(self):
        """收养 auth_dir 里新落盘的 .info（CLI 登录产出，如 trae-{uid}.info）。

        单写者＝服务进程；pool tick（60s）调用。按文件名去重（确定性文件名不重复收养），
        账号数 ≥100 停止。返回本次新增数量。
        """
        added = 0
        with self.lock:
            known = {item["file"] for item in self.data["accounts"].values()}
            for path in sorted(self.auth_dir.glob("*.info")):
                if path.name in known:
                    continue
                if len(self.data["accounts"]) >= 100:
                    break
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    self.validate_credential(doc)
                except (ValueError, OSError, HTTPException):
                    continue
                aid = secrets.token_hex(8)
                backend = doc.get("backend", default_backend())
                self.data["accounts"][aid] = {
                    "file": path.name,
                    "name": str(doc.get("account", {}).get("nickname") or "已收养账号")[:60],
                    "created": int(time.time()),
                    "enabled": not bool(doc.get("disabled")),
                    "backend": backend,
                }
                known.add(path.name)
                added += 1
            if added:
                self.events.appendleft({"time": int(time.time()), "event": "adopt_auth", "count": added})
                self.save()
        return added

    def health_detail(self):
        """深度健康检查的判据来源。

        「可用」= 启用 + 凭据文件存在且可解析 + 未被标 auth_invalid。
        注意这里**不打上游接口**：health 必须廉价，不能每次调用都产生外部请求。

        口径：**只统计可见账号**（account_rows 已隐藏禁用后端的），所以计数
        与概览表格行数永远对得上。禁用后端的账号既不算 total 也不算异常——
        它是被管理员下线了，不是坏了。
        """
        rows = self.account_rows()
        by_id = {row["id"]: row for row in rows}
        total = enabled = usable = auth_invalid = 0
        for aid, row in by_id.items():
            total += 1
            if not row.get("enabled"):
                continue
            enabled += 1
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

    # -- 后端总闸 ----------------------------------------------------------

    def _guard_enabled_invariant(self, flags: dict) -> dict:
        """加载期兜底：库里若记着「全部后端都关」，回落全启用并告警。

        HTTP 层已有校验，但那是**防手滑**不是**防一切**：手改库、恢复一份坏备份、
        或将来任何直接写 ``data["pool"]`` 的代码都可能绕过它。到那时进程活着、
        健康检查绿、所有请求 400/503——正是 specs/backend-toggle.md §4.1 要杀的
        「活着但没功能」状态。这里是最后一道：宁可全启用（总闸暂时失效）也不能
        全关（服务不可用），且必留告警。
        """
        live = [n for n in known_backends() if not spec_for(n).env_disabled()]
        if live and not any(flags.get(n, True) for n in live):
            logging.getLogger("admin.server").warning(
                "数据库里记录的后端启用集合为空，已回落为全部启用"
                "（请检查 settings.backend_enabled 是否被手工改坏）")
            return {}
        return flags

    def backend_flags(self) -> dict:
        """registry 的总闸数据源（注入点）。返回**副本**：调用方只读，
        直接暴露内部 dict 的话，任何一处误改都会静默改掉全局启用状态。"""
        with self.lock:
            return dict(self._backend_flags)

    def set_backend_flags(self, flags: dict) -> None:
        """写入总闸。调用方需已按注册表校验过键名与「至少留一个」不变量。

        两处必须同步：内存 map（registry 立刻生效，不等落库）与 pool 里的
        JSON 串（save_settings 是整行重写，从这里取值）。
        """
        with self.lock:
            self._backend_flags = {k: bool(v) for k, v in flags.items()}
            self.data["pool"]["backend_enabled"] = _dumps_backend_flags(self._backend_flags)

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
    """缓存 catalog 是否为旧格式（描述含英文、或未按单价升序）时需重新拉取。

    只检查**参与排序判定**的后端（默认后端）——另一侧的行往往没有单价、
    固定排在后面，混进来会让「是否升序」永远为假，于是每次启动都重新拉一遍。
    """
    default = default_backend()
    models = [m for m in (registry.snapshot().get("models") or []) if m.get("backend", default) == default]
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
    # 后端总闸（settings.backend_enabled）注入 registry。registry 是 core 层，
    # 不能反向 import admin（admin 依赖它，成环）——与上面的
    # set_catalog_provider 同一模式。数据源是**内存字典**，因为 is_enabled
    # 在请求路径上，不能每次查 SQLite。
    set_enabled_provider(store.backend_flags)
    # 目录凭据：默认后端用 any_manager(默认后端)，其余后端各自按名字取。
    # 由 needs_own_credential_for_catalog / ModelRegistry.credential_getter_for 决定
    # 谁被用到 —— server 这里不再为某个具体后端留一个专用函数。
    model_registry = ModelRegistry(get_credential=store.any_enabled_manager,
                                   backend_credential_getter=store.any_manager)
    # 供账号池选降级模型用（池先建、注册表后建，这里回填）。
    pool.model_registry = model_registry
    model_registry.load(store.db)
    # 主目录（默认后端）的静态兜底；各后端由下面 _ensure_catalogs 统一兜底。
    model_registry.ensure_builtin()
    # 每个后端的模型目录：注册「谁去拉」的提供者 + 静态兜底。
    # 后端列表来自注册表，加后端时这里不用改。
    for _name in known_backends():
        model_registry.ensure_static_catalog(_name)
        _spec = spec_for(_name)
        if _spec.catalog_method or _name == default_backend():
            _reg, _n = model_registry, _name
            _spec.set_catalog_provider(lambda _r=_reg, _b=_n: _r.catalog_entries(_b))
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
                    # 收养 CLI 新落盘的 .info（如 trae-{uid}.info），再跑 tick。
                    #
                    # 收养与 tick 分开兜底：收养读的是**所有**后端的凭据文件，
                    # 一个坏文件抛异常若不单独接住，tick 根本不会跑——于是两个
                    # 后端的签到与凭据刷新一起停摆。字节加进来之后，这个风险
                    # 才真正成立。
                    try:
                        await asyncio.to_thread(store.adopt_auth_files)
                    except Exception:
                        logging.getLogger("admin.pool").exception("收养凭据文件异常（不影响本轮 tick）")
                    await pool.tick()
                except Exception:
                    logging.getLogger("admin.pool").exception("账号池 tick 异常（不影响下一轮）")
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
            # 低峰期随机时刻，避免固定时间打点）。WorkBuddy 与 Trae 两张表都刷。
            while True:
                now = datetime.now(CN)
                next_run = (now + timedelta(days=1)).replace(hour=4, minute=random.randint(0, 59), second=random.randint(0, 59), microsecond=0)
                if not (4 <= now.hour < 5):
                    today = now.replace(hour=4, minute=random.randint(0, 59), second=random.randint(0, 59), microsecond=0)
                    if today > now:
                        next_run = today
                await asyncio.sleep(max(60, (next_run - now).total_seconds()))
                # 主目录（默认后端）走 fetch()；其它后端各自刷新。
                model_registry.fetch()
                for _name in known_backends():
                    if _name == default_backend():
                        continue
                    try:
                        model_registry.refresh_catalog(_name)
                    except Exception:
                        pass
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
    _original_available_models = converter.get_available_models  # 捕获原函数，防自递归

    def _managed_models():
        # WorkBuddy 动态表 + Trae 目录（trae/ 前缀）均由 ModelRegistry 统一管理
        #（含管理页禁用）；kill switch 时 registry 自动剔除 Trae 条目。
        models = model_registry.enabled_models()
        return list(models or _original_available_models())
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

    @app.get("/admin/api/backends")
    async def backends_api(req: Request):
        """后端元数据（需求 2/5/7）：前端不再写死后端名。

        返回每个已知后端的 label / icon / color / 前缀 / 登录方式 / 启用状态 /
        账号数。加第三个后端后前端自动多一个徽标与筛选项，不必改 app.js。
        """
        store.require_admin(req)
        with store.lock:
            counts = {}
            for item in store.data["accounts"].values():
                name = item.get("backend", default_backend())
                counts[name] = counts.get(name, 0) + 1
        out = []
        for meta in backend_meta():
            out.append({**meta, "accounts": counts.get(meta["name"], 0)})
        return {"backends": out}

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
            source=params.get("source") or None, outcome=params.get("outcome") or None,
            key_id=params.get("key_id") or None)
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
        # 各后端的动态表独立刷新：一个失败不影响另一个（后端隔离）。
        others_ok = True
        for _name in known_backends():
            if _name == default_backend():
                continue
            try:
                got = await asyncio.to_thread(model_registry.refresh_catalog, _name)
                others_ok = others_ok and bool(got)
            except Exception:
                others_ok = False
        model_registry.persist(store.db)
        snapshot = model_registry.snapshot()
        if not ok and not others_ok:
            raise HTTPException(502, snapshot.get("error") or "拉取失败")
        return {"ok": True, "count": len(snapshot["models"])}

    # mid 用 :path——Trae 模型 id 带斜杠（trae/glm-5.2），单段 {mid} 匹配不到。
    @app.patch("/admin/api/models/{mid:path}")
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
            if "backend_enabled" in body:
                flags_json = _validate_backend_flags(body["backend_enabled"], store)
                settings["backend_enabled"] = flags_json
                # 同步内存 map：registry 立刻生效，不必等落库/重启。
                store.set_backend_flags(_decode_backend_flags(flags_json))
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
        backend = doc.get("backend", default_backend())
        name = clean_name(body.get("name"), str(doc["account"].get("nickname") or "新账号")[:60])
        with store.lock:
            if len(store.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            filename = aid + ".info"
            write_json(store.auth_dir / filename, doc)
            store.data["accounts"][aid] = {"file": filename, "name": name, "created": int(time.time()), "enabled": True, "backend": backend}
            store.save()
            store.sync_any_enabled()
        return {"id": aid}

    @app.post("/admin/api/accounts/adopt")
    async def adopt_accounts(req: Request):
        store.require_admin(req)
        added = await asyncio.to_thread(store.adopt_auth_files)
        return {"ok": True, "added": added}

    # ---- Trae 授权登录 ----
    # Trae 的回调**必须是用户本机的 127.0.0.1 地址**（认证页面向桌面 IDE），
    # 因此流程是：管理台生成一条本机命令 → 用户执行 → 本机接收器起 127.0.0.1 监听
    # 并自动开浏览器 → 登录后回调落到本机 → 本机把回调原样回传本服务完成登录。
    # 全程无需复制粘贴任何链接或 token。
    def _trae_port():
        try:
            return int(os.environ.get("CODEBUDDY_TRAE_LOCAL_PORT", "18080"))
        except ValueError:
            return 18080

    def _public_base(req):
        env = os.environ.get("CODEBUDDY_TRAE_PUBLIC_BASE")
        if env:
            return env.rstrip("/")
        proto = (req.headers.get("x-forwarded-proto") or req.url.scheme or "https").split(",")[0].strip()
        host = req.headers.get("host") or req.url.netloc
        return f"{proto}://{host}"

    def _trae_flow(fid, token=None):
        if not isinstance(fid, str) or not fid:
            return None, "缺少登录流程标识"
        with store.lock:
            flow = store.trae_flows.get(fid)
        if not flow:
            return None, "登录流程不存在或已过期，请重新生成"
        if token is not None and not hmac.compare_digest(str(token), flow["secret"]):
            return None, "登录令牌无效，请重新生成"
        return flow, None

    @app.post("/admin/api/trae/authorize")
    async def trae_authorize(req: Request):
        store.require_admin(req)
        from core.backends.trae_login import build_login_url

        fid, secret = secrets.token_hex(8), secrets.token_urlsafe(24)
        machine_id, device_id = secrets.token_hex(16), secrets.token_hex(16)
        now = time.time()
        # machine_id/device_id 必须与登录 URL 一致（上游把会话绑定到该设备对），随 fid 服务端暂存。
        # auth_callback_url 必须是 127.0.0.1 形态——公网回调实测被授权页直接拒绝
        # （2026-10-15：授权页报"登录失败"，服务器从未收到任何请求）。
        # 配合 redirect=0：授权页跳 /login 出登录表单，登录后浏览器导航到该 127.0.0.1
        # 地址（无接收器时连接失败）——地址栏即含 refreshToken 的完整回调地址，供复制粘贴。
        # （redirect=1 实测会让授权页直接渲染"登录成功"终态、不出登录表单。）
        port = _trae_port()
        url = build_login_url(machine_id, device_id, callback=f"http://127.0.0.1:{port}/authorize")
        with store.lock:
            store.trae_flows = {k: v for k, v in store.trae_flows.items() if v["expires"] > now}
            if len(store.trae_flows) >= 20:
                store.trae_flows.pop(min(store.trae_flows, key=lambda k: store.trae_flows[k]["expires"]))
            store.trae_flows[fid] = {"machine_id": machine_id, "device_id": device_id, "secret": secret,
                                     "url": url, "expires": now + 600, "status": "pending",
                                     "message": None, "uid": None, "nickname": None}
        return {"id": fid, "url": url, "expires_at": int((now + 600) * 1000)}

    _TRAE_CALLBACK_CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    }

    @app.api_route("/trae/callback/{fid}/{secret}", methods=["GET", "POST", "OPTIONS"])
    async def trae_public_callback(fid: str, secret: str, req: Request):
        """Trae 授权页 fetch 投递的公网回调：免命令免粘贴完成登录。

        凭 path 里的 flow secret 鉴权（不校验管理会话）；授权页是跨域 fetch，
        必须回 CORS 头，否则浏览器拦截响应、授权页误报"网络错误"。
        """
        from core.backends import trae_login as trae_login_mod

        if req.method == "OPTIONS":
            return Response(status_code=204, headers=_TRAE_CALLBACK_CORS)
        flow, error = _trae_flow(fid, secret)
        if error:
            return JSONResponse({"ok": False, "error": error}, status_code=404, headers=_TRAE_CALLBACK_CORS)
        if flow["status"] == "done":
            return JSONResponse({"ok": True, "uid": flow.get("uid"), "nickname": flow.get("nickname"),
                                 "message": "该账号已完成登录"}, headers=_TRAE_CALLBACK_CORS)
        query = str(req.url.query)
        raw_body = (await req.body()).decode("utf-8", "replace")
        parsed = trae_login_mod.parse_received(req.method, str(req.url.path), query, raw_body)

        def _fail(message, status_code=400):
            with store.lock:
                current = store.trae_flows.get(fid)
                if current and current["status"] == "pending":
                    current.update(status="error", message=message)
            return JSONResponse({"ok": False, "error": message}, status_code=status_code,
                                headers=_TRAE_CALLBACK_CORS)

        if not parsed["refresh_token"] and not parsed["jwt_token"]:
            fields = ", ".join(trae_login_mod.received_fields(query=query, body=raw_body)[:20])
            return _fail(f"未从回调中识别到凭据（收到字段：{fields or '空'}）")
        try:
            summary = await asyncio.to_thread(
                trae_login_mod.login, parsed, flow["machine_id"], flow["device_id"], store.auth_dir
            )
        except (RuntimeError, ValueError, httpx.HTTPError) as e:
            return _fail(f"登录失败：{e}")
        added = await asyncio.to_thread(store.adopt_auth_files)
        with store.lock:
            current = store.trae_flows.get(fid)
            if current:
                current.update(status="done", message="登录成功", uid=summary["uid"],
                               nickname=summary["nickname"], url=None)
        return JSONResponse({"ok": True, "uid": summary["uid"], "nickname": summary["nickname"],
                             "updated": summary["updated"], "adopted": added, "message": "登录成功"},
                            headers=_TRAE_CALLBACK_CORS)

    @app.get("/trae/local.py")
    async def trae_local_script(req: Request, f: str = "", t: str = ""):
        """下发一次性本机接收器脚本（凭 flow secret 鉴权，无需管理会话）。"""
        from core.backends.trae_login import render_local_receiver

        flow, error = _trae_flow(f, t)
        if error:
            return Response(f"# {error}\n", media_type="text/plain; charset=utf-8", status_code=404)
        script = render_local_receiver(_public_base(req), f, flow["secret"], port=_trae_port())
        return Response(script, media_type="text/plain; charset=utf-8")

    @app.get("/trae/api/flow/{fid}")
    async def trae_flow_info(fid: str, t: str = ""):
        """本机接收器取登录 URL。"""
        flow, error = _trae_flow(fid, t)
        if error:
            return JSONResponse({"error": error}, status_code=404)
        if flow["status"] != "pending":
            return JSONResponse({"error": "该登录流程已结束"}, status_code=409)
        return {"url": flow["url"], "port": _trae_port()}

    @app.post("/trae/api/complete")
    async def trae_complete(req: Request):
        """本机接收器把收到的回调原样回传这里完成登录（凭 flow secret，不校验管理会话）。"""
        from core.backends import trae_login as trae_login_mod

        body = await payload(req)
        fid, token = body.get("id"), body.get("token")
        flow, error = _trae_flow(fid, token)
        if error:
            return JSONResponse({"ok": False, "error": error}, status_code=400)
        if flow["status"] == "done":
            return {"ok": True, "uid": flow.get("uid"), "nickname": flow.get("nickname"), "message": "该账号已完成登录"}
        query, raw_body = str(body.get("query") or ""), str(body.get("body") or "")
        parsed = trae_login_mod.parse_received(str(body.get("method") or "GET"), str(body.get("path") or ""), query, raw_body)

        def _fail(message):
            with store.lock:
                current = store.trae_flows.get(fid)
                if current:
                    current.update(status="error", message=message)
            return JSONResponse({"ok": False, "error": message}, status_code=400)

        if not parsed["refresh_token"] and not parsed["jwt_token"]:
            fields = ", ".join(trae_login_mod.received_fields(query=query, body=raw_body)[:20])
            return _fail(f"未从回调中识别到凭据（收到字段：{fields or '空'}）")
        try:
            summary = await asyncio.to_thread(
                trae_login_mod.login, parsed, flow["machine_id"], flow["device_id"], store.auth_dir
            )
        except (RuntimeError, ValueError, httpx.HTTPError) as e:
            return _fail(f"登录失败：{e}")
        added = await asyncio.to_thread(store.adopt_auth_files)
        with store.lock:
            current = store.trae_flows.get(fid)
            if current:
                current.update(status="done", message="登录成功", uid=summary["uid"],
                               nickname=summary["nickname"], url=None)
        return {"ok": True, "uid": summary["uid"], "nickname": summary["nickname"],
                "updated": summary["updated"], "adopted": added, "message": "登录成功"}

    @app.get("/admin/api/trae/flow/{fid}")
    async def trae_flow_status(fid: str, req: Request):
        """管理页轮询登录结果。"""
        store.require_admin(req)
        with store.lock:
            flow = store.trae_flows.get(fid)
            if not flow:
                return {"status": "expired"}
            return {"status": flow["status"], "message": flow.get("message"),
                    "uid": flow.get("uid"), "nickname": flow.get("nickname")}

    @app.post("/admin/api/trae/login")
    async def trae_login_finish(req: Request):
        """兜底：手动粘贴回调链接。"""
        store.require_admin(req)
        from core.backends import trae_login as trae_login_mod

        body = await payload(req)
        fid, callback_url = body.get("id"), body.get("callback_url")
        if not isinstance(callback_url, str) or not callback_url.strip():
            raise HTTPException(400, "请粘贴浏览器地址栏的完整回调链接")
        if len(callback_url) > 8192:
            raise HTTPException(400, "回调链接过长")
        flow, error = _trae_flow(fid)
        if error:
            raise HTTPException(400, error)
        try:
            summary = await asyncio.to_thread(
                trae_login_mod.login, callback_url.strip(), flow["machine_id"], flow["device_id"], store.auth_dir
            )
        except (RuntimeError, ValueError, httpx.HTTPError) as e:
            raise HTTPException(400, f"登录失败：{e}")
        added = await asyncio.to_thread(store.adopt_auth_files)
        with store.lock:
            current = store.trae_flows.get(fid)
            if current:
                current.update(status="done", message="登录成功", uid=summary["uid"],
                               nickname=summary["nickname"], url=None)
        return {"ok": True, "uid": summary["uid"], "nickname": summary["nickname"],
                "updated": summary["updated"], "adopted": added}

    @app.delete("/admin/api/trae/{fid}")
    async def trae_cancel(fid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            store.trae_flows.pop(fid, None)
        return {"ok": True}

    @app.patch("/admin/api/accounts/{aid}")
    async def edit_account(aid: str, req: Request):
        store.require_admin(req)
        body = await payload(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            new = dict(item)
            if "backend" in body and body["backend"] != item.get("backend", default_backend()):
                raise HTTPException(400, "账号后端不可修改")
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
        want = backend_name_for(model)
        if want != default_backend():
            # 非默认后端：必须真有一个该后端的启用账号，否则池子选不到号，
            # 请求会以「没有可用账号」告终——这里提前给出可操作的提示。
            if not store.has_enabled_account(want):
                raise HTTPException(400, f"请先启用一个 {spec_for(want).label} 账号")
        elif store.any_enabled_manager() is None:
            raise HTTPException(400, "请先导入并启用一个账号")
        async with store.test_lock:
            started = time.monotonic()
            # Use a short-lived API key with the same authentication path as clients.
            with store.lock:
                temp_key = secrets.token_urlsafe(48)
                store.test_keys.add(digest(temp_key))
            # 内部调用穿过 PoolMiddleware：与真实客户端一致，按路由规则（轮转/亲和/冷却/耗尽）
            # 自动选择匹配该模型后端的账号，而非固定"当前账号"。
            internal_app = PoolMiddleware(converter.app, pool=pool, model_registry=model_registry)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=MetricsMiddleware(internal_app, metrics, source="test", store=store)), base_url="http://internal") as client:
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

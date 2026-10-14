"""Small, persistent management layer for workbuddy2api. Single-process deployment."""
import asyncio
import hashlib
import hmac
import json
import os
import random
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from core import converter
from core.backends.registry import make_credential
from .browser_login import BrowserLogin
from .pool import AccountPool, PoolMiddleware
from .metrics import RequestMetrics, MetricsMiddleware
from .models import CN, ModelRegistry

COOKIE = "workbuddy_admin"
MAX_BODY = 1024 * 1024
ASSETS = Path(__file__).parent / "static"


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
        self.path = self.root / "state.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("model_registry", {})
        else:
            self.data = {"accounts": {}, "active": None, "keys": {}}
            for p in sorted(self.auth_dir.glob("*.info")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    self.validate_credential(doc)
                except (ValueError, OSError, HTTPException):
                    continue
                aid = secrets.token_hex(8)
                name = str(doc.get("account", {}).get("nickname") or "已迁移账号")[:60]
                self.data["accounts"][aid] = {"name": name, "file": p.name, "enabled": not bool(doc.get("disabled")), "created": int(time.time()), "backend": doc.get("backend", "workbuddy")}
                if self.data["active"] is None and not doc.get("disabled"):
                    self.data["active"] = aid
            if initial_key:
                self.add_key("原有 API Key", initial_key)
            self.save()
        self.sync_active()

    @staticmethod
    def validate_credential(doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict) or not isinstance(doc.get("account"), dict):
            raise HTTPException(400, "需要桌面端 .info／JSON 登录文件，包含 auth 和 account 对象")
        backend = doc.get("backend", "workbuddy")
        if backend not in ("workbuddy", "trae"):
            raise HTTPException(400, "backend 字段无效（仅支持 workbuddy / trae）")
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
        write_json(self.path, self.data)

    def save_browser_account(self, doc, name):
        self.validate_credential(doc)
        backend = doc.get("backend", "workbuddy")
        with self.lock:
            identity = (backend, doc["account"]["uid"], doc["account"].get("enterpriseId", ""))
            for aid, item in self.data["accounts"].items():
                try:
                    old = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if (old.get("backend", "workbuddy"), old.get("account", {}).get("uid"), old.get("account", {}).get("enterpriseId", "")) == identity:
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
                    self.sync_active()
                    return {"account_id": aid, "updated": True}
            if len(self.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            write_json(self.auth_dir / (aid + ".info"), doc)
            self.data["accounts"][aid] = {"file": aid + ".info", "name": name or str(doc["account"].get("nickname") or "浏览器授权账号")[:60], "created": int(time.time()), "enabled": True, "backend": backend}
            if self.data["active"] is None:
                self.data["active"] = aid
            self.save()
            self.sync_active()
            return {"account_id": aid, "updated": False}

    def file_for(self, item):
        path = self.auth_dir / item["file"]
        if path.parent.resolve() != self.auth_dir.resolve() or path.is_symlink():
            raise HTTPException(400, "凭据路径不合法")
        return path

    def sync_active(self):
        aid = self.data.get("active")
        item = self.data["accounts"].get(aid)
        converter.CONFIG["cred"] = self.manager_for(aid, item) if item and item["enabled"] and self.file_for(item).exists() else None

    def manager_for(self, aid, item):
        with self.lock:
            if aid not in self.managers:
                # 按 .info 的 backend 字段分派：trae → TraeCredentialManager，其余 → WorkBuddy。
                self.managers[aid] = make_credential(self.file_for(item))
            return self.managers[aid]

    def any_enabled_manager(self):
        """A credential manager from any enabled WorkBuddy account, for background pulls
        (model catalog). Trae accounts are excluded: their headers hit workbuddy.cn and 400."""
        with self.lock:
            for aid, item in self.data["accounts"].items():
                if item["enabled"] and item.get("backend", "workbuddy") == "workbuddy":
                    return self.manager_for(aid, item)
            return None

    def any_trae_manager(self):
        """任一启用的 Trae 账号凭据管理器（动态模型表 / 签到 / 积分用）。"""
        with self.lock:
            for aid, item in self.data["accounts"].items():
                if item["enabled"] and item.get("backend", "workbuddy") == "trae":
                    return self.manager_for(aid, item)
            return None

    def account_rows(self):
        result = []
        for aid, item in self.data["accounts"].items():
            backend = item.get("backend", "workbuddy")
            row = {"id": aid, "name": item["name"], "enabled": item["enabled"], "active": aid == self.data["active"], "created": item["created"], "backend": backend}
            try:
                doc = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                expiry = doc["auth"].get("expiresAt", 0)
                # Trae 的 expiresAt 是 Unix 秒；统一归一为毫秒（前端/池按毫秒语义处理）。
                if doc.get("backend", backend) == "trae":
                    expiry = expiry * 1000
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
                backend = doc.get("backend", "workbuddy")
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
        if added:
            self.sync_active()
        return added

    def add_key(self, name, value=None, daily_limit=None):
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
        self.data["keys"][kid] = {"name": name, "hash": digest(value), "hint": value[:5] + "…" + value[-4:], "created": int(time.time()), "daily_limit": limit, "usage": {}}
        return kid, value

    def _today(self):
        return datetime.fromtimestamp(time.time(), timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def check_api(self, authorization, x_api_key):
        """Return the matching key id, or None for the legacy single admin key."""
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else x_api_key or ""
        with self.lock:
            hashed = digest(token)
            for kid, item in self.data["keys"].items():
                if hmac.compare_digest(hashed, item["hash"]):
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
            self.save()

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
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            return await self.app(scope, receive, send)
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


def _stale_catalog(registry):
    """缓存 catalog 是否为旧格式（描述含英文、或未按单价升序）时需重新拉取。
    只检查 WorkBuddy 行——Trae 行无单价、固定排在后面，不参与排序判断。"""
    models = [m for m in (registry.snapshot().get("models") or []) if m.get("backend") != "trae"]
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
    store = Store(root or os.environ.get("MANAGEMENT_DATA_DIR", "/data/management"), auth_dir or os.environ.get("CODEBUDDY_AUTH_DIR", "/data/auth"), initial_key if initial_key is not None else os.environ.get("CODEBUDDY2OPENAI_KEY", ""), admin_key or os.environ.get("ADMIN_KEY", ""))
    converter._check_auth = store.check_api
    converter.CONFIG.update({"desensitize": True, "no_compact": False, "log_path": None})
    browser_login = BrowserLogin(store.save_browser_account)
    pool = AccountPool(store)
    metrics = RequestMetrics()
    model_registry = ModelRegistry(get_credential=store.any_enabled_manager,
                                   trae_credential_getter=store.any_trae_manager)
    model_registry.import_state(store.data.get("model_registry"))
    model_registry.ensure_builtin()
    # Trae 模型目录：动态表由 registry 拉取维护，bare id 经提供者暴露给
    # core.backends.trae（map_model / 模型列表）；无动态表时用静态 32 条兜底。
    model_registry.ensure_trae_builtin()
    from core.backends import trae as trae_module
    trae_module.set_catalog_provider(model_registry.trae_entries)
    # 缓存里的描述是旧逻辑（中文+英文拼接、上游原始顺序）时重新拉取一次，
    # 让展示立即变成「只有中文 + 按单价升序」。
    if _stale_catalog(model_registry):
        model_registry.fetch()
        model_registry.ensure_builtin()
        with store.lock:
            store.data["model_registry"] = model_registry.export_state()
            store.save()
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
                    await asyncio.to_thread(store.adopt_auth_files)
                    await pool.tick()
                except Exception:
                    pass  # Per-account errors are persisted without credential data.
                await asyncio.sleep(60)
        pool_task = asyncio.create_task(pool_worker())
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
                model_registry.fetch()
                try:
                    model_registry.refresh_trae_models()
                except Exception:
                    pass
                with store.lock:
                    store.data["model_registry"] = model_registry.export_state()
                    store.save()
        models_task = asyncio.create_task(models_worker())
        try:
            yield
        finally:
            cleanup_task.cancel()
            pool_task.cancel()
            models_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await browser_login.close()
            for task in (pool_task, models_task):
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
    app.add_middleware(AdminMiddleware)
    app.add_middleware(PoolMiddleware, pool=pool, model_registry=model_registry)
    app.add_middleware(MetricsMiddleware, metrics=metrics)

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
    async def health():
        return {"status": "ok"}

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
        with store.lock:
            keys = [{"id": kid, "usage": {"date": store._today(), "count": 0, "tokens": 0}, **{k: v for k, v in item.items() if k != "hash"}} for kid, item in store.data["keys"].items()]
            return {"accounts": pool.rows(store.account_rows()), "pool": dict(store.data["pool"]), "metrics": {**metrics.snapshot(), "affinity_hits": pool.affinity_hits, "affinity_rebinds": pool.affinity_rebinds}, "keys": keys, "models": model_registry.snapshot(), "uptime": int(time.time() - store.started), "events": list(store.events)}

    @app.post("/admin/api/pool/refresh-credits")
    async def pool_refresh_credits(req: Request):
        store.require_admin(req)
        return await pool.refresh_credits()

    @app.post("/admin/api/models/refresh")
    async def models_refresh(req: Request):
        store.require_admin(req)
        ok = await asyncio.to_thread(model_registry.fetch)
        # Trae 动态表独立刷新：失败不影响 WorkBuddy 表，反之亦然。
        trae_ok = await asyncio.to_thread(model_registry.refresh_trae_models)
        with store.lock:
            store.data["model_registry"] = model_registry.export_state()
            store.save()
        snapshot = model_registry.snapshot()
        if not ok and not trae_ok:
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
            state = model_registry.set_enabled(mid, body["enabled"])
        except KeyError:
            raise HTTPException(404, "模型不存在")
        with store.lock:
            store.data["model_registry"] = state
            store.save()
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
            if "routing" in body:
                if body["routing"] not in ("manual", "round_robin"):
                    raise HTTPException(400, "调度方式无效")
                settings["routing"] = body["routing"]
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
            if "sync_window" in body:
                window = body["sync_window"]
                pattern = r"(?:[01]\d|2[0-3]):[0-5]\d"
                if (not isinstance(window, list) or len(window) != 2
                        or not all(isinstance(t, str) and re.fullmatch(pattern, t) for t in window)):
                    raise HTTPException(400, "统计时段需为两个 HH:MM 时间")
                start, end = window
                settings["sync_window"] = [start, end] if start <= end else [end, start]
            if "batch_spread" in body:
                try:
                    spread = int(body["batch_spread"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "批量签到错峰需为整数秒")
                if not 0 <= spread <= 600:
                    raise HTTPException(400, "批量签到错峰需在 0-600 秒之间")
                settings["batch_spread"] = spread
            store.data["pool"] = settings
            store.save()
        return {"ok": True, "pool": settings}

    @app.post("/admin/api/accounts")
    async def add_account(req: Request):
        store.require_admin(req)
        body = await payload(req)
        doc = body.get("credential")
        store.validate_credential(doc)
        backend = doc.get("backend", "workbuddy")
        name = clean_name(body.get("name"), str(doc["account"].get("nickname") or "新账号")[:60])
        with store.lock:
            if len(store.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            filename = aid + ".info"
            write_json(store.auth_dir / filename, doc)
            store.data["accounts"][aid] = {"file": filename, "name": name, "created": int(time.time()), "enabled": True, "backend": backend}
            if store.data["active"] is None:
                store.data["active"] = aid
            store.save()
            store.sync_active()
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
        from core.backends.trae_login import build_login_url, local_command

        fid, secret = secrets.token_hex(8), secrets.token_urlsafe(24)
        machine_id, device_id = secrets.token_hex(16), secrets.token_hex(16)
        port = _trae_port()
        now = time.time()
        # machine_id/device_id 必须与登录 URL 一致（上游把会话绑定到该设备对），随 fid 服务端暂存
        url = build_login_url(machine_id, device_id, callback=f"http://127.0.0.1:{port}/authorize")
        with store.lock:
            store.trae_flows = {k: v for k, v in store.trae_flows.items() if v["expires"] > now}
            if len(store.trae_flows) >= 20:
                store.trae_flows.pop(min(store.trae_flows, key=lambda k: store.trae_flows[k]["expires"]))
            store.trae_flows[fid] = {"machine_id": machine_id, "device_id": device_id, "secret": secret,
                                     "url": url, "expires": now + 600, "status": "pending",
                                     "message": None, "uid": None, "nickname": None}
        base = _public_base(req)
        return {"id": fid, "url": url, "expires_at": int((now + 600) * 1000),
                "port": port, "command": local_command(base, fid, secret)}

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
            if "backend" in body and body["backend"] != item.get("backend", "workbuddy"):
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
                with store.lock:
                    store.data["account_status"].setdefault(aid, {})["weight"] = weight
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise HTTPException(400, "启用状态不正确")
                new["enabled"] = body["enabled"]
            if body.get("active") is True:
                if not new["enabled"]:
                    raise HTTPException(400, "请先启用账号")
                try:
                    store.validate_credential(json.loads(store.file_for(new).read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    raise HTTPException(400, "凭据文件无法读取，请重新导入")
                store.data["active"] = aid
            store.data["accounts"][aid] = new
            if not new["enabled"] and store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
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
            if store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
            # Keep the file so an in-flight token refresh may still complete safely.
            # Account storage is authoritative; deleted records cannot be selected.
        return {"ok": True}

    @app.post("/admin/api/keys")
    async def add_key(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), "新客户端")
        with store.lock:
            if len(store.data["keys"]) >= 100:
                raise HTTPException(400, "最多保存 100 枚 API Key")
            kid, key = store.add_key(name, daily_limit=body.get("daily_limit"))
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
            store.save()
        return {"ok": True}

    @app.delete("/admin/api/keys/{kid}")
    async def revoke_key(kid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            if kid not in store.data["keys"]:
                raise HTTPException(404, "密钥不存在")
            if len(store.data["keys"]) <= 1:
                raise HTTPException(400, "请先创建一枚新密钥，再撤销最后一枚密钥")
            del store.data["keys"][kid]
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
        if model.startswith("trae/"):
            with store.lock:
                has_trae = any(it.get("backend", "workbuddy") == "trae" and it["enabled"] for it in store.data["accounts"].values())
            if not has_trae:
                raise HTTPException(400, "请先启用一个 Trae 账号")
        elif converter.CONFIG.get("cred") is None:
            raise HTTPException(400, "请先导入并启用一个账号")
        async with store.test_lock:
            started = time.monotonic()
            # Use a short-lived API key with the same authentication path as clients.
            with store.lock:
                temp_key = secrets.token_urlsafe(48)
                store.test_keys.add(digest(temp_key))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=MetricsMiddleware(converter.app, metrics, source="test")), base_url="http://internal") as client:
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
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8787, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")

"""trae_login — TRAE SOLO 登录：生成授权链接 → 接收回调 → 换 token 落盘。

Trae 的 OAuth 回调**必须是 127.0.0.1 本地地址**（认证页是给桌面 IDE 用的，
社区实测「不接受其它类型的回调」），因此浏览器登录后会跳到用户自己电脑上的
`http://127.0.0.1:18080/authorize`。为了避免手抄地址栏链接，本模块提供
**本机接收器**：一条命令在用户电脑上起个小监听，自动开浏览器、自动把回调
回传服务器完成登录（管理页只显示一条命令，无需复制粘贴任何 token）。

用法：
    # A. 本机接收器（推荐，管理页会生成带一次性密钥的命令）
    curl -s "https://<host>/trae/local.py?f=<flow>&t=<secret>" | python3 -

    # B. CLI 手动粘贴回调链接
    python3 -m core.backends.trae_login [--auth-dir DIR] [--port 18080]

流程：
  1. 生成 machine_id（32 hex）/ device_id（32 hex），URL 与落盘共用同一对；
  2. 打印登录 URL（auth_callback_url=http://127.0.0.1:<port>/authorize）；
  3. 用户在浏览器登录 → 回调打到本机接收器（或用户复制地址栏链接）；
  4. 解析回调（refreshToken / userInfo / userJwt，双层 unquote 容错，支持
     GET query / POST JSON / form 三种投递形态）；
  5. ExchangeToken（refreshToken 轮换）→ GetUserInfo；
  6. 写 auth_dir/trae-{uid}.info（{"backend":"trae", ...}，tmp+rename 0600）。

服务端由 Store.adopt_auth_files() 在 pool tick（60s）自动收养新落盘的 .info。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

from . import trae as T

DEFAULT_API_HOST = "https://api.trae.com.cn"
DEFAULT_CALLBACK_PORT = 18080
AUTH_CALLBACK = f"http://127.0.0.1:{DEFAULT_CALLBACK_PORT}/authorize"
PLUGIN_VERSION = "2.3.62834"


def build_login_url(machine_id: str, device_id: str, login_trace_id: str | None = None, callback: str | None = None) -> str:
    """构造 TRAE 授权登录 URL（参数集来源 Go login.sh，缺一不可）。"""
    params = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": T.CLIENT_ID,
        "redirect": "0",
        "login_trace_id": login_trace_id or secrets.token_hex(8),
        "auth_callback_url": callback or AUTH_CALLBACK,
        "machine_id": machine_id,
        "device_id": device_id,
        "x_device_id": device_id,
        "x_machine_id": machine_id,
        "x_device_brand": "PC",
        "x_device_type": "PC",
        "x_os_version": "1.0",
        "x_app_version": T.IDE_VERSION,
        "x_app_type": "stable",
    }
    return T.CONSOLE_HOST + "/authorization?" + urllib.parse.urlencode(params)


def _parse_json_param(raw: str):
    """解回调里 URL 编码的 JSON 参数（parse_qs 已解一层，再容错解一层 unquote）。"""
    if not raw:
        return None
    for val in (raw, urllib.parse.unquote(raw)):
        try:
            obj = json.loads(val)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _query_fields(query: str) -> dict:
    try:
        return {k: v[0] for k, v in urllib.parse.parse_qs(query or "", keep_blank_values=True).items()}
    except Exception:
        return {}


def _extract_fields(query: str = "", body: str = "") -> dict:
    """把回调请求（query + body）摊平成字段表，容忍多种投递形态。"""
    fields = _query_fields(query)
    if body:
        obj = None
        try:
            obj = json.loads(body)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            merged = dict(obj)
            if isinstance(obj.get("data"), dict):  # {"code":0,"data":{...}}
                merged.update(obj["data"])
            if isinstance(obj.get("Result"), dict):
                merged.update(obj["Result"])
            for key, value in merged.items():
                if isinstance(value, str):
                    fields.setdefault(key, value)
                elif isinstance(value, (dict, list)):
                    fields.setdefault(key, json.dumps(value, ensure_ascii=False))
        else:
            for key, value in _query_fields(body).items():
                fields.setdefault(key, value)
    return fields


def _pick(fields: dict, *names):
    for name in names:
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            return value
        if value not in (None, ""):
            return value
    return ""


def _parsed_from_fields(fields: dict) -> dict:
    """字段表 → 归一化凭据 {refresh_token, uid, nickname, ent_id, jwt_token, jwt_refresh, jwt_expires_at}。"""
    refresh_token = _pick(fields, "refreshToken", "refresh_token")
    user_info = _parse_json_param(_pick(fields, "userInfo", "user_info") or "")
    if not user_info and isinstance(fields.get("userInfo"), dict):
        user_info = fields["userInfo"]
    user_jwt = _parse_json_param(_pick(fields, "userJwt", "user_jwt") or "")
    if not user_jwt and isinstance(fields.get("userJwt"), dict):
        user_jwt = fields["userJwt"]
    user_info = user_info or {}
    user_jwt = user_jwt or {}

    uid = str(user_info.get("UserID") or "")
    nickname = str(user_info.get("ScreenName") or "")
    ent_id = str(user_info.get("TenantID") or "")
    jwt_token = str(user_jwt.get("Token") or "")
    jwt_refresh = str(user_jwt.get("RefreshToken") or "")
    if not refresh_token:
        refresh_token = jwt_refresh
    return {
        "refresh_token": refresh_token,
        "uid": uid,
        "nickname": nickname,
        "ent_id": ent_id,
        "jwt_token": jwt_token,
        "jwt_refresh": jwt_refresh,
        "jwt_expires_at": T.normalize_expires_at(user_jwt.get("TokenExpireAt") or 0),
    }


def parse_callback(callback_url: str) -> dict:
    """解析回调链接 → 归一化凭据。"""
    return _parsed_from_fields(_query_fields(urllib.parse.urlparse(callback_url).query))


def parse_received(method: str = "GET", path: str = "", query: str = "", body: str = "") -> dict:
    """解析本机接收器收到的原始回调请求（GET query / POST JSON / form 均可）。"""
    return _parsed_from_fields(_extract_fields(query=query, body=body))


def received_fields(method: str = "GET", path: str = "", query: str = "", body: str = "") -> list[str]:
    """回传报文的字段名清单（仅字段名，便于排查上游报文形态变化，不泄露值）。"""
    return sorted(_extract_fields(query=query, body=body).keys())



def login(
    callback,
    machine_id: str,
    device_id: str,
    auth_dir: Path,
    *,
    api_host: str = DEFAULT_API_HOST,
    client_factory=None,
):
    """回调（URL 字符串或字段 dict）→ 换 token → 落盘。返回摘要 dict。"""
    parsed = _parsed_from_fields(callback) if isinstance(callback, dict) else parse_callback(callback)
    refresh_token = parsed["refresh_token"]
    uid, nickname, ent_id = parsed["uid"], parsed["nickname"], parsed["ent_id"]
    make_client = client_factory or (lambda: httpx.Client(timeout=60))

    token, new_refresh, expires_at = "", refresh_token, 0
    with make_client() as client:
        if refresh_token:
            result = T.exchange_token(client, api_host, refresh_token)
            token = result["Token"]
            new_refresh = result.get("RefreshToken") or refresh_token
            expires_at = T.normalize_expires_at(result.get("TokenExpireAt") or 0)
            if expires_at <= time.time():
                expires_at = int(time.time()) + int(result.get("TokenExpireDuration") or 1209600)
            print(f"[*] ExchangeToken 成功：Token {token[:20]}…（已隐藏）", file=sys.stderr)
        else:
            token = parsed["jwt_token"]
            expires_at = parsed["jwt_expires_at"]
            if not token:
                raise RuntimeError("回调链接缺少 refreshToken，且 userJwt 也没有 Token")
            print("[*] 无 refreshToken，使用 userJwt 的 Token 兜底", file=sys.stderr)

        # GetUserInfo 确认 uid/enterpriseId（失败不阻塞，回退回调 userInfo）
        try:
            u_uid, u_nick, u_ent = T.get_user_info(client, api_host, token)
            uid = u_uid or uid
            nickname = u_nick or nickname
            ent_id = u_ent or ent_id
        except Exception as e:  # noqa: BLE001
            print(f"[*] GetUserInfo 失败（沿用回调 userInfo）：{e}", file=sys.stderr)

    if not uid:
        raise RuntimeError("未能获取 uid，请检查 token 是否有效")

    doc = {
        "backend": "trae",
        "account": {"uid": uid, "enterpriseId": ent_id, "nickname": nickname},
        "auth": {
            "accessToken": token,
            "refreshToken": new_refresh,
            "expiresAt": expires_at,
            "domain": "trae.cn",
            "apiHost": api_host,
            "machineId": machine_id,
            "deviceId": device_id,
        },
    }
    auth_dir = Path(auth_dir)
    auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = auth_dir / f"trae-{uid}.info"
    existed = path.exists()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    return {
        "uid": uid,
        "nickname": nickname,
        "enterprise_id": ent_id,
        "expires_at": expires_at,
        "file": str(path),
        "updated": existed,
    }


LOCAL_RECEIVER_TEMPLATE = '''#!/usr/bin/env python3
# TRAE 登录本机接收器 —— 由管理台一次性生成，用完即弃。
# 作用：在本机 127.0.0.1:__PORT__ 接收 Trae 登录回调，自动回传服务器完成登录。
import json, sys, threading, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = "__BASE__"
FLOW = "__FLOW__"
TOKEN = "__TOKEN__"
PORT = __PORT__
TIMEOUT = __TIMEOUT__


def post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


done = threading.Event()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        split = urllib.parse.urlsplit(self.path)
        try:
            result = post("/trae/api/complete", {
                "id": FLOW, "token": TOKEN, "method": self.command, "path": split.path,
                "query": split.query, "body": body,
            })
        except Exception as exc:
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        payload = json.dumps({"ok": bool(result.get("ok")),
                              "message": result.get("message") or result.get("error") or ""}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        if result.get("ok"):
            print("\\n  \\u2705 \\u767b\\u5f55\\u6210\\u529f\\uff1a%s (%s)" % (result.get("nickname") or "-", result.get("uid") or "-"))
            print("     \\u8d26\\u53f7\\u5df2\\u5199\\u5165\\uff0c\\u7ba1\\u7406\\u53f0\\u4f1a\\u81ea\\u52a8\\u5237\\u65b0\\u3002\\u53ef\\u4ee5\\u5173\\u95ed\\u672c\\u7a97\\u53e3\\u4e86\\u3002\\n")
            done.set()
        else:
            print("\\n  \\u274c \\u56de\\u4f20\\u5931\\u8d25\\uff1a%s\\n" % (result.get("error") or result.get("message")))

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle

    def log_message(self, *args):
        pass


def main():
    try:
        flow = get("/trae/api/flow/%s?t=%s" % (FLOW, urllib.parse.quote(TOKEN)))
    except Exception as exc:
        print("\\u65e0\\u6cd5\\u8fde\\u63a5\\u670d\\u52a1\\u5668\\uff1a%s" % exc)
        return 1
    if not flow.get("url"):
        print("\\u767b\\u5f55\\u6d41\\u7a0b\\u5df2\\u5931\\u6548\\uff08%s\\uff09\\uff0c\\u8bf7\\u5728\\u7ba1\\u7406\\u53f0\\u91cd\\u65b0\\u751f\\u6210\\u3002" % flow.get("error", "unknown"))
        return 1
    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        print("\\u7aef\\u53e3 %d \\u88ab\\u5360\\u7528\\uff1a%s" % (PORT, exc))
        return 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("")
    print("  TRAE \\u767b\\u5f55\\u63a5\\u6536\\u5668\\u5df2\\u542f\\u52a8\\uff08\\u672c\\u673a 127.0.0.1:%d\\uff09" % PORT)
    print("  \\u6b63\\u5728\\u6253\\u5f00\\u767b\\u5f55\\u9875\\uff0c\\u8bf7\\u5728\\u6d4f\\u89c8\\u5668\\u91cc\\u5b8c\\u6210\\u767b\\u5f55\\u2026")
    print("  \\u63d0\\u793a\\uff1a\\u672c\\u7a97\\u53e3\\u4e0d\\u8981\\u5173\\u95ed\\uff0c\\u767b\\u5f55\\u5b8c\\u6210\\u540e\\u4f1a\\u81ea\\u52a8\\u7ed3\\u675f\\u3002")
    print("")
    try:
        webbrowser.open(flow["url"])
    except Exception:
        print("  \\u81ea\\u52a8\\u6253\\u5f00\\u6d4f\\u89c8\\u5668\\u5931\\u8d25\\uff0c\\u8bf7\\u624b\\u52a8\\u8bbf\\u95ee\\uff1a")
        print("  " + flow["url"])
    done.wait(TIMEOUT)
    server.shutdown()
    if not done.is_set():
        print("\\n  \\u7b49\\u5f85\\u8d85\\u65f6\\uff08%d \\u79d2\\uff09\\uff0c\\u8bf7\\u5728\\u7ba1\\u7406\\u53f0\\u91cd\\u65b0\\u751f\\u6210\\u94fe\\u63a5\\u3002" % TIMEOUT)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def render_local_receiver(base_url: str, fid: str, secret: str, port: int = DEFAULT_CALLBACK_PORT, timeout: int = 900) -> str:
    """渲染一次性本机接收器脚本（占位符替换，避开 f-string 花括号地狱）。"""
    return (
        LOCAL_RECEIVER_TEMPLATE
        .replace("__BASE__", base_url.rstrip("/"))
        .replace("__PORT__", str(int(port)))
        .replace("__TIMEOUT__", str(int(timeout)))
        .replace("__FLOW__", fid)
        .replace("__TOKEN__", secret)
    )


def local_command(base_url: str, fid: str, secret: str) -> dict:
    """各平台一行命令（管理页展示 / 一键复制）。"""
    quote = urllib.parse.quote
    url = f"{base_url.rstrip('/')}/trae/local.py?f={quote(fid)}&t={quote(secret)}"
    return {
        "posix": f'curl -s "{url}" | python3 -',
        "windows": f'python -c "import urllib.request as u;exec(u.urlopen(r\'{url}\').read())"',
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TRAE SOLO 登录（写入 trae-{uid}.info）")
    ap.add_argument(
        "--auth-dir",
        default=os.environ.get("CODEBUDDY_AUTH_DIR", "/data/auth"),
        help="auth 文件目录（默认 $CODEBUDDY_AUTH_DIR 或 /data/auth）",
    )
    ap.add_argument("--api-host", default=DEFAULT_API_HOST, help="ExchangeToken/GetUserInfo host")
    args = ap.parse_args(argv)

    machine_id = secrets.token_hex(16)
    device_id = secrets.token_hex(16)
    url = build_login_url(machine_id, device_id)

    print("=" * 60)
    print("  TRAE SOLO 登录")
    print("=" * 60)
    print()
    print("步骤：")
    print("  1. 在浏览器打开下面的链接，用手机号/验证码登录")
    print("  2. 登录成功后浏览器会跳到打不开的 127.0.0.1 地址")
    print("  3. 复制浏览器地址栏的完整链接，粘贴到下面（不回显）")
    print()
    print(f"  {url}")
    print()
    try:
        callback_url = getpass.getpass("登录完成后，请粘贴浏览器地址栏的完整回调链接: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消", file=sys.stderr)
        return 1
    if not callback_url.strip():
        print("未输入回调链接，已取消", file=sys.stderr)
        return 1

    try:
        summary = login(callback_url.strip(), machine_id, device_id, Path(args.auth_dir), api_host=args.api_host)
    except (RuntimeError, httpx.HTTPError) as e:
        print(f"[!] 登录失败：{e}", file=sys.stderr)
        return 1

    expiry = time.strftime("%Y-%m-%d %H:%M", time.localtime(summary["expires_at"])) if summary["expires_at"] else "?"
    print()
    print("=" * 60)
    print(f"  登录完成（{'覆盖' if summary['updated'] else '新增'}）！")
    print(f"  UID       : {summary['uid']}")
    print(f"  Nickname  : {summary['nickname'] or '（未获取到）'}")
    print(f"  有效期至  : {expiry}")
    print(f"  已写入    : {summary['file']}")
    print("=" * 60)
    print("服务端会在 60s 内自动收养该账号（pool tick），无需重启容器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

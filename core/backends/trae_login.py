"""trae_login — TRAE SOLO 交互式登录：生成授权链接 → 粘贴回调 → 换 token 落盘。

用法：
    python3 -m core.backends.trae_login [--auth-dir DIR] [--api-host URL]

流程（对应 Go 项目 login.sh，去掉签到/积分/重启容器，那些是 v1.1）：
  1. 生成 machine_id/device_id（secrets.token_hex(16)，URL 与落盘共用同一对）；
  2. 打印登录 URL（auth_callback_url=http://127.0.0.1:18080/authorize）；
  3. 用户在浏览器登录 → 浏览器跳到打不开的 127.0.0.1 地址 → 复制地址栏链接粘贴；
  4. 解析回调（refreshToken / userInfo / userJwt，双层 unquote 容错）；
  5. ExchangeToken（refreshToken 轮换）→ GetUserInfo；
  6. 写 auth_dir/trae-{uid}.info（{"backend":"trae", ...}，tmp+rename 0600）。

服务端由 Store.adopt_auth_files() 在 pool tick（60s）自动收养新落盘的 .info，
无需重启容器（单写者＝服务进程，无竞态）。
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
AUTH_CALLBACK = "http://127.0.0.1:18080/authorize"
PLUGIN_VERSION = "2.3.62834"


def build_login_url(machine_id: str, device_id: str, login_trace_id: str | None = None) -> str:
    """构造 TRAE 授权登录 URL（参数集来源 Go login.sh）。"""
    params = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": T.CLIENT_ID,
        "redirect": "0",
        "login_trace_id": login_trace_id or secrets.token_hex(8),
        "auth_callback_url": AUTH_CALLBACK,
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


def parse_callback(callback_url: str) -> dict:
    """解析回调链接 → {refresh_token, uid, nickname, ent_id, jwt_token, jwt_refresh}。"""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    refresh_token = (query.get("refreshToken") or [""])[0]
    user_info = _parse_json_param((query.get("userInfo") or [""])[0]) or {}
    user_jwt = _parse_json_param((query.get("userJwt") or [""])[0]) or {}

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


def login(
    callback_url: str,
    machine_id: str,
    device_id: str,
    auth_dir: Path,
    *,
    api_host: str = DEFAULT_API_HOST,
    client_factory=None,
):
    """解析回调 → 换 token → 落盘。返回摘要 dict。失败抛 SystemExit/RuntimeError。"""
    parsed = parse_callback(callback_url)
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

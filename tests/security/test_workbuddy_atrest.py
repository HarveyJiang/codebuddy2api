"""issue #23：WorkBuddy 5.6.0 $wbEncrypted 加密登录态支持测试（离线自洽，不读真实 auth）。"""

import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

# 项目根由 tests/conftest.py 统一放进 sys.path。

from core.workbuddy_atrest_crypto import (  # noqa: E402
    decrypt_auth_field,
    encrypt_auth_field,
    is_encrypted_field,
)

# 44 字符 canonical base64 == 32 字节，仅用于测试（与真实密钥无关）
TEST_SECRET = base64.b64encode(hashlib.sha256(b"test-seed").digest()).decode()


@pytest.fixture(autouse=True)
def _test_secret(monkeypatch):
    monkeypatch.setenv("WORKBUDDY_AT_REST_SECRET", TEST_SECRET)


def test_plaintext_passthrough():
    """旧版明文 token 原样返回，不触发任何解密。"""
    assert decrypt_auth_field("abc.jwt.token") == "abc.jwt.token"
    assert decrypt_auth_field("") == ""
    assert not is_encrypted_field("abc")


def test_encrypt_decrypt_roundtrip():
    token = "eyJhbGciOiJSUzI1NiJ9.payload.signature"
    sealed = encrypt_auth_field(token)
    assert is_encrypted_field(sealed)
    assert sealed["$wbEncrypted"] == 1
    env = json.loads(base64.b64decode(sealed["envelope"]))
    assert env["suite"] == 1
    assert env["keyId"] == hashlib.sha256(
        hashlib.sha256(TEST_SECRET.encode()).digest()
    ).hexdigest()[:16]
    assert decrypt_auth_field(sealed) == token


def test_build_headers_from_encrypted_auth(tmp_path, monkeypatch):
    """CredentialManager 能对 $wbEncrypted 格式构建有效 Authorization（issue #23 场景）。"""
    from core.converter import CredentialManager

    session = {
        "auth": {
            "accessToken": encrypt_auth_field("access-token-plain"),
            "refreshToken": encrypt_auth_field("refresh-token-plain"),
            "domain": "www.workbuddy.cn",
            "expiresAt": 9999999999999,
        },
        "account": {"uid": "u1", "enterpriseId": "e1"},
    }
    f = tmp_path / "workbuddy-desktop.info"
    f.write_text(json.dumps(session), encoding="utf-8")
    cm = CredentialManager(f)
    h = cm.get_headers()
    assert h["Authorization"] == "Bearer access-token-plain"
    assert h["X-User-Id"] == "u1"

# ---------------------------------------------------------------------------
# 分层标记（见 pytest.ini / tests/README.md）
#
# 本文件整体属于「core」。标记写在文件里而不是逐条标注：同一文件的用例
# 保护的是同一块行为，分域跑（`pytest tests/pool`）时就该一起跑。
#   core    : 必须守住的不变量，默认只跑这些
#   e2e     : 起整套 create_app 走真实请求链路
#   network : 经由 mock 传输层模拟上游交互
# 若某条用例其实不属于本文件默认域（例如纯形状断言、只测常量的），
# 请在该用例上单独加 @pytest.mark.<域>，或在下面改用更准确的一档。
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.core]


def test_refresh_persists_with_0600_permissions(tmp_path):
    """回归：token 刷新写回 auth 文件后权限必须是 0600。

    历史缺陷：converter 的 CredentialManager 写回路径没有 chmod，os.replace 保留
    tmp 权限（umask 022 下 0644），而文件里是 accessToken/refreshToken——
    trae 登录时写的 0600 会被之后每次刷新悄悄降级成全局可读。
    """
    import httpx
    from core import converter as mod

    session = {
        "auth": {
            "accessToken": "old-access", "refreshToken": "old-refresh",
            "domain": "www.workbuddy.cn", "expiresAt": 1,
        },
        "account": {"uid": "u1", "enterpriseId": "e1"},
    }
    f = tmp_path / "workbuddy-desktop.info"
    f.write_text(json.dumps(session), encoding="utf-8")
    os.chmod(f, 0o600)

    def handler(req):
        return httpx.Response(200, json={"code": 0, "data": {
            "accessToken": "NEW-ACCESS", "refreshToken": "NEW-REFRESH",
            "expiresIn": 3600}})

    orig = mod.httpx.Client
    mod.httpx.Client = lambda **kw: orig(transport=httpx.MockTransport(handler), **kw)
    try:
        cm = mod.CredentialManager(f)
        cm._refresh()
    finally:
        mod.httpx.Client = orig

    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600, f"刷新后 auth 文件权限是 {oct(mode)}，凭据全局可读"


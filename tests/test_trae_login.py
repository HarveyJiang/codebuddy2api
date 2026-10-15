"""trae_login CLI 测试：登录 URL 参数集、回调解析、换 token 落盘全流程（MockTransport）。"""
import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

from core.backends import trae_login as L

_REAL_CLIENT = httpx.Client


def _callback(**overrides):
    params = {"refreshToken": "RT-abc"}
    params.update(overrides)
    return "http://127.0.0.1:18080/authorize?" + urllib.parse.urlencode(params)


def test_build_login_url_params():
    url = L.build_login_url("M" * 32, "D" * 32, login_trace_id="t" * 16)
    assert url.startswith("https://www.trae.cn/authorization?")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["client_id"] == [L.T.CLIENT_ID]
    assert qs["auth_callback_url"] == [L.AUTH_CALLBACK]
    assert qs["machine_id"] == ["M" * 32]
    assert qs["x_machine_id"] == ["M" * 32]
    assert qs["device_id"] == ["D" * 32]
    assert qs["login_trace_id"] == ["t" * 16]
    assert qs["login_channel"] == ["native_ide"]
    assert qs["x_app_version"] == [L.T.IDE_VERSION]
    # redirect=0 是登录表单出现的前提（=1 会让授权页直接渲染"登录成功"终态）。
    assert qs["redirect"] == ["0"]


def test_parse_callback_direct_refresh_token():
    p = L.parse_callback(_callback(
        userInfo=json.dumps({"UserID": "u1", "ScreenName": "小明", "TenantID": "t1"}),
        userJwt=json.dumps({"Token": "jwt", "RefreshToken": "jwt-rt"}),
    ))
    assert p["refresh_token"] == "RT-abc"
    assert p["uid"] == "u1"
    assert p["nickname"] == "小明"
    assert p["ent_id"] == "t1"
    assert p["jwt_token"] == "jwt"


def test_parse_callback_jwt_refresh_fallback():
    p = L.parse_callback(_callback(refreshToken="", userJwt=json.dumps({"RefreshToken": "from-jwt"})))
    assert p["refresh_token"] == "from-jwt"


def test_parse_callback_double_encoded_json():
    inner = urllib.parse.quote(json.dumps({"UserID": "u2", "ScreenName": "A"}))
    p = L.parse_callback(_callback(userInfo=inner))
    assert p["uid"] == "u2"


def test_parse_callback_empty():
    p = L.parse_callback("http://127.0.0.1:18080/authorize")
    assert p["refresh_token"] == ""
    assert p["uid"] == ""


def _factory(handler):
    return lambda: _REAL_CLIENT(transport=httpx.MockTransport(handler))


def test_login_full_flow_writes_trae_info():
    calls = []
    future_ms = int(time.time() * 1000) + 30 * 86400 * 1000

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("ExchangeToken"):
            return httpx.Response(200, json={"Result": {"Token": "ACCESS", "RefreshToken": "RT-new", "TokenExpireAt": future_ms}})
        if req.url.path.endswith("GetUserInfo"):
            return httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "小明", "EnterpriseID": "ent9"}})
        return httpx.Response(404)

    with tempfile.TemporaryDirectory() as d:
        summary = L.login(_callback(), "m" * 32, "d" * 32, Path(d), client_factory=_factory(handler))
        path = Path(d) / "trae-u1.info"
        assert path.exists()
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["backend"] == "trae"
        assert doc["account"] == {"uid": "u1", "enterpriseId": "ent9", "nickname": "小明"}
        assert doc["auth"]["accessToken"] == "ACCESS"
        assert doc["auth"]["refreshToken"] == "RT-new"
        assert doc["auth"]["expiresAt"] == future_ms // 1000  # 毫秒→秒
        assert doc["auth"]["domain"] == "trae.cn"
        assert doc["auth"]["apiHost"] == L.DEFAULT_API_HOST
        assert doc["auth"]["machineId"] == "m" * 32
        assert summary["updated"] is False
        assert oct(os.stat(path).st_mode)[-3:] == "600"
    assert any("ExchangeToken" in c for c in calls) and any("GetUserInfo" in c for c in calls)


def test_login_marked_updated_on_re_login():
    def handler(req):
        if req.url.path.endswith("ExchangeToken"):
            return httpx.Response(200, json={"Result": {"Token": "A", "TokenExpireDuration": 3600}})
        return httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "x"}})

    with tempfile.TemporaryDirectory() as d:
        L.login(_callback(), "m" * 32, "d" * 32, Path(d), client_factory=_factory(handler))
        second = L.login(_callback(), "m" * 32, "d" * 32, Path(d), client_factory=_factory(handler))
        assert second["updated"] is True


def test_login_jwt_token_fallback_when_no_refresh_token():
    future_ms = int(time.time() * 1000) + 10 * 86400 * 1000

    def handler(req):
        if req.url.path.endswith("GetUserInfo"):
            return httpx.Response(200, json={"Result": {"UserID": "u9", "ScreenName": "Y"}})
        return httpx.Response(404)

    cb = _callback(refreshToken="", userJwt=json.dumps({"Token": "JWT-TOKEN", "TokenExpireAt": future_ms}))
    with tempfile.TemporaryDirectory() as d:
        summary = L.login(cb, "m" * 32, "d" * 32, Path(d), client_factory=_factory(handler))
        doc = json.loads((Path(d) / "trae-u9.info").read_text(encoding="utf-8"))
        assert doc["auth"]["accessToken"] == "JWT-TOKEN"
        assert doc["auth"]["expiresAt"] == future_ms // 1000
        assert summary["uid"] == "u9"


def test_login_missing_token_raises():
    cb = _callback(refreshToken="", userJwt=json.dumps({}))
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(RuntimeError):
            L.login(cb, "m" * 32, "d" * 32, Path(d), client_factory=_factory(lambda r: httpx.Response(404)))


def test_login_missing_uid_raises():
    def handler(req):
        if req.url.path.endswith("ExchangeToken"):
            return httpx.Response(200, json={"Result": {"Token": "A", "TokenExpireDuration": 3600}})
        return httpx.Response(200, json={"Result": {}})

    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(RuntimeError):
            L.login(_callback(), "m" * 32, "d" * 32, Path(d), client_factory=_factory(handler))

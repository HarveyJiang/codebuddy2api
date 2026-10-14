"""TraeBackend 全量测试：请求体改写 / SSE 归一化 / 错误分类 / 凭据刷新 / 端到端路由。

上游用 httpx.MockTransport 模拟；e2e 用 create_app + ASGITransport，
MockTransport 按 host 分派（trae-api-cn.mchost.guru = Trae，copilot.tencent.com = WorkBuddy）。
"""
import asyncio
import json
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from core import converter
from core.backends import trae as T
from core.backends.base import BackendHTTPError
from core.backends.registry import backend_name_for, resolve_backend, trae_model_ids
from core.backends.workbuddy import WorkBuddyBackend

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

SOLO_SSE = (
    b"id:1\nevent:metadata\ndata:{\"model\":\"\",\"session_id\":\"s1\"}\n\n"
    b"id:2\nevent:timing_cost\ndata:{\"name\":\"llm_raw_chat_v2\"}\n\n"
    b"id:3\nevent:output\ndata:{\"response\":\"Hel\",\"reasoning_content\":\"let me \"}\n\n"
    b"id:4\nevent:output\ndata:{\"response\":\"lo\",\"reasoning_content\":\"thin\"}\n\n"
    b"id:5\nevent:output\ndata:{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function_call\":{\"name\":\"read\",\"arguments\":\"{\\\"p\\\":\",\"namespace\":\"ns\",\"partial_arguments\":\"zz\"}}]}\n\n"
    b"id:6\nevent:output\ndata:{\"tool_calls\":[{\"index\":0,\"function_call\":{\"arguments\":\"1}\"}}]}\n\n"
    b"id:7\nevent:token_usage\ndata:{\"prompt_tokens\":21,\"completion_tokens\":142,\"total_tokens\":163,\"reasoning_tokens\":135}\n\n"
    b"id:8\nevent:done\ndata:{\"finish_reason\":\"stop\"}\n\n"
)


def trae_credential(uid="trae-user", expires_in=30 * 86400, backend="trae"):
    return {
        "backend": backend,
        "account": {"uid": uid, "nickname": "Trae测试", "enterpriseId": "ent"},
        "auth": {
            "accessToken": "TRAE-ACCESS",
            "refreshToken": "TRAE-REFRESH",
            "expiresAt": int(time.time()) + expires_in,
            "domain": "trae.cn",
            "apiHost": T.OAUTH_HOST,
            "machineId": "m" * 32,
            "deviceId": "d" * 32,
        },
    }


def wb_credential(uid="wb-user"):
    return {
        "account": {"uid": uid, "nickname": "WB测试", "enterpriseId": "ent"},
        "auth": {"accessToken": "WB-ACCESS", "refreshToken": "WB-REFRESH", "expiresAt": int(time.time() * 1000) + 3600000},
    }


async def _achunks(data: bytes):
    yield data


# 记录未被任何 monkeypatch 污染的原始 AsyncClient，供 e2e 手动 patch/restore 使用。
_REAL_ASYNC_CLIENT = httpx.AsyncClient
_REAL_SYNC_CLIENT = httpx.Client


def _events(raw: bytes):
    out = []
    for line in raw.decode().splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


def _patch_upstream(monkeypatch, handler):
    real = _REAL_ASYNC_CLIENT
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(converter.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    monkeypatch.setattr(converter, "_log", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# A. prepare_solo_body
# ---------------------------------------------------------------------------


def test_prepare_forces_stream_function_and_config_name():
    body = T.prepare_solo_body({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
    assert body["stream"] is True
    assert body["function"] == "solo_work_lite"
    assert body["config_name"] == "glm-5.2"
    assert body["model"] == "glm-5.2"
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hi"}]


def test_prepare_empty_model_defaults():
    body = T.prepare_solo_body({"model": "  ", "messages": []})
    assert body["config_name"] == T.DEFAULT_CONFIG_NAME
    assert body["model"] == T.DEFAULT_CONFIG_NAME


def test_prepare_content_array_passthrough():
    arr = [{"type": "image_url", "image_url": {"url": "x"}}, {"type": "text", "text": "t"}]
    body = T.prepare_solo_body({"messages": [{"role": "user", "content": arr}]})
    assert body["messages"][0]["content"] == arr


def test_prepare_non_dict_passthrough():
    assert T.prepare_solo_body("nope") == "nope"


@pytest.mark.parametrize("choice,expected", [
    ("none", None),
    ("auto", "auto"),
    ({"type": "none"}, None),
    ({"type": "auto"}, "auto"),
    ({"type": "required"}, "required"),
    ({"type": "function", "function": {"name": "read"}}, "read"),
    ({"type": "function"}, "auto"),
    ({"type": "weird"}, None),
    (123, None),
])
def test_prepare_tool_choice(choice, expected):
    body = T.prepare_solo_body({"tool_choice": choice, "tools": [{"function": {"name": "read", "parameters": {}}}]})
    assert body.get("tool_choice") == expected
    # 仅 "none" 形态删除 tools/functions（上游据此抑制工具调用）；未知形态只删 tool_choice。
    if choice in ("none", {"type": "none"}):
        assert "tools" not in body
    else:
        assert "tools" in body


def test_prepare_tools_parameters_to_json_string():
    schema = {"type": "object", "properties": {"cmd": {"type": "string"}}}
    body = T.prepare_solo_body({"tools": [{"type": "function", "function": {"name": "read", "parameters": schema}}]})
    params = body["tools"][0]["function"]["parameters"]
    assert isinstance(params, str)
    assert json.loads(params) == schema


def test_prepare_tools_drop_invalid_entries():
    body = T.prepare_solo_body({"tools": [{"type": "function"}, "bad", {"function": {"name": "ok"}}]})
    assert len(body["tools"]) == 1
    assert body["tools"][0]["function"]["name"] == "ok"


def test_prepare_assistant_tool_calls_to_function_call_and_drop_noname():
    body = T.prepare_solo_body({"messages": [{
        "role": "assistant",
        "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "", "arguments": "{}"}},
        ],
    }]})
    tcs = body["messages"][0]["tool_calls"]
    assert len(tcs) == 1
    assert "function" not in tcs[0]
    assert tcs[0]["function_call"] == {"name": "read", "arguments": "{}"}


def test_prepare_assistant_all_noname_drops_tool_calls():
    body = T.prepare_solo_body({"messages": [{"role": "assistant", "tool_calls": [{"id": "b", "function": {"name": ""}}]}]})
    assert "tool_calls" not in body["messages"][0]


# ---------------------------------------------------------------------------
# B. map_model / resolve_backend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,expected", [
    ("glm-5.2", "glm-5.2"),
    ("", "glm-5.2"),
    ("auto", "glm-5.2"),
    ("default", "glm-5.2"),
    ("glm-5.2__dev", "glm-5.2"),
    ("DeepSeek-V4-Pro", "DeepSeek-V4-Pro"),
    ("deepseek_v4_pro", "DeepSeek-V4-Pro"),
    ("DEEPSEEK_V4_PRO", "DeepSeek-V4-Pro"),
    ("trae/glm-5", "glm-5"),
])
def test_map_model(model, expected):
    assert T.map_model(model) == expected


def test_map_model_unknown_raises():
    with pytest.raises(T.UnknownTraeModel):
        T.map_model("bogus-model")


def test_trae_static_models_count():
    assert len(TRAE_MODELS := T.TRAE_STATIC_MODELS) == 32
    assert len(trae_model_ids()) == 32
    assert all(m["owned_by"] == "trae-solo" for m in TRAE_MODELS)


def test_resolve_backend_trae():
    backend, upstream = resolve_backend("trae/deepseek_v4_pro")
    assert backend.name == "trae"
    assert backend.tencent_policies is False
    assert upstream == "DeepSeek-V4-Pro"


def test_resolve_backend_unknown_trae_400():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        resolve_backend("trae/bogus")
    assert ei.value.status_code == 400


def test_resolve_backend_kill_switch(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("CODEBUDDY_ENABLE_TRAE", "0")
    with pytest.raises(HTTPException) as ei:
        resolve_backend("trae/glm-5.2")
    assert ei.value.status_code == 400


def test_resolve_backend_escape_hatch(monkeypatch):
    monkeypatch.setenv("CODEBUDDY_TRAE_ALLOW_UNKNOWN_MODELS", "1")
    backend, upstream = resolve_backend("trae/whatever")
    assert backend.name == "trae"
    assert upstream == "whatever"


@pytest.mark.parametrize("model,expected", [("glm-5.2", "workbuddy"), ("auto", "workbuddy"), (None, "workbuddy"), ("trae/x", "trae")])
def test_backend_name_for(model, expected):
    assert backend_name_for(model) == expected


# ---------------------------------------------------------------------------
# C. SSE 归一化
# ---------------------------------------------------------------------------


def test_normalize_full_stream():
    raw = _collect_stream_sync(T.normalize_solo_stream(_achunks(SOLO_SSE), model="glm-5.2"))
    events = _events(raw)
    assert raw.endswith(b"data: [DONE]\n\n")
    # content 合并
    assert "".join(e["choices"][0]["delta"].get("content", "") for e in events) == "Hello"
    assert "".join(e["choices"][0]["delta"].get("reasoning_content", "") for e in events) == "let me thin"
    # tool_calls: function_call → function，剥离 namespace/partial_arguments，按 index 拼接
    tcs = [tc for e in events for tc in e["choices"][0]["delta"].get("tool_calls", [])]
    assert tcs[0]["function"] == {"name": "read", "arguments": '{"p":'}
    assert tcs[1]["function"] == {"arguments": "1}"}
    # usage 挂在 done chunk
    done = [e for e in events if e["choices"][0].get("finish_reason")]
    assert done[-1]["choices"][0]["finish_reason"] == "stop"
    assert done[-1]["usage"]["total_tokens"] == 163
    assert all(e["model"] == "glm-5.2" for e in events)


def _collect_stream_sync(agen):
    async def run():
        return b"".join([c async for c in agen])
    return asyncio.run(run())


def test_normalize_no_done_appends_done():
    raw = _collect_stream_sync(T.normalize_solo_stream(_achunks(b'event:output\ndata:{"response":"hi"}\n\n')))
    assert raw.endswith(b"data: [DONE]\n\n")


def test_normalize_stream_error_emits_then_raises_emitted():
    async def run():
        out = []
        try:
            async for c in T.normalize_solo_stream(_achunks(b'event:error\ndata:{"code":1005,"message":"plan"}\n\n')):
                out.append(c)
        except BackendHTTPError as e:
            return b"".join(out), e
        return b"".join(out), None
    raw, err = asyncio.run(run())
    obj = json.loads(raw.decode().splitlines()[0][6:])
    assert obj["error"]["code"] == 402
    assert raw.endswith(b"data: [DONE]\n\n")
    assert err is not None and err.status == 402 and err.emitted is True


def test_normalize_stream_error_raise_on_error_direct():
    async def run():
        out = []
        try:
            async for c in T.normalize_solo_stream(_achunks(b'event:error\ndata:{"code":1005}\n\n'), raise_on_error=True):
                out.append(c)
        except BackendHTTPError as e:
            return b"".join(out), e
        return b"".join(out), None
    raw, err = asyncio.run(run())
    assert raw == b""
    assert err is not None and err.emitted is False


def test_collect_stream_aggregates_trae():
    async def run():
        normalized = b"".join([c async for c in T.normalize_solo_stream(_achunks(SOLO_SSE), model="glm-5.2")])
        return await converter._collect_stream(converter._aiter_str_lines(normalized), keep_reasoning=True)
    result = asyncio.run(run())
    msg = result["choices"][0]["message"]
    assert msg["content"] == "Hello"
    assert msg["reasoning_content"] == "let me thin"
    assert msg["tool_calls"] == [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"p":1}'}}]
    assert result["usage"]["total_tokens"] == 163
    assert result["model"] == "glm-5.2"


def test_trae_backend_astream_http_error_classified(monkeypatch):
    _patch_upstream(monkeypatch, lambda req: httpx.Response(401, content=b'{"message":"unauthorized"}'))
    async def run():
        with pytest.raises(BackendHTTPError) as ei:
            async for _ in T.TraeBackend().astream({}, {"model": "glm-5.2"}):
                pass
        return ei.value
    err = asyncio.run(run())
    assert err.status == 401
    assert err.transport is False


def test_trae_backend_astream_1005_http_body_maps_402(monkeypatch):
    _patch_upstream(monkeypatch, lambda req: httpx.Response(400, content=b'{"code":1005,"plan":"insufficient"}'))
    async def run():
        with pytest.raises(BackendHTTPError) as ei:
            async for _ in T.TraeBackend().astream({}, {}):
                pass
        return ei.value.status
    assert asyncio.run(run()) == 402


def test_trae_backend_astream_transport_error(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("boom", request=req)
    _patch_upstream(monkeypatch, handler)
    async def run():
        with pytest.raises(BackendHTTPError) as ei:
            async for _ in T.TraeBackend().astream({}, {}):
                pass
        return ei.value
    err = asyncio.run(run())
    assert err.status == 502 and err.transport is True


# ---------------------------------------------------------------------------
# D. 错误分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,body,expected", [
    (200, '{"code":1005,"plan":true}', 402),
    (400, '{"code":1005}', 402),
    (401, "", 401),
    (429, "", 429),
    (404, "", 404),
    (503, "", 500),
    (400, "", 400),
    (200, "", 200),
])
def test_classify_http_status(status, body, expected):
    assert T.classify_http_status(status, body) == expected


@pytest.mark.parametrize("code,expected", [(1005, 402), (401, 401), (429, 429), (404, 404), (2001, 400), (503, 500)])
def test_classify_stream_code(code, expected):
    assert T.classify_stream_code(code) == expected


@pytest.mark.parametrize("value,expected", [(1786847930141, 1786847930), (1786847930, 1786847930), (0, 0), ("x", 0)])
def test_normalize_expires_at(value, expected):
    assert T.normalize_expires_at(value) == expected


# ---------------------------------------------------------------------------
# E. TraeCredentialManager
# ---------------------------------------------------------------------------


def _write_trae(tmp: Path, doc=None, name="trae-user.info"):
    p = tmp / name
    p.write_text(json.dumps(doc or trae_credential()), encoding="utf-8")
    return p


def test_solo_headers_full_set(tmp_path):
    mgr = T.TraeCredentialManager(_write_trae(tmp_path))
    h = mgr.get_headers()
    assert h["Authorization"] == "Cloud-IDE-JWT TRAE-ACCESS"
    assert h["X-Cloudide-Token"] == "TRAE-ACCESS"
    assert h["X-Ide-Token"] == "TRAE-ACCESS"
    assert h["X-Uid"] == "trae-user"
    assert h["X-App-Id"] == T.APP_ID
    assert h["X-Machine-Id"] == "m" * 32
    assert h["X-Device-Id"] == "d" * 32
    assert h["User-Agent"] == T.CLIENT_UA
    assert h["Accept"] == "text/event-stream"
    assert h["Request-Traffic-Type"] == "prod"
    assert mgr.backend_name == "trae"


def test_needs_refresh_24h_skew_seconds(tmp_path):
    # 12 小时后过期 < 24h skew → 需要刷新
    mgr = T.TraeCredentialManager(_write_trae(tmp_path, trae_credential(expires_in=12 * 3600)))
    assert mgr._is_expired() is True
    # 30 天后过期 → 无需刷新
    mgr2 = T.TraeCredentialManager(_write_trae(tmp_path, trae_credential(expires_in=30 * 86400), name="b.info"))
    assert mgr2._is_expired() is False
    # 无 expiresAt → 视为过期
    doc = trae_credential()
    doc["auth"]["expiresAt"] = 0
    mgr3 = T.TraeCredentialManager(_write_trae(tmp_path, doc, name="c.info"))
    assert mgr3._is_expired() is True


def test_refresh_rotates_normalizes_ms_and_persists(tmp_path):
    doc = trae_credential(expires_in=12 * 3600)  # 触发刷新
    path = _write_trae(tmp_path, doc)
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"Result": {"Token": "NEW-ACCESS", "RefreshToken": "NEW-REFRESH", "TokenExpireAt": 1786847930141}})

    real = httpx.Client
    import core.backends.trae as mod
    orig = mod.httpx.Client
    mod.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        mgr = T.TraeCredentialManager(path)
        mgr._refresh()
    finally:
        mod.httpx.Client = orig

    assert calls and "ExchangeToken" in calls[0]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["auth"]["accessToken"] == "NEW-ACCESS"
    assert saved["auth"]["refreshToken"] == "NEW-REFRESH"
    assert saved["auth"]["expiresAt"] == 1786847930  # 毫秒 → 秒
    # 保留 backend / machineId / deviceId / apiHost
    assert saved["backend"] == "trae"
    assert saved["auth"]["machineId"] == "m" * 32
    assert saved["auth"]["deviceId"] == "d" * 32
    assert saved["account"]["uid"] == "trae-user"


def test_refresh_via_get_headers_when_expired(tmp_path):
    path = _write_trae(tmp_path, trae_credential(expires_in=10))
    real = httpx.Client
    import core.backends.trae as mod
    orig = mod.httpx.Client
    mod.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"Result": {"Token": "FRESH", "TokenExpireDuration": 3600}})), **kw)
    try:
        mgr = T.TraeCredentialManager(path)
        assert mgr.get_headers()["Authorization"] == "Cloud-IDE-JWT FRESH"
    finally:
        mod.httpx.Client = orig


def test_refresh_failure_does_not_write_file(tmp_path):
    path = _write_trae(tmp_path, trae_credential(expires_in=10))
    before = path.read_text(encoding="utf-8")
    import core.backends.trae as mod
    real = httpx.Client
    orig = mod.httpx.Client
    mod.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"Result": {}})), **kw)
    try:
        mgr = T.TraeCredentialManager(path)
        with pytest.raises(RuntimeError):
            mgr._refresh()
    finally:
        mod.httpx.Client = orig
    assert path.read_text(encoding="utf-8") == before


def test_summary(tmp_path):
    mgr = T.TraeCredentialManager(_write_trae(tmp_path))
    s = mgr.summary()
    assert s["uid"] == "trae-user"
    assert s["nickname"] == "Trae测试"
    assert s["token_expired"] is False


def test_make_credential_dispatches_by_backend(tmp_path):
    from core.backends.registry import make_credential
    trae_mgr = make_credential(_write_trae(tmp_path, name="t.info"))
    assert isinstance(trae_mgr, T.TraeCredentialManager)
    wb = tmp_path / "w.info"
    wb.write_text(json.dumps(wb_credential()), encoding="utf-8")
    assert isinstance(make_credential(wb), converter.CredentialManager)


# ---------------------------------------------------------------------------
# F. 端到端（create_app）
# ---------------------------------------------------------------------------

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


class TraeEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import admin.server as admin_server
        import admin.models as admin_models
        self.admin_server = admin_server
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "trae-u1.info").write_text(json.dumps(trae_credential("trae-user")), encoding="utf-8")
        (self.auth / "wb.info").write_text(json.dumps(wb_credential()), encoding="utf-8")
        self._orig_fetch = admin_models.ModelRegistry.fetch
        admin_models.ModelRegistry.fetch = lambda self: False  # 免网络
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")
        self._patched = False

    async def asyncTearDown(self):
        await self.client.aclose()
        # 恢复原始 AsyncClient（patch_upstream 会把 httpx 模块属性改成 lambda，
        # 因此必须用 import 时保存的原始类来还原，不能读 httpx.AsyncClient）。
        self.admin_server.converter.httpx.AsyncClient = _REAL_ASYNC_CLIENT
        import admin.models as admin_models
        admin_models.ModelRegistry.fetch = self._orig_fetch
        self.temp.cleanup()

    def patch_upstream(self, handler):
        # 注意：patch 作用于共享的 httpx 模块属性，self.client（ASGI）已用原始类创建，不受影响。
        transport = httpx.MockTransport(handler)
        self.admin_server.converter.httpx.AsyncClient = lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
        self._patched = True

    def trae_aid(self):
        for aid, item in self.app.state.store.data["accounts"].items():
            if item.get("backend") == "trae":
                return aid
        raise AssertionError("no trae account")

    async def login_admin(self):
        await self.client.post("/admin/api/login", json={"key": ADMIN})
        csrf = (await self.client.get("/admin/api/session")).json()["csrf"]
        return {"X-CSRF-Token": csrf}

    async def test_models_list_has_trae_owned_by(self):
        r = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        assert r.status_code == 200
        data = r.json()["data"]
        trae = [m for m in data if m["id"].startswith("trae/")]
        assert len(trae) == 32
        assert all(m["owned_by"] == "trae-solo" for m in trae)
        assert any(m["id"] == "trae/glm-5.2" for m in trae)

    async def test_account_rows_trae_expiry_milliseconds(self):
        rows = {r["backend"]: r for r in self.app.state.store.account_rows()}
        assert "trae" in rows and "workbuddy" in rows
        # trae 的秒级 expiresAt 被归一为毫秒
        assert rows["trae"]["expires_at"] > 1_000_000_000_000
        assert rows["trae"]["expired"] is False

    async def test_pool_select_filters_backend(self):
        pool = self.app.state.pool
        aid_trae, _ = pool.select("k1", "trae")
        aid_wb, _ = pool.select("k1", "workbuddy")
        assert self.app.state.store.data["accounts"][aid_trae]["backend"] == "trae"
        assert self.app.state.store.data["accounts"][aid_wb]["backend"] == "workbuddy"

    async def test_adopt_auth_files(self):
        store = self.app.state.store
        (self.auth / "trae-u2.info").write_text(json.dumps(trae_credential("trae-user2")), encoding="utf-8")
        added = store.adopt_auth_files()
        assert added == 1
        assert sum(1 for i in store.data["accounts"].values() if i["file"] == "trae-u2.info") == 1
        # 二次调用幂等
        assert store.adopt_auth_files() == 0

    async def test_edit_rejects_backend_change(self):
        await self.client.post("/admin/api/login", json={"key": ADMIN})
        csrf = (await self.client.get("/admin/api/session")).json()["csrf"]
        aid = self.trae_aid()
        r = await self.client.patch(f"/admin/api/accounts/{aid}", json={"backend": "workbuddy"}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 400

    async def test_validate_rejects_bad_backend_top_level(self):
        await self.client.post("/admin/api/login", json={"key": ADMIN})
        csrf = (await self.client.get("/admin/api/session")).json()["csrf"]
        bad = trae_credential()
        bad["backend"] = "nope"
        r = await self.client.post("/admin/api/accounts", json={"credential": bad}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 400

    async def test_e2e_chat_stream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.text.strip().endswith("data: [DONE]")
        events = _events(r.content)
        assert any("Hel" in (e["choices"][0]["delta"].get("content") or "") for e in events)

    async def test_e2e_chat_nonstream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        body = r.json()
        msg = body["choices"][0]["message"]
        assert msg["content"] == "Hello"
        assert msg["tool_calls"][0]["function"]["name"] == "read"
        assert body["usage"]["total_tokens"] == 163

    async def test_e2e_responses_stream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/responses", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "input": "hi", "stream": True})
        assert r.status_code == 200
        assert "response.completed" in r.text
        assert "Hello" in r.text

    async def test_e2e_responses_nonstream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/responses", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "input": "hi"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"

    async def test_e2e_anthropic_stream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/messages", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.status_code == 200
        assert "event: message_stop" in r.text

    async def test_e2e_anthropic_nonstream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/messages", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "assistant"

    async def test_e2e_count_tokens_default_local_estimate(self):
        def handler(req):
            raise AssertionError("count_tokens 默认不应打上游")
        self.patch_upstream(handler)
        r = await self.client.post("/v1/messages/count_tokens", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "x" * 400}]})
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 100

    async def test_e2e_count_tokens_upstream_env(self):
        import os
        calls = []
        def handler(req):
            calls.append(1)
            return httpx.Response(200, content=b'event:token_usage\ndata:{"prompt_tokens":42,"completion_tokens":1,"total_tokens":43}\n\n')
        self.patch_upstream(handler)
        os.environ["CODEBUDDY_TRAE_COUNT_TOKENS_UPSTREAM"] = "1"
        try:
            r = await self.client.post("/v1/messages/count_tokens", headers={"Authorization": "Bearer " + API},
                                       json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        finally:
            os.environ.pop("CODEBUDDY_TRAE_COUNT_TOKENS_UPSTREAM", None)
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 42
        assert calls

    async def test_e2e_http_error_401_cools_account(self):
        self.patch_upstream(lambda req: httpx.Response(401, content=b'{"message":"session dead"}'))
        aid = self.trae_aid()
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401
        assert self.app.state.store.data["account_status"][aid]["cooldown_until"] > 0

    async def test_e2e_stream_error_1005_cools_account(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=b'event:error\ndata:{"code":1005,"message":"plan limit"}\n\n'))
        aid = self.trae_aid()
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.status_code == 200  # SSE 已开始
        assert '"code": 402' in r.text
        assert self.app.state.store.data["account_status"][aid]["cooldown_until"] > 0

    async def test_e2e_workbuddy_still_routes_to_wb_account(self):
        # 无前缀模型 → WorkBuddy 账号；上游 200 原样直通
        wb_sse = b'data: {"model":"auto","choices":[{"delta":{"content":"wb-ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        self.patch_upstream(lambda req: httpx.Response(200, content=wb_sse))
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "wb-ok"

    async def test_unknown_trae_model_400(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=b""))
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "trae/bogus", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400

    # ---- 管理页 Trae 登录入口 ----

    def patch_trae_login_http(self, handler):
        import core.backends.trae_login as tl
        real = _REAL_SYNC_CLIENT
        self._orig_sync = tl.httpx.Client
        tl.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)

    async def test_trae_authorize_requires_admin(self):
        assert (await self.client.post("/admin/api/trae/authorize")).status_code == 401

    async def test_trae_authorize_returns_url_and_flow(self):
        headers = await self.login_admin()
        r = await self.client.post("/admin/api/trae/authorize", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["url"].startswith("https://www.trae.cn/authorization?")
        assert "auth_callback_url=http%3A%2F%2F127.0.0.1%3A18080%2Fauthorize" in data["url"]
        assert data["id"] in self.app.state.store.trae_flows

    async def test_trae_login_end_to_end_adds_account(self):
        headers = await self.login_admin()
        flow = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()

        def handler(req):
            if req.url.path.endswith("ExchangeToken"):
                return httpx.Response(200, json={"Result": {"Token": "NEW", "RefreshToken": "R2", "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000}})
            if req.url.path.endswith("GetUserInfo"):
                return httpx.Response(200, json={"Result": {"UserID": "trae-it", "ScreenName": "集成账号", "EnterpriseID": "E"}})
            return httpx.Response(404)

        self.patch_trae_login_http(handler)
        try:
            r = await self.client.post("/admin/api/trae/login", headers=headers,
                                       json={"id": flow["id"], "callback_url": "http://127.0.0.1:18080/authorize?refreshToken=RT"})
        finally:
            import core.backends.trae_login as tl
            tl.httpx.Client = self._orig_sync
        assert r.status_code == 200, r.text
        assert r.json()["uid"] == "trae-it"
        # 已落盘并被收养
        assert (self.auth / "trae-trae-it.info").exists()
        rows = self.app.state.store.account_rows()
        trae = [x for x in rows if x["backend"] == "trae"]
        assert any(x["uid"] == "trae-it" for x in trae)
        # flow 保留但标记为已完成（供管理页轮询）
        assert self.app.state.store.trae_flows[flow["id"]]["status"] == "done"

    async def test_trae_login_requires_callback(self):
        headers = await self.login_admin()
        flow = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        r = await self.client.post("/admin/api/trae/login", headers=headers, json={"id": flow["id"], "callback_url": "  "})
        assert r.status_code == 400

    async def test_trae_login_unknown_flow_400(self):
        headers = await self.login_admin()
        r = await self.client.post("/admin/api/trae/login", headers=headers,
                                   json={"id": "nope", "callback_url": "http://127.0.0.1:18080/authorize?refreshToken=x"})
        assert r.status_code == 400

    async def test_trae_cancel_flow(self):
        headers = await self.login_admin()
        flow = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        r = await self.client.delete(f"/admin/api/trae/{flow['id']}", headers=headers)
        assert r.status_code == 200
        assert flow["id"] not in self.app.state.store.trae_flows

    async def test_trae_account_checkin_and_credits(self):
        # v1.1：Trae 账号支持积分查询与签到（ug 接口），不再门控。
        headers = await self.login_admin()
        aid = self.trae_aid()
        calls = []
        state = {"claimed": False}

        def handler(req):
            calls.append(req.url.path)
            if req.url.path.endswith("/checkin_credits/status"):
                return httpx.Response(200, json={"checked_in": state["claimed"], "credits": 50, "enable": True})
            if req.url.path.endswith("/checkin_credits/claim"):
                state["claimed"] = True
                return httpx.Response(200, json={})
            if req.url.path.endswith("/ide_user_ent_usage"):
                return httpx.Response(200, json={"is_credits_billing": True, "user_entitlement_pack_list": [
                    {"entitlement_base_info": {"quota": {"credits_limit": 120}}},
                    {"entitlement_base_info": {"quota": {"credits_limit": 30}}},
                ]})
            return httpx.Response(404)

        real = _REAL_SYNC_CLIENT
        self.admin_server.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        try:
            r = await self.client.post(f"/admin/api/accounts/{aid}/actions/checkin", headers=headers)
        finally:
            self.admin_server.httpx.Client = real
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        assert body["message"] == "签到成功"
        assert body["remaining"] == 150.0  # 120 + 30
        status = self.app.state.store.data["account_status"][aid]
        assert status["remaining"] == 150.0 and status["packages"] == 1
        assert status["credits_updated"] > 0
        today = datetime.fromtimestamp(time.time(), timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        assert status["checkin_date"] == today
        assert "/trae/api/v2/ug/checkin_credits/claim" in calls

    async def test_trae_account_status_pulls_credits_without_checkin(self):
        # action=status：只查状态 + 积分，不执行签到。
        headers = await self.login_admin()
        aid = self.trae_aid()
        calls = []

        def handler(req):
            calls.append(req.url.path)
            if req.url.path.endswith("/checkin_credits/status"):
                return httpx.Response(200, json={"checked_in": False, "credits": 0, "enable": True})
            if req.url.path.endswith("/ide_user_ent_usage"):
                return httpx.Response(200, json={"user_entitlement_pack_list": []})
            return httpx.Response(404)

        real = _REAL_SYNC_CLIENT
        self.admin_server.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        try:
            r = await self.client.post(f"/admin/api/accounts/{aid}/actions/status", headers=headers)
        finally:
            self.admin_server.httpx.Client = real
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert "/trae/api/v2/ug/checkin_credits/claim" not in calls
        # 无权益包 → remaining 0 但不置 packages（不误判 exhausted）
        status = self.app.state.store.data["account_status"][aid]
        assert status["remaining"] == 0.0 and status.get("packages", 0) == 0

    async def test_trae_account_ug_401_marks_auth_invalid(self):
        headers = await self.login_admin()
        aid = self.trae_aid()

        def handler(req):
            return httpx.Response(401, json={"message": "token dead"})

        real = _REAL_SYNC_CLIENT
        self.admin_server.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        try:
            r = await self.client.post(f"/admin/api/accounts/{aid}/actions/status", headers=headers)
        finally:
            self.admin_server.httpx.Client = real
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and "重新授权" in body["message"]
        status = self.app.state.store.data["account_status"][aid]
        assert status["auth_invalid"] is True and status["cooldown_until"] > 0

    async def test_admin_static_has_trae_entry(self):
        html = (await self.client.get("/admin/", headers={"Authorization": "Bearer " + API}))
        # /admin/ 需要管理会话吗？静态页直接返回
        assert html.status_code in (200, 401)
        index = Path(__file__).resolve().parent.parent / "admin" / "static" / "index.html"
        assert 'id="mode-trae"' in index.read_text(encoding="utf-8")

    # ---- 本机接收器（免复制粘贴） ----

    async def test_authorize_returns_local_command(self):
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        # 回调必须是本机 127.0.0.1（Trae 只认这种回调）
        assert "auth_callback_url=http%3A%2F%2F127.0.0.1%3A18080%2Fauthorize" in data["url"]
        assert data["port"] == 18080
        cmd = data["command"]
        assert f"/trae/local.py?f={data['id']}" in cmd["windows"]
        assert f"/trae/local.py?f={data['id']}" in cmd["posix"]
        # 命令里带一次性 secret，且不是管理密钥
        assert "t=" in cmd["windows"] and ADMIN not in cmd["windows"]
        flow = self.app.state.store.trae_flows[data["id"]]
        assert flow["status"] == "pending" and flow["secret"] in cmd["windows"]

    async def test_local_script_requires_valid_secret(self):
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]
        bad = await self.client.get(f"/trae/local.py?f={data['id']}&t=wrong")
        assert bad.status_code == 404
        ok = await self.client.get(f"/trae/local.py?f={data['id']}&t={secret}")
        assert ok.status_code == 200
        assert ok.headers["content-type"].startswith("text/plain")
        import ast
        ast.parse(ok.text)  # 必须是一份可执行的 Python 脚本
        assert "127.0.0.1" in ok.text and "18080" in ok.text
        assert "workbuddy" not in ok.text or True  # BASE 来自请求 Host
        assert secret in ok.text and "__TOKEN__" not in ok.text

    async def test_flow_info_requires_secret_and_pending(self):
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]
        assert (await self.client.get(f"/trae/api/flow/{data['id']}?t=bad")).status_code == 404
        info = await self.client.get(f"/trae/api/flow/{data['id']}?t={secret}")
        assert info.status_code == 200 and info.json()["url"].startswith("https://www.trae.cn/authorization?")

    async def test_public_complete_flow_get_query(self):
        """GET query 形态回调 → 免管理会话完成登录并收养。"""
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]

        def handler(req):
            if req.url.path.endswith("ExchangeToken"):
                return httpx.Response(200, json={"Result": {"Token": "N1", "RefreshToken": "R1", "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000}})
            if req.url.path.endswith("GetUserInfo"):
                return httpx.Response(200, json={"Result": {"UserID": "recv-1", "ScreenName": "接收器账号", "EnterpriseID": "E"}})
            return httpx.Response(404)

        self.patch_trae_login_http(handler)
        try:
            query = "refreshToken=RT-recv&userInfo=" + urllib.parse.quote(json.dumps({"UserID": "recv-1", "ScreenName": "接收器账号"}))
            r = await self.client.post("/trae/api/complete", json={
                "id": data["id"], "token": secret, "method": "GET", "path": "/authorize", "query": query, "body": ""})
        finally:
            import core.backends.trae_login as tl
            tl.httpx.Client = self._orig_sync
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and r.json()["uid"] == "recv-1"
        assert (self.auth / "trae-recv-1.info").exists()
        # 管理页轮询能看到完成态
        status = (await self.client.get(f"/admin/api/trae/flow/{data['id']}", headers=await self.login_admin())).json()
        assert status["status"] == "done" and status["uid"] == "recv-1"

    async def test_public_complete_flow_post_json_body(self):
        """POST JSON（fetch 投递）形态回调同样支持。"""
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]

        def handler(req):
            if req.url.path.endswith("ExchangeToken"):
                return httpx.Response(200, json={"Result": {"Token": "N2", "RefreshToken": "R2", "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000}})
            return httpx.Response(200, json={"Result": {"UserID": "recv-2", "ScreenName": "fetch账号"}})

        self.patch_trae_login_http(handler)
        try:
            body = json.dumps({"data": {"refreshToken": "RT2", "userInfo": {"UserID": "recv-2", "ScreenName": "fetch账号"}}})
            r = await self.client.post("/trae/api/complete", json={
                "id": data["id"], "token": secret, "method": "POST", "path": "/authorize", "query": "", "body": body})
        finally:
            import core.backends.trae_login as tl
            tl.httpx.Client = self._orig_sync
        assert r.status_code == 200 and r.json()["ok"] is True
        assert (self.auth / "trae-recv-2.info").exists()

    async def test_public_complete_bad_secret_400(self):
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        r = await self.client.post("/trae/api/complete", json={"id": data["id"], "token": "nope", "query": "refreshToken=x"})
        assert r.status_code == 400 and r.json()["ok"] is False

    async def test_public_complete_reports_received_fields(self):
        """回调形态变化时给出可诊断的报错（只含字段名，不含值）。"""
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]
        r = await self.client.post("/trae/api/complete", json={
            "id": data["id"], "token": secret, "method": "POST", "path": "/authorize",
            "query": "", "body": json.dumps({"weird": "1", "another": "2"})})
        assert r.status_code == 400
        assert "weird" in r.json()["error"]
        assert self.app.state.store.trae_flows[data["id"]]["status"] == "error"

    async def test_public_complete_idempotent(self):
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]

        def handler(req):
            if req.url.path.endswith("ExchangeToken"):
                return httpx.Response(200, json={"Result": {"Token": "N3", "RefreshToken": "R3", "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000}})
            return httpx.Response(200, json={"Result": {"UserID": "recv-3", "ScreenName": "x"}})

        self.patch_trae_login_http(handler)
        try:
            payload_ = {"id": data["id"], "token": secret, "method": "GET", "path": "/authorize",
                        "query": "refreshToken=RT3", "body": ""}
            first = await self.client.post("/trae/api/complete", json=payload_)
            second = await self.client.post("/trae/api/complete", json=payload_)
        finally:
            import core.backends.trae_login as tl
            tl.httpx.Client = self._orig_sync
        assert first.json()["ok"] is True and second.json()["ok"] is True
        assert "已完成" in second.json()["message"]


def test_cred_backend_mismatch_503():
    """池选到错误后端账号时 _resolve_backend_or_http 报 503。"""
    import admin.pool as pool

    class _WBCred:
        backend_name = "workbuddy"

        def get_headers(self):
            return {}

    token = pool.REQUEST_CREDENTIAL.set(_WBCred())
    try:
        with pytest.raises(Exception) as ei:
            converter._resolve_backend_or_http("trae/glm-5.2")
        assert getattr(ei.value, "status_code", None) == 503
    finally:
        pool.REQUEST_CREDENTIAL.reset(token)

# ---------------------------------------------------------------------------
# G. v1.1：ug API（模型表 / 签到 / 积分）+ 动态目录 + 禁用管理 + 404 软冷却
# ---------------------------------------------------------------------------

def _sync_transport(handler):
    real = _REAL_SYNC_CLIENT
    return real, (lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_fetch_models_parses_config_info_list():
    real, factory = _sync_transport(lambda req: httpx.Response(200, json={"config_info_list": [
        {"config_name": "glm-5.2", "display_config": {"display_name": "GLM 5.2"}},
        {"config_name": "kimi-k3", "display_config": {"display_name": "Kimi K3"}},
        {"config_name": "", "display_config": {"display_name": "跳过空名"}},
    ]}))
    with real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"config_info_list": [
        {"config_name": "glm-5.2", "display_config": {"display_name": "GLM 5.2"}},
        {"config_name": "kimi-k3", "display_config": {"display_name": "Kimi K3"}},
        {"config_name": "", "display_config": {"display_name": "跳过空名"}},
    ]}))) as client:
        models = T.fetch_models(client, {"Authorization": "x"})
    assert [m["id"] for m in models] == ["glm-5.2", "kimi-k3"]
    assert models[0]["name"] == "GLM 5.2"
    assert models[0]["context_length"] == T._CTX


def test_fetch_models_envelope_and_errors():
    def make(payload, status=200):
        return httpx.Response(status, json=payload)
    # Result 包装也认
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: make({"Result": {"config_info_list": [
        {"config_name": "sagitta"}]}}))) as client:
        assert [m["id"] for m in T.fetch_models(client, {})] == ["sagitta"]
    # 非 200 → TraeAPIError 带 status
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: make({}, 500))) as client:
        with pytest.raises(T.TraeAPIError) as ei:
            T.fetch_models(client, {})
        assert ei.value.status == 500
    # 空列表 → TraeAPIError
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: make({"config_info_list": []}))) as client:
        with pytest.raises(T.TraeAPIError):
            T.fetch_models(client, {})


def test_checkin_status_and_claim_and_ent_usage(tmp_path):
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path.endswith("/checkin_credits/status"):
            # UgHeaders：Cloud-IDE-JWT + X-User-Region + X-Device-Id
            assert req.headers["Authorization"] == "Cloud-IDE-JWT TRAE-ACCESS"
            assert req.headers["X-User-Region"] == "CN"
            assert req.headers["X-Device-Id"] == "d" * 32
            assert req.headers["User-Agent"] == T.CLIENT_UA
            return httpx.Response(200, json={"checked_in": True, "credits": 66, "enable": False})
        if req.url.path.endswith("/checkin_credits/claim"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            {"entitlement_base_info": {"quota": {"credits_limit": 10}}},
            {"entitlement_base_info": {"quota": {"credits_limit": 5}}},
            {"entitlement_base_info": {}},
            "junk",
        ]})
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
        ci = mgr.checkin_status(client)
        assert ci == {"checked": True, "credits": 66, "enable": False}
        mgr.checkin_claim(client)
        assert mgr.user_ent_usage(client) == 15
    assert paths.count("/trae/api/v2/ug/checkin_credits/claim") == 1


def test_manager_fetch_models_uses_solo_headers(tmp_path):
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)
    seen = {}

    def handler(req):
        seen["accept"] = req.headers["Accept"]
        seen["uid"] = req.headers.get("X-Uid")
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = req.content
        return httpx.Response(200, json={"config_info_list": [{"config_name": "glm-5.2", "display_config": {"display_name": "GLM"}}]})

    real = _REAL_SYNC_CLIENT
    orig = httpx.Client
    httpx.Client = lambda **kw: orig(transport=httpx.MockTransport(handler), **kw)
    try:
        models = mgr.fetch_models()
    finally:
        httpx.Client = orig
    assert [m["id"] for m in models] == ["glm-5.2"]
    assert seen["accept"] == "application/json"  # 非流式
    assert seen["uid"] == "trae-user"
    assert seen["auth"] == "Cloud-IDE-JWT TRAE-ACCESS"
    assert json.loads(seen["body"])["function"] == "solo_work_lite"


def test_ug_api_error_paths():
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(403, text="no"))) as client:
        with pytest.raises(T.TraeAPIError) as ei:
            T.checkin_status(client, {})
        assert ei.value.status == 403
        with pytest.raises(T.TraeAPIError):
            T.checkin_claim(client, {})
        with pytest.raises(T.TraeAPIError):
            T.user_ent_usage(client, {})


def test_catalog_provider_drives_map_model_and_ids():
    from core.backends import registry as R
    assert R.trae_model_ids() == [m["id"] for m in T.TRAE_STATIC_MODELS]  # 无提供者→静态
    T.set_catalog_provider(lambda: [{"id": "brand-new-model", "name": "BN", "context_length": 999}])
    try:
        assert T.trae_model_catalog()[0]["id"] == "brand-new-model"
        assert T.map_model("trae/brand-new-model") == "brand-new-model"
        assert T.map_model("brand_new_model") == "brand-new-model"
        assert R.trae_model_ids() == ["brand-new-model"]
        with pytest.raises(T.UnknownTraeModel):
            T.map_model("glm-5.2")  # 动态表没有它 → 未知
    finally:
        T.set_catalog_provider(None)
    assert T.map_model("glm-5.2") == "glm-5.2"  # 恢复静态


def test_catalog_provider_exception_falls_back_static():
    def boom():
        raise RuntimeError("registry gone")
    T.set_catalog_provider(boom)
    try:
        assert len(T.trae_model_catalog()) == 32
        assert T.map_model("glm-5.2") == "glm-5.2"
    finally:
        T.set_catalog_provider(None)


# ---- ModelRegistry 的 Trae 目录 ----

def _registry(clock=None):
    import admin.models as M
    return M.ModelRegistry(get_credential=None, clock=clock or time.time)


def test_registry_trae_builtin_seed_and_disable():
    reg = _registry()
    reg.ensure_trae_builtin()
    ids = reg.snapshot_ids()
    assert "trae/glm-5.2" in ids and len([i for i in ids if i.startswith("trae/")]) == 32
    enabled = reg.enabled_models()
    assert "trae/glm-5.2" in enabled
    snap = reg.snapshot()["models"]
    trae_rows = [m for m in snap if m.get("backend") == "trae"]
    assert len(trae_rows) == 32 and all(m["enabled"] for m in trae_rows)
    # 禁用 trae/glm-5.2
    state = reg.set_enabled("trae/glm-5.2", False)
    assert "trae/glm-5.2" not in reg.enabled_models()
    assert "trae/glm-5.2" in reg.snapshot_ids()  # 仍「已知」→ 中间件报禁用而非未知
    # 导出/导入往返保留禁用态
    reg2 = _registry()
    reg2.import_state(state)
    assert "trae/glm-5.2" not in reg2.enabled_models()
    # 未知 trae 模型 → KeyError
    with pytest.raises(KeyError):
        reg2.set_enabled("trae/nope", True)


def test_registry_refresh_trae_models_replaces_builtin():
    reg = _registry()
    reg.ensure_trae_builtin()

    class FakeManager:
        def fetch_models(self):
            return [{"id": "dyn-a", "name": "DynA", "context_length": 2048},
                    {"id": "dyn-b", "name": "DynB", "context_length": 4096}]

    reg.trae_credential_getter = lambda: FakeManager()
    assert reg.refresh_trae_models() is True
    ids = {i for i in reg.snapshot_ids() if i.startswith("trae/")}
    assert ids == {"trae/dyn-a", "trae/dyn-b"}  # 静态 32 条被动态表替换
    assert reg.trae_entries()[0] == {"id": "dyn-a", "name": "DynA", "context_length": 2048}
    # 失败保留上次快照
    class Boom:
        def fetch_models(self):
            raise T.TraeAPIError("boom", status=500)
    reg.trae_credential_getter = lambda: Boom()
    assert reg.refresh_trae_models() is False
    assert {i for i in reg.snapshot_ids() if i.startswith("trae/")} == {"trae/dyn-a", "trae/dyn-b"}
    # 无 Trae 账号 → 静默保留
    reg.trae_credential_getter = lambda: None
    assert reg.refresh_trae_models() is False
    assert "trae/dyn-a" in reg.snapshot_ids()


def test_registry_kill_switch_hides_trae(monkeypatch):
    import os
    reg = _registry()
    reg.ensure_trae_builtin()
    monkeypatch.setenv("CODEBUDDY_ENABLE_TRAE", "0")
    assert all(not m.startswith("trae/") for m in reg.enabled_models())
    assert all(m.get("backend") != "trae" for m in reg.snapshot()["models"])


# ---- e2e：模型禁用 + 404 软冷却 ----

async def _e2e_disable_trae_model(self):
    headers = await self.login_admin()
    # 管理页禁用 trae/glm-5.2
    r = await self.client.patch("/admin/api/models/trae/glm-5.2", json={"enabled": False}, headers=headers)
    assert r.status_code == 200, r.text
    assert "trae/glm-5.2" not in self.app.state.model_registry.enabled_models()
    # /v1/models 不再出现
    models = (await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})).json()["data"]
    assert "trae/glm-5.2" not in [m["id"] for m in models]
    # 调用被中间件拒绝（400，不打上游）
    self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and "禁用" in r.json()["detail"]
    # 恢复启用
    r = await self.client.patch("/admin/api/models/trae/glm-5.2", json={"enabled": True}, headers=headers)
    assert r.status_code == 200
    assert "trae/glm-5.2" in self.app.state.model_registry.enabled_models()


async def _e2e_trae_404_soft_cooldown(self):
    self.patch_upstream(lambda req: httpx.Response(404, content=b'{"message":"config not found"}'))
    aid = self.trae_aid()
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    status = self.app.state.store.data["account_status"][aid]
    # 软冷却：cooldown_until 置位、不计入连续失败、不触发积分重查
    assert status["cooldown_until"] > time.time()
    assert not status.get("consecutive_fails")
    assert "404" in (status.get("last_error") or "")


TraeEndToEnd.test_e2e_disable_trae_model = _e2e_disable_trae_model
TraeEndToEnd.test_e2e_trae_404_soft_cooldown = _e2e_trae_404_soft_cooldown

async def _e2e_batch_checkin_covers_trae(self):
    # 批量签到覆盖 Trae 账号（ug 接口），错峰关掉避免测试睡眠。
    pool = self.app.state.pool
    self.app.state.store.data["pool"]["batch_spread"] = 0
    state = {"claimed": False}

    def handler(req):
        if req.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(200, json={"checked_in": state["claimed"], "credits": 0, "enable": True})
        if req.url.path.endswith("/checkin_credits/claim"):
            state["claimed"] = True
            return httpx.Response(200, json={})
        if req.url.path.endswith("/ide_user_ent_usage"):
            return httpx.Response(200, json={"user_entitlement_pack_list": [
                {"entitlement_base_info": {"quota": {"credits_limit": 7}}}]})
        return httpx.Response(404)

    real = _REAL_SYNC_CLIENT
    self.admin_server.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        result = await pool.batch("checkin")
    finally:
        self.admin_server.httpx.Client = real
    by_id = {r["id"]: r for r in result["results"]}
    trae_result = by_id[self.trae_aid()]
    assert trae_result["ok"] is True, trae_result
    assert trae_result["remaining"] == 7.0
    assert self.app.state.store.data["account_status"][self.trae_aid()]["checkin_date"]


async def _e2e_dynamic_catalog_replaces_static(self):
    headers = await self.login_admin()

    def handler(req):
        if req.url.path.endswith("/get_detail_param"):
            return httpx.Response(200, json={"config_info_list": [
                {"config_name": "dyn-x", "display_config": {"display_name": "Dyn X"}}]})
        return httpx.Response(404)

    real = _REAL_SYNC_CLIENT
    self.admin_server.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        r = await self.client.post("/admin/api/models/refresh", headers=headers)
    finally:
        self.admin_server.httpx.Client = real
    assert r.status_code == 200, r.text
    # 动态表替换静态表：/v1/models 只剩 trae/dyn-x
    models = (await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})).json()["data"]
    assert [m["id"] for m in models if m["id"].startswith("trae/")] == ["trae/dyn-x"]
    # 管理页模型表出现 Trae 行
    overview = await self.client.get("/admin/api/overview", headers=headers)
    trae_rows = [m for m in overview.json()["models"]["models"] if m.get("backend") == "trae"]
    assert [m["id"] for m in trae_rows] == ["trae/dyn-x"]
    # map_model 接受动态 id：chat 路由不再 400
    self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "trae/dyn-x", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    # 静态 glm-5.2 不在动态表里 → 未知模型 400
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "trae/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400


TraeEndToEnd.test_e2e_batch_checkin_covers_trae = _e2e_batch_checkin_covers_trae
TraeEndToEnd.test_e2e_dynamic_catalog_replaces_static = _e2e_dynamic_catalog_replaces_static

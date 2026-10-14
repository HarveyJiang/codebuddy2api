"""TraeBackend 全量测试：请求体改写 / SSE 归一化 / 错误分类 / 凭据刷新 / 端到端路由。

上游用 httpx.MockTransport 模拟；e2e 用 create_app + ASGITransport，
MockTransport 按 host 分派（trae-api-cn.mchost.guru = Trae，copilot.tencent.com = WorkBuddy）。
"""
import asyncio
import json
import tempfile
import time
import unittest
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

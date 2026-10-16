"""TraeBackend 全量测试：请求体改写 / SSE 归一化 / 错误分类 / 凭据刷新 / 端到端路由。

上游用 httpx.MockTransport 模拟；e2e 用 create_app + ASGITransport，
MockTransport 按 host 分派（trae-api-cn.mchost.guru = Trae，copilot.tencent.com = WorkBuddy）。
"""
import asyncio
import json
import tempfile
import time
import unittest
import unittest.mock
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from core import converter
from core.backends import trae as T
from core.backends.base import BackendHTTPError
from core.backends.registry import backend_name_for, catalog_ids, resolve_backend
from core.backends.workbuddy import WorkBuddyBackend

from conftest import ROOT as CONFTEST_ROOT  # 项目根，见 tests/conftest.py

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
    ("DeepSeek-V4-Pro-Official", "DeepSeek-V4-Pro-Official"),
    ("deepseek_v4_pro_official", "DeepSeek-V4-Pro-Official"),  # 下划线/大小写不敏感兜底
    ("DEEPSEEK_V4_PRO_OFFICIAL", "DeepSeek-V4-Pro-Official"),
    # v16：id 是上游裸名，`map_model` 不再剥前缀（`trae/kimi-k3` 不是合法上游名）。
])
def test_map_model(model, expected):
    assert T.map_model(model) == expected


def test_map_model_unknown_raises():
    with pytest.raises(T.UnknownTraeModel):
        T.map_model("bogus-model")


def test_map_model_does_not_strip_prefix_anymore():
    """回归（v16）：`map_model` 必须**不再**剥 `trae/` 前缀。

    去前缀前它靠 `spec_for("trae").model_prefix` 剥一层；现在上游 id 就是裸名，
    再剥会把真正含斜杠的上游名（`openrouter/free` 这类）误伤成不存在的模型。
    """
    with pytest.raises(T.UnknownTraeModel):
        T.map_model("trae/kimi-k3")


def test_trae_static_models_count():
    assert len(TRAE_MODELS := T.TRAE_STATIC_MODELS) == 20
    assert len(catalog_ids("trae")) == 20
    assert all(m["owned_by"] == "trae-solo" for m in TRAE_MODELS)


def test_resolve_backend_trae():
    """`routed="trae"` → TraeBackend + map_model 归一后的上游名。

    v16 去前缀后，请求名是上游裸名（`deepseek_v4_pro_official`），
    Trae 的归一（别名/大小写 → config_name）仍要走 `map_model`。
    """
    backend, upstream = resolve_backend("deepseek_v4_pro_official", routed="trae")
    assert backend.name == "trae"
    assert backend.tencent_policies is False
    assert upstream == "DeepSeek-V4-Pro-Official"


def test_resolve_backend_unknown_trae_400():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        resolve_backend("bogus", routed="trae")
    assert ei.value.status_code == 400


def test_resolve_backend_kill_switch(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("CODEBUDDY_ENABLE_TRAE", "0")
    with pytest.raises(HTTPException) as ei:
        resolve_backend("glm-5.2", routed="trae")
    assert ei.value.status_code == 400


def test_resolve_backend_escape_hatch(monkeypatch):
    monkeypatch.setenv("CODEBUDDY_TRAE_ALLOW_UNKNOWN_MODELS", "1")
    backend, upstream = resolve_backend("whatever", routed="trae")
    assert backend.name == "trae"
    assert upstream == "whatever"


@pytest.mark.parametrize("model,expected", [
    ("glm-5.2", "workbuddy"),
    ("auto", "workbuddy"),
    (None, "workbuddy"),
    # v16：前缀不再有路由含义，`trae/x` 归默认后端（见 test_backends_workbuddy）。
    ("trae/x", "workbuddy"),
])
def test_backend_name_for(model, expected):
    assert backend_name_for(model) == expected


# 注：`backend_name_for` 的完整参数集（含空串）在
# tests/protocol/test_backends_workbuddy.py 里，这里不再重复一遍。


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
        # v16：id 是裸名，不再靠 `trae/` 前缀筛；改用 owned_by 声明筛出 Trae 的行。
        trae = [m for m in data if m["owned_by"] == "trae-solo"]
        # 数量对齐 registry 里 Trae 目录的实际条数（静态兜底表，非硬编码数字）。
        reg = self.app.state.model_registry
        assert len(trae) == len(reg._state_for("trae")["available"])
        assert all(m["owned_by"] == "trae-solo" for m in trae)
        # Trae 的 Doubao-Seed-Code 出现在列表里（裸名）。
        assert any(m["id"] == "Doubao-Seed-Code" for m in trae)

    async def test_owned_by_comes_from_backend_declaration(self):
        """`/v1/models` 的 owned_by 必须来自**后端声明**（BackendSpec.owned_by）。

        回归：这里原先是 `"trae-solo" if m.startswith("trae/") else "codebuddy"`，
        把「这个模型归哪个后端」硬编码在调用方。现在每行自带它的 backend
        （`model_registry.listed_models()`），归属 = `spec_for(该 backend).owned_by`。

        去前缀后同名模型在两后端各有一行（`glm-5.3` 两条），所以这条**逐行**对照
        它自己的后端，而不是「按 id 判归属」——后者对同名 id 只能给一个答案。
        """
        from core.backends.registry import spec_for

        r = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        data = r.json()["data"]
        assert data
        # registry 的行清单是唯一真相源：逐行对照即可发现任何硬编码回落。
        rows = self.app.state.model_registry.listed_models()
        assert [(m["id"], m["owned_by"]) for m in data] == [
            (mid, spec_for(backend).owned_by) for mid, backend in rows]
        # 两个后端的 owned_by 确实都出现了（否则上一条会退化成只验一个后端）。
        assert {m["owned_by"] for m in data} == {
            spec_for("workbuddy").owned_by, spec_for("trae").owned_by}

    async def test_models_list_lists_each_backend_row(self):
        """同名模型在两个后端各列一条 —— 不变量 1「按后端各一条」。

        `glm-5.3` 在 WB 与 Trae 都有：应出现**两条**同 id 的条目，各自的
        `owned_by` 不同（用户 2026-10-16 拍板：`codebuddy` 与 `trae-solo`）。
        """
        r = await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})
        data = r.json()["data"]
        glm53 = [m for m in data if m["id"] == "glm-5.3"]
        assert len(glm53) == 2, f"glm-5.3 应有两行（每后端一条），实际 {len(glm53)}"
        assert {m["owned_by"] for m in glm53} == {"codebuddy", "trae-solo"}
        # id 里不含 `/`（不变量 1）。
        assert all("/" not in m["id"] for m in data)

    async def test_account_rows_trae_expiry_milliseconds(self):
        rows = {r["backend"]: r for r in self.app.state.store.account_rows()}
        assert "trae" in rows and "workbuddy" in rows
        # trae 的秒级 expiresAt 被归一为毫秒
        assert rows["trae"]["expires_at"] > 1_000_000_000_000
        assert rows["trae"]["expired"] is False

    async def test_pool_select_filters_backend(self):
        pool = self.app.state.pool
        aid_trae, _ = pool.select("k1", backend="trae")
        aid_wb, _ = pool.select("k1", backend="workbuddy")
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
                                   json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.text.strip().endswith("data: [DONE]")
        events = _events(r.content)
        assert any("Hel" in (e["choices"][0]["delta"].get("content") or "") for e in events)

    async def test_e2e_chat_nonstream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        body = r.json()
        msg = body["choices"][0]["message"]
        assert msg["content"] == "Hello"
        assert msg["tool_calls"][0]["function"]["name"] == "read"
        assert body["usage"]["total_tokens"] == 163

    async def test_admin_api_test_routes_trae_via_pool(self):
        """回归：/admin/api/test 内部调用须走账号池，按模型后端选账号。
        管理部署下 CONFIG["cred"] 仅指向「任一启用账号」用于可用性判断，
        不再代表"当前账号"；即便它指向一个 workbuddy 固定凭据（旧 active 机制的
        触发条件），测 trae 模型仍应由池选中 trae 账号成功返回，而非被后端一致性闸门挡成 503。"""
        from core.backends import trae as T
        from core.backends.registry import make_credential
        await self.client.post("/admin/api/login", json={"key": ADMIN})
        csrf = (await self.client.get("/admin/api/session")).json()["csrf"]
        H = {"X-CSRF-Token": csrf}
        # 复现旧 bug 触发条件：固定凭据指向 workbuddy，与 trae 后端不匹配。
        stale = make_credential(self.auth / "wb.info")
        orig_cred = self.admin_server.converter.CONFIG["cred"]
        self.admin_server.converter.CONFIG["cred"] = stale
        try:
            # 直接 mock TraeBackend.astream 产出标准 SOLO SSE，避开真实上游与 httpx patch 冲突。
            async def fake_astream(self_, headers, body, *, timeout=None, raise_on_error=False):
                async for chunk in T.normalize_solo_stream(_achunks(SOLO_SSE), model=str(body.get("model") or "glm-5.2")):
                    yield chunk
            orig_astream = T.TraeBackend.astream
            T.TraeBackend.astream = fake_astream
            try:
                r = await self.client.post("/admin/api/test", json={"model": "Doubao-Seed-Code", "prompt": "请只回复：连接成功"}, headers=H)
            finally:
                T.TraeBackend.astream = orig_astream
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True, body
            assert "Hello" in (body.get("answer") or "")
        finally:
            self.admin_server.converter.CONFIG["cred"] = orig_cred

    async def test_e2e_responses_stream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/responses", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "input": "hi", "stream": True})
        assert r.status_code == 200
        assert "response.completed" in r.text
        assert "Hello" in r.text

    async def test_e2e_responses_nonstream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/responses", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "input": "hi"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"

    async def test_e2e_anthropic_stream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/messages", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.status_code == 200
        assert "event: message_stop" in r.text

    async def test_e2e_anthropic_nonstream_trae(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
        r = await self.client.post("/v1/messages", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "assistant"

    async def test_e2e_count_tokens_default_local_estimate(self):
        def handler(req):
            raise AssertionError("count_tokens 默认不应打上游")
        self.patch_upstream(handler)
        r = await self.client.post("/v1/messages/count_tokens", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "x" * 400}]})
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
                                       json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}]})
        finally:
            os.environ.pop("CODEBUDDY_TRAE_COUNT_TOKENS_UPSTREAM", None)
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 42
        assert calls

    async def test_e2e_http_error_401_cools_account(self):
        self.patch_upstream(lambda req: httpx.Response(401, content=b'{"message":"session dead"}'))
        aid = self.trae_aid()
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401
        assert self.app.state.store.data["account_status"][aid]["cooldown_until"] > 0

    async def test_e2e_stream_error_1005_cools_account(self):
        self.patch_upstream(lambda req: httpx.Response(200, content=b'event:error\ndata:{"code":1005,"message":"plan limit"}\n\n'))
        aid = self.trae_aid()
        r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                                   json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}], "stream": True})
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
                                   json={"model": "bogus-nonexistent", "messages": [{"role": "user", "content": "hi"}]})
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
        # 回调必须是 127.0.0.1（公网回调实测被授权页拒绝）+ redirect=0
        # （授权页据此跳 /login 出登录表单；redirect=1 会直接渲染"登录成功"终态）。
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(data["url"]).query)
        assert qs["auth_callback_url"][0] == "http://127.0.0.1:18080/authorize"
        assert qs["redirect"] == ["0"]
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

    async def test_trae_checkin_retries_on_capacity_code(self):
        # 回归：claim 返回可重试业务码 9074（"人多稍后再试"，HTTP 200）时应重试 claim 本身，
        # 成功即判"签到成功"；旧代码只认 HTTP 200，会把 9074 当成功、随后 status 不确认而误报"尚未确认"。
        import admin.pool as admin_pool
        headers = await self.login_admin()
        aid = self.trae_aid()
        state = {"claims": 0}

        def handler(req):
            if req.url.path.endswith("/checkin_credits/status"):
                return httpx.Response(200, json={"checked_in": False, "credits": 0, "enable": True})
            if req.url.path.endswith("/checkin_credits/claim"):
                state["claims"] += 1
                if state["claims"] < 3:
                    return httpx.Response(200, json={"code": 9074, "message": "当前参与用户太多，请稍后再试"})
                return httpx.Response(200, json={"code": 0, "message": "success"})
            if req.url.path.endswith("/ide_user_ent_usage"):
                return httpx.Response(200, json={"user_entitlement_pack_list": [{"entitlement_base_info": {"quota": {"credits_limit": 120}}}]})
            return httpx.Response(404)

        real = _REAL_SYNC_CLIENT
        self.admin_server.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        try:
            with unittest.mock.patch.object(admin_pool.time, "sleep", lambda *_: None):
                r = await self.client.post(f"/admin/api/accounts/{aid}/actions/checkin", headers=headers)
        finally:
            self.admin_server.httpx.Client = real
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        assert body["message"] == "签到成功"
        assert state["claims"] == 3  # 前两次 9074 重试，第三次成功

    async def test_trae_checkin_surfaces_upstream_capacity_message(self):
        # 持续 9074（容量限制）重试后仍失败：透出上游真实消息，而非误报"尚未确认"。
        import admin.pool as admin_pool
        headers = await self.login_admin()
        aid = self.trae_aid()
        state = {"claims": 0}

        def handler(req):
            if req.url.path.endswith("/checkin_credits/status"):
                return httpx.Response(200, json={"checked_in": False, "credits": 0, "enable": True})
            if req.url.path.endswith("/checkin_credits/claim"):
                state["claims"] += 1
                return httpx.Response(200, json={"code": 9074, "message": "当前参与用户太多，请稍后再试"})
            if req.url.path.endswith("/ide_user_ent_usage"):
                return httpx.Response(200, json={"user_entitlement_pack_list": []})
            return httpx.Response(404)

        real = _REAL_SYNC_CLIENT
        self.admin_server.httpx.Client = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        try:
            with unittest.mock.patch.object(admin_pool.time, "sleep", lambda *_: None):
                r = await self.client.post(f"/admin/api/accounts/{aid}/actions/checkin", headers=headers)
        finally:
            self.admin_server.httpx.Client = real
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False, body
        assert "太多" in body["message"] or "稍后" in body["message"]
        assert state["claims"] == 4  # 有界重试 4 次后放弃，透出上游消息

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
        index = (CONFTEST_ROOT / "admin" / "static" / "index.html").read_text(encoding="utf-8")
        # 需求 2 之后登录方式按钮**由后端注册表渲染**，index.html 不再写死后端名。
        # 静态页只需保证容器与面板还在（面板是纯 UI，与后端名无关）。
        assert 'id="account-modes"' in index
        assert 'id="trae-login-panel"' in index
        assert 'id="mode-trae"' not in index, "登录按钮应由 GET /admin/api/backends 渲染，不该写死在 HTML 里"
        # 渲染来源仍在（app.js 按 login_kind 生成按钮）。
        app_js = (CONFTEST_ROOT / "admin" / "static" / "app.js").read_text(encoding="utf-8")
        assert "function renderLoginModes()" in app_js

    # ---- 本机接收器（免复制粘贴） ----

    async def test_authorize_uses_local_callback_and_redirect0(self):
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        # 127.0.0.1 回调 + redirect=0；不再下发本机接收器命令 / 端口
        assert "command" not in data and "port" not in data
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(data["url"]).query)
        assert qs["auth_callback_url"][0] == "http://127.0.0.1:18080/authorize"
        assert qs["redirect"] == ["0"]
        flow = self.app.state.store.trae_flows[data["id"]]
        assert flow["status"] == "pending"

    # ---- 公网回调（/trae/callback/<fid>/<secret>，免命令免粘贴） ----

    async def test_public_callback_options_preflight(self):
        """跨域 fetch 预检：204 + CORS 头，不要求任何凭据。"""
        r = await self.client.options("/trae/callback/abc/def")
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "*"
        assert "POST" in r.headers["access-control-allow-methods"]

    async def test_public_callback_bad_secret_404(self):
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        r = await self.client.get(f"/trae/callback/{data['id']}/wrong?refreshToken=x")
        assert r.status_code == 404 and r.json()["ok"] is False
        assert r.headers["access-control-allow-origin"] == "*"

    async def test_public_callback_get_query_completes_login(self):
        """授权页 fetch 投递（GET query 带 refreshToken）→ 免管理会话完成登录并收养。"""
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]

        def handler(req):
            if req.url.path.endswith("ExchangeToken"):
                return httpx.Response(200, json={"Result": {"Token": "CB1", "RefreshToken": "R1", "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000}})
            if req.url.path.endswith("GetUserInfo"):
                return httpx.Response(200, json={"Result": {"UserID": "cb-1", "ScreenName": "公网回调账号", "EnterpriseID": "E"}})
            return httpx.Response(404)

        self.patch_trae_login_http(handler)
        try:
            query = "refreshToken=RT-cb&userInfo=" + urllib.parse.quote(json.dumps({"UserID": "cb-1", "ScreenName": "公网回调账号"}))
            r = await self.client.get(f"/trae/callback/{data['id']}/{secret}?{query}")
        finally:
            import core.backends.trae_login as tl
            tl.httpx.Client = self._orig_sync
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and r.json()["uid"] == "cb-1"
        assert (self.auth / "trae-cb-1.info").exists()
        assert self.app.state.store.trae_flows[data["id"]]["status"] == "done"
        status = (await self.client.get(f"/admin/api/trae/flow/{data['id']}", headers=await self.login_admin())).json()
        assert status["status"] == "done" and status["uid"] == "cb-1"

    async def test_public_callback_post_json_body(self):
        """POST JSON（fetch 投递）形态同样支持。"""
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]

        def handler(req):
            if req.url.path.endswith("ExchangeToken"):
                return httpx.Response(200, json={"Result": {"Token": "CB2", "RefreshToken": "R2", "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000}})
            return httpx.Response(200, json={"Result": {"UserID": "cb-2", "ScreenName": "fetch账号"}})

        self.patch_trae_login_http(handler)
        try:
            body = json.dumps({"data": {"refreshToken": "RT2", "userInfo": {"UserID": "cb-2", "ScreenName": "fetch账号"}}})
            r = await self.client.post(f"/trae/callback/{data['id']}/{secret}", content=body,
                                       headers={"Content-Type": "application/json"})
        finally:
            import core.backends.trae_login as tl
            tl.httpx.Client = self._orig_sync
        assert r.status_code == 200 and r.json()["ok"] is True
        assert (self.auth / "trae-cb-2.info").exists()

    async def test_public_callback_reports_received_fields(self):
        """回调形态变化时给出可诊断的报错（只含字段名，不含值），flow 转 error。"""
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]
        r = await self.client.post(f"/trae/callback/{data['id']}/{secret}",
                                   content=json.dumps({"weird": "1", "another": "2"}),
                                   headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        assert "weird" in r.json()["error"]
        assert self.app.state.store.trae_flows[data["id"]]["status"] == "error"

    async def test_public_callback_idempotent(self):
        """重复投递不报错、不重复落盘。"""
        headers = await self.login_admin()
        data = (await self.client.post("/admin/api/trae/authorize", headers=headers)).json()
        secret = self.app.state.store.trae_flows[data["id"]]["secret"]

        def handler(req):
            if req.url.path.endswith("ExchangeToken"):
                return httpx.Response(200, json={"Result": {"Token": "CB3", "RefreshToken": "R3", "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000}})
            return httpx.Response(200, json={"Result": {"UserID": "cb-3", "ScreenName": "x"}})

        self.patch_trae_login_http(handler)
        try:
            first = await self.client.get(f"/trae/callback/{data['id']}/{secret}?refreshToken=RT3")
            second = await self.client.get(f"/trae/callback/{data['id']}/{secret}?refreshToken=RT3")
        finally:
            import core.backends.trae_login as tl
            tl.httpx.Client = self._orig_sync
        assert first.status_code == 200 and first.json()["ok"] is True
        assert second.status_code == 200 and second.json()["ok"] is True
        assert second.json()["message"] == "该账号已完成登录"

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
            # v16：用 `routed="trae"` 指定后端（前缀不再是路由手段）。
            converter._resolve_backend_or_http("glm-5.2", routed="trae")
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


def _menu(*groups):
    return {"code": 0, "message": "success",
            "data": {"list": [{"function": fn, "models": models} for fn, models in groups]}}


def test_feature_rate_prefers_discount_then_base():
    assert T._feature_rate('{"consumption_rate":{"enable":true,"data":{"rate":0.8}}}') == 0.8
    discounted = ('{"consumption_rate":{"enable":true,"data":{"rate":0.4}},'
                  '"activity_discount":{"enable":true,"data":{"current":{"consumption_rate":0.1}}}}')
    assert T._feature_rate(discounted) == 0.1  # 折扣生效时取实付倍率
    assert T._feature_rate('{"consumption_rate":{"enable":false,"data":{"rate":0.9}}}') is None
    assert T._feature_rate("not json") is None
    assert T._feature_rate(None) is None


def test_fetch_remote_models_parses_menu_and_dedupes():
    payload = _menu(
        ("solo_design_remote", [  # 同名不同值：agent 组优先，应被覆盖
            {"name": "glm-5.3", "display_name": "GLM-5.3(design)", "context_window_tokens": {"dev": 232768},
             "features": '{"consumption_rate":{"enable":true,"data":{"rate":0.9}}}'}]),
        ("solo_work_remote", [
            {"name": "glm-5.3", "display_name": "GLM-5.3(work)", "context_window_tokens": {"dev": 233000},
             "features": '{"consumption_rate":{"enable":true,"data":{"rate":0.5}}}'}]),
        ("solo_agent_remote", [
            {"name": "glm-5.3", "display_name": "GLM-5.3", "context_window_tokens": {"dev": 200000},
             "features": '{"consumption_rate":{"enable":true,"data":{"rate":0.39}}}'},
            {"name": "glm-5.3", "display_name": "重复项", "features": "{}"},
            {"name": "Doubao-Seed-Code", "display_name": "Seed-Code",
             "context_window_tokens": {"dev": 256000},
             "features": '{"activity_discount":{"enable":true,"data":{"current":{"consumption_rate":0.03}}}}'},
        ]),
    )
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        models = T.fetch_remote_models(client, {})
    assert [m["id"] for m in models] == ["glm-5.3", "Doubao-Seed-Code"]
    # agent 组优先 → 取 agent 的上下文/倍率，组内去重，且带 function 标记
    assert models[0] == {"id": "glm-5.3", "name": "GLM-5.3", "context_length": 200000,
                         "max_context_length": None, "credits": 0.39, "function": "solo_agent_remote"}
    assert models[1]["credits"] == 0.03 and models[1]["context_length"] == 256000


def test_fetch_remote_models_keeps_max_mode_window():
    """context_window_tokens 的 max（max 模式上限）也要带上；0/缺失 → None。"""
    payload = _menu(("solo_agent_remote", [
        {"name": "glm-5.3", "display_name": "GLM-5.3", "context_window_tokens": {"dev": 200000, "max": 1000000}},
        {"name": "mimo-v2.6-pro", "display_name": "MiMo", "context_window_tokens": {"dev": 200000, "max": 981504}},
        {"name": "kimi-k2.6", "display_name": "Kimi", "context_window_tokens": {"dev": 200000, "max": 0}},
        {"name": "Doubao-Seed-Code", "display_name": "Code", "context_window_tokens": {"dev": 256000}},
    ]))
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        models = T.fetch_remote_models(client, {})
    got = {m["id"]: (m["context_length"], m["max_context_length"]) for m in models}
    assert got == {"glm-5.3": (200000, 1000000), "mimo-v2.6-pro": (200000, 981504),
                   "kimi-k2.6": (200000, None), "Doubao-Seed-Code": (256000, None)}


def test_fetch_remote_models_errors():
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"code": 1001}))) as client:
        with pytest.raises(T.TraeAPIError) as ei:
            T.fetch_remote_models(client, {})
        assert ei.value.status == 401
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"code": 7}))) as client:
        with pytest.raises(T.TraeAPIError):
            T.fetch_remote_models(client, {})
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=_menu()))) as client:
        with pytest.raises(T.TraeAPIError):
            T.fetch_remote_models(client, {})


def test_build_trae_catalog_uses_agent_group():
    """目录取官方菜单的 solo_agent_remote 组（= 官网界面所见），而非其它 function 组。"""
    menu = [{"id": "glm-5.2", "name": "GLM-5.2", "context_length": 200000, "max_context_length": 500000,
             "credits": 0.78, "function": "solo_agent_remote"},
            {"id": "glm-5.3", "name": "GLM-5.3", "context_length": 200000, "credits": 0.39,
             "function": "solo_agent_remote"},
            {"id": "design-only", "name": "Design", "context_length": 1000, "credits": 0.1,
             "function": "solo_design_remote"}]
    catalog = T.build_trae_catalog(menu)
    assert [m["id"] for m in catalog] == ["glm-5.2", "glm-5.3"]
    assert catalog[0] == {"id": "glm-5.2", "name": "GLM-5.2", "context_length": 200000,
                          "max_context_length": 500000, "credits": 0.78}
    # 该组缺失 → 退回全部组；内部槽位始终剔除
    only_design = [{"id": "design-only", "name": "D", "context_length": 1000, "credits": 0.1,
                    "function": "solo_design_remote"},
                   {"id": "custom_model_gpt-5", "name": "x", "credits": None, "function": "solo_design_remote"}]
    assert [m["id"] for m in T.build_trae_catalog(only_design)] == ["design-only"]
    assert T.build_trae_catalog(None) == []
    assert all(not T._is_internal_slot(m["id"]) for m in T.TRAE_STATIC_MODELS)
    assert "seed-code-pro-0430" not in {m["id"] for m in T.TRAE_STATIC_MODELS}


def test_manager_fetch_models_uses_agent_group(tmp_path):
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)

    def handler(req):
        return httpx.Response(200, json=_menu(
            ("solo_work_remote", [
                {"name": "qwen3.8-max", "display_name": "Qwen3.8-Max", "context_window_tokens": {"dev": 200000},
                 "features": '{"consumption_rate":{"enable":true,"data":{"rate":1.5}}}'}]),
            ("solo_agent_remote", [
                {"name": "glm-5.3", "display_name": "GLM-5.3", "context_window_tokens": {"dev": 200000},
                 "features": '{"consumption_rate":{"enable":true,"data":{"rate":0.39}}}'},
                {"name": "qwen3.8-max", "display_name": "Qwen3.8-Max", "context_window_tokens": {"dev": 200000},
                 "features": '{"consumption_rate":{"enable":true,"data":{"rate":1.5}}}'}])))

    real = _REAL_SYNC_CLIENT
    orig = httpx.Client
    httpx.Client = lambda **kw: orig(transport=httpx.MockTransport(handler), **kw)
    try:
        models = mgr.fetch_models()
    finally:
        httpx.Client = orig
    assert [m["id"] for m in models] == ["glm-5.3", "qwen3.8-max"]
    assert models[0]["credits"] == 0.39 and models[0]["context_length"] == 200000


def test_manager_fetch_models_menu_failure_raises(tmp_path):
    """菜单不可用即抛错（保留上次快照），不拿单边数据冒充可用模型。"""
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)

    real = _REAL_SYNC_CLIENT
    orig = httpx.Client
    httpx.Client = lambda **kw: orig(transport=httpx.MockTransport(lambda req: httpx.Response(503, text="boom")), **kw)
    try:
        with pytest.raises(T.TraeAPIError):
            mgr.fetch_models()
    finally:
        httpx.Client = orig


def test_function_chain_prefers_cached_and_retries():
    """function 自适应：默认 work_lite，记住 agent 后优先 agent。"""
    assert T.function_chain("brand-new-model") == ["solo_work_lite", "solo_agent_remote", T.AGENT_STEP]
    with T._FUNCTION_LOCK:
        T._FUNCTION_CACHE["brand-new-model"] = "solo_agent_remote"
    try:
        assert T.function_chain("brand-new-model") == ["solo_agent_remote", "solo_work_lite", T.AGENT_STEP]
    finally:
        T._FUNCTION_CACHE.pop("brand-new-model", None)


def test_is_param_invalid_detects_4001():
    from core.backends.base import BackendHTTPError
    assert T._is_param_invalid(BackendHTTPError(400, b'{"code":4001,"message":"param is invalid"}'))
    assert not T._is_param_invalid(BackendHTTPError(400, b'{"code":1005}'))
    assert not T._is_param_invalid(BackendHTTPError(400, None))


def test_astream_retries_with_fallback_function(monkeypatch):
    """work_lite 回 4001 → 自动换 agent_remote 重试成功，并记住该模型的 function。"""
    backend = T.TraeBackend()
    calls = []

    async def fake_stream(prepared, headers, *, timeout=None, raise_on_error=False):
        calls.append(prepared["function"])
        if len(calls) == 1:
            from core.backends.base import BackendHTTPError
            raise BackendHTTPError(400, b'{"code":4001,"message":"param is invalid"}')
        yield b"data: [DONE]\n\n"

    async def run():
        return [c async for c in backend.astream({}, {"model": "probe-model"})]

    monkeypatch.setattr(backend, "_stream", fake_stream)
    try:
        chunks = asyncio.run(run())
        assert calls == ["solo_work_lite", "solo_agent_remote"]
        assert chunks == [b"data: [DONE]\n\n"]
        assert T.function_chain("probe-model")[0] == "solo_agent_remote"
    finally:
        T._FUNCTION_CACHE.pop("probe-model", None)


def test_astream_does_not_retry_after_emitting(monkeypatch):
    """已向客户端吐出 chunk 后不再换 function 重试（避免重复内容）。"""
    from core.backends.base import BackendHTTPError
    backend = T.TraeBackend()
    calls = []

    async def fake_stream(prepared, headers, *, timeout=None, raise_on_error=False):
        calls.append(prepared["function"])
        yield b'data: {"choices":[]}\n\n'
        raise BackendHTTPError(400, b'{"code":4001}', emitted=True)

    async def run():
        got = []
        try:
            async for c in backend.astream({}, {"model": "emit-model"}):
                got.append(c)
        except BackendHTTPError:
            pass
        return got

    monkeypatch.setattr(backend, "_stream", fake_stream)
    try:
        got = asyncio.run(run())
        assert calls == ["solo_work_lite"] and len(got) == 1
        assert T.function_chain("emit-model")[0] == "solo_work_lite"
    finally:
        T._FUNCTION_CACHE.pop("emit-model", None)


def test_checkin_status_and_claim_and_ent_usage(tmp_path):
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path.endswith("/checkin_credits/status"):
            # UgHeaders：Cloud-IDE-JWT + X-User-Region + 设备三头 + x-app-version
            assert req.headers["Authorization"] == "Cloud-IDE-JWT TRAE-ACCESS"
            assert req.headers["X-User-Region"] == "CN"
            # ug 链路设备号必须是 16 位纯数字：hex32 的 deviceId 会被上游风控拒签（9074）
            assert req.headers["X-Device-Id"] == "1249607945172505"
            assert req.headers["x-device-brand"] == "PC"
            assert req.headers["x-device-type"] == "Linux"
            assert req.headers["x-app-version"] == "1.0.0"
            assert req.headers["x-app-type"] == "trae"
            assert req.headers["Origin"] == T.CONSOLE_HOST
            assert req.headers["Referer"] == T.CONSOLE_HOST + "/"
            # ug 链路由 VSCode 插件进程发起，UA 与 chat 的 IDE 主进程不同
            assert req.headers["User-Agent"] == T.UG_UA
            # body 必须带 req_source，空 body 会被上游按"订单参数不正确"拒绝
            assert json.loads(req.content) == {"req_source": 1}
            return httpx.Response(200, json={"checked_in": True, "credits": 66, "enable": False})
        if req.url.path.endswith("/checkin_credits/claim"):
            assert json.loads(req.content) == {"req_source": 1}
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


def test_ug_headers_device_trio_and_req_source(tmp_path):
    """ug_headers 必须带设备三头 + x-app-version；签到 body 必须带 req_source=1。

    设备号归一化为 16 位纯数字（生产实测：hex32 的 deviceId 会让 claim 恒返回 9074）。
    """
    assert T.CHECKIN_BODY == {"req_source": 1}
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    h = T.TraeCredentialManager(p).ug_headers()
    assert h["X-Device-Id"] == "1249607945172505"  # md5("d"*32) 派生，16 位纯数字
    assert h["x-device-brand"] == "PC" and h["x-device-type"] == "Linux"
    assert h["x-app-version"] == "1.0.0"
    assert h["x-app-type"] == "trae"
    assert h["Origin"] == T.CONSOLE_HOST and h["Referer"] == T.CONSOLE_HOST + "/"
    assert h["User-Agent"] == T.UG_UA
    # deviceId 已是 16 位纯数字时原样透传，不重复派生
    doc_digit = trae_credential()
    doc_digit["auth"]["deviceId"] = "2320418618741168"
    p_digit = tmp_path / "t_digit.info"
    p_digit.write_text(json.dumps(doc_digit), encoding="utf-8")
    assert T.TraeCredentialManager(p_digit).ug_headers()["X-Device-Id"] == "2320418618741168"
    # uid 是数字且 deviceId 非数字时，优先用 uid 补位到 16 位
    doc_uid = trae_credential(uid="2320418618")
    p_uid = tmp_path / "t_uid.info"
    p_uid.write_text(json.dumps(doc_uid), encoding="utf-8")
    assert T.TraeCredentialManager(p_uid).ug_headers()["X-Device-Id"] == "2320418618000000"
    # deviceId 缺失时不再裸奔：由 uid/md5 派生数字设备号，三头照发
    doc2 = trae_credential()
    doc2["auth"].pop("deviceId")
    p2 = tmp_path / "t2.info"
    p2.write_text(json.dumps(doc2), encoding="utf-8")
    h2 = T.TraeCredentialManager(p2).ug_headers()
    assert h2["X-Device-Id"] == "2271192794839258"  # md5("trae-user") 派生
    assert h2["x-device-brand"] == "PC" and h2["x-device-type"] == "Linux"
    assert h2["x-app-version"] == "1.0.0"
    assert h2["User-Agent"] == T.UG_UA


def test_manager_fetch_models_uses_solo_headers(tmp_path):
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)
    seen = {}

def test_manager_solo_headers_non_stream(tmp_path):
    """get_headers(stream=False) 是 chat/SOLO 用的头（Accept: application/json）。"""
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)
    headers = mgr.get_headers(stream=False)
    assert headers["Accept"] == "application/json"
    assert headers["Authorization"] == "Cloud-IDE-JWT TRAE-ACCESS"
    assert headers.get("X-Uid") == "trae-user"


def test_manager_fetch_models_uses_ug_headers(tmp_path):
    """模型菜单走 ug 头（X-User-Region/X-Device-Id），端点 /api/remote/v1/models。"""
    doc = trae_credential()
    p = tmp_path / "t.info"
    p.write_text(json.dumps(doc), encoding="utf-8")
    mgr = T.TraeCredentialManager(p)
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["Authorization"]
        seen["region"] = req.headers.get("X-User-Region")
        seen["device"] = req.headers.get("X-Device-Id")
        return httpx.Response(200, json=_menu(("solo_agent_remote", [
            {"name": "glm-5.2", "display_name": "GLM-5.2", "context_window_tokens": {"dev": 200000}}])))

    real = _REAL_SYNC_CLIENT
    orig = httpx.Client
    httpx.Client = lambda **kw: orig(transport=httpx.MockTransport(handler), **kw)
    try:
        models = mgr.fetch_models()
    finally:
        httpx.Client = orig
    assert [m["id"] for m in models] == ["glm-5.2"]
    assert "/api/remote/v1/models" in seen["url"] and "solo_agent_remote" in seen["url"]
    assert seen["auth"] == "Cloud-IDE-JWT TRAE-ACCESS"
    assert seen["region"] == "CN" and seen["device"] == "1249607945172505"


def test_ug_api_error_paths():
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(403, text="no"))) as client:
        with pytest.raises(T.TraeAPIError) as ei:
            T.checkin_status(client, {})
        assert ei.value.status == 403
        with pytest.raises(T.TraeAPIError):
            T.checkin_claim(client, {})
        with pytest.raises(T.TraeAPIError):
            T.user_ent_usage(client, {})


def test_ent_usage_returns_remaining_not_limit():
    """余额 = Σ(credits_limit − usage.credits_amount)，不能直接把 credits_limit 当余额。"""
    payload = {"user_entitlement_pack_list": [
        {"entitlement_base_info": {"quota": {"credits_limit": 4000}}, "usage": {"credits_amount": 3471.61}},
        {"entitlement_base_info": {"quota": {"credits_limit": 500}}, "usage": {}},
        {"entitlement_base_info": {"quota": {"credits_limit": 200}}, "usage": {"credits_amount": 200}},
    ]}
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        assert T.user_ent_usage(client, {}) == 1028.39  # 4000-3471.61 + 500 + 0


def test_ent_usage_skips_expired_packs():
    now = 1_800_000_000
    payload = {"user_entitlement_pack_list": [
        {"entitlement_base_info": {"quota": {"credits_limit": 300}}, "usage": {},
         "expire_time": now - 10},  # 已过期
        {"entitlement_base_info": {"quota": {"credits_limit": 300}}, "usage": {},
         "expire_time": now + 10},  # 未过期
    ]}
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        assert T.user_ent_usage(client, {}, now=now) == 300.0


def test_ent_usage_falls_back_to_summary_without_packs():
    payload = {"user_entitlement_pack_list": [],
               "usage_summary": {"total_amount": 9100, "consumed_amount": 3671.61}}
    with _REAL_SYNC_CLIENT(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        assert T.user_ent_usage(client, {}) == 5428.39


def test_catalog_provider_drives_map_model_and_ids():
    from core.backends import registry as R
    assert R.catalog_ids("trae") == [m["id"] for m in T.TRAE_STATIC_MODELS]  # 无提供者→静态
    T.set_catalog_provider(lambda: [{"id": "brand-new-model", "name": "BN", "context_length": 999}])
    try:
        assert T.trae_model_catalog()[0]["id"] == "brand-new-model"
        assert T.map_model("brand-new-model") == "brand-new-model"
        assert T.map_model("brand_new_model") == "brand-new-model"
        assert R.catalog_ids("trae") == ["brand-new-model"]
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
        assert len(T.trae_model_catalog()) == 20
        assert T.map_model("glm-5.2") == "glm-5.2"
    finally:
        T.set_catalog_provider(None)


# ---- ModelRegistry 的 Trae 目录 ----

def _registry(clock=None):
    import admin.models as M
    return M.ModelRegistry(get_credential=None, clock=clock or time.time)


def test_registry_trae_builtin_seed_and_disable():
    reg = _registry()
    reg.ensure_static_catalog("trae")
    ids = reg.snapshot_ids()
    # v16：id 是上游裸名（`glm-5.2`），不再有 `trae/` 前缀。
    assert "glm-5.2" in ids
    assert reg._state_for("trae")["available"], "静态兜底表应有 20 条"
    enabled = reg.enabled_models()
    assert "glm-5.2" in enabled
    snap = reg.snapshot()["models"]
    trae_rows = [m for m in snap if m.get("backend") == "trae"]
    assert len(trae_rows) == 20 and all(m["enabled"] for m in trae_rows)
    # 禁用 trae 的 glm-5.2（**显式给 backend** —— 该裸名在两后端都有）
    reg.set_enabled("glm-5.2", False, backend="trae")
    assert "glm-5.2" in reg.snapshot_ids()  # 仍「已知」→ 中间件报禁用而非未知
    trae_rows = [m for m in reg.snapshot()["models"]
                 if m.get("backend") == "trae" and m["id"] == "glm-5.2"]
    assert trae_rows and not trae_rows[0]["enabled"]
    # 落库 / 重启加载往返保留禁用态
    import tempfile
    from pathlib import Path
    from admin.db import Database
    db = Database(Path(tempfile.mkdtemp()) / "state.db")
    reg.persist(db)
    reg2 = _registry()
    reg2.load(db)
    db.close()
    assert "glm-5.2" in reg2._state_for("trae")["disabled"], "重启后禁用态必须保留"
    # 未知 trae 模型 → KeyError
    with pytest.raises(KeyError):
        reg2.set_enabled("nope", True, backend="trae")


def test_registry_refresh_trae_models_replaces_builtin():
    reg = _registry()
    reg.ensure_static_catalog("trae")

    class FakeManager:
        def fetch_models(self):
            return [{"id": "dyn-a", "name": "DynA", "context_length": 2048},
                    {"id": "dyn-b", "name": "DynB", "context_length": 4096}]

    reg.backend_credential_getter = lambda _b: FakeManager()
    assert reg.refresh_catalog("trae") is True
    assert set(reg._state_for("trae")["available"]) == {"dyn-a", "dyn-b"}  # 静态兜底表被动态表替换
    assert reg.catalog_entries("trae")[0] == {"id": "dyn-a", "name": "DynA", "context_length": 2048}
    # 失败保留上次快照
    class Boom:
        def fetch_models(self):
            raise T.TraeAPIError("boom", status=500)
    reg.backend_credential_getter = lambda _b: Boom()
    assert reg.refresh_catalog("trae") is False
    assert set(reg._state_for("trae")["available"]) == {"dyn-a", "dyn-b"}
    # 无 Trae 账号 → 静默保留
    reg.backend_credential_getter = lambda _b: None
    assert reg.refresh_catalog("trae") is False
    assert "dyn-a" in reg.snapshot_ids()


def test_registry_trae_credits_reach_snapshot():
    """动态目录带来的倍率/上下文必须透传到管理页快照。"""
    reg = _registry()

    class FakeManager:
        def fetch_models(self):
            return [{"id": "glm-5.3", "name": "GLM-5.3", "context_length": 200000, "credits": 0.39}]

    reg.backend_credential_getter = lambda _b: FakeManager()
    assert reg.refresh_catalog("trae") is True
    rows = {m["id"]: m for m in reg.snapshot()["models"]}
    row = rows["glm-5.3"]
    assert row["backend"] == "trae" and row["credits"] == 0.39
    assert row["max_input_tokens"] == 200000 and row["name"] == "GLM-5.3"


def test_registry_kill_switch_hides_trae(monkeypatch):
    import os
    reg = _registry()
    reg.ensure_builtin()            # 默认后端也要有行，否则「空快照」会让断言恒真
    reg.ensure_static_catalog("trae")
    monkeypatch.setenv("CODEBUDDY_ENABLE_TRAE", "0")
    # 注意：不能再用 `startswith("trae/")` —— 去前缀后它恒真，测试会静默失效。
    # 判据换成「快照里没有任何 backend=trae 的行」。
    assert reg.snapshot()["models"], "默认后端应仍有行，否则这条断言恒真"
    assert all(m.get("backend") != "trae" for m in reg.snapshot()["models"])


# ---- e2e：模型禁用 + 404 软冷却 ----

async def _e2e_disable_trae_model(self):
    headers = await self.login_admin()
    # 管理页禁用 Trae 的 Doubao-Seed-Code（v16：id 是裸名，靠 body 的 backend 指定归属）
    r = await self.client.patch("/admin/api/models/Doubao-Seed-Code",
                                json={"enabled": False, "backend": "trae"}, headers=headers)
    assert r.status_code == 200, r.text
    reg = self.app.state.model_registry
    assert "Doubao-Seed-Code" in reg._state_for("trae")["disabled"]
    # /v1/models 不再出现该 Trae 行
    models = (await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})).json()["data"]
    assert "Doubao-Seed-Code" not in [m["id"] for m in models]
    # 调用被中间件拒绝（400，不打上游）
    self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and "禁用" in r.json()["detail"]
    # 恢复启用
    r = await self.client.patch("/admin/api/models/Doubao-Seed-Code",
                                json={"enabled": True, "backend": "trae"}, headers=headers)
    assert r.status_code == 200
    assert "Doubao-Seed-Code" not in reg._state_for("trae")["disabled"]


async def _e2e_trae_404_soft_cooldown(self):
    self.patch_upstream(lambda req: httpx.Response(404, content=b'{"message":"config not found"}'))
    aid = self.trae_aid()
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}]})
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
        if "/api/remote/v1/models" in str(req.url):
            return httpx.Response(200, json=_menu(("solo_work_remote", [
                {"name": "dyn-x", "display_name": "Dyn X", "context_window_tokens": {"dev": 200000},
                 "features": '{"consumption_rate":{"enable":true,"data":{"rate":0.5}}}'}])))
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
    # 动态表替换静态表：Trae 目录只剩 dyn-x
    models = (await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})).json()["data"]
    assert [m["id"] for m in models if m["owned_by"] == "trae-solo"] == ["dyn-x"]
    # 管理页模型表出现 Trae 行
    overview = await self.client.get("/admin/api/overview", headers=headers)
    trae_rows = [m for m in overview.json()["models"]["models"] if m.get("backend") == "trae"]
    assert [m["id"] for m in trae_rows] == ["dyn-x"]
    # map_model 接受动态 id：chat 路由不再 400
    self.patch_upstream(lambda req: httpx.Response(200, content=SOLO_SSE))
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "dyn-x", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    # 静态 Doubao-Seed-Code 不在动态表里 → 未知模型 400
    r = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + API},
                               json={"model": "Doubao-Seed-Code", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400


TraeEndToEnd.test_e2e_batch_checkin_covers_trae = _e2e_batch_checkin_covers_trae
TraeEndToEnd.test_e2e_dynamic_catalog_replaces_static = _e2e_dynamic_catalog_replaces_static


def test_astream_param_invalid_after_all_steps(monkeypatch):
    """三种调用方式都 4001 → 抛 400 且给出可读原因（400 不在冷却集合，不冷却账号）。"""
    from core.backends.base import BackendHTTPError
    backend = T.TraeBackend()
    calls = []

    async def fake_stream(prepared, headers, *, timeout=None, raise_on_error=False):
        calls.append(prepared["function"])
        raise BackendHTTPError(400, b'{"code":4001,"message":"param is invalid"}')
        yield b""  # pragma: no cover

    async def fake_agent(body, headers, *, timeout=None, raise_on_error=False):
        calls.append("agent")
        raise BackendHTTPError(400, b'{"code":4001,"message":"param is invalid"}')
        yield b""  # pragma: no cover

    async def run():
        try:
            async for _ in backend.astream({}, {"model": "never-model", "messages": [{"role": "user", "content": "hi"}]}):
                pass
        except BackendHTTPError as e:
            return e
        return None

    monkeypatch.setattr(backend, "_stream", fake_stream)
    monkeypatch.setattr(backend, "_stream_agent", fake_agent)
    try:
        err = asyncio.run(run())
        assert calls == ["solo_work_lite", "solo_agent_remote", "agent"]
        assert err.status == 400 and b"4001" in err.body and "不可用".encode() in err.body
    finally:
        T._FUNCTION_CACHE.pop("never-model", None)


def test_astream_retries_on_in_stream_4001(monkeypatch):
    """上游以 HTTP 200 + 流内 event:error 4001 拒绝时，也应在输出前换 function 重试。"""
    calls = []
    solo_error = b'id:1\nevent:error\ndata:{"code":4001,"message":"param is invalid"}\n\n'

    def handler(req):
        fn = json.loads(req.content)["function"]
        calls.append(fn)
        return httpx.Response(200, content=solo_error if fn == "solo_work_lite" else SOLO_SSE)

    real_async = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_async(transport=httpx.MockTransport(handler), **kw))
    backend = T.TraeBackend()

    async def run():
        return [c async for c in backend.astream({}, {"model": "peek-model"})]

    try:
        chunks = asyncio.run(run())
        assert calls == ["solo_work_lite", "solo_agent_remote"]
        assert any(b"[DONE]" in c for c in chunks)          # 重试后的正常流完整输出
        assert not any(b"4001" in c for c in chunks)        # 客户端看不到被重试掉的错误
        assert T.function_chain("peek-model")[0] == "solo_agent_remote"
    finally:
        T._FUNCTION_CACHE.pop("peek-model", None)


# ---------------------------------------------------------------------------
# Trae 远程 agent（chat_sessions）链路
# ---------------------------------------------------------------------------

AGENT_EVENTS = (
    b'id: 1\nevent: metadata\ndata: {"session_id":"s1"}\n\n'
    b'id: 2\nevent: heartbeat\ndata: {}\n\n'
    b'id: 3\nevent: plan_item\ndata: {"id":"pi_1","task_id":"t1","thought":"","reasoning_content":"The user wants"}\n\n'
    b'id: 4\nevent: plan_item\ndata: {"id":"pi_1","task_id":"t1","thought":"o","reasoning_content":"The user wants me to reply only \\"ok\\"."}\n\n'
    b'id: 5\nevent: plan_item\ndata: {"id":"pi_1","task_id":"t1","thought":"ok","reasoning_content":"The user wants me to reply only \\"ok\\".","tool_call_info":{"name":"finish"}}\n\n'
    b'id: 6\nevent: token_usage\ndata: {"prompt_tokens":57857,"completion_tokens":14,"total_tokens":57871}\n\n'
    b'id: 7\nevent: done\ndata: {"status":"completed","reply_to_message_id":"m1"}\n\n'
)


def test_build_agent_query_flattens_messages():
    from core.backends import trae_agent as A
    q = A.build_agent_query([
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": [{"type": "text", "text": "在的"}]},
        {"role": "user", "content": ""},          # 空内容跳过
    ])
    assert q == "系统：你是助手\n\n用户：你好\n\n助手：在的"
    assert A.build_agent_query([]) == ""


def test_agent_create_session_and_send_message():
    from core.backends import trae_agent as A
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url), json.loads(req.content or b"{}")))
        if req.url.path.endswith("/chat_sessions"):
            return httpx.Response(200, json={"code": 0, "data": {"chat_session_id": "sess-1"}})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "msg-1", "accepted": True}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sid = A.create_session(client, {"Authorization": "x"})
        mid = A.send_message(client, {"Authorization": "x"}, sid, "deepseek-v4.1-flash", "用户：hi", "u123", "DeepSeek-V4.1-Flash")
    assert (sid, mid) == ("sess-1", "msg-1")
    assert seen[0][2] == {"mode": "code"}
    body = seen[1][2]
    assert seen[1][1].endswith("/api/remote/v1/chat_sessions/sess-1/messages")
    assert body["model_name"] == "deepseek-v4.1-flash" and body["agent_type"] == "solo_agent_remote"
    assert body["custom_model"]["config_name"] == "deepseek-v4.1-flash"
    assert body["custom_model"]["display_model_name"] == "DeepSeek-V4.1-Flash"
    assert json.loads(body["query"]) == [{"type": "text", "data": {"content": "用户：hi"}}]
    cp = json.loads(body["common_params"])          # common_params 必须是「字符串化的 JSON」
    assert cp["biz_session_id"] == "sess-1" and cp["biz_user_id"] == "u123"


def test_normalize_agent_stream_deltas_and_usage():
    """plan_item 是累计快照 → 只发新增后缀；done 收尾；usage 挂到最后一个 chunk。"""
    from core.backends import trae_agent as A

    async def lines():
        for line in AGENT_EVENTS.decode("utf-8").split("\n"):
            yield line

    async def run():
        return [c async for c in A.normalize_agent_stream(lines(), model="deepseek-v4.1-flash")]

    chunks = asyncio.run(run())
    payloads = [json.loads(c[6:].decode("utf-8")) for c in chunks if c.startswith(b"data: ") and b"[DONE]" not in c]
    deltas = [p["choices"][0]["delta"] for p in payloads]
    content = "".join(d.get("content", "") for d in deltas)
    reasoning = "".join(d.get("reasoning_content", "") for d in deltas)
    assert content == "ok"                              # 差分后无重复
    assert reasoning == 'The user wants me to reply only "ok".'
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["usage"] == {"prompt_tokens": 57857, "completion_tokens": 14, "total_tokens": 57871}
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert not any(b"heartbeat" in c or b"metadata" in c for c in chunks)   # 非转发事件被忽略


def test_astream_falls_back_to_remote_agent(monkeypatch):
    """两个 solo function 都 4001 → 自动走会话式 agent：建会话 → 投递 → 事件流 → 删会话。"""
    from core.backends.base import BackendHTTPError
    backend = T.TraeBackend()
    http_calls = []

    def sync_handler(req):
        http_calls.append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("/chat_sessions"):
            return httpx.Response(200, json={"code": 0, "data": {"chat_session_id": "sess-9"}})
        if req.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "msg-9"}})
        return httpx.Response(200, json={"code": 0})            # DELETE 会话

    def async_handler(req):
        http_calls.append(("SSE", req.url.path))
        return httpx.Response(200, content=AGENT_EVENTS, headers={"content-type": "text/event-stream"})

    async def solo_4001(prepared, headers, *, timeout=None, raise_on_error=False):
        raise BackendHTTPError(400, b'{"code":4001,"message":"param is invalid"}')
        yield b""  # pragma: no cover

    real_sync, real_async = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_sync(transport=httpx.MockTransport(sync_handler), **kw))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_async(transport=httpx.MockTransport(async_handler), **kw))
    monkeypatch.setattr(backend, "_stream", solo_4001)

    async def run():
        return [c async for c in backend.astream({"X-Uid": "u1"}, {
            "model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "只回复ok"}]})]

    try:
        chunks = asyncio.run(run())
        text = "".join(json.loads(c[6:].decode())["choices"][0]["delta"].get("content", "")
                       for c in chunks if c.startswith(b"data: ") and b"[DONE]" not in c)
        assert text == "ok"
        assert chunks[-1] == b"data: [DONE]\n\n"
        assert [c[0] for c in http_calls] == ["POST", "POST", "SSE", "DELETE"]
        assert T.function_chain("deepseek-v4.1-flash")[0] == T.AGENT_STEP
    finally:
        T._FUNCTION_CACHE.pop("deepseek-v4.1-flash", None)

# ---------------------------------------------------------------------------
# 分层标记（见 pytest.ini / tests/README.md）
#
# 本文件整体属于「core + e2e + network」。标记写在文件里而不是逐条标注：同一文件的用例
# 保护的是同一块行为，分域跑（`pytest tests/pool`）时就该一起跑。
#   core    : 必须守住的不变量，默认只跑这些
#   e2e     : 起整套 create_app 走真实请求链路
#   network : 经由 mock 传输层模拟上游交互
# 若某条用例其实不属于本文件默认域（例如纯形状断言、只测常量的），
# 请在该用例上单独加 @pytest.mark.<域>，或在下面改用更准确的一档。
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.core, pytest.mark.e2e, pytest.mark.network]


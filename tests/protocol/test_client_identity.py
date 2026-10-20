"""客户端身份指纹与站点头（用户 2026-10-20 要求，防风控/封号）。

## 背景

对照 workbuddy2api-panel 的做法，上游判「这是客户端直连还是裸脚本」看三样东西：

1. **风控闸门头** `X-CodeBuddy-Request: 1` —— 官方客户端每个 API 都带；
2. **设备指纹** `X-Machine-ID` / `X-Session-ID` —— 官方客户端有，且**稳定**；
   完全不像是脚本（头全缺），或者每次都变（漂移），都会被单独归类；
3. **站点头** `Origin` / `Referer` / `Accept-Language` —— 来自真实网站。

## 三个关键取舍（都有对应的测试钉住）

- **指纹必须稳定**：按账号 uid 派生，跨重启不变、账号之间互异。
  用随机数就正好踩中「漂移」这个特征，等于把特征做得更明显。
- **站点头只加在 chat 路径**：账单/运维接口打的是 `www.workbuddy.cn`，
  让它们带 `codebuddy.cn` 的 Origin 会被 WAF 当跨站请求拦掉。
  所以站点头由 `core/backends/workbuddy.py` 的 `astream` 现场补，
  共享的 header builder 不碰。
- **设备令牌不编造**：上游登录时签发的 device token 我们没有签发记录，
  auth 文件里没有就不写这个头 —— 宁可缺，也不要写个假的。
"""
from __future__ import annotations

import httpx
import pytest

from core import converter
from core.backends import workbuddy as workbuddy_mod
from core.backends.workbuddy import WorkBuddyBackend, _site_headers


def _build(account: dict, auth: dict | None = None):
    """借用未走 __init__ 的实例调 header builder（不需要真凭据文件）。"""
    return converter.CredentialManager._build_headers_from(
        object(), auth or {"accessToken": "t", "domain": "www.codebuddy.cn"}, account)


class FingerprintTests:
    def test_fingerprint_is_36_hex(self):
        value = converter._fingerprint("machine", "uid-1")
        assert len(value) == 36
        assert all(c in "0123456789abcdef" for c in value)

    def test_fingerprint_is_stable(self):
        """同一输入必须每次都一样 —— 漂移是上游要抓的特征。"""
        assert converter._fingerprint("machine", "uid-1") == converter._fingerprint("machine", "uid-1")

    def test_fingerprint_differs_by_purpose(self):
        assert converter._fingerprint("machine", "uid-1") != converter._fingerprint("session", "uid-1")

    def test_fingerprint_differs_by_account(self):
        assert converter._fingerprint("machine", "uid-1") != converter._fingerprint("machine", "uid-2")

    def test_headers_carry_fingerprint_and_gate_header(self):
        h = _build({"uid": "uid-1", "enterpriseId": "e1"})
        assert h["X-CodeBuddy-Request"] == "1"
        assert h["X-Machine-ID"] == converter._fingerprint("machine", "uid-1")
        assert h["X-Session-ID"] == converter._fingerprint("session", "uid-1")

    def test_fingerprint_differs_between_accounts(self):
        """多号不能共用一个设备指纹 —— 否则会被认成同一台机器上的同一个客户端。"""
        a = _build({"uid": "uid-1", "enterpriseId": "e1"})
        b = _build({"uid": "uid-2", "enterpriseId": "e2"})
        assert a["X-Machine-ID"] != b["X-Machine-ID"]
        assert a["X-Session-ID"] != b["X-Session-ID"]

    def test_fingerprint_is_stable_across_builder_calls(self):
        """同一账号反复建头，指纹不变（跨重启也成立，因为只由 uid 派生）。"""
        first = _build({"uid": "uid-1", "enterpriseId": "e1"})
        second = _build({"uid": "uid-1", "enterpriseId": "e1"})
        assert first["X-Machine-ID"] == second["X-Machine-ID"]
        assert first["X-Session-ID"] == second["X-Session-ID"]

    def test_seed_falls_back_to_enterprise_id(self):
        """没有 uid 时退到 enterpriseId，仍然是稳定值而不是随机值。"""
        h = _build({"uid": "", "enterpriseId": "e-9"})
        assert h["X-Machine-ID"] == converter._fingerprint("machine", "e-9")

    def test_device_token_absent_when_not_provided(self):
        """上游没签发就不写这个头 —— 宁可缺，也不要写个假的。"""
        assert "X-Device-Token" not in _build({"uid": "u", "enterpriseId": "e"})

    def test_device_token_forwarded_when_present(self):
        h = _build({"uid": "u", "enterpriseId": "e"},
                   {"accessToken": "t", "domain": "d", "deviceToken": "dt-1"})
        assert h["X-Device-Token"] == "dt-1"


class SiteHeaderTests:
    def test_origin_follows_domain(self):
        out = _site_headers({"X-Domain": "www.codebuddy.cn"})
        assert out["Origin"] == "https://www.codebuddy.cn"
        assert out["Referer"] == "https://www.codebuddy.cn/"

    def test_accept_language_is_chinese_for_domestic_domain(self):
        assert _site_headers({"X-Domain": "www.codebuddy.cn"})["Accept-Language"] == "zh-CN"
        assert _site_headers({"X-Domain": "www.workbuddy.cn"})["Accept-Language"] == "zh-CN"

    def test_accept_language_is_english_for_overseas_domain(self):
        assert _site_headers({"X-Domain": "www.workbuddy.ai"})["Accept-Language"] == "en-US"

    def test_scheme_in_domain_is_stripped(self):
        """X-Domain 正常是裸域名，但多一层 scheme 也不能拼出 https://https://…。"""
        out = _site_headers({"X-Domain": "https://www.codebuddy.cn/"})
        assert out["Origin"] == "https://www.codebuddy.cn"
        assert out["Referer"] == "https://www.codebuddy.cn/"

    def test_no_domain_leaves_headers_untouched(self):
        """没有域名就不硬造来源 —— 宁可不加，也别加个错的。"""
        out = _site_headers({"Accept": "application/json"})
        assert "Origin" not in out
        assert "Referer" not in out
        assert "Accept-Language" not in out

    def test_original_headers_are_not_mutated(self):
        """共享 builder 产出的 dict 会被别处复用，不能就地改。"""
        original = {"X-Domain": "www.codebuddy.cn"}
        _site_headers(original)
        assert "Origin" not in original


class ChatOnlySiteHeaderTests:
    def test_billing_headers_have_no_site_headers(self):
        """账单/运维路径复用同一个 header builder —— 不许带站点头。

        账单接口打的是 www.workbuddy.cn，带 codebuddy.cn 的 Origin 会被 WAF
        当成跨站请求拦掉。
        """
        h = _build({"uid": "u", "enterpriseId": "e"})
        assert "Origin" not in h
        assert "Referer" not in h
        assert "Accept-Language" not in h

    def test_astream_sends_site_headers(self, monkeypatch):
        """chat 路径实际发出去的请求要带上站点头。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return httpx.Response(200, content=b"data: [DONE]\n\n")

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(workbuddy_mod.httpx, "AsyncClient",
                            lambda **kw: real_client(transport=transport, **kw))

        async def drain():
            backend = WorkBuddyBackend()
            chunks = []
            async for chunk in backend.astream({"X-Domain": "www.codebuddy.cn"}, {}):
                chunks.append(chunk)
            return chunks

        import asyncio

        asyncio.run(drain())
        assert seen.get("origin") == "https://www.codebuddy.cn"
        assert seen.get("referer") == "https://www.codebuddy.cn/"
        assert seen.get("accept-language") == "zh-CN"

    def test_trae_headers_are_not_affected(self):
        """Trae 自建 _solo_headers，不走 WorkBuddy 这条路。"""
        from pathlib import Path

        src = Path(workbuddy_mod.__file__).read_text(encoding="utf-8")
        assert "_site_headers" in src
        import core.backends.trae as trae_mod

        trae_src = Path(trae_mod.__file__).read_text(encoding="utf-8")
        assert "_site_headers" not in trae_src, "Trae 不应引用 WorkBuddy 的站点头"
        assert "_build_headers_from" not in trae_src


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_domain_is_ignored(bad):
    assert "Origin" not in _site_headers({"X-Domain": bad})

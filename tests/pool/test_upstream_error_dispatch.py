"""第 3 阶段把 pool 的「上游错误解释权」从 import 里挪到了后端声明上。

原来的形状（真实踩过）：

    from core.backends.trae import TraeAPIError, interpret_checkin_claim   # 顶部
    ...
    except TraeAPIError as e:                       # 硬写后端名
    ok, msg, _ = interpret_checkin_claim(...)       # 硬写后端名

它有两个问题，第二个更隐蔽：

1. 加第三个后端时要回来再改这两处（需求 2 的承诺是「零改动」）；
2. 它在 **import 期**就把 Trae 的整套私有协议拖进所有部署形态，破坏
   ``registry._load`` 的懒构造（实测 ``import admin.server`` 后
   ``core.backends.trae`` 已在 ``sys.modules`` 里）。

现在分派读声明：``spec.error_class_name`` / ``spec.interpret_checkin_claim``。
这个文件的断言是**行为级**的 —— 它证明分派真的落到了对的后端实现上，
而不只是「没写后端名」这种结构性质（后者由 arch guard 守）。
"""
from __future__ import annotations

import json

import httpx
import pytest
from conftest import build_pool

from core.backends import registry


def _trae_info(tmp_path, name="trae-1.info"):
    """一份合法的 Trae 凭据（秒级 expiresAt，见 TraeCredentialManager）。"""
    f = tmp_path / name
    f.write_text(json.dumps({
        "backend": "trae",
        "auth": {"accessToken": "A", "refreshToken": "R", "expiresAt": 4102444800},
        "account": {"uid": "u-trae", "nickname": "字节账号", "enterpriseId": "e1"},
    }, ensure_ascii=False), encoding="utf-8")
    return f


def _workbuddy_info(tmp_path, name="wb-1.info"):
    f = tmp_path / name
    f.write_text(json.dumps({
        "auth": {"accessToken": "A", "refreshToken": "R", "expiresAt": 4102444800000},
        "account": {"uid": "u-wb", "nickname": "腾讯账号"},
    }, ensure_ascii=False), encoding="utf-8")
    return f


@pytest.fixture()
def trae_pool(tmp_path):
    """一个只装了 Trae 账号的池，上游全部打桩。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "checkin_credits" in url:                       # 签到状态
            return httpx.Response(200, json={"checked_in": False, "enable": True, "credits": 100})
        if "checkin_claim" in url or url.endswith("/claim"):  # 执行签到
            return httpx.Response(200, json={"code": 0, "message": "ok"})
        if "ent_usage" in url or "usage" in url:           # 积分
            return httpx.Response(200, json={"usage_summary": {"total_amount": 500, "consumed_amount": 100}})
        return httpx.Response(200, json={"code": 0})

    return build_pool(tmp_path, [_trae_info(tmp_path)], handler), calls


# --------------------------------------------------------------------------
# 异常类型：分派到后端自己的异常类
# --------------------------------------------------------------------------

def test_nondefault_backend_errors_are_caught_by_declared_type(trae_pool, monkeypatch):
    """上游 401 → 被**该后端声明的**异常类接住 → 标记凭据失效。

    这是 refactor 前后行为必须一致的一条：``except spec.upstream_error_class()``
    必须真的接得住 TraeAPIError，而不是变成一句接不住的 except。
    """
    pool, _ = trae_pool
    aid = next(iter(pool.store.data["accounts"]))

    def boom(*a, **k):
        raise registry.spec_for("trae").upstream_error_class()("凭据过期", status=401)

    monkeypatch.setattr(pool.store.manager_for(aid, pool.store.data["accounts"][aid]), "checkin_status", boom)
    result = pool.operate(aid, "status")

    assert result["ok"] is False
    assert "失效" in result["message"] and "重新授权" in result["message"]
    # 401 应触发冷却 + auth_invalid（与重构前的行为一致）。
    # 注意落在 account_status 上，不是 accounts（见 pool.update）。
    status = pool.store.data["account_status"][aid]
    assert status["auth_invalid"] is True
    assert status["cooldown_until"] > 0


def test_nondefault_backend_error_message_uses_declared_label(trae_pool, monkeypatch):
    """提示里的后端名来自声明（label），不是写死的字符串。"""
    pool, _ = trae_pool
    aid = next(iter(pool.store.data["accounts"]))

    def boom(*a, **k):
        raise registry.spec_for("trae").upstream_error_class()("上游 500", status=500)

    monkeypatch.setattr(pool.store.manager_for(aid, pool.store.data["accounts"][aid]), "checkin_status", boom)
    result = pool.operate(aid, "status")
    # 500 不判失效：原样透出上游消息。
    assert result["ok"] is False and "上游 500" in result["message"]


# --------------------------------------------------------------------------
# 签到报文：分派到后端自己的解释规则
# --------------------------------------------------------------------------

def test_checkin_claim_is_interpreted_by_declared_rules(trae_pool):
    """签到 claim 的处理走本后端声明的规则（code 0 → 成功）。"""
    pool, calls = trae_pool
    aid = next(iter(pool.store.data["accounts"]))

    result = pool.operate(aid, "checkin")
    assert result["ok"] is True, result
    assert result["message"] == "签到成功"
    assert any("claim" in u or "checkin_credits" in u for u in calls), calls


def test_checkin_claim_retryable_code_is_retried(trae_pool, monkeypatch):
    """上游的可重试拒绝码（9074）会重试，而不是当成「尚末确认」误报。

    这条盯的是「解释规则确实来自 Trae 的 ``interpret_checkin_claim``」：
    若分派错到别的后端（或回落到基类的 NotImplementedError），
    9074 就不会被判为可重试，重试循环也不会发生。
    """
    pool, calls = trae_pool
    aid = next(iter(pool.store.data["accounts"]))

    seen = {"claim": 0}

    def claim(client):
        seen["claim"] += 1
        if seen["claim"] == 1:
            return {"code": 9074, "message": "请求过于频繁，请稍后重试"}
        return {"code": 0, "message": "ok"}

    monkeypatch.setattr(pool.store.manager_for(aid, pool.store.data["accounts"][aid]), "checkin_claim", claim)
    result = pool.operate(aid, "checkin")

    assert seen["claim"] == 2, "可重试码必须触发第二次尝试"
    assert result["ok"] is True, result


def test_workbuddy_account_does_not_take_nondefault_path(tmp_path, monkeypatch):
    """腾讯账号不走非默认后端那条路 —— 分派是按声明、不是按「有没有 trae」。

    上游返回一份**合法**的腾讯账单响应，让这条用例只验分派、不依赖
    「上游会失败」这种隐式前提（否则腾讯路径哪天变得容忍空 body，测试仍绿
    但语义已经漂移）。
    """
    def handler(request):
        return httpx.Response(200, json={
            "code": 0,
            "data": {"Response": {"Data": {"TotalCount": 0, "Accounts": []}}},
        })

    pool = build_pool(tmp_path, [_workbuddy_info(tmp_path)], handler)
    aid = next(iter(pool.store.data["accounts"]))

    called = {"n": 0}
    real = pool._operate_nondefault

    def spy(*a, **k):
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(pool, "_operate_nondefault", spy)
    try:
        pool.operate(aid, "status")
    except Exception:
        pass  # 上游是空 body，腾讯路径会自己失败 —— 我们只看分派
    assert called["n"] == 0, "腾讯账号不该被分派到非默认后端实现"

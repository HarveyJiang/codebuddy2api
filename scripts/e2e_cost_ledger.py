"""端到端（真起 PoolMiddleware 进程内链）：验证「上游不发 credit 时零行为变化」。

单元测试已经把账本逻辑覆盖了，但真正要证明的是**集成后**的保证：
上游只回 token 计数（这是当前已知的真实形态）时，选号行为必须与加这个功能
之前完全一致。这条只有走一遍真实中间件才验得出来。
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin.pool import AccountPool, PoolMiddleware  # noqa: E402
from admin.server import Store  # noqa: E402
from tests.test_admin_server import credential  # noqa: E402

STREAM = (
    b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n'
    b"data: [DONE]\n\n"
)
STREAM_WITH_CREDIT = (
    b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12,"credit":0}}\n\n'
    b"data: [DONE]\n\n"
)


async def _run(body, rounds=4):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    store = Store(root / "management", root / "auth", "api-test", "admin-long-enough-test-key")
    a = store.save_browser_account(credential("one"), "one")["account_id"]
    b = store.save_browser_account(credential("two"), "two")["account_id"]
    for aid, item in store.data["accounts"].items():
        store.manager_for(aid, item).get_headers = mock.Mock(
            return_value={"X-Domain": "www.codebuddy.cn"})
    pool = AccountPool(store, clock=lambda: 1800000000)

    picks = []
    original = pool.select

    def spy(*args, **kwargs):
        result = original(*args, **kwargs)
        picks.append(result[0])
        return result

    pool.select = spy

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    middleware = PoolMiddleware(app, pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware),
                                 base_url="http://t") as client:
        for _ in range(rounds):
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "glm-5.2", "messages": []},
                headers={"Authorization": "Bearer api-test"},
            )
            assert response.status_code == 200, response.status_code
    tmp.cleanup()
    return pool, picks, {a, b}


def test_no_credit_keeps_selection_unchanged():
    """上游只回 token → 账本为空，账号照常轮转。"""
    pool, picks, both = asyncio.run(_run(STREAM))
    assert pool.cost_ledger.stats()["entries"] == 0
    assert picks[0] in both
    assert pool.cost_ledger.stats()["observed"] == 0


def test_credit_observation_is_recorded():
    """上游回了 credit=0（实测免费）→ 真的记进账本。

    这条断言的是「功能确实能生效」，与上一条的「不生效时无害」互为一对。

    注意这里只断言**记录**，不断言轮转范围：同一客户端会被亲和绑定到同一个
    账号（既有行为），所以 picks 会重复同一个 id，账本也只覆盖到它。
    """
    pool, picks, both = asyncio.run(_run(STREAM_WITH_CREDIT))
    stats = pool.cost_ledger.stats()
    assert stats["entries"] == 1, stats
    assert stats["observed"] == 4, stats
    assert pool.cost_ledger.is_free(picks[0], "glm-5.2") is True
    snap = pool.cost_ledger.snapshot()
    assert snap[0]["cost_per_1k"] == 0.0


if __name__ == "__main__":
    test_no_credit_keeps_selection_unchanged()
    test_credit_observation_is_recorded()
    print("✅ 端到端验证通过")

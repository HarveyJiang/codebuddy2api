"""admin/cost_ledger.py 与池集成（冷却采信上游时刻、账本优先免费）的测试。"""

import unittest
from unittest import mock

from admin.cost_ledger import CostLedger


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CostLedgerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.ledger = CostLedger(clock=self.clock)

    def test_observe_computes_price_per_1k_tokens(self):
        # 500 tokens 花 0.5 credit → 每千 token 1.0 credit。
        self.assertTrue(self.ledger.observe("a1", "glm-5.2", 0.5, 500))
        self.assertAlmostEqual(self.ledger.cost_of("a1", "glm-5.2"), 1.0)

    def test_free_account_is_recognised(self):
        self.ledger.observe("a1", "step-5-preview", 0, 1000)
        self.assertTrue(self.ledger.is_free("a1", "step-5-preview"))
        self.assertFalse(self.ledger.is_free("a2", "step-5-preview") is True)

    def test_paid_account_is_not_free(self):
        self.ledger.observe("a1", "glm-5.2", 1.0, 1000)
        self.assertFalse(self.ledger.is_free("a1", "glm-5.2"))

    def test_unobserved_returns_none_not_false(self):
        """未知 ≠ 收费。混同会让新账号永远轮不上（第一次观测的机会都没有）。"""
        self.assertIsNone(self.ledger.is_free("never-seen", "m"))

    def test_observation_expires(self):
        """优惠是时段性的：陈旧观测不能继续指导选号。"""
        self.ledger.observe("a1", "m", 0, 1000)
        self.assertTrue(self.ledger.is_free("a1", "m"))
        self.clock.advance(7 * 3600)
        self.assertIsNone(self.ledger.cost_of("a1", "m"))
        self.assertIsNone(self.ledger.is_free("a1", "m"))

    def test_missing_or_unparsable_credit_is_skipped(self):
        """绝不猜测：拿不到 credit 就不记录，宁可靠账本为空。"""
        for credits, tokens in ((None, 100), (0.5, None), ("abc", 100), (0.5, 0), (-1, 100)):
            self.assertFalse(self.ledger.observe("a1", "m", credits, tokens))
        self.assertEqual(self.ledger.stats()["entries"], 0)
        self.assertGreater(self.ledger.stats()["skipped"], 0)

    def test_ema_smooths_single_observation(self):
        self.ledger.observe("a1", "m", 1.0, 1000)   # 1.0
        self.ledger.observe("a1", "m", 2.0, 1000)   # 跳到 2.0
        cost = self.ledger.cost_of("a1", "m")
        # 0.3 平滑：1.0*0.7 + 2.0*0.3 = 1.3，不该直接跳到 2.0。
        self.assertAlmostEqual(cost, 1.3, places=6)

    # -- prefer_free：本次迁移的核心安全保证 -------------------------------

    def test_empty_ledger_changes_nothing(self):
        """账本为空 → 原样返回。这是「上游不发 credit 就零行为变化」的保证。"""
        candidates = [{"id": "a1"}, {"id": "a2"}]
        got = self.ledger.prefer_free(candidates, "m", lambda row: row["id"])
        self.assertIs(got, candidates)

    def test_all_unknown_changes_nothing(self):
        """没有任何实测免费的号 → 原样返回，不因「未知」而收窄候选。"""
        candidates = [{"id": "a1"}, {"id": "a2"}]
        got = self.ledger.prefer_free(candidates, "m", lambda row: row["id"])
        self.assertEqual(len(got), 2)

    def test_free_accounts_are_preferred(self):
        self.ledger.observe("a1", "m", 0, 1000)     # 免费
        self.ledger.observe("a2", "m", 5.0, 1000)   # 收费
        self.ledger.observe("a3", "m", 0, 1000)     # 免费
        candidates = [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]
        got = self.ledger.prefer_free(candidates, "m", lambda row: row["id"])
        self.assertEqual(sorted(c["id"] for c in got), ["a1", "a3"])

    def test_unknown_survives_when_others_are_free(self):
        """有免费号时未知号被让开，但没免费号时它必须还能参与轮转。"""
        self.ledger.observe("a1", "m", 5.0, 1000)   # 实测收费
        candidates = [{"id": "a1"}, {"id": "a2"}]   # a2 未知
        got = self.ledger.prefer_free(candidates, "m", lambda row: row["id"])
        self.assertEqual(len(got), 2)

    def test_no_model_returns_candidates_untouched(self):
        self.ledger.observe("a1", "m", 0, 1000)
        candidates = [{"id": "a1"}, {"id": "a2"}]
        self.assertIs(self.ledger.prefer_free(candidates, None, lambda row: row["id"]), candidates)

    def test_snapshot_and_stats(self):
        self.ledger.observe("a1", "m", 0, 1000)
        snap = self.ledger.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["account_id"], "a1")
        self.assertEqual(self.ledger.stats()["active"], 1)


class PoolObserveTests(unittest.TestCase):
    """池侧：usage 里的 credit 该怎么读。"""

    def test_observe_cost_reads_credit_variants(self):
        from admin.pool import AccountPool

        pool = AccountPool.__new__(AccountPool)
        pool.cost_ledger = CostLedger()
        pool._observe_cost("a1", "m", {"credit": 0.5, "total_tokens": 500})
        self.assertAlmostEqual(pool.cost_ledger.cost_of("a1", "m"), 1.0)

    def test_observe_cost_falls_back_to_split_token_fields(self):
        from admin.pool import AccountPool

        pool = AccountPool.__new__(AccountPool)
        pool.cost_ledger = CostLedger()
        pool._observe_cost("a1", "m", {"credits": 1.0, "prompt_tokens": 400, "completion_tokens": 600})
        self.assertAlmostEqual(pool.cost_ledger.cost_of("a1", "m"), 1.0)

    def test_observe_cost_without_credit_does_nothing(self):
        """上游只回 token 计数（当前已知的真实形态）→ 账本保持为空。"""
        from admin.pool import AccountPool

        pool = AccountPool.__new__(AccountPool)
        pool.cost_ledger = CostLedger()
        pool._observe_cost("a1", "m", {"prompt_tokens": 10, "completion_tokens": 5,
                                       "total_tokens": 15})
        self.assertEqual(pool.cost_ledger.stats()["entries"], 0)

    def test_observe_cost_is_tolerant_of_garbage(self):
        from admin.pool import AccountPool

        pool = AccountPool.__new__(AccountPool)
        pool.cost_ledger = CostLedger()
        for usage in (None, "text", {}, {"credit": "x", "total_tokens": "y"}):
            pool._observe_cost("a1", "m", usage)   # 不该抛


if __name__ == "__main__":
    unittest.main()

import pytest  # noqa: E402  (标记需要)

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


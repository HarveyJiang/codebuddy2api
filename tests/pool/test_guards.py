"""admin/guards.py 与 admin/rotation.py 的测试。"""

import unittest

from admin.guards import (
    DailyUsage,
    DailyUsageCache,
    any_enabled,
    guard_block_reason,
    parse_guard_limits,
)
from admin.rotation import (
    BACKOFF_BASE,
    JITTER_FRACTION,
    WafIpGate,
    backoff_after,
    is_waf_error,
    jitter_duration,
)


class ParseGuardLimitsTests(unittest.TestCase):
    def test_defaults_are_all_zero(self):
        limits = parse_guard_limits({})
        self.assertEqual(set(limits), {"reserve_credits", "daily_token_limit",
                                       "daily_credit_limit", "model_daily_token_limit"})
        self.assertTrue(all(v == 0 for v in limits.values()))
        self.assertFalse(any_enabled(limits))

    def test_reads_prefixed_settings(self):
        limits = parse_guard_limits({"guard_reserve_credits": 100,
                                     "guard_daily_token_limit": 5000})
        self.assertEqual(limits["reserve_credits"], 100)
        self.assertEqual(limits["daily_token_limit"], 5000)
        self.assertTrue(any_enabled(limits))

    def test_garbage_and_negative_become_disabled(self):
        """非法值一律当 0（关闭）。护栏宁可不开，也不能因脏数据乱拦。"""
        limits = parse_guard_limits({"guard_reserve_credits": "abc",
                                     "guard_daily_token_limit": -5,
                                     "guard_daily_credit_limit": None})
        self.assertTrue(all(v == 0 for v in limits.values()))
        self.assertFalse(any_enabled(limits))


class GuardBlockReasonTests(unittest.TestCase):
    OFF = parse_guard_limits({})

    def _limits(self, **kw):
        return parse_guard_limits({f"guard_{k}": v for k, v in kw.items()})

    def test_disabled_never_blocks(self):
        reason = guard_block_reason(
            self.OFF, credits=0, daily_tokens=10**9, daily_credits=10**9,
            model="m", model_daily_tokens={"m": 10**9}, is_free_model=False)
        self.assertEqual(reason, "")

    def test_reserve_credits_blocks_account(self):
        limits = self._limits(reserve_credits=100)
        self.assertTrue(guard_block_reason(
            limits, credits=100, daily_tokens=0, daily_credits=0,
            model="m", model_daily_tokens={}, is_free_model=True))
        self.assertEqual(guard_block_reason(
            limits, credits=101, daily_tokens=0, daily_credits=0,
            model="m", model_daily_tokens={}, is_free_model=True), "")

    def test_unknown_counts_never_block(self):
        """计数未知（None）一律放行。

        读库失败时把账号全停掉，比被烧穿严重得多——这条是整个模块的底线。
        """
        limits = self._limits(reserve_credits=100, daily_token_limit=1,
                              daily_credit_limit=1, model_daily_token_limit=1)
        reason = guard_block_reason(
            limits, credits=None, daily_tokens=None, daily_credits=None,
            model="m", model_daily_tokens=None, is_free_model=False)
        self.assertEqual(reason, "")

    def test_daily_token_limit_blocks_account(self):
        limits = self._limits(daily_token_limit=1000)
        self.assertTrue(guard_block_reason(
            limits, credits=10**6, daily_tokens=1000, daily_credits=0,
            model="m", model_daily_tokens={}, is_free_model=True))

    def test_daily_credit_limit_blocks_paid_only(self):
        """积分到顶后免费模型仍然可用——这正是这条护栏的语义。"""
        limits = self._limits(daily_credit_limit=50)
        self.assertTrue(guard_block_reason(
            limits, credits=10**6, daily_tokens=0, daily_credits=50,
            model="m", model_daily_tokens={}, is_free_model=False))
        self.assertEqual(guard_block_reason(
            limits, credits=10**6, daily_tokens=0, daily_credits=50,
            model="m", model_daily_tokens={}, is_free_model=True), "")

    def test_model_daily_limit_scopes_to_one_model(self):
        """只挡触发的那个模型，同账号的其它模型仍可用。"""
        limits = self._limits(model_daily_token_limit=100)
        self.assertTrue(guard_block_reason(
            limits, credits=10**6, daily_tokens=0, daily_credits=0,
            model="hot", model_daily_tokens={"hot": 100}, is_free_model=True))
        self.assertEqual(guard_block_reason(
            limits, credits=10**6, daily_tokens=0, daily_credits=0,
            model="cold", model_daily_tokens={"hot": 100}, is_free_model=True), "")

    def test_no_model_skips_model_limit(self):
        limits = self._limits(model_daily_token_limit=1)
        self.assertEqual(guard_block_reason(
            limits, credits=10**6, daily_tokens=0, daily_credits=0,
            model=None, model_daily_tokens={"m": 10**9}, is_free_model=True), "")


class DailyUsageTests(unittest.TestCase):
    def test_note_accumulates(self):
        usage = DailyUsage()
        usage.note("a1", "m", 100, 0.5)
        usage.note("a1", "m", 50, 0.25)
        usage.note("a1", "other", 10, 0)
        self.assertEqual(usage.tokens_for("a1"), 160)
        self.assertAlmostEqual(usage.credits_for("a1"), 0.75)
        self.assertEqual(usage.model_tokens_for("a1", "m"), 150)
        self.assertEqual(usage.model_tokens_for("a1", "other"), 10)
        self.assertEqual(usage.model_tokens_for("a1", "missing"), 0)

    def test_unknown_account_reads_zero(self):
        usage = DailyUsage()
        self.assertEqual(usage.tokens_for("nope"), 0)
        self.assertEqual(usage.credits_for("nope"), 0.0)


class DailyUsageCacheTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.now = 1000.0
        self.cache = DailyUsageCache(ttl_seconds=5, clock=lambda: self.now)

    def _loader(self):
        def load():
            self.calls.append(self.now)
            usage = DailyUsage()
            usage.note("a1", "m", 7, 0)
            return usage
        return load

    def test_caches_within_ttl(self):
        load = self._loader()
        self.assertEqual(self.cache.get(load).tokens_for("a1"), 7)
        self.now += 1
        self.cache.get(load)
        self.assertEqual(len(self.calls), 1, "TTL 内不该重复查库")

    def test_refreshes_after_ttl(self):
        load = self._loader()
        self.cache.get(load)
        self.now += 6
        self.cache.get(load)
        self.assertEqual(len(self.calls), 2)

    def test_loader_exception_becomes_none_not_raise(self):
        """读库失败 → None（调用方据此放行），绝不向上抛。"""
        def boom():
            raise RuntimeError("db down")
        self.assertIsNone(self.cache.get(boom))

    def test_loader_none_is_cached_and_returned(self):
        self.assertIsNone(self.cache.get(lambda: None))

    def test_invalidate_forces_reload(self):
        load = self._loader()
        self.cache.get(load)
        self.cache.invalidate()
        self.cache.get(load)
        self.assertEqual(len(self.calls), 2)


class WafDetectionTests(unittest.TestCase):
    def test_known_code(self):
        self.assertTrue(is_waf_error(403, None, "11128"))
        self.assertTrue(is_waf_error(None, None, 11128))

    def test_403_with_channel_wording(self):
        self.assertTrue(is_waf_error(403, "Illegal API invocation from an unapproved channel"))

    def test_403_without_channel_wording_is_not_waf(self):
        """403 未必是 WAF（账号被禁也是 403），没有通道文案就不能当 IP 级处理。"""
        self.assertFalse(is_waf_error(403, "account suspended"))

    def test_unrelated_errors_are_not_waf(self):
        self.assertFalse(is_waf_error(429, "rate limited"))
        self.assertFalse(is_waf_error(500, "boom"))


class WafIpGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.gate = WafIpGate(clock=lambda: self.now)

    def test_single_account_never_activates(self):
        """单号反复 403 属账号级偶发，归软冷却管，不该停掉整个池子的轮转。"""
        for _ in range(5):
            self.assertFalse(self.gate.note_waf("a1"))
            self.now += 1
        self.assertFalse(self.gate.active())

    def test_two_distinct_accounts_activate(self):
        self.assertFalse(self.gate.note_waf("a1"))
        self.assertTrue(self.gate.note_waf("a2"))
        self.assertTrue(self.gate.active())

    def test_same_account_repeats_do_not_count_twice(self):
        self.assertFalse(self.gate.note_waf("a1"))
        self.assertFalse(self.gate.note_waf("a1"))
        self.assertFalse(self.gate.note_waf("a1"))
        self.assertFalse(self.gate.active())

    def test_hits_outside_window_expire(self):
        self.gate.note_waf("a1")
        self.now += 61
        self.assertFalse(self.gate.note_waf("a2"), "窗外命中不该参与判定")

    def test_active_period_does_not_extend(self):
        """激活期内不续期：一次误判不该变成长期停摆。"""
        self.gate.note_waf("a1")
        self.gate.note_waf("a2")
        self.now += 30
        self.assertTrue(self.gate.note_waf("a3"))
        self.now += 31   # 距首次激活 61 秒，已过窗口
        self.assertFalse(self.gate.active())

    def test_reactivates_after_expiry_with_fresh_hits(self):
        self.gate.note_waf("a1")
        self.gate.note_waf("a2")
        self.now += 61
        self.assertFalse(self.gate.active())
        self.assertFalse(self.gate.note_waf("a3"))
        self.assertTrue(self.gate.note_waf("a4"))

    def test_reset(self):
        self.gate.note_waf("a1")
        self.gate.note_waf("a2")
        self.gate.reset()
        self.assertFalse(self.gate.active())

    def test_snapshot(self):
        self.gate.note_waf("a1")
        self.gate.note_waf("a2")
        snap = self.gate.snapshot(backend="workbuddy")
        self.assertTrue(snap["active"])
        self.assertGreater(snap["remaining_seconds"], 0)
        self.assertEqual(snap["threshold"], 2)

    def test_snapshot_aggregates_both_backends_by_default(self):
        """不指定 backend 时按后端分列：两个后端要能各自独立判断。"""
        self.gate.note_waf("a1")
        self.gate.note_waf("a2")
        snap = self.gate.snapshot()
        self.assertIn("by_backend", snap)
        # 恒定列出两个已知后端，结构稳定，前端不用处理「桶时有时无」。
        self.assertEqual(set(snap["by_backend"]), {"workbuddy", "trae"})
        self.assertTrue(snap["by_backend"]["workbuddy"]["active"])
        self.assertFalse(snap["by_backend"]["trae"]["active"])


class BackoffTests(unittest.TestCase):
    def test_zero_base_disables_backoff(self):
        self.assertEqual(backoff_after(0, base=0), 0.0)
        self.assertEqual(backoff_after(5, base=0), 0.0)

    def test_grows_exponentially_and_caps(self):
        # 用固定 rng 消掉抖动，只看增长形态。
        values = [backoff_after(n, base=0.5, cap=8.0) for n in range(6)]
        self.assertTrue(all(v > 0 for v in values))
        self.assertLessEqual(max(values), 8.0 * (1 + JITTER_FRACTION))

    def test_jitter_stays_within_bounds(self):
        for draw in (0.0, 0.5, 1.0):
            value = jitter_duration(1.0, rng=lambda d=draw: d)
            self.assertGreaterEqual(value, 1.0 * (1 - JITTER_FRACTION) - 1e-9)
            self.assertLessEqual(value, 1.0 * (1 + JITTER_FRACTION) + 1e-9)

    def test_zero_duration_is_not_jittered(self):
        self.assertEqual(jitter_duration(0), 0)
        self.assertEqual(jitter_duration(-1), -1)

    def test_jitter_actually_varies(self):
        """抖动的意义就是打散相位：多次取值不该全都一样。"""
        values = {round(jitter_duration(1.0), 6) for _ in range(20)}
        self.assertGreater(len(values), 1)


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


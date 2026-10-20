"""模型级冷却（用户 2026-10-20）。

用户原话：「目前不要因为单个模型影响账号冷却」——上游按**模型**限流时，
不该把整个账号冷掉。之前的实现把 429 之类的失败一律记在账号上，一个模型
撞了限流，池子里就少一个人，连别的模型也一起用不了。

本文件锁住三件事：

1. 限流落在**模型**上：账号状态仍是「可用」，冷却只挂在那个模型名下；
2. 选号时按模型筛：这个模型跳过这个号，换个模型照常用这个号；
3. 认证/额度类失败（401/402/403）**仍然**是账号级——那些换模型也救不了。

另外钉住「不知道是哪个模型」时的回落：无法判定影响范围时宁可多冷一个号，
也不要让请求在同一个限流模型上反复撞墙。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from admin.pool import AccountPool
from admin.server import Store
from conftest import credential


class ModelCooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test',
                           'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.b = self.store.save_browser_account(credential('two'), 'two')['account_id']
        self.now = 1800000000
        self.pool = AccountPool(self.store, clock=lambda: self.now,
                                 client_factory=lambda: httpx.Client(
                                     transport=httpx.MockTransport(lambda req: httpx.Response(200))))
        for aid, item in self.store.data['accounts'].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={'X-Domain': 'www.codebuddy.cn'})

    def status(self, aid=None):
        return self.store.data['account_status'][aid or self.a]

    def row(self, aid=None):
        aid = aid or self.a
        return dict(self.store.data['accounts'][aid], id=aid, status='ok')

    # ---- 1. 限流落在模型上，账号不被冷掉 ----

    def test_429_with_model_only_cools_that_model(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        state = self.status()
        # 账号本身没有进冷却
        self.assertEqual(state.get('cooldown_until', 0), 0)
        self.assertEqual(self.pool.state(self.row()), 'available')
        # 冷却只挂在那个模型名下
        self.assertGreater(state['model_cooldowns']['step-5-preview'], self.now)
        # 不计入账号级连续失败（否则好账号会被升级成 2h 硬冷却）
        self.assertFalse(state.get('consecutive_fails'))

    def test_model_cooldown_reason_names_model_and_time(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        err = self.status()['last_error']
        self.assertIn('step-5-preview', err)   # 知道该切哪个模型
        self.assertIn('模型级', err)             # 知道是模型级、不是账号被冷
        self.assertNotIn('None', err)

    def test_different_models_cool_independently(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        self.pool.record(self.a, 429, model='doubao-pro')
        cooldowns = self.status()['model_cooldowns']
        self.assertIn('step-5-preview', cooldowns)
        self.assertIn('doubao-pro', cooldowns)
        # 账号依然可用
        self.assertEqual(self.pool.state(self.row()), 'available')

    def test_success_clears_model_fail_counter(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        self.pool.record(self.a, 429, model='step-5-preview')
        self.assertEqual(self.status()['model_fails']['step-5-preview'], 2)
        self.pool.record(self.a, 200, model='step-5-preview')
        self.assertNotIn('step-5-preview', self.status().get('model_fails', {}))

    def test_upstream_reset_time_is_used_at_model_level(self):
        """上游明说几点重置 → 采信它（本地猜的必然偏）。"""
        from datetime import datetime, timezone, timedelta
        CN = timezone(timedelta(hours=8))
        later = datetime.fromtimestamp(self.now + 7200, CN).strftime('%Y-%m-%d %H:%M:%S')
        self.pool.record(self.a, 429, model='step-5-preview',
                         hint_text=f'当前您在 Step-5-Preview 模型的使用量已超出频率限制，可在 {later} 重置后使用')
        until = self.status()['model_cooldowns']['step-5-preview']
        self.assertAlmostEqual(until - self.now, 7200, delta=2)
        # 账号没被冷
        self.assertEqual(self.status().get('cooldown_until', 0), 0)

    # ---- 2. 选号按模型筛 ----

    def test_select_skips_account_for_cooled_model_only(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        # 请求被限流的模型 → 跳过 a
        self.assertEqual(self.pool.select(model='step-5-preview')[0], self.b)
        # 请求别的模型 → a 照常可用
        self.assertIn(self.pool.select(model='doubao-pro')[0], (self.a, self.b))
        picked = self.pool.select(model='doubao-pro')[0]
        self.assertNotEqual(picked, 'blocked')

    def test_select_raises_clear_error_when_model_fully_cooled(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        self.pool.record(self.b, 429, model='step-5-preview')
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.pool.select(model='step-5-preview')
        # 报错要说清是「这个模型在冷却」，而不是让人去查登录态
        self.assertIn('step-5-preview', str(ctx.exception.detail))
        self.assertIn('冷却', str(ctx.exception.detail))

    def test_expired_model_cooldown_frees_account(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        self.now += 301
        self.assertFalse(self.pool.model_cooling(self.a, 'step-5-preview'))
        self.assertEqual(self.pool.select(model='step-5-preview')[0], self.a)

    # ---- 3. 账号级失败仍旧是账号级 ----

    def test_auth_failures_still_cool_the_account(self):
        for status in (401, 403, 402):
            self.pool.record(self.a, status, model='step-5-preview')
            state = self.status()
            self.assertGreater(state['cooldown_until'], self.now,
                               f'{status} 应冷却账号')
            self.assertEqual(self.pool.state(self.row()), 'cooling')
            # 认证类失败**不**进模型级：换模型也救不了
            self.assertFalse(state.get('model_cooldowns'))

    def test_unknown_model_falls_back_to_account_cooldown(self):
        """不知道是哪个模型 → 无法判定范围，回落成账号级冷却（宁可多冷一个号）。"""
        self.pool.record(self.a, 429)
        state = self.status()
        self.assertGreater(state['cooldown_until'], self.now)
        self.assertEqual(state.get('consecutive_fails'), 1)
        self.assertFalse(state.get('model_cooldowns'))

    def test_soft_cooldown_without_model_keeps_legacy_behaviour(self):
        """后端声明的软冷却（如 Trae 404）在不知道模型时仍按旧行为：账号级、不计连续失败。"""
        from core.backends.registry import spec_for
        self.store.data['accounts'][self.a]['backend'] = 'trae'
        soft = spec_for('trae').soft_cooldown_for(404)
        self.assertTrue(soft, 'Trae 应声明 404 的软冷却')
        self.pool.record(self.a, 404)
        state = self.status()
        self.assertAlmostEqual(state['cooldown_until'], self.now + soft, delta=1)
        self.assertFalse(state.get('consecutive_fails'))

    def test_soft_cooldown_with_model_is_model_level(self):
        """Trae 的 404 = 这个模型不可用，账号本身是好的 → 只冷该模型。"""
        self.store.data['accounts'][self.a]['backend'] = 'trae'
        self.pool.record(self.a, 404, model='doubao-seed-code')
        state = self.status()
        self.assertEqual(state.get('cooldown_until', 0), 0)
        self.assertGreater(state['model_cooldowns']['doubao-seed-code'], self.now)

    # ---- 4. 手动放行 ----

    def test_clear_cooldown_also_clears_model_cooldowns(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        result = self.pool.operate(self.a, 'clear-cooldown')
        self.assertTrue(result['ok'])
        self.assertEqual(self.status().get('model_cooldowns', {}), {})
        self.assertEqual(self.pool.select(model='step-5-preview')[0], self.a)

    # ---- 5. 管理端能看到 ----

    def test_rows_expose_model_cooldowns(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        rows = {r['id']: r for r in self.pool.rows(self.store.account_rows())}
        self.assertIn('step-5-preview', rows[self.a]['model_cooldowns'])
        self.assertEqual(rows[self.a]['model_cooling_count'], 1)
        # 账号状态仍是「可用」——不是「冷却中」
        self.assertEqual(rows[self.a]['pool_state'], 'available')
        # 别的账号没有模型冷却
        self.assertEqual(rows[self.b]['model_cooling_count'], 0)

    def test_cooling_models_lists_only_live_ones(self):
        self.pool.record(self.a, 429, model='step-5-preview')
        self.pool.record(self.a, 429, model='doubao-pro')
        self.assertEqual(sorted(self.pool.cooling_models(self.a)),
                         ['doubao-pro', 'step-5-preview'])
        self.now += 301   # 第一个冷却到期
        self.assertNotIn('step-5-preview', self.pool.cooling_models(self.a))


class ModelCooldownMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """端到端：上游真的返回 429 时，冷却落在模型上，账号不被摘掉。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test',
                           'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.b = self.store.save_browser_account(credential('two'), 'two')['account_id']
        self.now = 1800000000
        self.pool = AccountPool(self.store, clock=lambda: self.now,
                                 client_factory=lambda: httpx.Client(
                                     transport=httpx.MockTransport(lambda req: httpx.Response(200))))
        for aid, item in self.store.data['accounts'].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={'X-Domain': 'www.codebuddy.cn'})

    async def test_upstream_429_cools_model_not_account(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 429, 'headers': []})
            await send({'type': 'http.response.body',
                        'body': b'{"error":{"message":"rate limited"}}'})
        from admin.pool import PoolMiddleware
        middleware = PoolMiddleware(app, self.pool)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware),
                                     base_url='http://test') as client:
            response = await client.post('/v1/chat/completions', json={'model': 'step-5-preview'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 429)
        state = self.store.data['account_status'][self.a]
        # 账号没被冷掉：换个模型它照常参与分配
        self.assertEqual(state.get('cooldown_until', 0), 0)
        self.assertGreater(state['model_cooldowns']['step-5-preview'], self.now)
        self.assertEqual(self.pool.state(dict(self.store.data['accounts'][self.a],
                                              id=self.a, status='ok')), 'available')


if __name__ == '__main__':
    unittest.main()

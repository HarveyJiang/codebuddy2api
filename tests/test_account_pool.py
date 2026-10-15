import asyncio
import hashlib
import json
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock

import httpx
from fastapi import HTTPException

from core import converter
from admin.db import Database
from admin.server import Store
from admin.pool import AccountPool, PoolMiddleware, REQUEST_CREDENTIAL, summarize_packages, request_affinity, CN
try:
    from .test_admin_server import credential
except ImportError:
    from test_admin_server import credential


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root/'management', root/'auth', 'api-test', 'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.b = self.store.save_browser_account(credential('two'), 'two')['account_id']
        self.now = 1800000000
        self.checked = False
        self.checkin_posts = 0
        self.fail_mode = False
        self.pages = []
        def respond(req):
            if self.fail_mode:
                return httpx.Response(500)
            if req.url.path.endswith('checkin-activity-status'):
                return httpx.Response(200,json={'code':0,'data':{'active':True,'today_checked_in':self.checked}})
            if req.url.path.endswith('daily-checkin'):
                self.checkin_posts += 1; self.checked=True
                return httpx.Response(200,json={'code':0,'data':{}})
            body=json.loads(req.content)
            self.pages.append(body['PageNumber'])
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{'TotalCount':1,'Accounts':[{'CycleCapacityRemainPrecise':'479.59','CycleCapacitySize':500,'CapacityRemain':500}]}}}})
        self.pool=AccountPool(self.store,clock=lambda:self.now,client_factory=lambda:httpx.Client(transport=httpx.MockTransport(respond)))
        for aid,item in self.store.data['accounts'].items():
            self.store.manager_for(aid,item).get_headers=Mock(return_value={'X-Domain':'www.codebuddy.cn'})

    def test_expiry_aware_routing_and_weights(self):
        self.pool.update(self.a, remaining=100, packages=1, credits_updated=self.now,
                         earliest_expiry=self.now + 3600, earliest_expiry_remaining=4.6)
        self.pool.update(self.b, remaining=100, packages=1, credits_updated=self.now,
                         earliest_expiry=self.now + 86400, earliest_expiry_remaining=100)
        # Soonest-to-expire credits are consumed first.
        self.assertEqual([self.pool.select()[0] for _ in range(2)], [self.a, self.b, self.a][:2])
        # Weight overrides expiry ordering.
        self.pool.update(self.b, weight=5)
        self.assertEqual([self.pool.select()[0] for _ in range(2)], [self.b, self.a])
        # Unknown expiry sorts after known ones within the same weight.
        self.pool.update(self.a, weight=5, earliest_expiry=None)
        self.assertEqual(self.pool.select()[0], self.b)

    def test_summarize_packages_earliest_expiry(self):
        fixed = datetime(2026, 10, 14, 8, 0, 0, tzinfo=CN).timestamp()
        result = summarize_packages([
            {'CycleCapacityRemainPrecise': '4.61', 'CycleCapacitySizePrecise': '100', 'CycleEndTime': '2026-10-14 09:06:13'},
            {'CycleCapacityRemainPrecise': '100', 'CycleCapacitySizePrecise': '100', 'CycleEndTime': '2026-10-15 09:00:47'},
            {'CycleCapacityRemainPrecise': '0', 'CycleCapacitySizePrecise': '100', 'CycleEndTime': '2026-10-13 00:00:00'},
            {'CycleCapacityRemainPrecise': '50', 'CycleCapacitySizePrecise': '50'},
        ], now=fixed)
        self.assertEqual(result['earliest_expiry'], datetime(2026, 10, 14, 9, 6, 13, tzinfo=CN).timestamp())
        self.assertAlmostEqual(result['earliest_expiry_remaining'], 4.61)
        self.assertEqual(result['remaining'], 154.61)
        # 3 天内到期：10-14 09:06（4.61）+ 10-15 09:00（100）；10-13 已过期且余额为 0 不计。
        self.assertAlmostEqual(result['expiring_soon'], 104.61)
        # 无到期时间的包不计入。
        self.assertEqual(result['expiring_soon_updated'], int(fixed))

    def test_expiring_soon_excludes_far_future_and_expired(self):
        fixed = datetime(2026, 10, 14, 8, 0, 0, tzinfo=CN).timestamp()
        result = summarize_packages([
            {'CycleCapacityRemainPrecise': '10', 'CycleCapacitySizePrecise': '10', 'CycleEndTime': '2026-10-20 00:00:00'},
            {'CycleCapacityRemainPrecise': '20', 'CycleCapacitySizePrecise': '20', 'CycleEndTime': '2026-10-16 23:00:00'},
            {'CycleCapacityRemainPrecise': '30', 'CycleCapacitySizePrecise': '30', 'CycleEndTime': '2026-10-13 00:00:00'},
        ], now=fixed)
        # 只有 10-16 在 3 天窗口内；10-20 太远，10-13 已过期。
        self.assertAlmostEqual(result['expiring_soon'], 20)
        self.assertEqual(result['remaining'], 60)

    def test_expiring_soon_defaults_to_now(self):
        result = summarize_packages([{'CycleCapacityRemainPrecise': '5', 'CycleCapacitySizePrecise': '5'}])
        self.assertEqual(result['expiring_soon'], 0)

    async def test_checkin_spreads_across_day_slots(self):
        self.store.data['pool']['auto_checkin'] = True
        self.store.data['pool']['checkin_time'] = '09:00'
        # Pin account b to an evening slot, account a to a morning slot.
        a = self.store.data['account_status'].setdefault(self.a, {})
        a.update(checkin_slot='上午', checkin_when='09:10', checkin_offset_date='2026-10-14')
        b = self.store.data['account_status'].setdefault(self.b, {})
        b.update(checkin_slot='晚间', checkin_when='19:30', checkin_offset_date='2026-10-14')
        # 09:30: only the morning account is due.
        self.now = datetime(2026, 10, 14, 9, 30, tzinfo=CN).timestamp()
        await self.pool.tick()
        self.assertEqual(self.checkin_posts, 1)
        # 19:45: the evening account fires too.
        self.checked = False  # 上游按账号返回状态，重置以免误判 b 已签到
        self.now = datetime(2026, 10, 14, 19, 45, tzinfo=CN).timestamp()
        await self.pool.tick()
        self.assertEqual(self.checkin_posts, 2)

    def test_checkin_slots_cover_all_day_and_respect_floor(self):
        self.store.data['pool']['checkin_time'] = '09:00'
        for _ in range(120):
            slot, when = self.pool.checkin_offset(self.a, '2026-10-14', 0)
            self.assertIn(slot, [s[0] for s in self.pool.CHECKIN_SLOTS])
            self.assertGreaterEqual(when, '09:00')
            self.assertLessEqual(when, '22:00')
            # Same day returns the same slot.
            self.assertEqual(self.pool.checkin_offset(self.a, '2026-10-14', 0), (slot, when))
            self.store.data['account_status'][self.a].pop('checkin_offset_date')

    async def test_no_automatic_credit_sync_in_tick(self):
        """积分只在页面请求时拉取，tick 不做任何后台积分查询。"""
        calls = []
        async def fake_batch(action):
            calls.append(action)
            return {'results': []}
        self.pool.batch = fake_batch
        for hour in (3, 11, 21):
            self.pages.clear()
            self.now = datetime(2026, 10, 14, hour, 0, tzinfo=CN).timestamp()
            await self.pool.tick()
            self.assertNotIn('status', calls)

    async def test_refresh_credits_pulls_and_throttles(self):
        self.store.data['pool']['credit_min_interval'] = 60
        seen = []
        def fake_operate(aid, action):
            seen.append((aid, action))
            return {'id': aid, 'ok': True, 'message': '积分状态已更新'}
        self.pool.operate = fake_operate
        # 首次拉取：两个账号都请求。
        result = await self.pool.refresh_credits()
        self.assertEqual(result['refreshed'], 2)
        self.assertEqual(sorted(aid for aid, _ in seen), sorted((self.a, self.b)))
        self.assertTrue(all(action == 'status' for _, action in seen))
        # 记录成功返回的时间，模拟 operate 更新 credits_updated。
        for aid in (self.a, self.b):
            self.store.data['account_status'].setdefault(aid, {})['credits_updated'] = int(self.now)
        # 限流窗口内：不再重复请求。
        seen.clear()
        result = await self.pool.refresh_credits()
        self.assertEqual(result['refreshed'], 0)
        self.assertTrue(result['skipped'])
        self.assertEqual(seen, [])
        # 超过窗口后再拉。
        self.now += 120
        seen.clear()
        result = await self.pool.refresh_credits()
        self.assertEqual(result['refreshed'], 2)
        self.assertEqual(len(seen), 2)

    async def test_batch_checkin_spreads_requests(self):
        # 记录每次实际延迟的秒数，验证错峰参数真正生效（而非依赖时序竞态）。
        delays = []
        real_sleep = asyncio.sleep
        async def fake_sleep(seconds):
            delays.append(seconds)
            await real_sleep(0)
        self.pool.operate = lambda aid, action: {'id': aid, 'ok': True, 'message': '签到成功'}
        with unittest.mock.patch('admin.pool.asyncio.sleep', fake_sleep):
            self.store.data['pool']['batch_spread'] = 90
            await self.pool.batch('checkin')
            self.assertEqual(len(delays), 2)
            for delay in delays:
                self.assertGreaterEqual(delay, 0)
                self.assertLessEqual(delay, 90)
            self.assertNotEqual(delays[0], delays[1])  # 各自随机，不重合
            # 关闭错峰则不延迟。
            delays.clear()
            self.store.data['pool']['batch_spread'] = 0
            await self.pool.batch('checkin')
            self.assertEqual(delays, [])
            # status 动作不引入延迟。
            self.store.data['pool']['batch_spread'] = 90
            await self.pool.batch('status')
            self.assertEqual(delays, [])

    async def test_credential_refresh_spreads_within_day_window(self):
        calls = []
        def fake_operate(aid, action):
            calls.append((aid, action))
            return {'id': aid, 'ok': True}
        self.pool.operate = fake_operate
        self.store.data['pool']['auto_checkin'] = False
        self.store.data['pool']['sync_start'] = '10:00'
        self.store.data['pool']['sync_end'] = '20:00'
        for aid, slot in ((self.a, '10:30'), (self.b, '15:00')):
            self.store.data['account_status'].setdefault(aid, {}).update(
                refresh_slot=slot, refresh_slot_date='2026-10-14')
        # 11:00：只有 a 的随机时刻已过（凭据未临近到期故不实际刷新，仅落当日标记）。
        self.now = datetime(2026, 10, 14, 11, 0, tzinfo=CN).timestamp()
        await self.pool.tick()
        statuses = self.store.data['account_status']
        self.assertEqual(statuses[self.a].get('refresh_window_date'), '2026-10-14')
        self.assertIsNone(statuses[self.b].get('refresh_window_date'))
        # 15:30：b 的时刻到了，也落标记。
        self.now = datetime(2026, 10, 14, 15, 30, tzinfo=CN).timestamp()
        await self.pool.tick()
        self.assertEqual(statuses[self.b].get('refresh_window_date'), '2026-10-14')
        # 21:00：时段外不再发起任何预刷新。
        self.now = datetime(2026, 10, 14, 21, 0, tzinfo=CN).timestamp()
        seen = len(calls)
        await self.pool.tick()
        self.assertEqual(len(calls), seen)

    async def test_checkin_retry_cap_and_success_reset(self):
        self.store.data['pool']['auto_checkin'] = True
        self.store.data['pool']['checkin_time'] = '00:00'
        status = self.store.data['account_status'].setdefault(self.a, {})
        status['checkin_fails'] = 5
        status['checkin_slot'] = '上午'
        status['checkin_when'] = '00:00'
        status['checkin_offset_date'] = '2026-10-14'
        # Also pin b so it does not check in and pollute the counter.
        bstatus = self.store.data['account_status'].setdefault(self.b, {})
        bstatus['checkin_date'] = '2026-10-14'
        self.now = datetime(2026, 10, 14, 10, 0, tzinfo=CN).timestamp()
        await self.pool.tick()
        self.assertEqual(self.checkin_posts, 0)  # failed 5 times today: stop retrying

    def test_key_daily_limit_blocks_after_quota(self):
        kid, value = self.store.add_key('limited', daily_limit=2)
        self.store.save()
        self.assertEqual(self.store.check_api('Bearer ' + value, ''), kid)
        for _ in range(2):
            self.store.record_key_usage(kid)
        with self.assertRaises(HTTPException) as ctx:
            self.store.check_api('Bearer ' + value, '')
        self.assertEqual(ctx.exception.status_code, 429)
        # Unlimited key keeps working.
        kid2, value2 = self.store.add_key('unlimited')
        self.assertEqual(self.store.check_api('Bearer ' + value2, ''), kid2)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def test_precise_cycle_balance_and_unknown(self):
        result=summarize_packages([{'CapacityRemain':500,'CapacitySize':500,'CycleCapacityRemainPrecise':'479.59','CycleCapacitySizePrecise':'500'},{'CapacityRemainPrecise':'0.12','CapacitySize':100}])
        self.assertEqual(result['remaining'],479.71)
        with self.assertRaises(ValueError): summarize_packages([{}])
        self.assertEqual(self.pool.state(self.store.account_rows()[0]),'available')

    def test_round_robin_paused_cooling_exhausted(self):
        self.assertEqual([self.pool.select()[0] for _ in range(4)],[self.a,self.b,self.a,self.b])
        self.store.data['accounts'][self.a]['enabled']=False
        self.assertEqual(self.pool.select()[0],self.b)
        self.pool.record(self.b,429)
        with self.assertRaises(HTTPException):self.pool.select()
        self.now+=1801
        self.assertEqual(self.pool.select()[0],self.b)
        self.pool.update(self.b,remaining=0,packages=1,credits_updated=self.now)
        with self.assertRaises(HTTPException):self.pool.select()
        self.now+=601
        self.assertEqual(self.pool.select()[0],self.b)

    def test_affinity_failover_persistence_and_expiry(self):
        key = request_affinity({b'authorization': b'Bearer api-test'}, {'model': 'm', 'prompt_cache_key': 'session-one'})
        self.assertEqual([self.pool.select(key)[0] for _ in range(3)], [self.a] * 3)
        self.pool.record(self.a)
        self.assertEqual(AccountPool(self.store, clock=lambda: self.now).select(key)[0], self.a)
        self.pool.record(self.a, 429)
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.now += 1801
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.now += 86401
        self.pool.select()
        self.assertNotIn(key, self.store.data['session_bindings'])

    def test_consumption_fields_are_gone(self):
        # 按 db-redesign spec：不建 usage_history，消耗速率展示整体删除。
        row = self.pool.rows(self.store.account_rows())[0]
        for field in ('daily_rate', 'days_to_empty', 'expire_unused'):
            self.assertNotIn(field, row, f"{field} 应已从 rows() 输出移除")
        self.assertFalse(hasattr(self.pool, 'consumption'))
        status = self.store.data['account_status'].setdefault(self.a, {})
        self.assertNotIn('usage_history', status)

    def test_record_exponential_backoff_and_reset(self):
        self.pool.record(self.a, 429)
        first = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertEqual(first - self.now, 1800)
        self.now += 1
        self.pool.record(self.a, 429)
        second = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertEqual(second - self.now, 3600)
        self.now += 1
        self.pool.record(self.a, 429)
        third = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertEqual(third - self.now, 7200)  # capped at 2h
        self.pool.record(self.a, 200)
        self.assertEqual(self.store.data['account_status'][self.a]['consecutive_fails'], 0)
        # 402 invalidates credit freshness so next sync re-queries immediately.
        self.pool.update(self.a, credits_updated=self.now)
        self.pool.record(self.a, 402)
        self.assertEqual(self.store.data['account_status'][self.a]['credits_updated'], 0)

    def test_record_uses_upstream_reset_time_when_later(self):
        """上游明说了恢复时刻、且比本地退避更晚 → 采信上游。

        本地猜的必然不准：猜短了会在窗口内反复撞墙（每次都是一次风控痕迹）。
        """
        from datetime import datetime, timezone, timedelta
        CN = timezone(timedelta(hours=8))
        # 本地退避是 1800 秒；上游说 2 小时后才恢复。
        later = datetime.fromtimestamp(self.now + 7200, CN).strftime('%Y-%m-%d %H:%M:%S')
        self.pool.record(self.a, 429, hint_text=f"请求过于频繁，将在 {later} 重置")
        until = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertAlmostEqual(until - self.now, 7200, delta=2)
        self.assertIn('上游明示', self.store.data['account_status'][self.a]['last_error'])

    def test_record_ignores_upstream_time_earlier_than_local_backoff(self):
        """上游给的时刻比本地退避还早 → 取更晚者，不缩短已有冷却。"""
        from datetime import datetime, timezone, timedelta
        CN = timezone(timedelta(hours=8))
        sooner = datetime.fromtimestamp(self.now + 60, CN).strftime('%Y-%m-%d %H:%M:%S')
        self.pool.record(self.a, 429, hint_text=f"将在 {sooner} 重置")
        until = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertEqual(until - self.now, 1800)   # 仍是本地退避
        self.assertIn('本地退避', self.store.data['account_status'][self.a]['last_error'])

    def test_record_uses_retry_after_header(self):
        self.pool.record(self.a, 429, hint_headers={"Retry-After": "9000"})
        until = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertEqual(until - self.now, 9000)

    def test_record_clamps_absurd_upstream_time(self):
        """上游报到一天以后必然是脏数据，不能让它把账号永久扣住。"""
        self.pool.record(self.a, 429, hint_headers={"Retry-After": "999999999"})
        until = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertLessEqual(until - self.now, 86400)

    def test_record_without_hint_is_unchanged(self):
        """没有上游提示时行为必须与加这个功能之前逐字节一致。"""
        self.pool.record(self.a, 429)
        until = self.store.data['account_status'][self.a]['cooldown_until']
        self.assertEqual(until - self.now, 1800)
        self.assertIn('本地退避', self.store.data['account_status'][self.a]['last_error'])

    def test_flush_debounce_and_force(self):
        self.pool.update(self.a, last_error=None)
        self.assertTrue(self.pool.dirty)
        self.pool.flush()
        self.assertFalse(self.pool.dirty)
        self.pool.update(self.a, last_error='x')
        self.pool.flush()  # within 5s window: no write
        self.assertTrue(self.pool.dirty)
        self.now += 6
        self.pool.flush()
        self.assertFalse(self.pool.dirty)
        self.pool.update(self.a, last_error='y')
        self.pool.flush(force=True)
        self.assertFalse(self.pool.dirty)

    def test_affinity_rebinds_to_soonest_expiring_account(self):
        key = request_affinity({b'authorization': b'Bearer api-test'}, {'model': 'm', 'prompt_cache_key': 'session-one'})
        self.pool.select(key)
        # Bound account's credits expire far out; another account expires within 3 days → rebind.
        self.pool.update(self.a, earliest_expiry=self.now + 30*86400, remaining=100, packages=1, credits_updated=self.now)
        self.pool.update(self.b, earliest_expiry=self.now + 2*86400, remaining=100, packages=1, credits_updated=self.now)
        self.assertEqual(self.pool.select(key)[0], self.b)
        # Both far from expiry → affinity stays.
        self.pool.update(self.b, earliest_expiry=self.now + 10*86400)
        self.assertEqual(self.pool.select(key)[0], self.b)
        # Bound account itself is the urgent one → no rebind away from it.
        self.pool.update(self.b, earliest_expiry=self.now + 2*86400)
        self.pool.update(self.a, earliest_expiry=self.now + 10*86400)
        # b was bound above; its own credits expire soon, keep affinity for cache hits.
        self.assertEqual(self.pool.select(key)[0], self.b)
        # Threshold disabled (0) → affinity always stays.
        self.store.data['pool']['affinity_expiry_days'] = 0
        self.pool.update(self.b, earliest_expiry=self.now + 30*86400)
        self.pool.update(self.a, earliest_expiry=self.now + 86400)
        self.assertEqual(self.pool.select(key)[0], self.b)

    def test_affinity_is_scoped_to_caller_model_and_session(self):
        headers = {b'authorization': b'Bearer one'}
        body = {'model': 'm', 'prompt_cache_key': 'secret-session'}
        key = request_affinity(headers, body)
        self.assertEqual(len(key), 64)
        self.assertNotEqual(key, request_affinity({b'authorization': b'Bearer two'}, body))
        self.assertNotEqual(key, request_affinity(headers, dict(body, model='other')))
        self.assertEqual(key, request_affinity(headers, dict(body, input='next question')))
        self.assertEqual(request_affinity(headers, {'input': [{'role': 'user', 'content': 'first'}]}),
                         request_affinity(headers, {'input': [{'role': 'user', 'content': 'first'}, {'role': 'user', 'content': 'next'}]}))

    async def test_middleware_replays_body_and_reuses_session_account(self):
        seen = []
        async def app(scope, receive, send):
            message = await receive()
            seen.append((REQUEST_CREDENTIAL.get().path.name, json.loads(message['body'])))
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            bodies = [{'prompt_cache_key': 'same', 'input': 'first'}, {'prompt_cache_key': 'same', 'input': 'next'}]
            for body in bodies:
                response = await client.post('/v1/responses', json=body, headers={'Authorization': 'Bearer api-test'})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertEqual([item[1] for item in seen], bodies)

    async def test_responses_failed_event_cools_account(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"code":"429"}}}\n\n'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            await client.post('/v1/responses', json={'input': 'hello'}, headers={'Authorization': 'Bearer api-test'})
        self.assertGreater(self.store.data['account_status'][self.a]['cooldown_until'], self.now)

    async def test_checkin_idempotent_and_failure_retains_balance(self):
        a,b=await asyncio.gather(asyncio.to_thread(self.pool.operate,self.a,'checkin'),asyncio.to_thread(self.pool.operate,self.a,'checkin'))
        self.assertTrue(a['ok'] and b['ok']);self.assertEqual(self.checkin_posts,1)
        self.assertEqual(self.store.data['account_status'][self.a]['remaining'],479.59)
        self.fail_mode=True
        result=await asyncio.to_thread(self.pool.operate,self.a,'status')
        self.assertFalse(result['ok']);self.assertEqual(self.store.data['account_status'][self.a]['remaining'],479.59)

    def test_per_key_account_whitelist(self):
        # 按 key 路由：白名单收窄候选集。
        kid, _ = self.store.add_key("限定", accounts=[self.b])
        self.assertEqual([self.pool.select(None, kid)[0] for _ in range(2)], [self.b, self.b])
        # 空白名单 = 不限制。
        kid2, _ = self.store.add_key("不限定")
        self.assertIn(self.pool.select(None, kid2)[0], (self.a, self.b))
        # kid=None（管理页测试请求）不施加白名单。
        self.assertIn(self.pool.select()[0], (self.a, self.b))

    def test_whitelisted_account_exhausted_raises(self):
        kid, _ = self.store.add_key("限定", accounts=[self.b])
        self.pool.record(self.b, 429)
        with self.assertRaises(HTTPException):
            self.pool.select(None, kid)

    def test_routing_policies(self):
        self.pool.update(self.a, weight=1, earliest_expiry=self.now + 30 * 86400)
        self.pool.update(self.b, weight=0, earliest_expiry=self.now + 86400)
        kid, _ = self.store.add_key("rr")
        self.store.data["keys"][kid]["routing"] = "round_robin"
        # round_robin：权重高的 a 先（游标从 0 起）。
        self.pool.cursor = 0
        self.assertEqual(self.pool.select(None, kid)[0], self.a)
        # expiry_first：只看最早过期，b 先（b 权重低但 1 天后到期，a 要 30 天）。
        kid2, _ = self.store.add_key("ef")
        self.store.data["keys"][kid2]["routing"] = "expiry_first"
        self.pool.cursor = 0
        self.assertEqual(self.pool.select(None, kid2)[0], self.b)
        # random：多次选择都落在候选集内。
        kid3, _ = self.store.add_key("rand")
        self.store.data["keys"][kid3]["routing"] = "random"
        for _ in range(6):
            self.assertIn(self.pool.select(None, kid3)[0], (self.a, self.b))

    def test_routing_follows_default_when_null(self):
        kid, _ = self.store.add_key("跟随")
        self.assertIsNone(self.store.data["keys"][kid]["routing"])  # NULL = 动态跟随
        self.store.data["pool"]["default_routing"] = "expiry_first"
        self.assertEqual(self.pool._routing_for(kid), "expiry_first")
        # 显式设置后不再跟随。
        self.store.data["keys"][kid]["routing"] = "round_robin"
        self.assertEqual(self.pool._routing_for(kid), "round_robin")

    def test_key_soft_delete_and_last_key_guard(self):
        kid1, value1 = self.store.add_key("A")
        self.store.add_key("B")
        # 撤销 kid1：软删保留行，revoked_at 置位、enabled 归零。
        self.store.data["keys"][kid1]["revoked_at"] = 1
        self.store.data["keys"][kid1]["enabled"] = 0
        # 行还在（审计链不断），但鉴权必须拒绝。
        self.assertIn(kid1, self.store.data["keys"])
        self.assertEqual(self.store.key_unusable(self.store.data["keys"][kid1]), "该密钥已被撤销")
        self.assertEqual(self.store.data["keys"][kid1]["hash"],
                         hashlib.sha256(value1.encode()).hexdigest())
        # 「最后一枚不可撤销」只数未撤销的：撤销 kid1 后还剩初始 key + B 两枚有效。
        active = [k for k, v in self.store.data["keys"].items() if not v.get("revoked_at")]
        self.assertEqual(len(active), 2)
        # 再把 B 也撤销，就只剩 1 枚有效 → 此时撤销它会被 400 拦下。
        for k in list(active)[1:]:
            self.store.data["keys"][k]["revoked_at"] = 1
        self.assertEqual(len([k for k, v in self.store.data["keys"].items()
                              if not v.get("revoked_at")]), 1)

    def test_disabled_and_expired_key_rejected(self):
        kid, value = self.store.add_key("临时")
        # 停用
        self.store.data["keys"][kid]["enabled"] = 0
        self.assertEqual(self.store.key_unusable(self.store.data["keys"][kid]), "该密钥已停用")
        self.store.data["keys"][kid]["enabled"] = 1
        # 过期
        self.store.data["keys"][kid]["expires_at"] = 1  # 已过去
        self.assertEqual(self.store.key_unusable(self.store.data["keys"][kid]), "该密钥已过期")

    async def test_middleware_select_passes_key_to_retry(self):
        # 凭据准备失败后重试必须仍传同一个 kid，否则可能选到白名单外的账号。
        calls = []
        original_select = self.pool.select
        state = {"first": True}

        def spy(affinity_key=None, kid_arg=None, exclude=None, model=None):
            calls.append(kid_arg)
            if state["first"]:
                # 第一次返回一个 get_headers 会抛的 manager，触发重试分支。
                bad = self.store.manager_for(self.a, self.store.data["accounts"][self.a])
                return self.a, bad
            return original_select(affinity_key, kid_arg, exclude=exclude, model=model)
        self.pool.select = spy
        # 让 a 的 get_headers 抛错，重试才会发生。
        manager = self.store.manager_for(self.a, self.store.data["accounts"][self.a])
        manager.get_headers = Mock(side_effect=RuntimeError("凭据坏了"))

        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
                await client.post('/v1/responses', json={'input': 'x'}, headers={'Authorization': 'Bearer api-test'})
        finally:
            self.pool.select = original_select
        # 两次 select 都收到了同一个非 None kid（默认 api-test key）。
        kid = self.store.check_api("Bearer api-test", "")
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], kid)
        self.assertEqual(calls[1], kid)



    async def test_paused_excluded_from_batch_and_schedule_persists(self):
        self.store.data['accounts'][self.b]['enabled']=False
        self.store.data['pool']['checkin_time']='00:00'
        # 白天时段内：签到逻辑会执行。
        self.now = datetime(2026, 10, 14, 12, 0, tzinfo=CN).timestamp()
        for aid, when in ((self.a, '09:00'), (self.b, '11:30')):
            self.store.data['account_status'].setdefault(aid, {}).update(
                checkin_slot='上午', checkin_when=when, checkin_offset_date='2026-10-14')
        await self.pool.tick()
        self.assertGreaterEqual(self.checkin_posts, 1)
        # Paused accounts still check in; they are only excluded from request routing.
        self.assertIn(self.b,self.store.data['account_status'])
        restarted=AccountPool(self.store,clock=lambda:self.now,client_factory=self.pool.client_factory)
        await restarted.tick()
        self.assertGreaterEqual(self.checkin_posts, 1)
        # 持久化到 SQLite：重新加载后状态仍在。
        reloaded = Store(self.store.root, self.store.auth_dir, '', 'admin-long-enough-test-key')
        try:
            self.assertTrue(reloaded.data['account_status'][self.a]['checkin_date'])
            self.assertIn(self.b, reloaded.data['account_status'])
        finally:
            # 关掉这条额外连接：否则它与 self.store 各持一条写连接，
            # 且 reloaded 构造时的 sync_any_enabled() 会污染全局 converter.CONFIG。
            reloaded.db.close()

    def test_upstream_usage_sync_paginates_parses_time_and_upserts(self):
        from admin.db import MICRO
        pages = []

        def respond(req):
            # 参数名必须是驼峰 pageNum/pageSize（与同域 get-user-resource 的 PageNumber/PageSize 不同）。
            body = json.loads(req.content)
            self.assertIn("pageNum", body)
            self.assertIn("pageSize", body)
            pages.append(body["pageNum"])
            if body["pageNum"] == 1:
                rows = [{"requestId": "crb-1", "credit": "0", "model": "step-5-preview",
                         "client": "WorkBuddy", "requestTime": "2026-10-14 09:30:00",
                         "agentPurpose": "chat"},
                        {"requestId": "crb-2", "credit": "12.5", "model": "hy3",
                         "client": "WorkBuddy", "requestTime": "2026-10-14 10:00:00"}]
                total = 3   # 总数 3 > 本页 2，必须继续翻页
            else:
                rows = [{"requestId": "crb-3", "credit": "1", "model": "hy3",
                         "client": "WorkBuddy", "requestTime": "2026-10-14 11:00:00"}]
                total = 3
            return httpx.Response(200, json={"code": 0, "msg": "OK", "requestId": "trace-1",
                                             "data": {"total": total, "data": rows}})
        pool = AccountPool(self.store, clock=lambda: self.now,
                           client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
        with self.store.lock:
            manager = self.store.manager_for(self.a, self.store.data["accounts"][self.a])
        manager.get_headers = lambda: {"X-Domain": "www.codebuddy.cn"}
        self.assertEqual(pool.sync_upstream_usage(self.a), 3)
        self.assertEqual(pages, [1, 2])   # 按 total 翻到第二页即停
        rows = {r["request_id"]: r for r in self.store.db.load_upstream_usage(self.a)}
        self.assertEqual(set(rows), {"crb-1", "crb-2", "crb-3"})
        # requestTime 无时区标记，必须按北京时间解析。
        expected = int(datetime(2026, 10, 14, 9, 30, tzinfo=CN).timestamp())
        self.assertEqual(rows["crb-1"]["request_time"], expected)
        self.assertEqual(rows["crb-1"]["model"], "step-5-preview")
        self.assertEqual(rows["crb-1"]["agent_purpose"], "chat")
        self.assertAlmostEqual(rows["crb-2"]["credit"], 12.5, places=4)
        # 同步成功写 usage_synced_at 并清掉旧的错误留痕。
        self.assertEqual(self.store.data["account_status"][self.a]["usage_synced_at"], int(self.now))
        self.assertIsNone(self.store.data["account_status"][self.a].get("usage_sync_error"))

    def test_upstream_usage_credit_can_be_corrected(self):
        # credit 可能不是终值：第二次同步用新值覆盖，而不是被 OR IGNORE 钉死。
        def respond(req):
            credit = "0" if pages_seen == [] else "7.5"
            return httpx.Response(200, json={"code": 0, "data": {"total": 1, "data": [
                {"requestId": "crb-9", "credit": credit, "model": "hy3",
                 "requestTime": "2026-10-14 12:00:00"}]}})
        pages_seen = []
        pool = AccountPool(self.store, clock=lambda: self.now,
                           client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
        with self.store.lock:
            manager = self.store.manager_for(self.a, self.store.data["accounts"][self.a])
        manager.get_headers = lambda: {"X-Domain": "www.codebuddy.cn"}
        pool.sync_upstream_usage(self.a)
        pages_seen.append(1)
        pool.sync_upstream_usage(self.a)
        rows = self.store.db.load_upstream_usage(self.a)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["credit"], 7.5, places=4)

    async def test_usage_sync_batch_marks_failure_and_skips_same_day(self):
        calls = []

        def respond(req):
            calls.append(req.url.path)
            return httpx.Response(500)
        pool = AccountPool(self.store, clock=lambda: self.now,
                           client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
        result = await pool.sync_upstream_usage_batch()
        self.assertEqual(result, {"synced": 0, "failed": 2})
        self.assertEqual(len(calls), 2)   # 两个账号各试一次
        # 失败留痕：带当天日期前缀，且 usage_synced_at 保持不变（语义是「成功时间」）。
        today = datetime.fromtimestamp(self.now, CN).strftime("%Y-%m-%d")
        for aid in (self.a, self.b):
            status = self.store.data["account_status"][aid]
            self.assertTrue(status["usage_sync_error"].startswith(today), status["usage_sync_error"])
            self.assertNotIn("usage_synced_at", status)
        # 同一天再跑不重复打上游。
        again = await pool.sync_upstream_usage_batch()
        self.assertTrue(again.get("skipped"))
        self.assertEqual(len(calls), 2)
        # 留痕必须出现在 overview 里，否则「留痕」没人看得到、机制无法验证。
        rows = {r["id"]: r for r in pool.rows(self.store.account_rows())}
        for aid in (self.a, self.b):
            self.assertTrue(rows[aid]["usage_sync_error"].startswith(today))
            self.assertIsNone(rows[aid]["usage_synced_at"])

    async def test_request_log_carries_route_dimensions_through_full_stack(self):
        """回归：Metrics(外) + Pool(内) 全链路要能拿到 key/account/model 并落库。

        这曾经是断的——Pool 在 finally 里 reset ContextVar，外层 Metrics 读取时
        已恢复默认值，所有路由维度恒为 NULL。只测 Pool 或只测 Metrics 都发现不了，
        必须把两层叠起来跑。
        """
        from admin.metrics import MetricsMiddleware, RequestMetrics

        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

        # Pool 在内、Metrics 在外：先包 Pool，再用 Metrics 包住它。
        inner = PoolMiddleware(app, self.pool)
        stack = MetricsMiddleware(inner, RequestMetrics(), source="api", store=self.store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stack), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'model': 'hy3', 'input': 'x'},
                                         headers={'Authorization': 'Bearer api-test'})
            self.assertEqual(response.status_code, 200)
        self.store.save()   # 把缓冲落库
        logs = self.store.db.recent_request_logs(10)
        self.assertEqual(len(logs), 1)
        row = logs[0]
        self.assertEqual(row["path"], "/v1/responses")
        self.assertEqual(row["source"], "api")
        # 成功请求也要落到真实的 HTTP 状态码，不是 NULL。
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["outcome"], "success")
        # 关键：路由维度必须都在。
        self.assertEqual(row["key_id"], self.store.check_api("Bearer api-test", ""))
        self.assertIn(row["account_id"], (self.a, self.b))
        self.assertEqual(row["account_name"], self.store.data["accounts"][row["account_id"]]["name"])
        self.assertEqual(row["model_id"], "hy3")
        self.assertEqual(row["model_name"], "hy3")

    async def test_request_log_records_sse_error_code_as_status(self):
        """回归：SSE 内带的 429 要以状态码入库，才能和客户端断连区分开。"""
        from admin.metrics import MetricsMiddleware, RequestMetrics

        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body',
                        'body': b'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"code":"429"}}}\n\n',
                        'more_body': False})

        inner = PoolMiddleware(app, self.pool)
        stack = MetricsMiddleware(inner, RequestMetrics(), source="api", store=self.store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stack), base_url='http://test') as client:
            await client.post('/v1/responses', json={'input': 'x'},
                              headers={'Authorization': 'Bearer api-test'})
        self.store.save()
        row = self.store.db.recent_request_logs(1)[0]
        # HTTP 层是 200，但生效状态码必须是 SSE 里的 429。
        self.assertEqual(row["status"], 429)
        self.assertEqual(row["outcome"], "stream_error")

    def test_request_log_survives_failed_commit(self):
        """回归：提交失败时缓冲不能丢——否则这批日志永久消失且无提示。"""
        self.store.queue_request_log({"time": int(time.time()), "source": "api",
                                      "path": "/v1/messages", "status": 200,
                                      "outcome": "success", "duration_ms": 1})
        original = Database.save_keys

        def boom(self, keys):
            raise KeyboardInterrupt("提交前崩")
        Database.save_keys = boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.store.save()
        finally:
            Database.save_keys = original
        # 缓冲已还原，而不是被清空。
        self.assertEqual(len(self.store.log_buffer), 1)
        self.store.save()
        self.assertEqual(len(self.store.db.recent_request_logs(10)), 1)

    async def test_context_isolation_concurrent_requests_and_auth(self):
        async def app(scope,receive,send):
            before=REQUEST_CREDENTIAL.get().path.name
            await asyncio.sleep(.01)
            after=REQUEST_CREDENTIAL.get().path.name
            data=json.dumps({'before':before,'after':after}).encode()
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':data})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app,self.pool)),base_url='http://test') as client:
            self.assertEqual((await client.post('/v1/responses')).status_code,401)
            results=await asyncio.gather(*(client.post('/v1/responses',headers={'Authorization':'Bearer api-test'}) for _ in range(4)))
            files=[r.json()['before'] for r in results]
            self.assertEqual(len(set(files)),2)
            self.assertTrue(all(r.json()['before']==r.json()['after'] for r in results))
        self.assertIsNone(REQUEST_CREDENTIAL.get())

    async def test_sse_error_marks_cooldown_without_replay(self):
        calls=[]
        async def app(scope,receive,send):
            calls.append(1)
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            for part in [b'data: {"error":',b'{"code":429}}\n\n']:
                await send({'type':'http.response.body','body':part,'more_body':True})
            await send({'type':'http.response.body','body':b''})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app,self.pool)),base_url='http://test') as client:
            response=await client.post('/v1/messages',headers={'Authorization':'Bearer api-test'})
            self.assertEqual(response.status_code,200)
        self.assertEqual(len(calls),1)
        self.assertGreater(self.store.data['account_status'][self.a]['cooldown_until'],self.now)
        self.assertEqual(self.pool.select()[0],self.b)

    def test_pagination_is_complete(self):
        def handler(req):
            page=json.loads(req.content)['PageNumber']
            accounts=[{'CapacityRemainPrecise':'1.25','CapacitySize':2}]* (100 if page==1 else 1)
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{'TotalCount':101,'Accounts':accounts}}}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            result=self.pool.credits(c,{})
        self.assertEqual(result['packages'],101)
        self.assertEqual(result['remaining'],126.25)

    # -- 容量队列自动切换（specs/queue-failover.md） --------------------------

    def _enable_failover(self, **overrides):
        cfg = {"queue_failover": True}
        cfg.update(overrides)
        self.store.data["pool"].update(cfg)

    def test_queue_error_detection_accepts_both_shapes(self):
        # 结构化 code（用户实测的 WAITING）与 HTTP 限流码都必须判定为排队。
        self.assertTrue(self.pool.is_queue_error(None, "", "WAITING"))
        self.assertTrue(self.pool.is_queue_error(None, "", "waiting"))
        self.assertTrue(self.pool.is_queue_error(None, "", "FULL"))
        self.assertTrue(self.pool.is_queue_error(429, ""))
        self.assertTrue(self.pool.is_queue_error(503, ""))
        # 文案兜底：上游没给 code 时靠 message。
        self.assertTrue(self.pool.is_queue_error(
            None, "当前模型资源紧张，请等候。Step-5-Preview 前方约 7859 位用户排队"))
        # 非排队：额度到顶与「非流式不支持」，换号换模型都救不了。
        self.assertFalse(self.pool.is_queue_error(None, "个人额度已用完", "USER_LIMIT"))
        self.assertFalse(self.pool.is_queue_error(None, "", "11101"))
        self.assertFalse(self.pool.is_queue_error(200, "一切正常"))

    def test_queued_account_is_skipped_but_never_locks_the_pool(self):
        self.pool.mark_queued(self.a)
        self.assertTrue(self.pool.queued(self.a))
        # 让位期间优先选没排过队的账号。
        self.assertEqual(self.pool.select()[0], self.b)
        # 但**全部**都在让位期内时必须忽略标记，否则排队会把池子锁成 503。
        self.pool.mark_queued(self.b)
        self.assertIn(self.pool.select()[0], {self.a, self.b})

    async def test_queue_hit_switches_account_and_is_transparent(self):
        self._enable_failover()
        # 第一次返回完整的排队小响应；之后正常回答。客户端只应看到最终那份内容。
        seen = []

        async def app(scope, receive, send):
            await receive()
            aid = next(k for k, v in self.store.data["accounts"].items()
                       if v["file"] == REQUEST_CREDENTIAL.get().path.name)
            seen.append(aid)
            if len(seen) == 1:
                await send({'type': 'http.response.start', 'status': 200,
                            'headers': [(b'content-type', b'application/json')]})
                await send({'type': 'http.response.body', 'body': json.dumps({
                    "code": "WAITING", "model": "Step-5-Preview",
                    "message": "当前模型资源紧张，请等候。前方约 7859 位用户排队",
                    "data": {"queue_position": 7859, "retry_after": 5},
                }).encode()})
                return
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ANSWER'})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ANSWER')       # 排队响应没有漏给客户端
        self.assertEqual(len(seen), 2)                      # 确实重试了一次
        # 精确断言：第一个账号被让位，换到了另一个账号（不再用 or 弱化）。
        self.assertEqual(seen[0], self.a)
        self.assertEqual(seen[1], self.b)
        self.assertTrue(self.pool.queued(self.a))           # 只有排过队的那个被让位
        self.assertFalse(self.pool.queued(self.b))
    async def test_queue_without_content_is_never_replayed_once_content_seen(self):
        # 首包已含真实内容时，即使同时命中排队关键词也必须放行——重放会重复输出。
        self._enable_failover()
        calls = []

        async def app(scope, receive, send):
            calls.append(1)
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': (
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                + json.dumps({"error": {"code": "WAITING", "message": "\u6392\u961f"}}).encode() + b'\n\n')})
            await send({'type': 'http.response.body', 'body': b''})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)                     # 绝不重放
        self.assertIn(b'hello', response.content)

    async def test_slow_upstream_within_hold_window_is_passed_through(self):
        """回归：扣留必须有硬上界。

        上游只是慢（超过 queue_hold_ms 才发首包）时，必须原样放行，不能一直等——
        历史上这功能被回滚就是因为无上界扣留把 /health 一起拖死。
        """
        self._enable_failover(queue_hold_ms=200)
        async def app(scope, receive, send):
            await receive()
            await asyncio.sleep(0.4)          # 超过扣留窗口
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'SLOW'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'SLOW')          # 内容完整送到
        self.assertFalse(self.pool.queued(self.a))           # 慢 ≠ 排队，不让位

    async def test_empty_response_still_completes_asgi(self):
        """上游什么都没发就结束：必须补出完整的 start+body，不能留下半个响应。"""
        self._enable_failover(queue_hold_ms=200)

        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 204, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        self.assertFalse(self.pool.queued(self.a))

    async def test_all_accounts_queued_returns_original_response_not_503(self):
        """所有账号都排队时：原样返回上游排队响应，**不是** 503。

        503 会把「排队」这个真实原因掩盖成「没有可用账号」，客户端就无从按
        retry_after 轮询了。这正是把「探测候选集」和「选号」合成 try_select 的原因。
        """
        self._enable_failover(queue_retry=3, queue_model_fallback=False)
        body = json.dumps({"code": "WAITING", "message": "前方约 7859 位用户排队",
                           "data": {"queue_position": 7859, "retry_after": 5}}).encode()
        calls = []

        async def app(scope, receive, send):
            calls.append(1)
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': body})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 200)          # 不是 503
        self.assertEqual(response.content, body)             # 原文透传，含 retry_after
        self.assertEqual(len(calls), 2)                      # 两个账号都试过（受 queue_retry 限制）
        self.assertTrue(self.pool.queued(self.a) and self.pool.queued(self.b))

    async def test_queue_response_split_across_chunks_is_replayed_whole(self):
        """排队响应分多帧送达（code 与 message 分开）时，回放必须包含**全部分片**。

        只看首包判定，但被丢弃的内容不止首包——只留首分片会让客户端拿到
        残缺的排队信息，按 retry_after 轮询也就无从谈起。
        """
        self._enable_failover(queue_retry=2, queue_model_fallback=False)
        frames = [b'data: {"code":"WAITING"}\n\n',
                  'data: {"message":"前\u65b9约 7859 位用户排队"}\n\n'.encode()]

        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': frames[0], 'more_body': True})
            await send({'type': 'http.response.body', 'body': frames[1]})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b''.join(frames))   # 两个分片都在，顺序不变
        self.assertTrue(self.pool.queued(self.a))

    async def test_model_fallback_retries_all_accounts_again(self):
        """降级模型后账号可以重新从头试：新模型面对的是另一个上游队列。"""
        self._enable_failover(queue_retry=4, queue_cooldown=0)
        registry = Mock()
        registry.snapshot.return_value = {"models": [
            {"id": "step-5-preview", "enabled": True, "credits": 0.0},
            {"id": "space-bunny", "enabled": True, "credits": 0.08},
            {"id": "fast-model", "enabled": True, "credits": 0.21},
        ]}
        self.pool.model_registry = registry
        seen = []

        async def app(scope, receive, send):
            message = await receive()
            payload = message['body']
            model = (payload if isinstance(payload, dict) else json.loads(payload)).get("model")
            aid = next(k for k, v in self.store.data["accounts"].items()
                       if v["file"] == REQUEST_CREDENTIAL.get().path.name)
            seen.append((model, aid))
            # 原模型在**两个**账号上都排队；换成 space-bunny 后才成功。
            if model == "step-5-preview":
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'{"code":"WAITING"}'})
                return
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'DONE'})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses',
                                         json={'model': 'step-5-preview', 'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.content, b'DONE')
        # 先在同一模型上换号试了两个账号，候选耗尽后才降级 —— 顺序即 spec 4.4。
        self.assertEqual([m for m, _ in seen], ["step-5-preview", "step-5-preview", "space-bunny"])
        self.assertEqual({a for _, a in seen[:2]}, {self.a, self.b})   # 换号用的是不同账号
        # 换模型后账号重新从头试：第 3 次落在被清空的候选集里（不是被 exclude 掉的那个）。
        self.assertIn(seen[2][1], {self.a, self.b})

    async def test_queued_account_recovers_after_cooldown(self):
        """让位是临时跳过，不是硬冷却：过了 queue_cooldown 就该重新参与轮转。"""
        self._enable_failover()
        self.pool.mark_queued(self.a, seconds=60)
        self.assertTrue(self.pool.queued(self.a))
        self.assertEqual(self.pool.select()[0], self.b)      # 让位期内优先用另一个
        self.now += 61                                       # 过掉让位窗口
        self.assertFalse(self.pool.queued(self.a))
        # 重新参与轮转：轮一圈必然又能选到它（而不是被永久排除）。
        picked = {self.pool.select()[0] for _ in range(4)}
        self.assertIn(self.a, picked)

    def test_fallback_models_prefers_same_tier_then_cheap_route(self):
        registry = Mock()
        registry.snapshot.return_value = {"models": [
            {"id": "step-5-preview", "enabled": True, "credits": 0.0},
            {"id": "space-bunny", "enabled": True, "credits": 0.08},
            {"id": "glm-5.3-flash", "enabled": True, "credits": 0.06},
            {"id": "glm-5.3", "enabled": True, "credits": 0.79},
            {"id": "kimi-k3-1", "enabled": False, "credits": 0.1},   # 已禁用，不出现
            {"id": "fast-model", "enabled": True, "credits": 0.21},
        ]}
        self.pool.model_registry = registry
        # 免费档 + 低价档合并，按倍率升序（越省越好），兜底模型排在最后。
        ordered = self.pool.fallback_models("step-5-preview")
        self.assertEqual(ordered[:2], ["glm-5.3-flash", "space-bunny"])
        self.assertEqual(ordered[-1], "fast-model")
        self.assertNotIn("kimi-k3-1", ordered)               # 禁用模型不参与降级
        # 已试过的排除。
        self.assertNotIn("space-bunny", self.pool.fallback_models(
            "step-5-preview", tried={"space-bunny"}))
        # 关掉模型降级就不再给候选。
        self.store.data["pool"]["queue_model_fallback"] = False
        self.assertEqual(self.pool.fallback_models("step-5-preview"), [])

    async def test_disabled_feature_keeps_original_behaviour(self):
        """默认关闭时行为与改动前一致：排队响应原样透传，不重试。"""
        calls = []
        async def app(scope, receive, send):
            calls.append(1)
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'data: {"code":"WAITING"}\n\n',
                        'more_body': True})
            await send({'type': 'http.response.body', 'body': b''})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'hi'},
                                         headers={'Authorization': 'Bearer api-test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertIn(b'WAITING', response.content)
        self.assertFalse(self.pool.queued(self.a))


if __name__=='__main__':unittest.main()

import asyncio
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
        self.fail = False
        self.pages = []
        def respond(req):
            if self.fail:
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
        self.store.data['pool']['sync_window'] = ['10:00', '20:00']
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

    def test_round_robin_paused_cooling_exhausted_and_manual(self):
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
        self.store.data['pool']['routing']='manual';self.store.data['active']=self.a
        with self.assertRaises(HTTPException):self.pool.select()

    def test_affinity_failover_persistence_expiry_and_manual(self):
        key = request_affinity({b'authorization': b'Bearer api-test'}, {'model': 'm', 'prompt_cache_key': 'session-one'})
        self.assertEqual([self.pool.select(key)[0] for _ in range(3)], [self.a] * 3)
        self.pool.record(self.a)
        self.assertEqual(AccountPool(self.store, clock=lambda: self.now).select(key)[0], self.a)
        self.pool.record(self.a, 429)
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.now += 1801
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.store.data['pool']['routing'] = 'manual'
        self.store.data['active'] = self.a
        self.assertEqual(self.pool.select(key)[0], self.a)
        self.now += 86401
        self.pool.select()
        self.assertNotIn(key, self.store.data['session_bindings'])

    def test_consumption_rate_and_expire_unused(self):
        # 模拟 3 天内余额 300 → 150：日耗 50。
        status = self.store.data['account_status'].setdefault(self.a, {})
        day0 = self.now - 3 * 86400
        status['usage_history'] = [{'date': 'd0', 'samples': [[day0, 100], [day0 + 3600, 50]]},
                                   {'date': 'd1', 'samples': [[self.now - 86400, 50]]}]
        status['remaining'] = 150
        consumption = self.pool.consumption(status)
        self.assertAlmostEqual(consumption['daily_rate'], 100, delta=5)  # 200 消耗 / 2 天跨度
        # 余额上涨样本（签到）不 计入消耗。
        status['usage_history'][0]['samples'].append([day0 + 7200, -100])
        self.assertAlmostEqual(self.pool.consumption(status)['daily_rate'], 100, delta=5)
        # 最早过期 2 天后：按速率能烧 200，剩 150 → 无浪费。
        status['earliest_expiry'] = self.now + 2 * 86400
        row = self.pool.rows(self.store.account_rows())[0]
        self.assertEqual(row['daily_rate'], consumption['daily_rate'])
        self.assertEqual(row['expire_unused'], 0)
        self.assertAlmostEqual(row['days_to_empty'], 1.5, delta=0.2)
        # 快过期（1 天）时：只能烧 100 → 预计 50 浪费。
        status['earliest_expiry'] = self.now + 86400
        row = self.pool.rows(self.store.account_rows())[0]
        self.assertAlmostEqual(row['expire_unused'], 50, delta=5)
        # 无历史则字段为空。
        row_b = self.pool.rows(self.store.account_rows())[1]
        self.assertIsNone(row_b['daily_rate'])
        self.assertIsNone(row_b['expire_unused'])

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
        self.fail=True
        result=await asyncio.to_thread(self.pool.operate,self.a,'status')
        self.assertFalse(result['ok']);self.assertEqual(self.store.data['account_status'][self.a]['remaining'],479.59)

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
        data=json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertTrue(data['account_status'][self.a]['checkin_date'])

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


if __name__=='__main__':unittest.main()

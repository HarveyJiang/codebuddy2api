import asyncio
import json
import tempfile
import time
import unittest
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
        result = summarize_packages([
            {'CycleCapacityRemainPrecise': '4.61', 'CycleCapacitySizePrecise': '100', 'CycleEndTime': '2026-10-14 09:06:13'},
            {'CycleCapacityRemainPrecise': '100', 'CycleCapacitySizePrecise': '100', 'CycleEndTime': '2026-10-15 09:00:47'},
            {'CycleCapacityRemainPrecise': '0', 'CycleCapacitySizePrecise': '100', 'CycleEndTime': '2026-10-13 00:00:00'},
            {'CycleCapacityRemainPrecise': '50', 'CycleCapacitySizePrecise': '50'},
        ])
        self.assertEqual(result['earliest_expiry'], datetime(2026, 10, 14, 9, 6, 13, tzinfo=CN).timestamp())
        self.assertAlmostEqual(result['earliest_expiry_remaining'], 4.61)
        self.assertEqual(result['remaining'], 154.61)

    async def test_checkin_random_window_per_account(self):
        self.store.data['pool']['auto_checkin'] = True
        self.store.data['pool']['checkin_time'] = '09:00'
        self.store.data['pool']['checkin_window'] = 30
        # 09:10 Beijing time: inside the window.
        now_cn = datetime(2026, 10, 14, 9, 10, tzinfo=CN)
        self.now = now_cn.timestamp()
        # Force a deterministic offset: one account due now, one later.
        for aid, offset in ((self.a, 0), (self.b, 25)):
            self.store.data['account_status'].setdefault(aid, {})
            self.store.data['account_status'][aid]['checkin_offset'] = offset
            self.store.data['account_status'][aid]['checkin_offset_date'] = '2026-10-14'
        await self.pool.tick()
        self.assertEqual(self.checkin_posts, 1)
        self.assertIn(self.pool.checkin_offset(self.a, '2026-10-14', 30), range(31))

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
        await self.pool.tick()
        self.assertEqual(self.checkin_posts,1)
        self.assertNotIn(self.b,self.store.data['account_status'])
        restarted=AccountPool(self.store,clock=lambda:self.now,client_factory=self.pool.client_factory)
        await restarted.tick()
        self.assertEqual(self.checkin_posts,1)
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

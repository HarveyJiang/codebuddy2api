import asyncio
import json
import unittest

from admin.metrics import MetricsMiddleware, RequestMetrics


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, status=200, chunks=None, stream=False, source="api", path="/v1/responses", crash=False):
        metrics = RequestMetrics()
        # 记录每次 finish 的 outcome：recent 现在读 request_logs（需要 store），
        # 这里直接断言计数中间件算出的结果，不依赖内存队列。
        finished = []
        original_finish = metrics.finish

        def spy(*args):
            original_finish(*args)
            finished.append({"outcome": args[5], "status": args[2], "source": args[1]})
        metrics.finish = spy
        chunks = chunks if chunks is not None else [b'{"ok":true}']
        sent = []
        async def app(scope, receive, send):
            self.assertEqual(metrics.snapshot()['in_flight'], int(path == '/v1/responses'))
            await send({'type':'http.response.start','status':status,'headers':[(b'content-type', b'text/event-stream' if stream else b'application/json')]})
            for chunk in chunks:
                await send({'type':'http.response.body','body':chunk,'more_body':True})
            if crash:
                raise RuntimeError('interrupted')
            await send({'type':'http.response.body','body':b'','more_body':False})
        async def receive():
            return {'type':'http.request','body':b''}
        async def send(message):
            sent.append(message)
        scope={'type':'http','method':'POST','path':path}
        middleware=MetricsMiddleware(app,metrics,source)
        if crash:
            with self.assertRaises(RuntimeError): await middleware(scope,receive,send)
        else:
            await middleware(scope,receive,send)
        self.assertEqual(b''.join(m.get('body',b'') for m in sent), b''.join(chunks))
        return {**metrics.snapshot(), "requests": finished}

    async def test_success_and_http_error(self):
        s=await self.run_request(); self.assertEqual(s['completed'],1);self.assertEqual(s['success_rate'],100);self.assertEqual(s['in_flight'],0)
        s=await self.run_request(status=401);self.assertEqual(s['failed'],1);self.assertEqual(s['http_success_rate'],0)

    async def test_stream_error_with_200_is_not_success(self):
        s=await self.run_request(stream=True,chunks=[b'data: {"err',b'or":{"code":429}}\n\ndata: [DONE]\n\n'])
        self.assertEqual(s['http_success_rate'],100);self.assertEqual(s['success_rate'],0)
        self.assertEqual(s['requests'][0]['outcome'],'stream_error')

    async def test_complete_and_incomplete_stream(self):
        s=await self.run_request(stream=True,chunks=[b'data: {"type":"response.completed"}\n\n'])
        self.assertEqual(s['success_rate'],100)
        s=await self.run_request(stream=True,chunks=[b'data: {"delta":"text"}\n\n'])
        self.assertEqual(s['success_rate'],0);self.assertEqual(s['requests'][0]['outcome'],'interrupted')
        s=await self.run_request(stream=True,chunks=[b'data: {"type":"response.completed","response":{"status":"failed"}}\n'])
        self.assertEqual(s['failed'],1)

    async def test_exception_and_excluded_paths(self):
        s=await self.run_request(crash=True);self.assertEqual(s['failed'],1);self.assertEqual(s['in_flight'],0)
        s=await self.run_request(path='/admin/api/overview');self.assertEqual(s['completed'],0)
        s=await self.run_request(source='test');self.assertEqual(s['test_count'],1);self.assertEqual(s['api_count'],0)

    async def test_privacy_never_stores_payloads(self):
        s=await self.run_request(chunks=[b'{"secret":"do-not-store"}'])
        self.assertNotIn('do-not-store',json.dumps(s))
        metrics=RequestMetrics()
        for _ in range(110):
            metrics.begin();metrics.finish('/v1/messages','api',200,True,10,'success')
        s=metrics.snapshot();self.assertEqual(s['completed'],110);self.assertEqual(s['avg_duration_ms'],10)

    async def test_client_disconnect(self):
        metrics=RequestMetrics()
        finished=[]
        original_finish=metrics.finish
        metrics.finish=lambda *a: (original_finish(*a), finished.append(a[5]))
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            await receive()
        async def receive():return {'type':'http.disconnect'}
        async def send(m):pass
        await MetricsMiddleware(app,metrics)({'type':'http','method':'POST','path':'/v1/messages'},receive,send)
        s=metrics.snapshot();self.assertEqual(s['failed'],1);self.assertEqual(finished[0],'interrupted')

    async def test_token_detail_lands_in_request_log(self):
        # 用户 2026-10-19：请求日志要带 token 明细。PoolMiddleware 把归一化后的
        # tokens_detail 挂在 scope 上，metrics 层原样透传进 queue_request_log。
        from admin.pool import REQUEST_LOG_CONTEXT
        queued=[]
        class FakeStore:
            def queue_request_log(self, row): queued.append(row)
        metrics=RequestMetrics()
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{}','more_body':False})
        async def receive():return {'type':'http.request','body':b''}
        async def send(m):pass
        scope={'type':'http','method':'POST','path':'/v1/messages',
               REQUEST_LOG_CONTEXT:{'tokens_detail':{'in':100,'out':20,'cache_read':5,'cache_creation':1}}}
        await MetricsMiddleware(app,metrics,'api',store=FakeStore())(scope,receive,send)
        self.assertEqual((queued[0]['tokens_in'],queued[0]['tokens_out'],
                          queued[0]['tokens_cache_read'],queued[0]['tokens_cache_creation']),
                         (100,20,5,1))

    async def test_missing_token_detail_is_none(self):
        # 不经过池的路径（管理页测试等）没有 tokens_detail：四项落 None，不猜 0。
        from admin.pool import REQUEST_LOG_CONTEXT
        queued=[]
        class FakeStore:
            def queue_request_log(self, row): queued.append(row)
        metrics=RequestMetrics()
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{}','more_body':False})
        async def receive():return {'type':'http.request','body':b''}
        async def send(m):pass
        scope={'type':'http','method':'POST','path':'/v1/messages',REQUEST_LOG_CONTEXT:{}}
        await MetricsMiddleware(app,metrics,'api',store=FakeStore())(scope,receive,send)
        for k in ('tokens_in','tokens_out','tokens_cache_read','tokens_cache_creation'):
            self.assertIsNone(queued[0][k])

    # -- v22：首字延迟 + 来源 IP / UA -------------------------------------
    async def _logged_row(self, app, scope_extra=None):
        """跑一次请求并返回落库的那一行（store 用 FakeStore 接住）。"""
        queued=[]
        class FakeStore:
            def queue_request_log(self, row): queued.append(row)
        async def receive():return {'type':'http.request','body':b''}
        async def send(m):pass
        scope={'type':'http','method':'POST','path':'/v1/messages'}
        scope.update(scope_extra or {})
        await MetricsMiddleware(app,RequestMetrics(),'api',store=FakeStore())(scope,receive,send)
        return queued[0]

    async def test_first_token_ms_measures_first_body_chunk(self):
        # 首字 = 第一个非空响应体的时刻（客户端视角），不是总耗时。
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,
                        'headers':[(b'content-type',b'text/event-stream')]})
            await asyncio.sleep(0.05)
            await send({'type':'http.response.body','body':b'data: {"delta":"x"}\n','more_body':True})
            await send({'type':'http.response.body','body':b'data: [DONE]\n','more_body':False})
        row=await self._logged_row(app)
        self.assertIsNotNone(row['first_token_ms'])
        self.assertGreaterEqual(row['first_token_ms'], 40)   # 至少等过那个 sleep
        self.assertLessEqual(row['first_token_ms'], row['duration_ms'])

    async def test_first_token_ms_null_when_no_body(self):
        # 上游一个字节都没吐（失败/超时）：落 NULL，不写 0（0 = 瞬间出字）。
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':429,
                        'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'','more_body':False})
        row=await self._logged_row(app)
        self.assertIsNone(row['first_token_ms'])

    async def test_client_ip_prefers_x_forwarded_for(self):
        # 容器 + 反代部署：XFF 首段是真实客户端，优先于 scope["client"]。
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,
                        'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{}','more_body':False})
        row=await self._logged_row(app, {
            'client':('172.18.0.1', 51000),
            'headers':[(b'x-forwarded-for', b'203.0.113.9, 172.18.0.1'),
                       (b'user-agent', b'claude-cli/1.0.60')]})
        self.assertEqual(row['client_ip'], '203.0.113.9')
        self.assertEqual(row['user_agent'], 'claude-cli/1.0.60')

    async def test_client_ip_falls_back_to_scope_client(self):
        # 反代没透传 XFF：用 ASGI 的 client（uvicorn proxy_headers 已经处理过）。
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,
                        'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{}','more_body':False})
        row=await self._logged_row(app, {'client':('198.51.100.7', 40000), 'headers':[]})
        self.assertEqual(row['client_ip'], '198.51.100.7')
        self.assertIsNone(row['user_agent'])   # 客户端没发 UA：NULL，不是空串

    async def test_source_unknown_when_nothing_available(self):
        # 什么来源信息都没有（单测直接调中间件）：两列都是 NULL。
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,
                        'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{}','more_body':False})
        row=await self._logged_row(app)
        self.assertIsNone(row['client_ip'])
        self.assertIsNone(row['user_agent'])


if __name__=='__main__':unittest.main()

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


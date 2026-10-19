"""Bounded process-lifetime counters; never store prompts, responses or keys.

计数与内存 recent 仍是进程级的；request_logs 的落库在 MetricsMiddleware（单一观察点）
组装成一行后交给 Store 的批量缓冲，由 5 秒防抖统一写库。
"""
import json
import threading
import time

from admin.pool import REQUEST_LOG_CONTEXT

PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"}


class RequestMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = int(time.time())
        self.in_flight = self.total = self.success = self.http_success = 0
        self.duration_sum = 0
        self.api_count = self.test_count = 0

    def begin(self):
        with self.lock:
            self.in_flight += 1

    def finish(self, path, source, status, ok, duration, outcome):
        with self.lock:
            self.in_flight -= 1
            self.total += 1
            self.success += int(ok)
            self.http_success += int(status is not None and 200 <= status < 300)
            self.duration_sum += duration
            self.api_count += int(source == "api")
            self.test_count += int(source == "test")

    def snapshot(self):
        with self.lock:
            return {"started_at": self.started_at, "completed": self.total, "in_flight": self.in_flight,
                    "succeeded": self.success, "failed": self.total - self.success,
                    "success_rate": round(100*self.success/self.total, 1) if self.total else None,
                    "http_success_rate": round(100*self.http_success/self.total, 1) if self.total else None,
                    "avg_duration_ms": round(self.duration_sum/self.total) if self.total else None,
                    "api_count": self.api_count, "test_count": self.test_count}


class MetricsMiddleware:
    def __init__(self, app, metrics, source="api", store=None):
        self.app, self.metrics, self.source, self.store = app, metrics, source, store

    async def __call__(self, scope, receive, send):
        path = scope.get("path")
        if scope["type"] != "http" or scope["method"] != "POST" or path not in PATHS:
            return await self.app(scope, receive, send)
        start = time.monotonic()
        self.metrics.begin()
        status = None
        completed = failed = streaming = terminal = disconnected = False
        buffer = b""
        oversized_line = False

        def event(line):
            nonlocal failed, terminal
            if not line.startswith(b"data:"):
                return
            payload = line[5:].strip()
            if payload == b"[DONE]":
                terminal = True
                return
            try:
                value = json.loads(payload)
            except ValueError:
                return
            if not isinstance(value, dict):
                return
            typ = value.get("type")
            if value.get("error") or typ in ("error", "response.failed", "response.incomplete"):
                failed = True
            if typ in ("response.completed", "message_stop"):
                terminal = True
            response = value.get("response")
            if isinstance(response, dict) and (response.get("error") or response.get("status") in ("failed", "incomplete")):
                failed = True

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not completed:
                disconnected = True
            return message

        async def observed_send(message):
            nonlocal status, streaming, completed, buffer, failed, oversized_line
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = dict(message.get("headers", []))
                streaming = b"text/event-stream" in headers.get(b"content-type", b"").lower()
            elif message["type"] == "http.response.body":
                if streaming:
                    # Bound storage even if the upstream sends a huge unterminated line.
                    for fragment in message.get("body", b"").splitlines(keepends=True):
                        end = fragment.endswith(b"\n")
                        if not oversized_line and len(buffer) + len(fragment) <= 65536:
                            buffer += fragment
                        else:
                            oversized_line = True
                            buffer = b""
                        if end:
                            if not oversized_line:
                                event(buffer)
                            buffer = b""
                            oversized_line = False
                if not message.get("more_body", False):
                    if streaming and buffer:
                        event(buffer)
                    await send(message)
                    completed = True
                    return
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException:
            failed = True
            raise
        finally:
            http_ok = status is not None and 200 <= status < 300
            ok = http_ok and completed and not disconnected and not failed and (not streaming or terminal)
            outcome = "success" if ok else "stream_error" if failed and streaming else "interrupted" if not completed or disconnected or (streaming and not terminal) else "http_error"
            duration = (time.monotonic() - start) * 1000
            self.metrics.finish(path, self.source, status, ok, duration, outcome)
            if self.store is not None:
                self._record_request_log(scope, path, status, outcome, duration)

    def _record_request_log(self, scope, path, status, outcome, duration):
        """一行日志只在这里写。

        PoolMiddleware（内层）把 key/account/model 与 SSE 内的 error.code 挂在 scope 上；
        生效状态码在此统一：HTTP 非 2xx 用 HTTP 码，SSE 内带的 401/402/403/429 优先，
        两者都没有时（正常 2xx）仍落 HTTP 状态码，避免成功请求在库里 status 为 NULL。
        落库走 Store 的批量缓冲，不每请求写一次库。
        """
        context = scope.get(REQUEST_LOG_CONTEXT) or {}
        # error_code 来自上游 SSE 扫描，可能是字符串（如 WAF 的 "11128"）；
        # request_logs.status 是 INTEGER 列，SQLite 动态类型会把文本原样存回来，
        # 之后每次 AVG/比较都脏。这里统一归一成 int，失败回落 HTTP 状态码。
        effective = status
        code = context.get("error_code")
        if code is not None:
            try:
                effective = int(code)
            except (TypeError, ValueError):
                effective = status
        self.store.queue_request_log({
            "time": int(time.time()), "source": self.source, "path": path,
            "status": effective, "outcome": outcome, "duration_ms": round(duration),
            "key_id": context.get("key_id"), "account_id": context.get("account_id"),
            "account_name": context.get("account_name"), "model_id": context.get("model_id"),
            "model_name": context.get("model_name"), "upstream_request_id": None,
            # 走哪条链路（trae / workbuddy）。context 由 PoolMiddleware 填；
            # 管理页测试等不经过池的路径没有该键，留 NULL 由前端显示「—」。
            "backend": context.get("backend"),
            # token 明细（用户 2026-10-19 要求）：从 PoolMiddleware 透传的 tokens_detail
            # 拿归一化后的四项；拿不到（上游不返回 usage / 请求失败）则为 None，
            # 落 INTEGER 列即 NULL。不在 metrics 层猜 0。
            "tokens_in": (context.get("tokens_detail") or {}).get("in"),
            "tokens_out": (context.get("tokens_detail") or {}).get("out"),
            "tokens_cache_read": (context.get("tokens_detail") or {}).get("cache_read"),
            "tokens_cache_creation": (context.get("tokens_detail") or {}).get("cache_creation"),
        })

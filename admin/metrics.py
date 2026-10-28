"""Bounded process-lifetime counters; never store prompts, responses or keys.

计数与内存 recent 仍是进程级的；request_logs 的落库在 MetricsMiddleware（单一观察点）
组装成一行后交给 Store 的批量缓冲，由 5 秒防抖统一写库。
"""
import json
import logging
import os
import threading
import time

from admin.pool import REQUEST_LOG_CONTEXT
from admin.rotation import is_waf_error
from core.session_headers import SCOPE_REQUEST_ID, SCOPE_UPSTREAM_MODEL

PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"}

_logger = logging.getLogger(__name__)

# 慢请求阈值（用户 2026-10-28）：超过就单独打一行 WARN。
# 用途是把「慢」拆成三种可区分的情况：
#   ttft 大、总耗时大        → 上游 prefill 慢（上下文太大）
#   ttft 小、总耗时大        → 上游吐字慢 / 输出太长
#   ttft 小、总耗时也小      → 慢在两次请求之间（客户端工具执行），服务端无责
SLOW_MS = int(os.environ.get("WORKBUDDY_SLOW_MS") or 10000)


def _numeric_code(value):
    """上游错误码 → int；转不了（文本码、None）返回 None。

    上游码有两种形态：数字（429、11128）与文本（"USER_LIMIT"、"WAITING"）。
    只有**数字码**能参与 HTTP 语义的分类（限流/鉴权/额度/上游错误），
    文本码原样落库给人看，不硬转成 0 —— 0 是合法码，会把「未知」说成「正常」。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def classify_outcome(status=None, error_code=None, *, ok=False, failed=False,
                     streaming=False, completed=False, disconnected=False,
                     terminal=False, local_reject=None, queued=False, waf=False):
    """把一次请求归到**互斥**的一个类别（v24）。

    ## 为什么要有这一层

    改动前只有四类（success / stream_error / interrupted / http_error），而
    「失败」这一大块里混着**四种完全不同**的事：本地没账号可用、上游限流、
    凭据失效、上游报错。混在一起的后果是：看日志的人只能自己再猜一遍
    「400 是模型被禁还是密钥没权限」「503 是池子空了还是上游挂了」。

    ## 优先级（从高到低）

    1. **本地拒绝**（根本没打上游）：model_cooling / no_account / client_error
    2. **上游排队**（打到了上游，但拿回的是「你在排队中」）
    3. **WAF/通道拦截**（出口 IP 级，换号无用——与「账号被禁」要分开看）
    4. **业务码分类**：限流 429 / 鉴权 401,403 / 额度 402 / 上游 5xx
    5. **成功**
    6. **其他非 2xx**
    7. **流内报错** / **未完整结束**

    第 4 条用**业务码优先于 HTTP 状态**：上游在 SSE 里报 429 时 HTTP 仍是
    200，但那**就是**限流；归成「流式错误」会让人误判成流断了。它也优先于
    「成功」——有业务错误码就说明这次请求出错了，哪怕 HTTP 是 200（上游用
    200 裹错误是常态）。

    ## 兼容性

    老行的 outcome 取值不变（success/stream_error/interrupted/http_error
    仍在集合里），统计口径 `outcome='success'` 不受影响。
    """
    if local_reject:
        return local_reject
    if queued:
        return "queue"
    if waf or is_waf_error(status, None, error_code):
        # 11128 这类通道拦截：换遍全池都会被拦，问题在出口 IP 而不在账号。
        # 归成「鉴权失败」会让人去查账号凭据，所以单列一类。
        return "waf_blocked"
    code = _numeric_code(error_code)
    # 业务码优先（能转成数字时）；转不了就只看 HTTP 状态。
    effective = code if code is not None else status
    if effective == 429:
        return "rate_limited"
    if effective in (401, 403):
        return "auth_error"
    if effective == 402:
        return "quota_exhausted"
    if effective is not None and 500 <= int(effective) < 600:
        return "upstream_error"
    if ok:
        return "success"
    if effective is not None and not (200 <= int(effective) < 300):
        return "http_error"
    if failed:
        return "stream_error"
    if not completed or disconnected or (streaming and not terminal):
        return "interrupted"
    return "http_error"


def _header(scope, name):
    """从 ASGI scope 里取一个请求头（bytes → str）；没有返回 None。"""
    target = name.encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key.lower() == target:
            try:
                return value.decode("latin-1").strip() or None
            except (AttributeError, UnicodeDecodeError):
                return None
    return None


def _client_ip(scope):
    """来源 IP：优先 X-Forwarded-For 首段，回落 ASGI 的 ``scope["client"]``。

    本项目线上跑在容器 + 反代后面，uvicorn 已经带 ``proxy_headers=True``
    （见 admin/server.py 的 serve 调用），所以 ``scope["client"]`` 通常已是
    真实客户端 IP；但反代如果没透传（或换了链路），XFF 首段更接近真相。
    两者都取不到就返回 None——不拿容器网关地址冒充客户端。
    """
    forwarded = _header(scope, "x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client and client[0]:
        return str(client[0])
    return None



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
        # 首字延迟（v22）：客户端视角「多久见到第一个字节」。只记第一次真正
        # 发出去的非空响应体；整条请求没有任何响应体（失败/超时）就保持 None，
        # 落库为 NULL —— 不写 0（0 会被误读成「瞬间出字」）。
        first_token_ms = [None]

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
                if first_token_ms[0] is None and message.get("body"):
                    # 首个非空响应体 = 首字。排队扣留之后池会把首片补发到这里，
                    # 所以量到的是客户端真实看到的等待时间。
                    first_token_ms[0] = round((time.monotonic() - start) * 1000)
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
            # 分类（v24）：本地拒绝信息与上游业务码都来自 PoolMiddleware 挂在
            # scope 上的上下文，缺上下文时按 HTTP 状态分类。
            context = scope.get(REQUEST_LOG_CONTEXT) or {}
            outcome = classify_outcome(
                status, context.get("error_code"), ok=ok, failed=failed, streaming=streaming,
                completed=completed, disconnected=disconnected, terminal=terminal,
                local_reject=context.get("local_reject"), queued=bool(context.get("queued")),
                waf=bool(context.get("waf")))
            duration = (time.monotonic() - start) * 1000
            self.metrics.finish(path, self.source, status, ok, duration, outcome)
            if self.store is not None:
                self._record_request_log(scope, path, status, outcome, duration,
                                         first_token_ms[0])

    def _record_request_log(self, scope, path, status, outcome, duration, first_token_ms=None):
        """一行日志只在这里写。

        PoolMiddleware（内层）把 key/account/model 与 SSE 内的 error.code 挂在 scope 上。

        ## v24：status 与 error_code 分开

        - ``status`` = **HTTP 状态码**（客户端实际收到的那个），不再被上游
          业务码覆盖。改动前 11128 会盖掉 200，于是「HTTP 200 但流内报错」
          的行看不出客户端拿到的状态，纯本地 503 又与上游 503 混作一团。
        - ``error_code`` = **上游业务码原样**（字符串）。上游码可能是
          "11128"、"USER_LIMIT"、"WAITING" 这类非数字文本，所以落 TEXT 列；
          拿不到就是 NULL，不猜 0。

        落库走 Store 的批量缓冲，不每请求写一次库。
        """
        context = scope.get(REQUEST_LOG_CONTEXT) or {}
        code = context.get("error_code")
        if code is not None:
            code = str(code).strip() or None
        if duration >= SLOW_MS:
            td = context.get("tokens_detail") or {}
            _logger.warning(
                "[SLOW] %.1fs | ttft=%s | model=%s -> %s | in=%s out=%s | account=%s | status=%s/%s | %s",
                duration / 1000.0,
                ("%.1fs" % (first_token_ms / 1000.0)) if first_token_ms else "n/a",
                context.get("model_name") or "?",
                scope.get(SCOPE_UPSTREAM_MODEL) or "-",
                td.get("in"),
                td.get("out"),
                context.get("account_name") or "-",
                status, outcome, path,
            )
        self.store.queue_request_log({
            "time": int(time.time()), "source": self.source, "path": path,
            "status": status, "outcome": outcome, "duration_ms": round(duration),
            "error_code": code,
            "key_id": context.get("key_id"), "account_id": context.get("account_id"),
            "account_name": context.get("account_name"), "model_id": context.get("model_id"),
            "model_name": context.get("model_name"), "upstream_request_id": context.get("upstream_request_id"),
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
            # 首字延迟（v22）：本中间件直接量到的值优先（它是客户端真正看到首片
            # 的时刻，池那层的 sniff 时刻可能略早），池没走到这条链路时也就它有。
            "first_token_ms": first_token_ms,
            # 来源识别（v22）：哪个客户端在打、从哪个 IP 来，用于排查。
            "client_ip": _client_ip(scope),
            "user_agent": _header(scope, "user-agent"),
            # v26：把「我们这边看到的名字」和「上游那边看到的名字」都记下来。
            # upstream_model = 上游最终收到的模型名（trae 还要过一遍别名表），
            #   与 model_id（客户端请求的裸名）可能不是同一个字符串。
            # account_uid = 上游侧的身份标识，跨「本地备注名 / 云端昵称」唯一对齐。
            # client_request_id = 我们发给上游的 X-Request-ID（uuid7 hex），
            #   上游的用量报表认的就是它——没有它，日志和上游报表永远对不上账。
            "upstream_model": scope.get(SCOPE_UPSTREAM_MODEL),
            "account_uid": context.get("account_uid"),
            "client_request_id": scope.get(SCOPE_REQUEST_ID),
        })

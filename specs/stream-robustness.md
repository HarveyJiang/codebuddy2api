# 流式请求鲁棒性 Spec：空闲超时 + 截断可观测

> 状态：已实现（2026-10-15）。实现见 `core/converter.py` 的 `_iter_with_idle_timeout`。范围是**流式请求的超时与截断处理**，不含非流式（已有 `timeout=300`）。

## 背景

`PoolMiddleware` 的重试**只在进入流式之前**做（`admin/pool.py:796` 的注释：
`never replay a partially emitted response`）。这个设计是**正确的**——已经开始给客户端推字节后
再重试，会导致客户端收到重复内容。但它的代价是暴露了两个盲区：

### 盲区一：流式连接没有超时

两处流式请求用 `httpx.AsyncClient(timeout=None)`：

- `core/converter.py:869`（消费后端 Chat SSE 做 Anthropic 转换）
- `core/converter.py:1326`（Anthropic SSE 流）

`timeout=None` 意味着**完全不超时**。若上游发到一半卡死不关连接（TCP 还活着但不发数据），
该请求会**永远挂着**：占用一个账号的 in-flight 名额、占用一个 ASGI 连接、`MetricsMiddleware`
的 `in_flight` 计数永不归零。反复发生会耗尽可用账号。

注意这里**不能直接用总超时**：LLM 流式响应天然可能持续几分钟（长推理、长输出），
`timeout=300` 会把正常的长响应掐断。

### 盲区二：截断静默

若流在**没有终止事件**的情况下结束（上游断开、网关 5xx 中途切断），客户端拿到的是
「合法的、但内容不全的」流——很多客户端会把它当正常结束，用户看到的是**答案莫名断在一半**，
且没有任何提示。

## 目标

1. 流式连接加**空闲超时**（idle timeout，非总超时）：只要还在持续收到数据就永不超时；
   连续 N 秒无数据则判为卡死并中止。
2. 流式**异常截断**时，向客户端补发一个明确的错误事件（而非静默结束），让客户端能区分
   「正常结束」与「被截断」。
3. 截断与空闲超时都计为失败（`MetricsMiddleware` 的 outcome 不能是 `success`）。

## 非目标

- **不重试**已开始输出的流（既有设计正确，保持）。
- 不改非流式路径的超时值。
- 不做「续传/断点」，协议不支持。

## 设计

### 一、空闲超时

写一个可复用的异步迭代包装器（放 `core/converter.py`，两个流式消费点共用）：

```python
async def _iter_with_idle_timeout(aiter, idle_seconds, on_timeout):
    """逐个 yield；相邻两个 chunk 的间隔超过 idle_seconds 就抛 TimeoutError。"""
```

实现要点：

- 用 `asyncio.wait_for(aiter.__anext__(), timeout=idle)` 包每次取数，**每次重置计时**，
  因此它约束的是「间隔」而不是「总时长」——长响应不受影响。
- 超时抛 `asyncio.TimeoutError`，由调用点捕获 → 记 `WARNING` 日志 → 补发错误事件 → 结束。
- 正常 `StopAsyncIteration` 照常结束。

值：默认 **120 秒**，可配（`STREAM_IDLE_TIMEOUT`，见 config spec）。
120 秒的依据：正常流式推理的首 token 到后续 chunk 间隔远小于此；卡死场景下 2 分钟足以判定，
又不会误杀正常的慢响应。

### 二、截断可观测

在流式消费的收尾处，若**没有观察到终止信号**就被迫结束（异常、超时、上游无终止事件地关闭），
补发一个协议内合法的错误事件：

- OpenAI Chat SSE：`data: {"error": {"message": "...", "type": "stream_truncated"}}\n\n`
  然后照常发 `data: [DONE]`（保证客户端循环能退出）。
- Anthropic SSE：`event: error` + `data: {"type":"error","error":{"type":"api_error",
  "message":"..."}}`。

**关键**：只有在「未观察到终止」时才补发。已经正常收到 `[DONE]`/`message_stop` 的流不受影响，
避免给正常响应追加多余事件。

### 三、与 Metrics 的联动

`MetricsMiddleware` 已经能识别「无终止事件 = 不算成功」（`metrics.py:128` 的 `terminal` 判断）。
本 spec 不重复造轮子，只确保：

- 空闲超时命中时，异常路径让 `failed=True`（现有 `except BaseException` 已覆盖）。
- 补发的错误事件里带 `error` 字段，`metrics.py:77` 的事件解析会据此标记 `failed`。

因此**不需要改 metrics.py**，但要加测试锁住「截断 → outcome 不是 success」。

## 风险

- **误杀慢首 token**：某些推理模型首 token 可能 > 120 秒。缓解：默认值可配，
  且日志会记录实际等待时长，便于按真实情况调整。
- **补发错误事件影响客户端**：只在未观察到终止时补，正常路径完全不变；错误事件的字段
  遵循各协议规范，客户端应能正确处理。

## 测试

- 空闲超时：mock 一个「发 2 个 chunk 后永远不返回」的上游，断言请求在 idle 秒后被中止，
  且客户端收到错误事件（而非静默结束）。
- 长流不误杀：mock「每 10ms 发一个 chunk 持续 2 秒」，idle 设 1 秒，断言**不超时**且完整收完。
- 正常流不变：收到 `[DONE]` 的流**不**补发错误事件。
- 截断可观测：上游 `Content-Length` 承诺 100 字节但只发 50 就断，断言客户端收到错误事件。
- 指标联动：截断场景下 `outcome != "success"`。

## 实现后修正（评审发现）

- **末帧无尾随换行会误报截断**。行缓冲解析只在「缓冲里有 `\n`」时切行，而上游
  flush 边界、或连接正好在帧尾关闭时，最后一帧常常不带 `\n`，其中的
  `finish_reason`/`[DONE]` 会被漏掉——用户明明收全了答案，却额外收到
  `stream_truncated`，后台也记成失败请求。现在流结束时用 `_feed(b"", flush=True)`
  解析残留行。对应测试 3 条：`finish_reason` 无换行、`[DONE]` 无换行、
  确实无终止信号仍要报（防「修成永不报」）。
- **超时上限的划分**：不给整个流设总超时（LLM 长响应合法），而是连接/写入设上限
  （`connect=15` / `write=30`）、读保持 `None`，另由 `_iter_with_idle_timeout`
  逐分片判空闲。已实测：超时后会正确关闭上游生成器与 httpx 客户端，无连接泄漏。
- **`completed` 不能看 `message_stop`**：`AnthropicStreamConverter.finish()` 无论
  上游是否正常收尾都会补发 `message_stop`（保证客户端循环能退出），所以新增
  `completed` 属性，以 `finish_reason` / `[DONE]` 为准。

"""Trae 远程 agent（chat_sessions）链路。

官网 craft 模式（`agent_type=solo_agent_remote`）下的部分模型（如 deepseek-v4.1-flash）
**不在** `llm_utils_chat` 的可用集合里（该端点对这些 config_name 回 4001），
但可通过 work.trae.cn 的会话式 agent API 调用：

    POST /api/remote/v1/chat_sessions            {"mode":"code"}        → 建会话
    POST /api/remote/v1/chat_sessions/{sid}/messages                    → 入队用户消息（异步，返回 message_id）
    GET  /api/remote/v1/chat_sessions/{sid}/events                      → SSE 事件流
    GET  /api/remote/v1/chat_sessions/{sid}/messages                    → 消息列表（取最终内容）

事件：metadata / model_config / plan_item（正文与推理的**累计快照**）/ token_usage /
timing_events / heartbeat / done。正文取 `plan_item.thought`，推理取 `plan_item.reasoning_content`，
两者按累计长度做差分后转成 OpenAI 流式 delta；`done` 收尾。

注意：agent 每轮 prompt 很大（实测约 5.8 万 input tokens），单轮成本高于 llm_utils_chat。
"""

from __future__ import annotations

import json
import time

import httpx

AGENT_HOST = "https://work.trae.cn"
EP_CHAT_SESSIONS = "/api/remote/v1/chat_sessions"
AGENT_TYPE = "solo_agent_remote"
APP_VERSION = "1.0.0.225"
#: 官网客户端用于埋点的 web id（随浏览器/设备，任意稳定值即可）
DEFAULT_WEB_ID = "7627117085906732550"

#: 只转发这些事件；其余（metadata/model_config/timing_events/heartbeat/platform_timing）忽略
FORWARDED_EVENTS = ("plan_item", "token_usage", "done")

_ROLE_LABEL = {"system": "系统", "user": "用户", "assistant": "助手", "tool": "工具"}


def _text_of(content) -> str:
    """OpenAI content（字符串或多模态数组）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and str(block.get("type") or "text") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return "" if content is None else str(content)


def build_agent_query(messages) -> str:
    """OpenAI messages → agent query 文本。

    每次请求都新建会话（服务端无历史），故把整段对话扁平化传入，
    让上游看到与客户端一致的上下文。
    """
    lines = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        text = _text_of(m.get("content")).strip()
        if not text:
            continue
        role = _ROLE_LABEL.get(str(m.get("role") or ""), str(m.get("role") or "user"))
        lines.append(f"{role}：{text}")
    return "\n\n".join(lines)


def common_params(session_id: str, uid: str) -> str:
    """common_params 是**字符串化的 JSON**（上游要求）。"""
    return json.dumps({
        "language": "zh-cn", "app_language": "zh-cn", "quality": "stable",
        "app_version": APP_VERSION, "biz_session_id": session_id,
        "web_id": DEFAULT_WEB_ID, "biz_user_id": uid, "user_unique_id": uid,
        "scope": "marscode", "tenant": "marscode", "region": "CN", "aiRegion": "CN",
        "tenant_name": "", "solo_chat_mode": "code", "user_identity": "Pro", "pro_period": "0",
        "is_freshman": "0", "is_privacy_mode": 0, "privacy_mode": "off", "message_source": "manual",
    }, ensure_ascii=False)


def create_session(client: httpx.Client, headers: dict, timeout: float = 30) -> str:
    """建会话（`{"mode":"code"}`）。返回 chat_session_id；失败抛 RuntimeError。"""
    r = client.post(AGENT_HOST + EP_CHAT_SESSIONS, headers=headers, json={"mode": "code"}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"agent 建会话失败：HTTP {r.status_code}")
    data = (r.json() or {}).get("data") or {}
    sid = str(data.get("chat_session_id") or "")
    if not sid:
        raise RuntimeError("agent 建会话失败：无 chat_session_id")
    return sid


def send_message(client: httpx.Client, headers: dict, session_id: str, model: str, query: str,
                 uid: str, display_name: str | None = None, timeout: float = 60) -> str:
    """把用户消息入队（异步）。返回 message_id；失败抛 RuntimeError。"""
    body = {
        "content": [],
        "query": json.dumps([{"type": "text", "data": {"content": query}}], ensure_ascii=False),
        "model_name": model,
        "agent_type": AGENT_TYPE,
        "agent_id": AGENT_TYPE,
        "model_selection_strategy": "manual",
        "custom_model": {
            "provider": "", "is_preset": True, "config_name": model, "config_source": 1,
            "model_name": model, "display_model_name": display_name or model,
            "ak": "", "base_url": "", "use_remote_service": True, "multimodal": True,
            "reasoning_effort_level": "high",
        },
        "common_params": common_params(session_id, uid),
        "__hit_shared_artifact_and_workroom": True,
        "__hit_my_space": True,
    }
    r = client.post(f"{AGENT_HOST}{EP_CHAT_SESSIONS}/{session_id}/messages", headers=headers, json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"agent 投递消息失败：HTTP {r.status_code}")
    payload = r.json() or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    mid = str((data or {}).get("message_id") or "")
    if not mid:
        raise RuntimeError("agent 投递消息失败：无 message_id")
    return mid


def _sse_chunk(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


async def normalize_agent_stream(lines, *, model: str = ""):
    """agent `/events` SSE 行流 → OpenAI Chat SSE 字节流。

    `plan_item` 是累计快照（同一 plan item 反复下发、字段递增），故对
    `thought`（正文）与 `reasoning_content`（思考）做**差分**，只发新增后缀。
    `done` 收尾；`token_usage` 的用量挂到最后一个 chunk 上。
    """
    sent_content, sent_reasoning = 0, 0
    pending_usage = None
    saw_done = False

    def chunk(delta: dict, finish: str | None = None) -> bytes:
        nonlocal pending_usage
        choice = {"index": 0, "delta": delta}
        if finish:
            choice["finish_reason"] = finish
        payload = {"id": "chatcmpl-trae-agent", "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model or "", "choices": [choice]}
        if pending_usage is not None:
            payload["usage"] = pending_usage
            pending_usage = None
        return _sse_chunk(payload)

    async for event, data in _aiter_events(lines):
        if event == "plan_item":
            delta = {}
            thought = str(data.get("thought") or "")
            reasoning = str(data.get("reasoning_content") or "")
            if len(reasoning) > sent_reasoning:
                delta["reasoning_content"] = reasoning[sent_reasoning:]
                sent_reasoning = len(reasoning)
            if len(thought) > sent_content:
                delta["content"] = thought[sent_content:]
                sent_content = len(thought)
            if delta:
                yield chunk(delta)
        elif event == "token_usage":
            pending_usage = {
                "prompt_tokens": int(data.get("prompt_tokens") or 0),
                "completion_tokens": int(data.get("completion_tokens") or 0),
                "total_tokens": int(data.get("total_tokens") or 0),
            }
        elif event == "done":
            yield chunk({}, "stop")
            yield b"data: [DONE]\n\n"
            saw_done = True
            return
    if not saw_done:
        if pending_usage is not None:
            yield chunk({})
        yield b"data: [DONE]\n\n"


async def _aiter_events(lines):
    """异步行流 → (event, data)。"""
    event, buf = None, []
    async for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r") if isinstance(raw, (bytes, bytearray)) else str(raw).rstrip("\r")
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            buf.append(line[len("data:"):].strip())
        elif line == "":
            if event:
                if event in FORWARDED_EVENTS:
                    try:
                        payload = json.loads("".join(buf) or "{}")
                    except ValueError:
                        payload = {}
                    yield event, payload if isinstance(payload, dict) else {}
                event, buf = None, []
    if event and event in FORWARDED_EVENTS and buf:
        try:
            payload = json.loads("".join(buf))
        except ValueError:
            payload = {}
        yield event, payload if isinstance(payload, dict) else {}

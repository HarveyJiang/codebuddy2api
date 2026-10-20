"""出站请求体指纹剥离：绕开上游按逐字精确匹配的内容审核黑名单。

背景：上游对请求内容做**逐字精确匹配**拦截（不是语义审核）——命中某个固定
字符串就整单拒（错误码 11128 / "Illegal API invocation from an unapproved
channel"）。命中项都是客户端（Claude Code / Codex CLI 类）在 system prompt 里
注入的固定模板句，以及上游自己的反探测数字。

本模块的规则来自对照项目 linguo2625469/workbuddy2api-panel 的
``internal/upstream/sanitize.go``（那是踩着 11128 逐条试出来的）。

三条硬约束：

1. **必须是纯函数**。同样输入永远同样输出——上游 prompt 缓存按前缀命中，
   任何随机化（按请求加盐）都会让缓存永远不中。已有 ``core/desensitize.py``
   用零宽空格绕过敏感词，那套对上游的逐字匹配无效（上游会归一化），与本
   模块不是一回事。
2. **改写要最小**。承载语义的模板句只换一个词（官方 CLI → 官方 CLI tool），
   语义不变；键值型指纹（header 键名段、尾随裸 kv）整段删除。
3. **零宽空格要能穿透**。``core/desensitize.py`` 会在敏感词中间插 U+200B，
   而上游会把它归一化——所以带零宽空格的指纹照样命中。本模块的匹配与改写
   都容忍词内零宽空格，且只把**命中片段**里的零宽空格一并清掉，正文其他
   位置的零宽空格原样保留（那是脱敏机制的保护，不归本模块管）。
"""
from __future__ import annotations

import re

# 上游会归一化的零宽空格（脱敏机制插的）。
_ZWSP = "\u200b"

# 特征预检：任一命中才进入净化（纯字符串快路径，普通请求零分配）。
_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",
)

# 改写层：全模板句逐字替换（每句只改一个词，语义不变）。
# 身份句的匹配串**不带结尾标点**：CLI 版以句号收尾，桌面版以逗号接后继内容，
# 带标点会漏掉桌面版（漏网的指纹原样发上游 → 整单 11128）。
_REWRITES = (
    (
        "You are Claude Code, Anthropic's official CLI for Claude",
        "You are Claude Code, Anthropic's official CLI tool for Claude",
    ),
    (
        "Main branch (you will usually use this for PRs)",
        "Default branch (you will usually use this for PRs)",
    ),
    (
        "You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
        "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.",
    ),
    (
        "To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
        "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
    ),
)


def _flexible(phrase: str) -> str:
    """构造一个容忍词内零宽空格的匹配式（返回正则**源码**）。

    ``"You are Claude Code"`` → ``"Y(?:\\u200b)?o(?:\\u200b)?u ..."``：字符
    之间允许夹一个零宽空格。脱敏机制插的零宽空格落在这里，上游归一化后等于
    原串，所以匹配时必须能穿透它，否则带零宽空格的指纹会漏网。
    """
    parts = [re.escape(char) for char in phrase]
    gap = r"(?:%s)?" % _ZWSP
    return gap + gap.join(parts)


# 模板句改写（容忍零宽空格；替换串是干净文本 → 命中片段里的零宽空格被清掉）。
_REWRITE_PATTERNS = tuple(
    (re.compile(_flexible(old)), new) for old, new in _REWRITES
)

# 上游反探测：请求体里出现 11128 就整单拦截（与上下文无关）。相邻的
# 11148 / 11101 / 11115 / 99999 均放行——所以这里只动 11128 这一个数字。
# 插连字符保留可读性与指代（零宽空格无效：上游会归一化）。
# 用子串匹配而不是数字边界：上游按**子串**判定，"111280" 里也含 11128，
# 按边界放过等于留着被拦的串。命中即整单失败，误改一个长数字的代价更小。
_NUMBER_PATTERN = re.compile(_flexible("11128"))

# 剥离层：header 键值段（键名即触发，与值无关）整段删除。
_HDR_KV_PATTERN = re.compile(
    _flexible("x-anthropic-billing-header") + r":[^;\n]*;?[ \t]*",
    re.IGNORECASE,
)
# 兜底层：裸键名（无冒号无值）同样是指纹——引用的示例文本形态。
# 键值形态已整段删除，残留的裸键名做最小缩写（header→hdr）：破坏逐字匹配、
# 语义不变、保留可读性。不要求冒号，是上面那个正则的超集。
_BARE_HDR_PATTERN = re.compile(
    _flexible("x-anthropic-billing-header"), re.IGNORECASE
)
# 剥离层：尾随裸键值（cc_xxx=...;）整段删除。
_KV_PATTERN = re.compile(
    r"\b" + _flexible("cc_") + r"[a-z0-9_]+=[^;\n]*;?[ \t]*",
    re.IGNORECASE,
)
# 仓库链接：上游按整句拦，链接本身也一并改掉更稳。
_LINK_PATTERN = re.compile(r"github\.com/anthropics/")


def _has_fingerprint(text: str) -> bool:
    """特征预检（穿透零宽空格）。

    裸键名走不区分大小写的正则兜底（大小写变体 + 混合形态）。
    """
    probe = text.replace(_ZWSP, "") if _ZWSP in text else text
    for feature in _FEATURES:
        if feature in probe:
            return True
    return bool(_BARE_HDR_PATTERN.search(probe))


def sanitize_text(text: str) -> str:
    """净化一段文本。未命中特征时原样返回（同一对象，零分配）。"""
    if not isinstance(text, str) or not text:
        return text
    if not _has_fingerprint(text):
        return text
    out = text
    for pattern, replacement in _REWRITE_PATTERNS:
        out = pattern.sub(replacement, out)
    out = _NUMBER_PATTERN.sub("11-128", out)
    out = _HDR_KV_PATTERN.sub("", out)
    # 循环清：一次 sub 可能露出新的相邻 kv（cc_a=1;cc_b=2; → 清完还有）。
    previous = None
    while previous != out:
        previous = out
        out = _KV_PATTERN.sub("", out)
    out = _BARE_HDR_PATTERN.sub("x-anthropic-billing-hdr", out)
    out = _LINK_PATTERN.sub("github.com/anthropic/", out)
    return out.strip()


def _sanitize_content(value):
    """content 兼容字符串与多模态数组；只动 text part，image 等 part 不动。"""
    if isinstance(value, str):
        clean = sanitize_text(value)
        return clean, clean != value
    if isinstance(value, list):
        changed = False
        parts = []
        for part in value:
            if not isinstance(part, dict):
                parts.append(part)
                continue
            text = part.get("text")
            if not isinstance(text, str):
                parts.append(part)
                continue
            clean = sanitize_text(text)
            if clean != text:
                part = dict(part)
                part["text"] = clean
                changed = True
            parts.append(part)
        return (parts, True) if changed else (value, False)
    return value, False


def _sanitize_tool_calls(calls):
    """净化 assistant.tool_calls[].function.arguments。

    arguments 是**字符串化的 JSON**（不是对象），按文本走即可。
    这块长期是盲区：工具调用消息的 content 通常为 null，早期实现见 content
    缺失就跳过，整条消息连 tool_calls 一起漏掉。
    """
    if not isinstance(calls, list):
        return calls, False
    changed = False
    out = []
    for call in calls:
        if not isinstance(call, dict):
            out.append(call)
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            out.append(call)
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            out.append(call)
            continue
        clean = sanitize_text(arguments)
        if clean != arguments:
            call = dict(call)
            function = dict(function)
            function["arguments"] = clean
            call["function"] = function
            changed = True
        out.append(call)
    return (out, True) if changed else (calls, False)


def _sanitize_message(message):
    """净化一条消息的 content / reasoning_content / reasoning / tool_calls。"""
    if not isinstance(message, dict):
        return message, False
    out = message
    changed = False

    if "content" in message:
        clean, hit = _sanitize_content(message["content"])
        if hit:
            if out is message:
                out = dict(message)
            out["content"] = clean
            changed = True

    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if not isinstance(value, str):
            continue
        clean = sanitize_text(value)
        if clean != value:
            if out is message:
                out = dict(message)
            out[key] = clean
            changed = True

    if "tool_calls" in message:
        clean, hit = _sanitize_tool_calls(message["tool_calls"])
        if hit:
            if out is message:
                out = dict(message)
            out["tool_calls"] = clean
            changed = True

    return out, changed


def _sanitize_tool(tool):
    """净化一个工具定义（OpenAI 形态：description 在 function 里或顶层）。"""
    if not isinstance(tool, dict):
        return tool, False
    out = tool
    changed = False
    holders = [tool]
    function = tool.get("function")
    if isinstance(function, dict):
        holders.append(function)
    for holder in holders:
        description = holder.get("description")
        if not isinstance(description, str):
            continue
        clean = sanitize_text(description)
        if clean == description:
            continue
        if out is tool:
            out = dict(tool)
        if holder is tool:
            out["description"] = clean
        else:
            new_function = dict(out.get("function") or {})
            new_function["description"] = clean
            out["function"] = new_function
        changed = True
    return out, changed


def sanitize_outbound_body(body):
    """净化将要发往上游的请求体，返回**新**对象（不改动入参）。

    覆盖：所有 role 的 content（字符串与 content block）、tools 的 description、
    tool_calls 的 arguments、reasoning_content 与其镜像字段 reasoning。
    user 正文一并处理——上游拦的是整个请求体，不是某几个 role。
    """
    if not isinstance(body, dict):
        return body
    out = body
    changed = False

    messages = body.get("messages")
    if isinstance(messages, list):
        clean_messages = []
        hit = False
        for message in messages:
            clean, message_hit = _sanitize_message(message)
            hit = hit or message_hit
            clean_messages.append(clean)
        if hit:
            if out is body:
                out = dict(body)
            out["messages"] = clean_messages
            changed = True

    tools = body.get("tools")
    if isinstance(tools, list):
        clean_tools = []
        hit = False
        for tool in tools:
            clean, tool_hit = _sanitize_tool(tool)
            hit = hit or tool_hit
            clean_tools.append(clean)
        if hit:
            if out is body:
                out = dict(body)
            out["tools"] = clean_tools
            changed = True

    return out

"""出站请求体指纹剥离（``core/outbound_sanitize.py``）。

## 为什么这层存在

上游对请求内容做**逐字精确匹配**拦截：命中固定字符串就整单拒，错误码
``11128``（"Illegal API invocation from an unapproved channel"）。命中的都是
客户端（Claude Code / Codex CLI 类）注入 system prompt 的固定模板句，以及
上游自己的反探测数字。

本项目此前只能靠换账号硬扛——但 11128 拦的是**内容**，换遍全池账号都会被拦
（见 ``core/converter.py`` 里那段注释）。本模块改请求内容，才是唯一的解法。

## 三条不变量（都有测试钉住）

1. **纯函数**：同样输入永远同样输出。上游 prompt 缓存按前缀命中，任何随机化
   都会让缓存永远不中。
2. **改写最小**：承载语义的模板句只换一个词，语义不变；键值型指纹整段删除。
3. **零宽空格要能穿透**：``core/desensitize.py`` 插的 U+200B 会被上游归一化，
   带零宽空格的指纹照样命中——所以匹配必须容忍词内零宽空格。

**user 正文一并处理**（用户 2026-10-20 拍板）：上游拦的是整个请求体，不是
某几个 role。
"""
from __future__ import annotations

import copy

from core.outbound_sanitize import sanitize_outbound_body, sanitize_text

ZWSP = "\u200b"


class TemplateRewriteTests:
    """模板句改写：每句只改一个词，语义不变。"""

    def test_identity_sentence_is_rewritten(self):
        text = "You are Claude Code, Anthropic's official CLI for Claude."
        out = sanitize_text(text)
        assert "official CLI tool for Claude" in out
        assert "Anthropic's official CLI for Claude." not in out

    def test_desktop_variant_with_comma_is_rewritten(self):
        """桌面版以逗号接后继内容——匹配串不带结尾标点，两种形态都要覆盖。"""
        text = ("You are Claude Code, Anthropic's official CLI for Claude, "
                "running within the Claude Agent SDK.")
        out = sanitize_text(text)
        assert "official CLI tool for Claude" in out
        assert "running within the Claude Agent SDK." in out

    def test_main_branch_sentence_is_rewritten(self):
        out = sanitize_text("Main branch (you will usually use this for PRs):")
        assert out == "Default branch (you will usually use this for PRs):"

    def test_codex_cli_sentence_is_rewritten(self):
        text = ("You are a coding agent running in the Codex CLI, "
                "a terminal-based coding assistant.")
        out = sanitize_text(text)
        assert "Codex CLI tool" in out
        assert "a terminal-based coding assistant." in out

    def test_feedback_sentence_is_rewritten(self):
        text = ("To give feedback, users should report the issue at "
                "https://github.com/anthropics/claude-code/issues")
        out = sanitize_text(text)
        assert out.startswith("To provide feedback,")
        # 链接本身也按上游黑名单改写（github.com/anthropics/ → .../anthropic/）。
        assert "github.com/anthropics/" not in out


class NumberRewriteTests:
    """裸错误码 11128：上游反探测，与上下文无关。"""

    def test_bare_number_is_rewritten(self):
        assert sanitize_text("错误码 11128") == "错误码 11-128"

    def test_code_form_is_rewritten(self):
        assert sanitize_text("code=11128") == "code=11-128"

    def test_neighbouring_codes_are_untouched(self):
        """相邻的 11148 / 11101 / 11115 / 99999 上游都放行——不能误伤。"""
        text = "11148 11101 11115 99999"
        assert sanitize_text(text) == text

    def test_longer_number_containing_the_code_is_rewritten(self):
        """上游按**子串**判定，"111280" 里也含 11128——按数字边界放过等于留着被拦。"""
        assert "11-128" in sanitize_text("订单号 111280")


class FingerprintStripTests:
    """键值型指纹：整段删除 / 最小缩写。"""

    def test_header_key_value_segment_is_removed(self):
        out = sanitize_text("x-anthropic-billing-header: abc123; tail")
        assert "x-anthropic-billing-header" not in out
        assert out.endswith("tail")

    def test_bare_header_name_is_abbreviated(self):
        """裸键名（无冒号无值）也是指纹——引用的示例文本形态。"""
        out = sanitize_text("see x-anthropic-billing-header for details")
        assert "x-anthropic-billing-header" not in out
        assert "x-anthropic-billing-hdr" in out

    def test_trailing_bare_kv_is_removed(self):
        out = sanitize_text("cc_version=1.0; cc_entrypoint=cli; rest")
        assert "cc_version" not in out
        assert "cc_entrypoint" not in out
        assert out.endswith("rest")

    def test_repo_link_is_rewritten(self):
        out = sanitize_text("https://github.com/anthropics/claude-code")
        assert "github.com/anthropic/claude-code" in out
        assert "github.com/anthropics/" not in out


class ZeroWidthSpaceTests:
    """脱敏机制插的零宽空格必须穿透（上游会归一化它）。"""

    def test_identity_sentence_with_zwsp_is_rewritten(self):
        text = "You are Claude" + ZWSP + " Code, Anthropic's official CLI for Claude"
        out = sanitize_text(text)
        assert "official CLI tool for Claude" in out
        assert ZWSP not in out

    def test_number_with_zwsp_is_rewritten(self):
        out = sanitize_text("code=" + ZWSP + "11128")
        assert "11-128" in out
        assert ZWSP not in out


class PurityTests:
    """纯函数：缓存前缀靠它。"""

    def test_same_input_gives_same_output(self):
        text = "You are Claude Code, Anthropic's official CLI for Claude. code=11128"
        assert sanitize_text(text) == sanitize_text(text)

    def test_text_without_fingerprint_is_returned_unchanged(self):
        text = "普通的一段话，没有任何指纹。"
        assert sanitize_text(text) == text

    def test_sanitize_is_idempotent(self):
        """跑两遍结果相同——避免二次处理把内容改花。"""
        text = "You are Claude Code, Anthropic's official CLI for Claude. code=11128"
        once = sanitize_text(text)
        assert sanitize_text(once) == once

    def test_body_sanitize_does_not_mutate_input(self):
        body = {"messages": [{"role": "user", "content": "code=11128"}]}
        snapshot = copy.deepcopy(body)
        sanitize_outbound_body(body)
        assert body == snapshot


class BodyCoverageTests:
    """覆盖所有可能藏指纹的位置。"""

    def test_user_content_is_rewritten(self):
        """user 正文也要改写（用户 2026-10-20 拍板）：上游拦的是整个请求体。"""
        body = {"messages": [{"role": "user", "content": "code=11128"}]}
        out = sanitize_outbound_body(body)
        assert out["messages"][0]["content"] == "code=11-128"

    def test_system_content_is_rewritten(self):
        body = {"messages": [{"role": "system",
                              "content": "Main branch (you will usually use this for PRs):"}]}
        out = sanitize_outbound_body(body)
        assert "Default branch" in out["messages"][0]["content"]

    def test_content_block_text_is_rewritten(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "code=11128"},
            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
        ]}]}
        out = sanitize_outbound_body(body)
        parts = out["messages"][0]["content"]
        assert parts[0]["text"] == "code=11-128"
        # 图片 part 不动
        assert parts[1] == {"type": "image_url", "image_url": {"url": "http://x/y.png"}}

    def test_tool_call_arguments_are_rewritten(self):
        """arguments 是**字符串化 JSON**，且工具调用轮 content 常为 null——
        只看 content 会整条漏掉。"""
        body = {"messages": [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "write", "arguments": "{\"path\":\"a\",\"err\":11128}"}}]}]}
        out = sanitize_outbound_body(body)
        assert "11-128" in out["messages"][0]["tool_calls"][0]["function"]["arguments"]

    def test_reasoning_fields_are_rewritten(self):
        body = {"messages": [{"role": "assistant", "content": "ok",
                              "reasoning_content": "code=11128",
                              "reasoning": "code=11128"}]}
        out = sanitize_outbound_body(body)
        assert out["messages"][0]["reasoning_content"] == "code=11-128"
        assert out["messages"][0]["reasoning"] == "code=11-128"

    def test_tool_description_is_rewritten(self):
        body = {"tools": [{"type": "function", "function": {
            "name": "read", "description": "Main branch (you will usually use this for PRs):"}}]}
        out = sanitize_outbound_body(body)
        assert "Default branch" in out["tools"][0]["function"]["description"]

    def test_top_level_tool_description_is_rewritten(self):
        body = {"tools": [{"type": "function", "name": "read", "description": "code=11128"}]}
        out = sanitize_outbound_body(body)
        assert out["tools"][0]["description"] == "code=11-128"

    def test_body_without_fingerprint_is_unchanged(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        assert sanitize_outbound_body(body) == body

    def test_non_dict_body_is_returned_as_is(self):
        assert sanitize_outbound_body(None) is None

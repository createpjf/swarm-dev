"""Tests for MiniMax adapter tool_call argument parsing.

Verifies the fix for MiniMax returning Python-style \\UXXXXXXXX Unicode
escapes (invalid JSON) in tool_call arguments, which caused generate_doc
and other tools to receive empty parameters.
"""
from __future__ import annotations

import json


# Import the function under test
from adapters.llm.minimax import _tool_calls_to_text


class TestToolCallsToText:
    """Test _tool_calls_to_text with various argument formats."""

    def _parse_first_block(self, text: str) -> dict:
        """Extract the first <tool_code> block and parse JSON."""
        import re
        m = re.search(r'<tool_code>\n(.*?)\n</tool_code>', text, re.DOTALL)
        assert m, f"No <tool_code> block found in: {text}"
        return json.loads(m.group(1))

    def test_normal_json_args(self):
        """Standard JSON args should pass through unchanged."""
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": '{"format": "docx", "content": "Hello world", "title": "Test"}'
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["tool"] == "generate_doc"
        assert parsed["params"]["format"] == "docx"
        assert parsed["params"]["content"] == "Hello world"
        assert parsed["params"]["title"] == "Test"

    def test_unicode_escape_repair(self):
        r"""Args with \UXXXXXXXX escapes should be repaired and parsed."""
        # Simulate MiniMax returning Python-style Unicode escapes
        # \U000e0067 = TAG LATIN SMALL LETTER G (used in flag sequences)
        raw = '{"format": "docx", "content": "# Flag \\U000e0067\\U000e0062", "title": "Test"}'
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": raw,
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["tool"] == "generate_doc"
        assert parsed["params"]["format"] == "docx"
        # Content should exist and contain the original text prefix
        assert "Flag" in parsed["params"]["content"]
        assert parsed["params"]["title"] == "Test"

    def test_emoji_with_unicode_escapes(self):
        r"""Mixed emoji and \U escapes should be handled."""
        raw = '{"content": "# \\U0001f3f4\\U000e0067 Plan", "format": "pdf"}'
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": raw,
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert "Plan" in parsed["params"]["content"]
        assert parsed["params"]["format"] == "pdf"

    def test_completely_malformed_args(self):
        """Totally broken JSON should propagate _parse_error and _raw_args."""
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": "this is not json at all {{{",
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["tool"] == "generate_doc"
        assert "_parse_error" in parsed["params"]
        assert "_raw_args" in parsed["params"]

    def test_empty_args(self):
        """Empty/null arguments should produce empty params dict."""
        tool_calls = [{
            "function": {
                "name": "web_search",
                "arguments": None,
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["tool"] == "web_search"
        assert parsed["params"] == {}

    def test_multiple_tool_calls(self):
        """Multiple tool calls should all be converted."""
        tool_calls = [
            {"function": {"name": "web_search", "arguments": '{"query": "test"}'}},
            {"function": {"name": "generate_doc", "arguments": '{"format": "pdf", "content": "hi"}'}},
        ]
        result = _tool_calls_to_text(tool_calls)
        assert result.count("<tool_code>") == 2
        assert "web_search" in result
        assert "generate_doc" in result

    def test_chinese_content_preserved(self):
        """Chinese content should pass through correctly."""
        content = "# 英国曼彻斯特看曼联比赛行程计划"
        raw = json.dumps({"format": "docx", "content": content, "title": "行程"})
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": raw,
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["params"]["content"] == content

"""Tests for MiniMax adapter tool_call argument parsing.

Verifies fixes for:
  1. MiniMax returning Python-style \\UXXXXXXXX Unicode escapes (invalid JSON)
  2. MiniMax truncating long tool_call argument strings (incomplete JSON)

Both issues caused generate_doc and other tools to receive empty parameters.
"""
from __future__ import annotations

import json


# Import the functions under test
from adapters.llm.minimax import _tool_calls_to_text, _repair_truncated_json


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

    def test_truncated_json_repair(self):
        """Truncated JSON args should be repaired and parsed successfully."""
        # Simulate MiniMax truncating mid-content
        truncated = '{"content": "# Travel Plan\\n\\nDay 1: Arrive\\n\\nDay 2: Visit'
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": truncated,
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["tool"] == "generate_doc"
        # Should have repaired content (not _parse_error)
        assert "content" in parsed["params"]
        assert "Travel Plan" in parsed["params"]["content"]

    def test_truncated_json_with_format(self):
        """Truncated JSON with format field should preserve format."""
        truncated = '{"format": "pdf", "content": "# Report\\n\\nSection 1\\n\\nDetails here'
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": truncated,
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["tool"] == "generate_doc"
        if "_parse_error" not in parsed["params"]:
            assert parsed["params"]["format"] == "pdf"
            assert "Report" in parsed["params"]["content"]

    def test_truncated_json_chinese(self):
        """Truncated Chinese content should be repaired."""
        truncated = '{"content": "# 英国曼彻斯特5日观赛之旅\\n\\n## 行程概览\\n\\n| 项目 | 详情 |\\n| 出发地 | 深圳'
        tool_calls = [{
            "function": {
                "name": "generate_doc",
                "arguments": truncated,
            }
        }]
        result = _tool_calls_to_text(tool_calls)
        parsed = self._parse_first_block(result)
        assert parsed["tool"] == "generate_doc"
        # Either repaired or has _raw_args for downstream recovery
        params = parsed["params"]
        has_content = "content" in params and "英国" in params.get("content", "")
        has_raw = "_raw_args" in params
        assert has_content or has_raw


class TestRepairTruncatedJson:
    """Test _repair_truncated_json helper directly."""

    def test_valid_json_returns_none(self):
        """Already-valid JSON should return None (no repair needed)."""
        assert _repair_truncated_json('{"key": "value"}') is None

    def test_simple_truncated_string(self):
        """Simple truncated string value should be repaired."""
        result = _repair_truncated_json('{"content": "hello world')
        assert result is not None
        parsed = json.loads(result)
        assert "content" in parsed
        assert "hello" in parsed["content"]

    def test_truncated_with_newlines(self):
        """Truncation after newline markers should truncate at last newline."""
        raw = '{"content": "# Title\\n\\nParagraph 1\\n\\nParagraph 2\\n\\nParagraph 3 trunc'
        result = _repair_truncated_json(raw)
        assert result is not None
        parsed = json.loads(result)
        assert "Title" in parsed["content"]

    def test_truncated_with_trailing_backslash(self):
        """Trailing backslash (incomplete escape) should be cleaned."""
        raw = '{"content": "Some text\\'
        result = _repair_truncated_json(raw)
        assert result is not None
        parsed = json.loads(result)
        assert "text" in parsed["content"]

    def test_empty_string_returns_none(self):
        assert _repair_truncated_json("") is None
        assert _repair_truncated_json("   ") is None

    def test_not_truncated_returns_none(self):
        assert _repair_truncated_json('{"ok": true}') is None

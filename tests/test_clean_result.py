"""Tests for ChannelManager._clean_result() method."""
from __future__ import annotations

import re


# Extract _clean_result logic for standalone testing
# (mirrors adapters/channels/manager.py::_clean_result)
def _clean_result(raw: str) -> str:
    """Cleaned copy of the production _clean_result for unit testing."""
    if not raw:
        return "(Task completed — no displayable output)"

    text = raw

    # Strip TASK: and COMPLEXITY: lines (internal delegation markers)
    text = re.sub(r'^TASK:.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^COMPLEXITY:.*$', '', text, flags=re.MULTILINE)

    # Strip HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

    # Strip <think> tags
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)

    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    cleaned = text.strip()
    if not cleaned:
        return "(Task completed — no displayable output)"
    return cleaned


class TestCleanResult:
    """Test _clean_result with various inputs."""

    def test_normal_content(self):
        result = _clean_result("Hello, this is a normal response.")
        assert result == "Hello, this is a normal response."

    def test_empty_string(self):
        """Empty string should return fallback message, not empty."""
        result = _clean_result("")
        assert result == "(Task completed — no displayable output)"

    def test_none_like(self):
        """None-ish input should return fallback."""
        result = _clean_result("")
        assert "(Task completed" in result

    def test_only_task_lines(self):
        """Input with only TASK: lines should produce fallback."""
        text = "TASK: do something\nCOMPLEXITY: normal\n"
        result = _clean_result(text)
        assert "(Task completed" in result

    def test_mixed_content(self):
        """TASK: lines removed, real content kept."""
        text = "TASK: internal delegation\nHere is the actual answer.\nCOMPLEXITY: simple"
        result = _clean_result(text)
        assert "actual answer" in result
        assert "TASK:" not in result
        assert "COMPLEXITY:" not in result

    def test_html_comments_stripped(self):
        text = "Hello <!-- hidden --> world"
        result = _clean_result(text)
        assert "hidden" not in result
        assert "Hello" in result
        assert "world" in result

    def test_think_tags_stripped(self):
        text = "Answer: 42\n<think>internal reasoning</think>"
        result = _clean_result(text)
        assert "internal reasoning" not in result
        assert "42" in result

    def test_excessive_newlines_collapsed(self):
        text = "Line 1\n\n\n\n\nLine 2"
        result = _clean_result(text)
        assert "\n\n\n" not in result
        assert "Line 1" in result
        assert "Line 2" in result

    def test_whitespace_only_returns_fallback(self):
        result = _clean_result("   \n\n   \n  ")
        assert "(Task completed" in result

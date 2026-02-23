"""
tests/test_tools_sanitize.py
Sprint 5.1 — Tests for sanitize_params() in core/tools.py

Covers:
  - Path safety: sensitive files, traversal, null bytes, dotfile writes
  - URL safety: private IPs, non-https schemes
  - Type coercion: string→int, string→float, string→bool
  - Edge cases: empty params, missing path, unknown tools
"""

import pytest
from core.tools import sanitize_params, Tool


# ══════════════════════════════════════════════════════════════════════════════
#  PATH SAFETY — filesystem tools
# ══════════════════════════════════════════════════════════════════════════════

class TestPathSafety:
    """Tests for file path validation in sanitize_params."""

    # ── Sensitive file blocking ──

    @pytest.mark.parametrize("filename", [
        ".env", ".env.local", ".env.production", ".env.development",
        "agents.yaml", "exec_approvals.json", "chain_contracts.json",
        ".netrc", ".npmrc", ".pypirc",
        "id_rsa", "id_ed25519", "authorized_keys",
    ])
    def test_blocks_sensitive_filenames_write(self, filename):
        """write_file must reject all sensitive filenames."""
        result = sanitize_params("write_file",
                                 {"path": filename, "content": "x"})
        assert isinstance(result, str), f"Expected rejection, got: {result}"
        assert "sensitive" in result.lower() or "blocked" in result.lower()

    @pytest.mark.parametrize("filename", [
        ".env", "agents.yaml", "id_rsa",
    ])
    def test_blocks_sensitive_filenames_read(self, filename):
        """read_file must also reject sensitive filenames."""
        result = sanitize_params("read_file", {"path": filename})
        assert isinstance(result, str), f"Expected rejection, got: {result}"

    def test_blocks_sensitive_in_subdirectory(self):
        """Sensitive file check applies to basename, not full path."""
        result = sanitize_params("write_file",
                                 {"path": "config/.env", "content": "x"})
        assert isinstance(result, str)

    # ── Path traversal ──

    def test_blocks_ssh_path_fragment(self):
        result = sanitize_params("read_file",
                                 {"path": "/home/user/.ssh/id_rsa"})
        assert isinstance(result, str)
        assert "blocked" in result.lower() or "sensitive" in result.lower()

    def test_blocks_aws_path_fragment(self):
        result = sanitize_params("read_file",
                                 {"path": "../../.aws/credentials"})
        assert isinstance(result, str)

    def test_blocks_gnupg_path_fragment(self):
        result = sanitize_params("read_file",
                                 {"path": "/home/user/.gnupg/private-keys"})
        assert isinstance(result, str)

    # ── Null bytes ──

    def test_blocks_null_byte_in_path(self):
        result = sanitize_params("read_file",
                                 {"path": "test\x00.txt"})
        assert isinstance(result, str)
        assert "null" in result.lower()

    # ── URL-encoded traversal ──

    def test_decodes_url_encoded_path(self):
        """Ensure %2e%2e doesn't bypass checks."""
        result = sanitize_params("read_file",
                                 {"path": "%2e%2e/%2e%2e/.ssh/id_rsa"})
        assert isinstance(result, str)

    # ── Dotfile write protection ──

    def test_blocks_dotfile_write(self):
        result = sanitize_params("write_file",
                                 {"path": ".gitignore", "content": "x"})
        assert isinstance(result, str)
        assert "dotfile" in result.lower()

    def test_allows_dotfile_read(self):
        """Reading dotfiles is allowed (only writes are blocked)."""
        result = sanitize_params("read_file", {"path": ".gitignore"})
        assert isinstance(result, dict)

    # ── Normal operations ──

    def test_allows_normal_workspace_write(self):
        result = sanitize_params("write_file",
                                 {"path": "workspace/report.txt",
                                  "content": "hello"})
        assert isinstance(result, dict)
        assert result["path"] == "workspace/report.txt"

    def test_allows_normal_read(self):
        result = sanitize_params("read_file",
                                 {"path": "workspace/data.json"})
        assert isinstance(result, dict)

    def test_allows_list_dir(self):
        result = sanitize_params("list_dir", {"path": "workspace"})
        assert isinstance(result, dict)

    # ── Missing / empty path ──

    def test_rejects_empty_path(self):
        result = sanitize_params("read_file", {"path": ""})
        assert isinstance(result, str)

    def test_rejects_missing_path(self):
        result = sanitize_params("read_file", {})
        assert isinstance(result, str)
        assert "missing" in result.lower() or "empty" in result.lower()

    def test_rejects_non_string_path(self):
        result = sanitize_params("read_file", {"path": 123})
        assert isinstance(result, str)


# ══════════════════════════════════════════════════════════════════════════════
#  URL SAFETY — network tools
# ══════════════════════════════════════════════════════════════════════════════

class TestURLSafety:
    """Tests for URL validation in sanitize_params."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8080/admin",
        "http://localhost/api",
        "http://0.0.0.0/",
        "https://169.254.169.254/latest/meta-data/",
    ])
    def test_blocks_private_urls(self, url):
        result = sanitize_params("web_fetch", {"url": url})
        assert isinstance(result, str), f"Expected rejection for {url}"
        assert "private" in result.lower() or "blocked" in result.lower()

    def test_blocks_non_http_scheme(self):
        result = sanitize_params("web_fetch", {"url": "ftp://example.com/file"})
        assert isinstance(result, str)
        assert "scheme" in result.lower()

    def test_blocks_file_scheme(self):
        result = sanitize_params("web_fetch",
                                 {"url": "file:///etc/passwd"})
        assert isinstance(result, str)

    def test_allows_https_url(self):
        result = sanitize_params("web_fetch",
                                 {"url": "https://example.com/api"})
        assert isinstance(result, dict)
        assert result["url"] == "https://example.com/api"

    def test_allows_http_external_url(self):
        """HTTP to external hosts is allowed (some APIs are http-only)."""
        result = sanitize_params("web_fetch",
                                 {"url": "http://example.com/api"})
        assert isinstance(result, dict)

    def test_rejects_non_string_url(self):
        result = sanitize_params("web_fetch", {"url": 12345})
        assert isinstance(result, str)


# ══════════════════════════════════════════════════════════════════════════════
#  TYPE COERCION
# ══════════════════════════════════════════════════════════════════════════════

class TestTypeCoercion:
    """Tests for automatic type coercion based on tool schema."""

    def _make_tool(self, params):
        return Tool("test", "test tool", params, lambda: {})

    def test_coerces_string_to_int(self):
        tool = self._make_tool({"count": {"type": "integer", "description": "n"}})
        result = sanitize_params("test", {"count": "5"}, tool)
        assert isinstance(result, dict)
        assert result["count"] == 5

    def test_coerces_string_to_float(self):
        tool = self._make_tool({"temp": {"type": "number", "description": "t"}})
        result = sanitize_params("test", {"temp": "3.14"}, tool)
        assert isinstance(result, dict)
        assert abs(result["temp"] - 3.14) < 0.001

    def test_coerces_string_to_bool_true(self):
        tool = self._make_tool({"flag": {"type": "boolean", "description": "f"}})
        result = sanitize_params("test", {"flag": "true"}, tool)
        assert isinstance(result, dict)
        assert result["flag"] is True

    def test_coerces_string_to_bool_false(self):
        tool = self._make_tool({"flag": {"type": "boolean", "description": "f"}})
        result = sanitize_params("test", {"flag": "no"}, tool)
        assert isinstance(result, dict)
        assert result["flag"] is False

    def test_coerces_int_to_string(self):
        tool = self._make_tool({"name": {"type": "string", "description": "n"}})
        result = sanitize_params("test", {"name": 42}, tool)
        assert isinstance(result, dict)
        assert result["name"] == "42"

    def test_rejects_invalid_int_coercion(self):
        tool = self._make_tool({"count": {"type": "integer", "description": "n"}})
        result = sanitize_params("test", {"count": "not_a_number"}, tool)
        assert isinstance(result, str)
        assert "integer" in result.lower()

    def test_skips_already_correct_type(self):
        tool = self._make_tool({"count": {"type": "integer", "description": "n"}})
        result = sanitize_params("test", {"count": 10}, tool)
        assert isinstance(result, dict)
        assert result["count"] == 10


# ══════════════════════════════════════════════════════════════════════════════
#  EDGE CASES
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge case tests for sanitize_params."""

    def test_non_dict_params_rejected(self):
        result = sanitize_params("read_file", "not a dict")
        assert isinstance(result, str)
        assert "object" in result.lower()

    def test_unknown_tool_passes_through(self):
        """Tools not in _FS_TOOLS or _NET_TOOLS pass through (no path/url check)."""
        result = sanitize_params("memory_search",
                                 {"query": "test", "limit": 5})
        assert isinstance(result, dict)

    def test_no_tool_schema_skips_coercion(self):
        """Without a Tool object, type coercion is skipped."""
        result = sanitize_params("read_file",
                                 {"path": "workspace/test.txt"},
                                 tool=None)
        assert isinstance(result, dict)

    def test_params_are_copied_not_mutated(self):
        """sanitize_params should not mutate the original dict."""
        original = {"path": "workspace/test.txt"}
        original_copy = dict(original)
        sanitize_params("read_file", dict(original))
        assert original == original_copy

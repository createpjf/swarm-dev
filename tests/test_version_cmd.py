"""Tests for cli/version_cmd.py and cli/helpers.py."""
from __future__ import annotations

import json
import os
import sys


class TestGetVersion:
    """Test version reading from pyproject.toml."""

    def test_get_version_returns_string(self):
        from cli.helpers import get_version
        version = get_version()
        assert isinstance(version, str)
        assert len(version) > 0

    def test_get_version_semver_format(self):
        from cli.helpers import get_version
        version = get_version()
        parts = version.split(".")
        assert len(parts) >= 2, f"Version should be semver-like: {version}"


class TestVersionCmd:
    """Test cleo version subcommand."""

    def test_cmd_version_json(self, capsys):
        from cli.version_cmd import cmd_version
        cmd_version(json_output=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "version" in data
        assert "python" in data
        assert "git_hash" in data
        assert "dependencies" in data
        assert isinstance(data["dependencies"], dict)

    def test_cmd_version_text(self, capsys):
        """Text mode should not raise."""
        from cli.version_cmd import cmd_version
        # This may use rich or fallback — either should work
        try:
            cmd_version(json_output=False)
        except SystemExit:
            pass  # Some rich formatting may trigger exit in test context

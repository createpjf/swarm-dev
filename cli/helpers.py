"""Shared utilities for CLI modules."""
from __future__ import annotations

import os
import re


def get_version() -> str:
    """Read version from pyproject.toml, fallback to '0.1.0'."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

    pyproject = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "pyproject.toml")
    if tomllib and os.path.exists(pyproject):
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            return data.get("project", {}).get("version", "0.1.0")
        except Exception:
            pass

    # Fallback: simple regex parse
    if os.path.exists(pyproject):
        try:
            with open(pyproject) as f:
                for line in f:
                    if line.strip().startswith("version"):
                        m = re.search(r'"([^"]+)"', line)
                        if m:
                            return m.group(1)
        except Exception:
            pass
    return "0.1.0"

"""Non-interactive config management: get / set / unset via dot-path."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import yaml

from core.theme import theme as _theme

# ── Default config path ──────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "agents.yaml",
)


# ── Dot-path helpers ─────────────────────────────────────────────────────────

def parse_dot_path(raw: str) -> list[str]:
    """Parse a dot-separated path into segments.

    'agents.0.model' → ['agents', '0', 'model']
    'llm.provider'   → ['llm', 'provider']
    """
    return [seg for seg in raw.split(".") if seg]


def get_nested(data: dict, path: list[str]) -> tuple[bool, Any]:
    """Walk *data* along *path*, returning (found, value)."""
    current: Any = data
    for seg in path:
        if isinstance(current, dict):
            if seg in current:
                current = current[seg]
            else:
                return False, None
        elif isinstance(current, list):
            try:
                idx = int(seg)
                current = current[idx]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, current


def set_nested(data: dict, path: list[str], value: Any) -> None:
    """Set *value* at *path*, creating intermediate dicts as needed."""
    current: Any = data
    for i, seg in enumerate(path[:-1]):
        next_seg = path[i + 1]
        if isinstance(current, dict):
            if seg not in current:
                # Create list if next key looks numeric, else dict
                current[seg] = [] if _looks_numeric(next_seg) else {}
            current = current[seg]
        elif isinstance(current, list):
            try:
                idx = int(seg)
                while len(current) <= idx:
                    current.append({})
                current = current[idx]
            except ValueError:
                raise KeyError(f"Cannot index list with '{seg}'")
        else:
            raise KeyError(f"Cannot traverse into {type(current).__name__} at '{seg}'")

    # Set the leaf
    leaf = path[-1]
    if isinstance(current, dict):
        current[leaf] = value
    elif isinstance(current, list):
        idx = int(leaf)
        while len(current) <= idx:
            current.append(None)
        current[idx] = value
    else:
        raise KeyError(f"Cannot set '{leaf}' on {type(current).__name__}")


def unset_nested(data: dict, path: list[str]) -> bool:
    """Delete the leaf at *path*. Returns True if deleted, False if not found."""
    if not path:
        return False
    parent_path, leaf = path[:-1], path[-1]
    if parent_path:
        found, parent = get_nested(data, parent_path)
        if not found:
            return False
    else:
        parent = data

    if isinstance(parent, dict) and leaf in parent:
        del parent[leaf]
        return True
    elif isinstance(parent, list):
        try:
            idx = int(leaf)
            if 0 <= idx < len(parent):
                parent.pop(idx)
                return True
        except ValueError:
            pass
    return False


def parse_value(raw: str) -> Any:
    """Smartly coerce a CLI string to Python type.

    true/false → bool,  123 → int,  3.14 → float,  rest → str
    """
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low == "null" or low == "none":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    # JSON array / object?
    if raw.startswith(("[", "{")):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


def _looks_numeric(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


# ── Pretty output ────────────────────────────────────────────────────────────

def _format_value(value: Any) -> str:
    """Format a config value for display."""
    if isinstance(value, (dict, list)):
        return yaml.dump(value, default_flow_style=False).rstrip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


# ── Main command ─────────────────────────────────────────────────────────────

def cmd_config(action: str, path: str = "", value: str = "",
               json_output: bool = False):
    """Handle `cleo config get|set|unset <path> [value]`."""
    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    def _print(msg: str):
        if console:
            console.print(msg)
        else:
            print(msg)

    config_path = os.environ.get("CLEO_CONFIG", _CONFIG_PATH)

    # ── get ───────────────────────────────────────────────────────────
    if action == "get":
        if not os.path.exists(config_path):
            _print(f"  [{_theme.error}]✗[/{_theme.error}] Config not found: {config_path}")
            sys.exit(1)

        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        if not path:
            # Show entire config
            if json_output:
                print(json.dumps(cfg, indent=2, default=str))
            else:
                print(yaml.dump(cfg, default_flow_style=False))
            return

        segments = parse_dot_path(path)
        found, val = get_nested(cfg, segments)
        if not found:
            _print(f"  [{_theme.error}]✗[/{_theme.error}] Key not found: {path}")
            sys.exit(1)

        if json_output:
            print(json.dumps(val, indent=2, default=str))
        else:
            _print(_format_value(val))

    # ── set ───────────────────────────────────────────────────────────
    elif action == "set":
        if not path:
            _print(f"  [{_theme.error}]✗[/{_theme.error}] Usage: cleo config set <path> <value>")
            sys.exit(1)

        if not os.path.exists(config_path):
            cfg: dict = {}
        else:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

        segments = parse_dot_path(path)
        parsed = parse_value(value)

        # Snapshot before write
        try:
            from core.config_manager import snapshot
            snapshot(config_path, reason=f"config set {path}")
        except Exception:
            pass

        set_nested(cfg, segments, parsed)

        # Validate
        try:
            from core.config_manager import safe_write_yaml
            safe_write_yaml(config_path, cfg, reason=f"config set {path}={value}")
        except Exception:
            # Fallback: direct write
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False)

        # Post-write validation (non-blocking)
        warnings = []
        try:
            from core.config_schema import validate_config
            warnings = validate_config(config_path)
        except Exception:
            pass

        _print(f"  [{_theme.success}]✓[/{_theme.success}] {path} = {_format_value(parsed)}")
        if warnings:
            for w in warnings[:3]:
                _print(f"  [{_theme.warning}]![/{_theme.warning}] {w}")

    # ── unset ─────────────────────────────────────────────────────────
    elif action == "unset":
        if not path:
            _print(f"  [{_theme.error}]✗[/{_theme.error}] Usage: cleo config unset <path>")
            sys.exit(1)

        if not os.path.exists(config_path):
            _print(f"  [{_theme.error}]✗[/{_theme.error}] Config not found: {config_path}")
            sys.exit(1)

        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        segments = parse_dot_path(path)

        # Snapshot before write
        try:
            from core.config_manager import snapshot
            snapshot(config_path, reason=f"config unset {path}")
        except Exception:
            pass

        deleted = unset_nested(cfg, segments)
        if not deleted:
            _print(f"  [{_theme.warning}]![/{_theme.warning}] Key not found: {path}")
            return

        try:
            from core.config_manager import safe_write_yaml
            safe_write_yaml(config_path, cfg, reason=f"config unset {path}")
        except Exception:
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False)

        _print(f"  [{_theme.success}]✓[/{_theme.success}] Removed: {path}")

    else:
        _print(f"  [{_theme.error}]✗[/{_theme.error}] Unknown action: {action}")
        _print(f"  [{_theme.muted}]Usage: cleo config <get|set|unset> <path> [value][/{_theme.muted}]")
        sys.exit(1)

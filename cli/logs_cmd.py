"""Log viewer CLI commands."""
from __future__ import annotations

import json
import sys

from core.theme import theme as _theme


def cmd_logs(follow: bool = False, agent: str = "",
             level: str = "", since: str = "", lines: int = 50,
             export: str = ""):
    """View aggregated agent logs. Supports --export json/jsonl for structured output."""
    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    from core.log_viewer import LogViewer
    viewer = LogViewer()

    # ── Export mode: output structured JSON/JSONL to stdout ────────────
    if export in ("json", "jsonl"):
        entries = viewer.tail(n=lines, agent=agent,
                              level=level, since=since)
        if export == "json":
            # Single JSON array
            out = []
            for entry in entries:
                out.append(_entry_to_dict(entry))
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            # JSONL: one JSON object per line
            for entry in entries:
                json.dump(_entry_to_dict(entry), sys.stdout,
                          ensure_ascii=False, default=str)
                sys.stdout.write("\n")
        return

    # ── Follow mode ───────────────────────────────────────────────────
    if follow:
        if console:
            console.print(f"[{_theme.muted}]Following logs (Ctrl+C to stop)...[/{_theme.muted}]\n")
        else:
            print("Following logs (Ctrl+C to stop)...\n")
        try:
            for entry in viewer.follow(agent=agent, level=level):
                line = viewer.format_entry(entry, color=console is not None)
                if console:
                    console.print(line)
                else:
                    print(line)
        except KeyboardInterrupt:
            if console:
                console.print(f"\n[{_theme.muted}]Stopped.[/{_theme.muted}]")
            else:
                print("\nStopped.")
    else:
        entries = viewer.tail(n=lines, agent=agent,
                              level=level, since=since)
        if not entries:
            if console:
                console.print(f"  [{_theme.muted}]No log entries found.[/{_theme.muted}]")
            else:
                print("  No log entries found.")
            return
        for entry in entries:
            line = viewer.format_entry(entry, color=console is not None)
            if console:
                console.print(line)
            else:
                print(line)


def _entry_to_dict(entry) -> dict:
    """Convert a log entry (dict or object) to a plain dict for JSON export."""
    if isinstance(entry, dict):
        return entry
    # LogViewer entries are typically dicts, but handle object-like entries
    result = {}
    for key in ("ts", "level", "logger", "msg", "agent", "cid", "extra"):
        val = entry.get(key) if isinstance(entry, dict) else getattr(entry, key, None)
        if val is not None:
            result[key] = val
    return result

"""
core/log_viewer.py
Unified log viewer — aggregates logs from multiple sources for `cleo logs`.

Supports:
  - Tail mode: show last N lines
  - Follow mode: tail -f with live updates
  - Agent filtering: --agent jerry
  - Level filtering: --level error
  - Time range: --since 1h

Log sources:
  - .logs/cleo.log          — system log (all modules)
  - .logs/exec.log          — command execution log (JSONL)
  - .logs/tool_audit.log    — tool usage audit (JSONL)
  - .logs/{agent_id}.log    — per-agent log files
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

try:
    from core.theme import theme as _theme
except ImportError:
    class _FallbackTheme:
        success = "green"; error = "red"; warning = "yellow"
        muted = "dim"; heading = "bold"
    _theme = _FallbackTheme()

LOG_DIR = ".logs"
POLL_INTERVAL = 0.5  # seconds between checks in follow mode


@dataclass
class LogEntry:
    """Normalized log entry from any source."""
    timestamp: float        # Unix timestamp
    ts_str: str             # Human-readable timestamp
    level: str              # INFO/WARNING/ERROR/DEBUG
    source: str             # "system" | "exec" | "audit" | agent_id
    message: str            # Log message content
    agent: str = ""         # Agent ID (if applicable)
    extra: dict | None = None  # Additional structured data


class LogViewer:
    """Aggregated log viewer with filtering and follow mode."""

    def __init__(self, log_dir: str = LOG_DIR):
        self.log_dir = log_dir

    def tail(self, n: int = 50, agent: str = "",
             level: str = "", since: str = "") -> list[LogEntry]:
        """Return the last N log entries, optionally filtered.

        Args:
            n: Number of entries to return
            agent: Filter by agent ID
            level: Filter by level (error, warning, info, debug)
            since: Time range string (e.g., "1h", "30m", "2d")
        """
        entries = self._collect_all_entries(since_str=since)

        # Apply filters
        if agent:
            entries = [e for e in entries if e.agent == agent or e.source == agent]
        if level:
            level_up = level.upper()
            level_set = _level_and_above(level_up)
            entries = [e for e in entries if e.level in level_set]

        # Sort by timestamp and return last N
        entries.sort(key=lambda e: e.timestamp)
        return entries[-n:]

    def follow(self, agent: str = "", level: str = "",
               callback=None):
        """Follow mode — yield new log entries as they appear.

        Args:
            agent: Filter by agent ID
            level: Filter by level
            callback: Called with each new LogEntry (for Rich display)
        """
        # Track file positions
        positions: dict[str, int] = {}
        for path in self._log_paths():
            try:
                positions[path] = os.path.getsize(path)
            except OSError:
                positions[path] = 0

        while True:
            new_entries = []
            for path in self._log_paths():
                try:
                    current_size = os.path.getsize(path)
                except OSError:
                    continue

                prev_pos = positions.get(path, 0)
                if current_size <= prev_pos:
                    # File might have been rotated
                    if current_size < prev_pos:
                        prev_pos = 0
                    else:
                        continue

                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(prev_pos)
                        new_data = f.read()
                    positions[path] = current_size
                except OSError:
                    continue

                source = self._source_from_path(path)
                for line in new_data.splitlines():
                    entry = self._parse_line(line, source)
                    if entry:
                        new_entries.append(entry)

            # Apply filters
            if agent:
                new_entries = [e for e in new_entries
                               if e.agent == agent or e.source == agent]
            if level:
                level_up = level.upper()
                level_set = _level_and_above(level_up)
                new_entries = [e for e in new_entries if e.level in level_set]

            # Sort and yield
            new_entries.sort(key=lambda e: e.timestamp)
            for entry in new_entries:
                if callback:
                    callback(entry)
                yield entry

            time.sleep(POLL_INTERVAL)

    # ── Formatting ──────────────────────────────────────────────────────

    @staticmethod
    def format_entry(entry: LogEntry, color: bool = True) -> str:
        """Format a log entry for terminal display."""
        level_style = {
            "ERROR": (_theme.error, "ERR"),
            "WARNING": (_theme.warning, "WRN"),
            "INFO": (_theme.info, "INF"),
            "DEBUG": (_theme.muted, "DBG"),
        }
        style, abbr = level_style.get(entry.level, ("", entry.level[:3]))

        source = entry.agent or entry.source
        if color:
            return (f"[{_theme.muted}]{entry.ts_str}[/{_theme.muted}] "
                    f"[{style}]{abbr}[/{style}] "
                    f"[{_theme.heading}]{source:>8}[/{_theme.heading}] "
                    f"{entry.message}")
        else:
            return f"{entry.ts_str} {abbr} {source:>8} {entry.message}"

    # ── Internal ────────────────────────────────────────────────────────

    def _log_paths(self) -> list[str]:
        """Get all log file paths."""
        paths = []
        if not os.path.isdir(self.log_dir):
            return paths
        for fname in os.listdir(self.log_dir):
            if fname.startswith(".") or fname.startswith("_"):
                continue
            if fname.endswith(".log") or fname.endswith(".jsonl"):
                paths.append(os.path.join(self.log_dir, fname))
        return sorted(paths)

    def _source_from_path(self, path: str) -> str:
        """Determine log source from file path."""
        basename = os.path.basename(path)
        if basename == "cleo.log":
            return "system"
        elif basename == "exec.log":
            return "exec"
        elif basename == "tool_audit.log":
            return "audit"
        else:
            # Agent log: jerry.log → "jerry"
            return basename.rsplit(".", 1)[0]

    def _collect_all_entries(self, since_str: str = "") -> list[LogEntry]:
        """Read all log files and collect entries."""
        cutoff = _parse_since(since_str) if since_str else 0
        entries = []

        for path in self._log_paths():
            source = self._source_from_path(path)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        entry = self._parse_line(line, source)
                        if entry and entry.timestamp >= cutoff:
                            entries.append(entry)
            except OSError:
                continue

        return entries

    def _parse_line(self, line: str, source: str) -> Optional[LogEntry]:
        """Parse a single log line into a LogEntry."""
        line = line.strip()
        if not line:
            return None

        # Try JSON (structured logs, exec.log, audit.log)
        if line.startswith("{"):
            return self._parse_json_line(line, source)

        # Try standard format: [HH:MM:SS][logger][LEVEL] message
        m = _STD_LOG_RE.match(line)
        if m:
            ts_str = m.group(1)
            logger = m.group(2)
            level = m.group(3) if m.group(3) else "INFO"
            message = m.group(4)

            # Extract agent from logger name
            agent = ""
            if logger.startswith("agent."):
                agent = logger.split(".", 1)[1]
            elif source not in ("system", "exec", "audit"):
                agent = source

            return LogEntry(
                timestamp=_ts_str_to_unix(ts_str),
                ts_str=ts_str,
                level=level.upper(),
                source=source,
                message=message,
                agent=agent,
            )

        # Fallback: treat as info message
        return LogEntry(
            timestamp=time.time(),
            ts_str=time.strftime("%H:%M:%S"),
            level="INFO",
            source=source,
            message=line[:200],
        )

    def _parse_json_line(self, line: str, source: str) -> Optional[LogEntry]:
        """Parse a JSON log line."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        ts_raw = data.get("ts", "")
        if isinstance(ts_raw, (int, float)):
            timestamp = ts_raw
            ts_str = time.strftime("%H:%M:%S", time.localtime(ts_raw))
        elif isinstance(ts_raw, str):
            timestamp = _iso_to_unix(ts_raw)
            # Extract time portion
            ts_str = ts_raw.split("T")[-1][:8] if "T" in ts_raw else ts_raw[:8]
        else:
            timestamp = time.time()
            ts_str = time.strftime("%H:%M:%S")

        level = data.get("level", "INFO").upper()
        agent = data.get("agent", "")
        msg = data.get("msg", "")

        # For exec.log: format command execution info
        if source == "exec" and "cmd" in data:
            ok = "✓" if data.get("ok") else "✗"
            cmd = data.get("cmd", "")[:60]
            elapsed = data.get("elapsed_s", 0)
            msg = f"{ok} {cmd} ({elapsed}s)"
            agent = data.get("agent", "")

        # For audit log
        if source == "audit" and "tool" in data:
            tool = data.get("tool", "")
            agent = data.get("agent", "")
            msg = f"tool:{tool} agent:{agent}"

        return LogEntry(
            timestamp=timestamp,
            ts_str=ts_str,
            level=level,
            source=source,
            message=msg,
            agent=agent,
            extra=data,
        )


# ── Patterns & Helpers ─────────────────────────────────────────────────────

# Standard log format: [HH:MM:SS][logger_name][LEVEL] message
# or: [HH:MM:SS][logger_name] message (no level)
_STD_LOG_RE = re.compile(
    r"\[(\d{2}:\d{2}:\d{2})\]"      # [HH:MM:SS]
    r"\[([^\]]+)\]"                   # [logger_name]
    r"(?:\[([A-Z]+)\])?\s*(.*)"       # optional [LEVEL] message
)


def _level_and_above(level: str) -> set[str]:
    """Return the set of log levels at or above the given level."""
    hierarchy = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    try:
        idx = hierarchy.index(level)
        return set(hierarchy[idx:])
    except ValueError:
        return {"INFO", "WARNING", "ERROR", "CRITICAL"}


def _parse_since(since_str: str) -> float:
    """Parse a time range string like '1h', '30m', '2d' into a cutoff timestamp."""
    if not since_str:
        return 0
    m = re.match(r"(\d+)\s*([smhd])", since_str.lower())
    if not m:
        return 0
    value = int(m.group(1))
    unit = m.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return time.time() - (value * multiplier.get(unit, 3600))


def _ts_str_to_unix(ts_str: str) -> float:
    """Convert HH:MM:SS to today's Unix timestamp (approximate)."""
    try:
        parts = ts_str.split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        now = time.localtime()
        t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                          h, m, s, 0, 0, -1))
        return t
    except (ValueError, IndexError):
        return time.time()


def _iso_to_unix(iso_str: str) -> float:
    """Convert ISO 8601 string to Unix timestamp."""
    try:
        # Handle both 2024-01-15T12:30:45 and 2024-01-15T12:30:45Z
        clean = iso_str.replace("Z", "+00:00")
        if "+" not in clean and len(clean) == 19:
            clean += "+00:00"
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(clean)
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()

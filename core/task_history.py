"""
core/task_history.py — Persistent task-round history for cross-task context.

Stores completed task rounds in `.task_history.jsonl` so agents can reference
prior work when handling new tasks.  Each "round" = one user submission with
all its subtasks and results.

Usage:
    from core.task_history import save_round, load_recent

    save_round(board_data)          # called before board.clear()
    recent = load_recent(n=3)       # returns last 3 round summaries
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HISTORY_FILE = ".task_history.jsonl"
MAX_RESULT_CHARS = 300          # truncate individual result summaries
MAX_DESCRIPTION_CHARS = 200     # truncate task descriptions
MAX_ROUNDS_KEPT = 50            # rotate file when it exceeds this


def _history_path() -> str:
    return os.path.join(os.getcwd(), HISTORY_FILE)


def save_round(board_data: Dict[str, Any]) -> bool:
    """Archive the current task board as one completed round.

    Args:
        board_data: dict of task_id → task dict from TaskBoard._read()

    Returns:
        True if saved successfully, False otherwise.
    """
    if not board_data:
        return False

    # Summarize each task
    tasks_summary: List[Dict[str, str]] = []
    root_description = ""

    for tid, task in board_data.items():
        status = task.get("status", "unknown")
        desc = (task.get("description") or "")[:MAX_DESCRIPTION_CHARS]
        result = (task.get("result") or "")[:MAX_RESULT_CHARS]
        agent = task.get("claimed_by", "")

        # Find root task description
        if task.get("parent") is None or task.get("parent") == "":
            root_description = desc

        tasks_summary.append({
            "id": tid,
            "status": status,
            "description": desc,
            "result": result,
            "agent": agent,
        })

    round_entry = {
        "ts": time.time(),
        "root_description": root_description,
        "task_count": len(tasks_summary),
        "tasks": tasks_summary,
    }

    try:
        path = _history_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(round_entry, ensure_ascii=False) + "\n")
        logger.info("Saved task round (%d tasks) to %s",
                     len(tasks_summary), path)
        _rotate_if_needed(path)
        return True
    except Exception as e:
        logger.error("Failed to save task round: %s", e)
        return False


def load_recent(n: int = 3) -> str:
    """Load last N task rounds and format as a context string for agents.

    Returns a human-readable summary string, or empty string if no history.
    """
    path = _history_path()
    if not os.path.exists(path):
        return ""

    try:
        rounds: List[Dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rounds.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if not rounds:
            return ""

        # Take last N
        recent = rounds[-n:]

        sections: List[str] = []
        for i, rd in enumerate(recent, 1):
            ts = rd.get("ts", 0)
            time_str = _format_ts(ts) if ts else "unknown"
            root = rd.get("root_description", "N/A")
            tasks = rd.get("tasks", [])

            task_lines: List[str] = []
            for t in tasks:
                status_icon = "✅" if t["status"] == "done" else "❌"
                line = f"  {status_icon} [{t.get('agent', '?')}] {t['description']}"
                if t.get("result"):
                    # Compact result preview
                    preview = t["result"].replace("\n", " ")[:120]
                    line += f"\n     → {preview}"
                task_lines.append(line)

            section = (
                f"### Round {i} ({time_str})\n"
                f"**Task:** {root}\n"
                f"**Subtasks ({len(tasks)}):**\n"
                + "\n".join(task_lines)
            )
            sections.append(section)

        return "\n\n".join(sections)

    except Exception as e:
        logger.error("Failed to load task history: %s", e)
        return ""


def _format_ts(ts: float) -> str:
    """Format unix timestamp to relative or absolute string."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(ts)
        now = datetime.datetime.now()
        diff = now - dt
        if diff.total_seconds() < 3600:
            mins = int(diff.total_seconds() / 60)
            return f"{mins}m ago"
        elif diff.total_seconds() < 86400:
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}h ago"
        else:
            return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(int(ts))


def _rotate_if_needed(path: str) -> None:
    """Keep only the last MAX_ROUNDS_KEPT entries."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_ROUNDS_KEPT:
            keep = lines[-MAX_ROUNDS_KEPT:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(keep)
            logger.info("Rotated task history: kept %d of %d rounds",
                         len(keep), len(lines))
    except Exception as e:
        logger.warning("Task history rotation failed: %s", e)

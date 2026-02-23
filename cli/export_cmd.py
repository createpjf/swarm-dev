"""Export CLI commands."""
from __future__ import annotations

import json
import os

from core.theme import theme as _theme


def cmd_export(task_id: str, fmt: str = "md", console=None):
    """Export a task and its subtask results to markdown or JSON."""
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    if not os.path.exists(".task_board.json"):
        if console:
            console.print(f"  [{_theme.muted}]No task board found.[/{_theme.muted}]")
        else:
            print("  No task board found.")
        return

    data = json.load(open(".task_board.json"))

    match_id = None
    for tid in data:
        if tid == task_id or tid.startswith(task_id):
            match_id = tid
            break

    if not match_id:
        if console:
            console.print(f"  [{_theme.error}]Task not found: {task_id}[/{_theme.error}]")
        else:
            print(f"  Task not found: {task_id}")
        return

    task = data[match_id]

    subtasks = []
    for tid, t in data.items():
        if t.get("parent_id") == match_id:
            subtasks.append((tid, t))

    if fmt == "json":
        export = {
            "task_id": match_id,
            "description": task.get("description", ""),
            "status": task.get("status", ""),
            "result": task.get("result", ""),
            "agent_id": task.get("agent_id"),
            "cost_usd": task.get("cost_usd", 0),
            "subtasks": [
                {
                    "task_id": tid,
                    "description": t.get("description", ""),
                    "status": t.get("status", ""),
                    "result": t.get("result", ""),
                    "agent_id": t.get("agent_id"),
                    "cost_usd": t.get("cost_usd", 0),
                }
                for tid, t in subtasks
            ],
        }
        output = json.dumps(export, indent=2, ensure_ascii=False)
    else:
        lines = []
        lines.append(f"# Task: {task.get('description', 'Untitled')}")
        lines.append(f"")
        lines.append(f"**Status:** {task.get('status', '?')}")
        lines.append(f"**Agent:** {task.get('agent_id') or '—'}")
        cost = task.get("cost_usd", 0)
        if cost:
            lines.append(f"**Cost:** ~${cost:.4f}")
        lines.append(f"**ID:** `{match_id}`")
        lines.append("")

        if task.get("result"):
            lines.append("## Result")
            lines.append("")
            lines.append(task["result"])
            lines.append("")

        if subtasks:
            lines.append("## Subtasks")
            lines.append("")
            for tid, t in subtasks:
                st = t.get("status", "?")
                icon = "✓" if st == "completed" else "✗" if st == "failed" else "○"
                lines.append(f"### {icon} {t.get('description', 'Subtask')}")
                lines.append(f"**Agent:** {t.get('agent_id') or '—'}  |  **Status:** {st}")
                sub_cost = t.get("cost_usd", 0)
                if sub_cost:
                    lines.append(f"**Cost:** ~${sub_cost:.4f}")
                if t.get("result"):
                    lines.append("")
                    lines.append(t["result"])
                lines.append("")

        total_cost = cost + sum(t.get("cost_usd", 0) for _, t in subtasks)
        if total_cost:
            lines.append(f"---\n**Total Cost:** ~${total_cost:.4f}")

        output = "\n".join(lines)

    ext = "json" if fmt == "json" else "md"
    filename = f"export_{match_id[:8]}.{ext}"
    with open(filename, "w") as f:
        f.write(output)

    if console:
        console.print(f"  [{_theme.success}]✓[/{_theme.success}] Exported to [{_theme.heading}]{filename}[/{_theme.heading}]")
        if fmt == "md":
            console.print(f"  [{_theme.muted}]{len(subtasks)} subtask(s) included[/{_theme.muted}]")
    else:
        print(f"  Exported to {filename}")

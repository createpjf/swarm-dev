"""Status and scores CLI commands."""
from __future__ import annotations

import json
import os
import re

from core.theme import theme as _theme


def cmd_status(json_output: bool = False):
    data = json.load(open(".task_board.json")) if os.path.exists(".task_board.json") else {}
    if json_output:
        print(json.dumps(data, indent=2, default=str))
        return
    print(f"\n{'ID':36}  {'STATUS':12}  {'AGENT':12}  DESCRIPTION")
    print("-" * 110)
    for tid, t in data.items():
        print(f"{tid:36}  {t['status']:12}  {(t.get('agent_id') or '-'):12}  "
              f"{t['description'][:40]}")
    print()


def cmd_scores(console=None, json_output: bool = False):
    path = "memory/reputation_cache.json"
    if not os.path.exists(path):
        if json_output:
            print(json.dumps({"agents": {}}, indent=2))
            return
        if console:
            console.print(f"  [{_theme.muted}]No scores yet.[/{_theme.muted}]\n")
        else:
            print("No scores yet.")
        return
    cache = json.load(open(path))
    if json_output:
        print(json.dumps(cache, indent=2, default=str))
        return

    from reputation.scorer import ScoreAggregator
    sc = ScoreAggregator()

    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            pass

    if console:
        from rich.table import Table
        console.print()
        tbl = Table(box=None, padding=(0, 1), show_header=True)
        tbl.add_column("Agent", style=_theme.heading, min_width=12)
        tbl.add_column("Score", justify="right", min_width=6)
        tbl.add_column("Trend", min_width=10)
        tbl.add_column("Status")
        for agent_id, data in cache.items():
            score  = data.get("composite", 0)
            trend  = sc.trend(agent_id)
            status = sc.threshold_status(agent_id)
            if score >= 70:
                sc_style = _theme.success
            elif score >= 50:
                sc_style = _theme.warning
            else:
                sc_style = _theme.error
            status_style = {"healthy": _theme.success, "watch": _theme.warning,
                            "warning": _theme.error, "evolve": f"bold {_theme.error}"}.get(status, "")
            tbl.add_row(
                agent_id,
                f"[{sc_style}]{score:.1f}[/{sc_style}]",
                trend,
                f"[{status_style}]{status}[/{status_style}]",
            )
        console.print(tbl)
        console.print()
    else:
        print(f"\n{'AGENT':15}  {'SCORE':6}  TREND / STATUS")
        print("-" * 50)
        for agent_id, data in cache.items():
            score  = data.get("composite", 0)
            trend  = sc.trend(agent_id)
            status = sc.threshold_status(agent_id)
            print(f"{agent_id:15}  {score:6.1f}  {trend:10}  {status}")
        print()


def cmd_run(task: str):
    from core.orchestrator import Orchestrator
    orch = Orchestrator()
    print(f"\n  Submitting task: {task!r}\n")
    orch.run(task)
    print("\n  All agents finished.\n")
    cmd_status()

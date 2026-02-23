"""Evolution management CLI commands."""
from __future__ import annotations

import json
import os

from core.theme import theme as _theme


def cmd_evolve_confirm(agent_id: str):
    path = f"memory/pending_swaps/{agent_id}.json"
    if not os.path.exists(path):
        print(f"No pending model swap for {agent_id}.")
        return
    swap = json.load(open(path))
    try:
        import questionary
        from core.onboard import STYLE
        from rich.console import Console
        console = Console()
        console.print(f"\n  [{_theme.heading}]Pending model swap for {agent_id}[/{_theme.heading}]")
        console.print(f"  New model : [{_theme.info}]{swap['new_model']}[/{_theme.info}]")
        console.print(f"  Reason    : [{_theme.muted}]{swap['reason']}[/{_theme.muted}]\n")
        ok = questionary.confirm("Apply model swap?", default=False, style=STYLE).ask()
        if ok:
            from reputation.evolution import EvolutionEngine
            from reputation.scorer import ScoreAggregator
            from core.task_board import TaskBoard
            eng = EvolutionEngine(ScoreAggregator(), TaskBoard())
            eng.apply_model_swap(agent_id)
            console.print(f"  [{_theme.success}]✓[/{_theme.success}] Model swap applied for {agent_id}.\n")
        else:
            console.print(f"  [{_theme.muted}]Cancelled.[/{_theme.muted}]\n")
    except ImportError:
        print(f"\nPending model swap for {agent_id}:")
        print(f"  New model : {swap['new_model']}")
        print(f"  Reason    : {swap['reason']}\n")
        confirm = input("Confirm? [y/N] ").strip().lower()
        if confirm == "y":
            from reputation.evolution import EvolutionEngine
            from reputation.scorer import ScoreAggregator
            from core.task_board import TaskBoard
            eng = EvolutionEngine(ScoreAggregator(), TaskBoard())
            eng.apply_model_swap(agent_id)
            print(f"  Model swap applied for {agent_id}.")
        else:
            print("  Cancelled.")

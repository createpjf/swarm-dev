"""Workflow management CLI commands."""
from __future__ import annotations

import os

from core.theme import theme as _theme


def cmd_workflows(console=None):
    """List available workflow definitions."""
    from core.workflow import list_workflows
    workflows = list_workflows()

    if not workflows:
        if console:
            console.print(f"  [{_theme.muted}]No workflows found in workflows/ directory.[/{_theme.muted}]\n")
        else:
            print("  No workflows found.")
        return

    if console:
        from rich.table import Table
        console.print()
        tbl = Table(box=None, padding=(0, 1), show_header=True)
        tbl.add_column("Workflow", style=_theme.heading)
        tbl.add_column("Steps", justify="right")
        tbl.add_column("Description", style=_theme.muted)
        for w in workflows:
            tbl.add_row(w["name"], str(w["steps"]), w["description"][:50])
        console.print(tbl)
        console.print(f"\n  [{_theme.muted}]Use: workflow <name> <task> to run a workflow[/{_theme.muted}]\n")
    else:
        for w in workflows:
            print(f"  {w['name']:25} ({w['steps']} steps)  {w['description'][:40]}")


def cmd_workflow_run(name: str, task_input: str = ""):
    """Run a named workflow with the given input."""
    import asyncio
    from core.workflow import list_workflows, load_workflow, WorkflowEngine

    workflows = list_workflows()
    match = None
    for w in workflows:
        fname = w["file"].replace(".yaml", "").replace(".yml", "")
        if fname == name or w["name"].lower() == name.lower():
            match = w
            break

    if not match:
        available = ", ".join(w["file"].replace(".yaml", "") for w in workflows)
        print(f"  Workflow '{name}' not found.\n  Available: {available}")
        return

    if not task_input:
        task_input = input("  Task input: ").strip()
        if not task_input:
            print("  No input provided. Aborting.")
            return

    print(f"\n  Running workflow: {match['name']}")
    print(f"  Input: {task_input[:60]}{'…' if len(task_input) > 60 else ''}\n")

    workflow = load_workflow(os.path.join("workflows", match["file"]))

    if not os.path.exists("config/agents.yaml"):
        print("  No config found. Run `cleo onboard` first.")
        return

    import yaml
    with open("config/agents.yaml") as f:
        config = yaml.safe_load(f) or {}

    engine = WorkflowEngine(config)

    def on_step_complete(step):
        status = "✓" if step.status.value == "completed" else "✗"
        print(f"  {status} Step '{step.id}' ({step.agent}) — {step.status.value}")

    try:
        result = asyncio.run(engine.run_workflow(
            workflow,
            initial_vars={"task": task_input},
            on_step_complete=on_step_complete,
        ))
        print(f"\n  Workflow {result.status} ({len(result.steps)} steps)")
        for step in reversed(result.steps):
            if step.result:
                preview = step.result[:200]
                print(f"\n  Final output ({step.id}):\n  {preview}{'…' if len(step.result) > 200 else ''}\n")
                break
    except Exception as e:
        print(f"\n  Workflow failed: {e}")

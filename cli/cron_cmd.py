"""Cron scheduler management CLI commands."""
from __future__ import annotations

from core.theme import theme as _theme


def cmd_cron(action: str, name: str = "", act: str = "", payload: str = "",
             schedule_type: str = "", schedule: str = "", job_id: str = "",
             console=None):
    """Cron scheduler management CLI."""
    try:
        from rich.console import Console
        from rich.table import Table
        console = console or Console()
    except ImportError:
        console = None

    from core.cron import list_jobs, add_job, remove_job, get_job, _execute_job

    if action == "list":
        jobs = list_jobs()
        if not jobs:
            print("  No cron jobs.")
            return
        if console:
            table = Table(title="Cron Jobs", show_lines=True)
            table.add_column("ID", style=_theme.info, width=12)
            table.add_column("Name", style=_theme.heading)
            table.add_column("Action")
            table.add_column("Schedule")
            table.add_column("Next Run")
            table.add_column("Runs", justify="right")
            table.add_column("Enabled")
            for j in jobs:
                next_run = j.get("next_run", "—")
                if next_run and len(next_run) > 19:
                    next_run = next_run[:19]
                table.add_row(
                    j["id"], j["name"], j["action"],
                    f"{j['schedule_type']}: {j['schedule']}",
                    next_run or "—",
                    str(j.get("run_count", 0)),
                    "✓" if j.get("enabled") else "✗",
                )
            console.print(table)
        else:
            for j in jobs:
                print(f"  {j['id']}  {j['name']}  {j['action']}:{j['payload'][:30]}  "
                      f"{j['schedule_type']}:{j['schedule']}  "
                      f"runs={j.get('run_count', 0)}")

    elif action == "add":
        if not all([name, act, payload, schedule_type, schedule]):
            print("Usage: cleo cron add --name NAME --action task|exec|webhook "
                  "--payload PAYLOAD --type once|interval|cron --schedule SCHEDULE")
            return
        job = add_job(name, act, payload, schedule_type, schedule)
        print(f"  ✓ Job created: {job['id']} ({job['name']})")
        print(f"    Next run: {job.get('next_run', '—')}")

    elif action == "remove":
        if not job_id:
            print("Usage: cleo cron remove --id JOB_ID")
            return
        if remove_job(job_id):
            print(f"  ✓ Job {job_id} removed")
        else:
            print(f"  ✗ Job {job_id} not found")

    elif action == "run":
        if not job_id:
            print("Usage: cleo cron run --id JOB_ID")
            return
        job = get_job(job_id)
        if not job:
            print(f"  ✗ Job {job_id} not found")
            return
        print(f"  Running job: {job['name']}...")
        ok, msg = _execute_job(job)
        print(f"  {'✓' if ok else '✗'} {msg}")

    else:
        print("Usage: cleo cron <list|add|remove|run>")

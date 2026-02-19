#!/usr/bin/env python3
"""
main.py  —  Agent Stack CLI
Usage:
  swarm                       # interactive chat mode (default)
  swarm configure             # full setup wizard (re-configure)
  swarm --setup               # full setup wizard, then enter chat
  swarm run "..."             # one-shot task
  swarm status                # show task board
  swarm scores                # show reputation scores
  swarm doctor                # system health check
  swarm gateway [start]       # start HTTP gateway (foreground)
  swarm gateway status        # gateway status + health probe
  swarm gateway stop          # stop gateway process
  swarm gateway restart       # restart gateway
  swarm gateway install       # install as background daemon
  swarm gateway uninstall     # remove background daemon
  swarm agents add <name>     # add an agent to the team
  swarm chain status           # on-chain identity status
  swarm chain init <agent>     # initialize agent on-chain
  swarm chain balance          # check USDC balances
  swarm chain health           # chain health check

Chat commands:
  /configure  — re-run full setup wizard
  /config     — show current agent team
  /status     — task board
  /scores     — reputation scores
  /gateway    — gateway status & control
  /doctor     — system health check
  /clear      — clear task history
  /help       — show commands
  exit        — quit
"""

import argparse
import json
import os
import sys

# Load .env before anything else
from core.env_loader import load_dotenv
load_dotenv()


# ── Interactive Chat Mode ──────────────────────────────────────────────────────

def interactive_main():
    """OpenClaw-style interactive CLI: onboard → chat loop."""

    # Handle --setup flag → run full wizard then enter chat
    if "--setup" in sys.argv:
        cmd_init()
        load_dotenv()  # reload .env after wizard

    try:
        from rich.console import Console
        from rich.prompt import Prompt
        from rich.panel import Panel
        from rich.markdown import Markdown
        from rich import box
    except ImportError:
        print("ERROR: 'rich' is required.  pip3 install rich")
        sys.exit(1)

    console = Console()

    # ── Banner ──
    console.print(r"""[bold magenta]
   ███████╗██╗    ██╗ █████╗ ██████╗ ███╗   ███╗
   ██╔════╝██║    ██║██╔══██╗██╔══██╗████╗ ████║
   ███████╗██║ █╗ ██║███████║██████╔╝██╔████╔██║
   ╚════██║██║███╗██║██╔══██║██╔══██╗██║╚██╔╝██║
   ███████║╚███╔███╔╝██║  ██║██║  ██║██║ ╚═╝ ██║
   ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝[/bold magenta]
[dim]       type a task · /help · /config · exit[/dim]
""")

    # ── First-run check ──
    if not os.path.exists("config/agents.yaml"):
        from core.onboard import run_quick_setup
        ok = run_quick_setup()
        if not ok:
            return
        # Reload env after setup wrote .env
        load_dotenv()

    # ── Load config & show team ──
    import yaml
    with open("config/agents.yaml") as f:
        config = yaml.safe_load(f)

    agents = config.get("agents", [])
    agent_names = ", ".join(a["id"] for a in agents)
    console.print(f"  [dim]Agents: {agent_names}[/dim]")

    # ── Auto-start Gateway (daemon) ──
    if "--no-gateway" not in sys.argv:
        try:
            from core.gateway import start_gateway, DEFAULT_PORT
            gw_port = int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
            gw_token = os.environ.get("SWARM_GATEWAY_TOKEN", "")
            gw_server = start_gateway(port=gw_port, token=gw_token, daemon=True)
            if gw_server:
                console.print(f"  [dim]Gateway:[/dim] [bold]http://127.0.0.1:{gw_port}/[/bold]  [dim]/gateway for status[/dim]")
            else:
                console.print(f"  [yellow]Gateway: port {gw_port} in use (use --force or /gateway restart)[/yellow]")
        except Exception as e:
            console.print(f"  [yellow]Gateway: {e}[/yellow]")
    else:
        console.print(f"  [dim]Gateway: skipped (--no-gateway)[/dim]")
    console.print()

    # ── Chat loop ──
    from core.orchestrator import Orchestrator
    from core.task_board import TaskBoard

    while True:
        try:
            task_text = Prompt.ask("[bold green]You[/bold green]")
        except (KeyboardInterrupt, EOFError):
            from core.i18n import t as _t
            console.print(f"\n[dim]{_t('cmd.bye')}[/dim]")
            break

        task_text = task_text.strip()
        if not task_text:
            continue

        # ── Commands ──
        # /command → slash command;  bare "exit"/"quit"/"configure" also recognized
        _lower = task_text.lower()
        cmd = _lower.lstrip("/") if _lower.startswith("/") else None

        if _lower in ("exit", "quit"):
            from core.i18n import t as _t
            console.print(f"[dim]{_t('cmd.bye')}[/dim]")
            break

        if cmd == "help":
            from core.i18n import t as _t
            from rich.table import Table as _HelpTbl
            htbl = _HelpTbl(
                show_header=False, show_edge=False, box=None,
                padding=(0, 1), expand=False,
            )
            htbl.add_column("cmd", style="bold", min_width=18)
            htbl.add_column("desc", style="dim")
            _cmds = [
                ("/status",          _t("help.status")),
                ("/scores",          _t("help.scores")),
                ("/usage",           _t("help.usage")),
                ("/budget",          _t("help.budget")),
                ("/cancel",          _t("help.cancel")),
                ("/workflows",       _t("help.workflows")),
                ("/config",          _t("help.config")),
                ("/config history",  _t("help.config_hist")),
                ("/config rollback", _t("help.config_roll")),
                ("/configure",       _t("help.configure")),
                ("/gateway",         _t("help.gateway")),
                ("/chain",           _t("help.chain")),
                ("/doctor",          _t("help.doctor")),
                ("/clear",           _t("help.clear")),
                ("exit",             _t("help.exit")),
            ]
            for c, d in _cmds:
                htbl.add_row(c, d)
            console.print(Panel(htbl, title="Commands", border_style="dim"))
            continue

        if cmd == "status":
            _show_status_rich(console)
            continue

        if cmd == "scores":
            cmd_scores(console)
            continue

        if cmd == "doctor":
            cmd_doctor(console)
            continue

        if cmd == "usage":
            cmd_usage(console)
            continue

        if cmd == "workflows":
            cmd_workflows(console)
            continue

        if cmd == "cancel":
            from core.i18n import t as _t
            board = TaskBoard()
            data = board._read()
            active = {tid: t for tid, t in data.items()
                      if t.get("status") in ("pending", "claimed", "review", "paused")}
            if not active:
                console.print(f"  [dim]{_t('cmd.no_active')}[/dim]\n")
                continue
            # If only 1 active task, cancel directly
            if len(active) == 1:
                tid = next(iter(active))
                board.cancel(tid)
                console.print(f"  [yellow]{_t('cancel.done', n=1)}[/yellow]\n")
                continue
            # Multiple tasks — offer checkbox picker
            try:
                import questionary
                from core.onboard import STYLE
                choices = []
                for tid, t in active.items():
                    agent = t.get("agent_id") or "—"
                    desc = t.get("description", "")[:40]
                    st = t.get("status", "?")
                    label = f"[{st}] {agent}: {desc}"
                    choices.append(questionary.Choice(label, value=tid, checked=True))
                selected = questionary.checkbox(
                    _t("cancel.select"),
                    choices=choices,
                    style=STYLE,
                ).ask()
                if selected is None:
                    console.print(f"  [dim]{_t('cmd.cancelled')}[/dim]\n")
                    continue
                if not selected:
                    console.print(f"  [dim]{_t('cancel.none_selected')}[/dim]\n")
                    continue
                for tid in selected:
                    board.cancel(tid)
                console.print(f"  [yellow]{_t('cancel.done', n=len(selected))}[/yellow]\n")
            except ImportError:
                # Fallback: cancel all
                count = board.cancel_all()
                console.print(f"  [yellow]{_t('cancel.done', n=count)}[/yellow]\n")
            continue

        if cmd == "budget":
            cmd_budget(console)
            continue

        if cmd == "config":
            from rich.table import Table as RichTable
            tbl = RichTable(box=None, padding=(0, 1), show_header=True)
            tbl.add_column("Agent", style="bold")
            tbl.add_column("Provider")
            tbl.add_column("Model")
            tbl.add_column("Skills", style="dim")
            global_provider = config.get("llm", {}).get("provider", "?")
            for a in agents:
                llm = a.get("llm", {})
                p = llm.get("provider", global_provider)
                m = a.get("model", "?")
                sk = ", ".join(a.get("skills", []))
                tbl.add_row(a["id"], p, m, sk)
            console.print(tbl)
            mem = config.get("memory", {}).get("backend", "?")
            chain_on = "✓" if config.get("chain", {}).get("enabled") else "✗"
            console.print(f"  [dim]Memory: {mem}  |  Chain: {chain_on}[/dim]\n")
            continue

        if cmd == "clear":
            from core.i18n import t as _t
            try:
                import questionary
                from core.onboard import STYLE
                import glob as _glob
                choices = [
                    questionary.Choice(_t("clear.tasks"), value="tasks", checked=True),
                    questionary.Choice(_t("clear.context"), value="context", checked=True),
                    questionary.Choice(_t("clear.mailboxes"), value="mailboxes", checked=True),
                    questionary.Choice(_t("clear.usage"), value="usage", checked=False),
                ]
                selected = questionary.checkbox(
                    _t("clear.select"), choices=choices, style=STYLE,
                ).ask()
                if not selected:
                    console.print(f"  [dim]{_t('cmd.cancelled')}[/dim]\n")
                    continue
                cleared_parts = []
                if "tasks" in selected:
                    board = TaskBoard()
                    result = board.clear(force=False)
                    if result == -1:
                        force = questionary.confirm(
                            _t("cmd.active_exist"), default=False, style=STYLE,
                        ).ask()
                        if force:
                            board.clear(force=True)
                            cleared_parts.append(_t("clear.tasks"))
                    else:
                        cleared_parts.append(_t("clear.tasks"))
                if "context" in selected:
                    if os.path.exists(".context_bus.json"):
                        os.remove(".context_bus.json")
                    cleared_parts.append(_t("clear.context"))
                if "mailboxes" in selected:
                    for fp in _glob.glob(".mailboxes/*.jsonl"):
                        os.remove(fp)
                    cleared_parts.append(_t("clear.mailboxes"))
                if "usage" in selected:
                    if os.path.exists("memory/usage.json"):
                        os.remove("memory/usage.json")
                    cleared_parts.append(_t("clear.usage"))
                if cleared_parts:
                    console.print(f"  [dim]{_t('cmd.cleared')}: {', '.join(cleared_parts)}[/dim]\n")
                else:
                    console.print(f"  [dim]{_t('cmd.cancelled')}[/dim]\n")
            except ImportError:
                # Fallback: simple clear
                board = TaskBoard()
                result = board.clear(force=True)
                for fp in [".context_bus.json"]:
                    if os.path.exists(fp):
                        os.remove(fp)
                import glob as _glob
                for fp in _glob.glob(".mailboxes/*.jsonl"):
                    os.remove(fp)
                console.print(f"  [dim]{_t('cmd.cleared')} ({result})[/dim]\n")
            continue

        if cmd == "config history":
            from core.i18n import t as _t
            from core.config_manager import history as config_history
            entries = config_history("config/agents.yaml")
            if not entries:
                console.print(f"  [dim]{_t('config.no_backups')}[/dim]\n")
            else:
                from rich.table import Table as RichTable
                tbl = RichTable(box=None, padding=(0, 1), show_header=True)
                tbl.add_column("#", justify="right", style="bold", width=3)
                tbl.add_column("Timestamp", min_width=19)
                tbl.add_column("Hash", style="dim", width=12)
                tbl.add_column("Reason")
                import time as _t2
                for i, e in enumerate(entries):
                    ts = _t2.strftime("%Y-%m-%d %H:%M:%S", _t2.localtime(e["timestamp"]))
                    tbl.add_row(str(i), ts, e["hash"], e.get("reason", ""))
                console.print(tbl)
                console.print()
            continue

        if cmd and cmd.startswith("config rollback"):
            from core.i18n import t as _t
            from core.config_manager import rollback as config_rollback
            from core.config_manager import history as config_hist
            entries = config_hist("config/agents.yaml")
            if not entries:
                console.print(f"  [red]{_t('config.rollback_fail')}[/red]\n")
                continue
            # Try interactive select
            try:
                import questionary
                from core.onboard import STYLE
                import time as _t2
                choices = []
                for i, e in enumerate(entries):
                    ts = _t2.strftime("%Y-%m-%d %H:%M", _t2.localtime(e["timestamp"]))
                    reason = e.get("reason", "")
                    label = f"{ts}  {e['hash']}  {reason}"
                    choices.append(questionary.Choice(label, value=i))
                selected = questionary.select(
                    _t("config.select_ver"), choices=choices, style=STYLE,
                ).ask()
                if selected is None:
                    console.print(f"  [dim]{_t('cmd.cancelled')}[/dim]\n")
                    continue
                version = selected
            except ImportError:
                # Fallback: use version number from command
                parts = cmd.split()
                version = int(parts[2]) if len(parts) > 2 else -1
            ok = config_rollback("config/agents.yaml", version=version)
            if ok:
                import yaml as _yaml
                with open("config/agents.yaml") as f:
                    config = _yaml.safe_load(f)
                agents = config.get("agents", [])
                console.print(f"  [green]{_t('config.rolled_back')}[/green] Agents: {', '.join(a['id'] for a in agents)}\n")
            else:
                console.print(f"  [red]{_t('config.rollback_fail')}[/red]\n")
            continue

        if cmd and cmd.startswith("gateway"):
            parts = cmd.split()
            gw_action = parts[1] if len(parts) > 1 else "status"
            if gw_action == "status":
                _show_gateway_status(console)
            elif gw_action == "stop":
                from core.gateway import kill_port, DEFAULT_PORT
                gw_port = int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
                from core.i18n import t as _t
                console.print(f"  [dim]{_t('gw.stopping')}[/dim]")
                killed = kill_port(gw_port)
                if killed:
                    console.print(f"  [green]\u2713[/green] Gateway stopped\n")
                else:
                    console.print(f"  [dim]{_t('gw.not_running')}[/dim]\n")
            elif gw_action == "restart":
                from core.gateway import kill_port, start_gateway, DEFAULT_PORT
                gw_port = int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
                gw_token = os.environ.get("SWARM_GATEWAY_TOKEN", "")
                from core.i18n import t as _t
                console.print(f"  [dim]{_t('gw.restarting')}[/dim]")
                kill_port(gw_port)
                import time as _time
                _time.sleep(0.5)
                srv = start_gateway(port=gw_port, token=gw_token, daemon=True)
                if srv:
                    console.print(f"  [green]\u2713[/green] {_t('gw.started')} — http://127.0.0.1:{gw_port}/\n")
                else:
                    console.print(f"  [red]\u2717[/red] {_t('gw.port_in_use', port=gw_port)}\n")
            elif gw_action == "install":
                from core.gateway import generate_token, DEFAULT_PORT
                from core.daemon import install_daemon
                gw_port = int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
                gw_token = os.environ.get("SWARM_GATEWAY_TOKEN", "")
                if not gw_token:
                    gw_token = generate_token()
                ok, msg = install_daemon(gw_port, gw_token)
                icon = "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"
                console.print(f"  {icon} {msg}\n")
            elif gw_action == "uninstall":
                from core.daemon import uninstall_daemon
                ok, msg = uninstall_daemon()
                icon = "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"
                console.print(f"  {icon} {msg}\n")
            else:
                # Default: show status
                _show_gateway_status(console)
            continue

        if cmd and cmd.startswith("chain"):
            parts = cmd.split()
            action = parts[1] if len(parts) > 1 else "status"
            aid = parts[2] if len(parts) > 2 else None
            cmd_chain(action, aid, console)
            continue

        if cmd in ("setup", "configure") or _lower in ("configure", "swarm configure"):
            from core.config_manager import snapshot_all
            snapshot_all(reason="pre-configure")
            cmd_init()
            # Reload env + config after reconfiguration
            load_dotenv()
            if os.path.exists("config/agents.yaml"):
                with open("config/agents.yaml") as f:
                    config = yaml.safe_load(f)
                agents = config.get("agents", [])
                agent_names = ", ".join(a["id"] for a in agents)
                console.print(f"  [dim]Agents: {agent_names}[/dim]\n")
            continue

        # If it looks like a slash command but isn't recognized, show help hint
        if cmd is not None:
            from core.i18n import t as _t
            console.print(f"  [yellow]{_t('cmd.unknown_cmd', cmd=task_text)}[/yellow]  /help\n")
            continue

        # ── Submit task to agents ──
        try:
            # Clean state for this turn
            board = TaskBoard()
            board.clear()
            for fp in [".context_bus.json"]:
                if os.path.exists(fp):
                    os.remove(fp)
            import glob
            for fp in glob.glob(".mailboxes/*.jsonl"):
                os.remove(fp)

            orch = Orchestrator()
            task_id = orch.submit(task_text, required_role="planner")

            # ── Live status display (Claude Code style) ──
            from core.live_status import LiveStatus
            import time as _time

            live = LiveStatus(console, config.get("agents", []))
            live.start()
            orch._launch_all()

            # Poll task board while agents work
            while any(p.is_alive() for p in orch.procs):
                live.poll(board)
                _time.sleep(0.5)

            live.poll(board)   # final snapshot
            live.stop()

            # ── Display results ──
            result_text = board.collect_results(task_id)

            # Show failure details if any tasks failed
            all_data = board._read()
            failures = [(t.get("description", ""), t.get("evolution_flags", []))
                        for t in all_data.values()
                        if t.get("status") == "failed"]

            if failures:
                from core.i18n import t as _t
                console.print()
                for desc, flags in failures:
                    reason = ""
                    for f in flags:
                        if f.startswith("failed:"):
                            err = f[7:]
                            if "401" in err:
                                reason = _t("error.api_key_expired")
                            elif "429" in err:
                                reason = _t("error.rate_limit")
                            elif "timeout" in err.lower():
                                reason = _t("error.timeout")
                            elif "connect" in err.lower():
                                reason = _t("error.connect")
                            else:
                                reason = err.split("\n")[0][:80]
                            break
                    console.print(f"  [red]✗ {desc[:50]}[/red]")
                    if reason:
                        console.print(f"    [dim]{reason}[/dim]")

            if result_text:
                console.print()
                try:
                    console.print(Markdown(result_text))
                except Exception:
                    console.print(result_text)
                console.print()
            elif not failures:
                from core.i18n import t as _t
                console.print(f"\n  [yellow]{_t('cmd.no_result')}[/yellow] /status\n")

        except Exception as e:
            console.print(f"\n  [red]Error: {e}[/red]\n")


# ── Rich status display ───────────────────────────────────────────────────────

def _show_status_rich(console):
    """Show task board with rich formatting."""
    from core.i18n import t as _t
    if not os.path.exists(".task_board.json"):
        console.print(f"  [dim]{_t('cmd.no_tasks')}[/dim]\n")
        return

    data = json.load(open(".task_board.json"))
    if not data:
        console.print(f"  [dim]{_t('cmd.no_tasks')}[/dim]\n")
        return

    from rich.table import Table
    table = Table(box=None, padding=(0, 1), show_header=True)
    table.add_column("Status", style="bold", min_width=10)
    table.add_column("Agent", min_width=10)
    table.add_column("Description", min_width=30)
    table.add_column("ID", style="dim", max_width=8)

    status_style = {
        "completed": "green", "failed": "red", "pending": "yellow",
        "claimed": "cyan", "review": "magenta", "blocked": "dim",
        "cancelled": "dim yellow", "paused": "bold yellow",
    }

    # Sort: active first, then completed, then failed, then cancelled
    sort_order = {"claimed": 0, "review": 1, "pending": 2, "paused": 3,
                  "completed": 4, "failed": 5, "cancelled": 6, "blocked": 7}
    sorted_items = sorted(data.items(),
                          key=lambda kv: sort_order.get(kv[1].get("status", ""), 9))

    for tid, t in sorted_items:
        st = t["status"]
        style = status_style.get(st, "")
        table.add_row(
            f"[{style}]{st}[/{style}]",
            t.get("agent_id") or "—",
            t["description"][:55],
            tid[:8],
        )

    console.print(table)
    console.print()


# ── Legacy subcommands ─────────────────────────────────────────────────────────

def cmd_init():
    from core.onboard import run_onboard
    run_onboard()


def cmd_run(task: str):
    from core.orchestrator import Orchestrator
    orch = Orchestrator()
    print(f"\n  Submitting task: {task!r}\n")
    orch.run(task)
    print("\n  All agents finished.\n")
    cmd_status()


def cmd_status():
    data = json.load(open(".task_board.json")) if os.path.exists(".task_board.json") else {}
    print(f"\n{'ID':36}  {'STATUS':12}  {'AGENT':12}  DESCRIPTION")
    print("-" * 110)
    for tid, t in data.items():
        print(f"{tid:36}  {t['status']:12}  {(t.get('agent_id') or '-'):12}  "
              f"{t['description'][:40]}")
    print()


def cmd_scores(console=None):
    path = "memory/reputation_cache.json"
    if not os.path.exists(path):
        if console:
            console.print("  [dim]No scores yet.[/dim]\n")
        else:
            print("No scores yet.")
        return
    cache = json.load(open(path))

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
        tbl.add_column("Agent", style="bold", min_width=12)
        tbl.add_column("Score", justify="right", min_width=6)
        tbl.add_column("Trend", min_width=10)
        tbl.add_column("Status")
        for agent_id, data in cache.items():
            score  = data.get("composite", 0)
            trend  = sc.trend(agent_id)
            status = sc.threshold_status(agent_id)
            # Color score by health
            if score >= 70:
                sc_style = "green"
            elif score >= 50:
                sc_style = "yellow"
            else:
                sc_style = "red"
            status_style = {"healthy": "green", "watch": "yellow",
                            "warning": "red", "evolve": "bold red"}.get(status, "")
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


def cmd_doctor(console=None):
    from core.doctor import run_doctor
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            pass
    results = run_doctor(rich_console=console)
    if console is None:
        # Plain text fallback
        for ok, label, detail in results:
            icon = "✓" if ok else "✗"
            print(f"  {icon} {label:14} {detail}")


def cmd_workflows(console=None):
    """List available workflow definitions."""
    from core.workflow import list_workflows
    workflows = list_workflows()

    if not workflows:
        if console:
            console.print("  [dim]No workflows found in workflows/ directory.[/dim]\n")
        else:
            print("  No workflows found.")
        return

    if console:
        from rich.table import Table
        console.print()
        tbl = Table(box=None, padding=(0, 1), show_header=True)
        tbl.add_column("Workflow", style="bold")
        tbl.add_column("Steps", justify="right")
        tbl.add_column("Description", style="dim")
        for w in workflows:
            tbl.add_row(w["name"], str(w["steps"]), w["description"][:50])
        console.print(tbl)
        console.print(f"\n  [dim]Use: workflow <name> <task> to run a workflow[/dim]\n")
    else:
        for w in workflows:
            print(f"  {w['name']:25} ({w['steps']} steps)  {w['description'][:40]}")


def cmd_budget(console=None):
    """Show and manage budget limits."""
    from core.usage_tracker import UsageTracker
    budget = UsageTracker.get_budget()

    if console:
        console.print()
        enabled = budget.get("enabled", False)
        if not enabled:
            console.print("  [dim]Budget: not configured[/dim]")
            console.print("  [dim]Set via: POST /v1/budget or config/budget.json[/dim]\n")
        else:
            max_cost = budget.get("max_cost_usd", 0)
            current = budget.get("current_cost_usd", 0)
            pct = budget.get("percent_used", 0)
            tokens = budget.get("current_tokens", 0)

            # Color based on usage
            if pct >= 90:
                style = "red"
            elif pct >= 70:
                style = "yellow"
            else:
                style = "green"

            console.print(f"  [bold]Budget:[/bold]  ${max_cost:.2f}")
            console.print(f"  [bold]Spent:[/bold]   [{style}]${current:.4f}  ({pct:.0f}%)[/{style}]")
            console.print(f"  [bold]Tokens:[/bold]  {tokens:,}")
            max_tokens = budget.get("max_tokens", 0)
            if max_tokens:
                console.print(f"  [bold]Token Limit:[/bold] {max_tokens:,}")
            console.print()
    else:
        print(json.dumps(budget, indent=2))


def cmd_usage(console=None):
    """Show token usage and cost statistics."""
    from core.usage_tracker import UsageTracker
    tracker = UsageTracker()
    summary = tracker.get_summary()
    agg = summary.get("aggregate", {})

    if not agg.get("total_calls"):
        if console:
            console.print("  [dim]No usage data yet.[/dim]\n")
        else:
            print("  No usage data yet.")
        return

    if console:
        from rich.table import Table
        console.print()

        # Summary header
        total_calls  = agg.get("total_calls", 0)
        total_tokens = agg.get("total_tokens", 0)
        total_cost   = agg.get("total_cost_usd", 0)
        successes    = agg.get("success_count", 0)
        failures     = agg.get("failure_count", 0)
        retries      = agg.get("total_retries", 0)
        failovers    = agg.get("total_failovers", 0)

        console.print(f"  [bold]Total Calls:[/bold] {total_calls}  "
                       f"([green]{successes} ok[/green]"
                       f"{f', [red]{failures} fail[/red]' if failures else ''})")
        console.print(f"  [bold]Total Tokens:[/bold] {total_tokens:,}  "
                       f"[bold]Est. Cost:[/bold] ${total_cost:.4f}")
        if retries or failovers:
            console.print(f"  [dim]Retries: {retries}  Failovers: {failovers}[/dim]")

        # Per-agent table
        by_agent = summary.get("by_agent", {})
        if by_agent:
            console.print()
            tbl = Table(box=None, padding=(0, 1), show_header=True)
            tbl.add_column("Agent", style="bold")
            tbl.add_column("Calls", justify="right")
            tbl.add_column("Tokens", justify="right")
            tbl.add_column("Cost", justify="right")
            for aid, stats in sorted(by_agent.items()):
                tbl.add_row(
                    aid,
                    str(stats["calls"]),
                    f"{stats['tokens']:,}",
                    f"${stats['cost']:.4f}",
                )
            console.print(tbl)

        # Per-model table
        by_model = summary.get("by_model", {})
        if by_model:
            console.print()
            tbl2 = Table(box=None, padding=(0, 1), show_header=True)
            tbl2.add_column("Model", style="bold")
            tbl2.add_column("Calls", justify="right")
            tbl2.add_column("Tokens", justify="right")
            tbl2.add_column("Cost", justify="right")
            for mid, stats in sorted(by_model.items()):
                tbl2.add_row(
                    mid,
                    str(stats["calls"]),
                    f"{stats['tokens']:,}",
                    f"${stats['cost']:.4f}",
                )
            console.print(tbl2)

        console.print()
    else:
        # Plain text fallback
        print(f"\nUsage: {agg.get('total_calls', 0)} calls, "
              f"{agg.get('total_tokens', 0):,} tokens, "
              f"${agg.get('total_cost_usd', 0):.4f}")


def cmd_gateway(action: str = "start", port: int = 0, token: str = "",
                 force: bool = False):
    """Gateway lifecycle management (OpenClaw-style subcommands)."""
    from core.gateway import DEFAULT_PORT
    from core.i18n import t as _t

    port = port or int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
    token = token or os.environ.get("SWARM_GATEWAY_TOKEN", "")

    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    if action in ("start", "run"):
        # ── Force kill existing process on port ──
        if force:
            from core.gateway import kill_port
            if console:
                console.print(f"  [dim]{_t('gw.killing_port', port=port)}[/dim]")
            kill_port(port)

        from core.gateway import run_gateway_cli
        run_gateway_cli(port=port, token=token)

    elif action == "status":
        _show_gateway_status(console, port)

    elif action == "stop":
        from core.gateway import kill_port
        if console:
            console.print(f"  [dim]{_t('gw.stopping')}[/dim]")
        killed = kill_port(port)
        if killed:
            if console:
                console.print(f"  [green]\u2713[/green] Gateway stopped on port {port}")
            else:
                print(f"  Gateway stopped on port {port}")
        else:
            if console:
                console.print(f"  [dim]{_t('gw.not_running')}[/dim]")
            else:
                print(f"  {_t('gw.not_running')}")

    elif action == "restart":
        from core.gateway import kill_port
        if console:
            console.print(f"  [dim]{_t('gw.restarting')}[/dim]")
        kill_port(port)
        import time as _time
        _time.sleep(0.5)
        from core.gateway import run_gateway_cli
        run_gateway_cli(port=port, token=token)

    elif action == "install":
        from core.gateway import generate_token
        from core.daemon import install_daemon
        if not token:
            token = generate_token()
        ok, msg = install_daemon(port, token)
        if console:
            icon = "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"
            console.print(f"  {icon} {msg}")
            if ok:
                console.print(f"  [dim]Port: {port}  Token: {token}[/dim]")
        else:
            print(f"  {'OK' if ok else 'FAIL'}: {msg}")

    elif action == "uninstall":
        from core.daemon import uninstall_daemon
        ok, msg = uninstall_daemon()
        if console:
            icon = "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"
            console.print(f"  {icon} {msg}")
        else:
            print(f"  {'OK' if ok else 'FAIL'}: {msg}")

    else:
        print(f"Unknown gateway action: {action}")
        print("Available: start, stop, restart, status, install, uninstall")


def _show_gateway_status(console, port: int = 0):
    """Rich gateway status display — service state + RPC probe + agents."""
    from core.gateway import DEFAULT_PORT, probe_gateway
    from core.daemon import daemon_status
    from core.i18n import t as _t

    port = port or int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))

    # Probe the gateway
    probe = probe_gateway(port)

    # Check daemon status
    daemon_ok, daemon_msg = daemon_status()

    if console:
        from rich.panel import Panel
        from rich.table import Table
        from rich import box

        tbl = Table(show_header=False, show_edge=False, box=None,
                     padding=(0, 1), expand=False)
        tbl.add_column("key", style="bold", min_width=16)
        tbl.add_column("val")

        # ── Connection status ──
        if probe["reachable"]:
            tbl.add_row(
                "Status",
                f"[green]{_t('gw.running')}[/green]",
            )
            tbl.add_row(_t("gw.url"), f"http://127.0.0.1:{port}/")

            # Mask token for display
            token = os.environ.get("SWARM_GATEWAY_TOKEN", "")
            if token:
                masked = token[:10] + "..." + token[-4:] if len(token) > 16 else "***"
                tbl.add_row(_t("gw.token"), f"[dim]{masked}[/dim]")

            # Uptime
            uptime_s = probe.get("uptime_seconds", 0)
            if uptime_s < 60:
                uptime_str = f"{uptime_s:.0f}s"
            elif uptime_s < 3600:
                m, s = divmod(int(uptime_s), 60)
                uptime_str = f"{m}m{s:02d}s"
            else:
                h, rem = divmod(int(uptime_s), 3600)
                m = rem // 60
                uptime_str = f"{h}h{m:02d}m"
            tbl.add_row(_t("gw.uptime"), uptime_str)

            # Agents
            online = probe.get("agents_online", 0)
            total = probe.get("agents_total", 0)
            if total > 0:
                tbl.add_row(
                    "Agents",
                    f"[green]{online}[/green]/{total} online",
                )
            else:
                tbl.add_row("Agents", f"[dim]{_t('gw.no_agents')}[/dim]")

            # Active tasks
            task_count = probe.get("task_count", 0)
            active = probe.get("active_tasks", 0)
            if task_count > 0:
                tbl.add_row("Tasks", f"{active} active / {task_count} total")

            # Agent detail rows
            for ag in probe.get("agents", []):
                aid = ag.get("agent_id", "?")
                online_flag = "[green]●[/green]" if ag.get("online") else "[dim]○[/dim]"
                status = ag.get("status", "idle")
                task_id = ag.get("task_id", "")
                detail = f"{status}"
                if task_id:
                    detail += f" ({task_id[:8]})"
                tbl.add_row(f"  {online_flag} {aid}", f"[dim]{detail}[/dim]")

        else:
            tbl.add_row(
                "Status",
                f"[red]{_t('gw.stopped')}[/red]",
            )
            tbl.add_row("Port", str(port))
            error = probe.get("error", "")
            if error:
                tbl.add_row("Error", f"[dim]{error}[/dim]")

        # Daemon status
        tbl.add_row("", "")  # spacer
        if daemon_ok:
            tbl.add_row("Service", f"[green]{_t('gw.daemon_running')}[/green]")
        elif "Not installed" in daemon_msg:
            tbl.add_row("Service", f"[dim]{_t('gw.daemon_not_inst')}[/dim]")
        else:
            tbl.add_row("Service", f"[yellow]{daemon_msg}[/yellow]")

        # Key endpoints
        tbl.add_row("", "")  # spacer
        tbl.add_row(
            _t("gw.endpoints"),
            "[dim]POST /v1/task · GET /v1/status · GET /v1/events[/dim]",
        )

        console.print()
        console.print(Panel(
            tbl,
            title=f"[bold magenta]{_t('gw.title')}[/bold magenta]",
            border_style="magenta",
            box=box.ROUNDED,
        ))
        console.print()
    else:
        # Plain text fallback
        if probe["reachable"]:
            print(f"\n  Gateway: RUNNING on http://127.0.0.1:{port}/")
            print(f"  Uptime: {probe.get('uptime_seconds', 0):.0f}s")
            print(f"  Agents: {probe.get('agents_online', 0)}/{probe.get('agents_total', 0)} online")
        else:
            print(f"\n  Gateway: STOPPED (port {port})")
            print(f"  Error: {probe.get('error', '?')}")
        print(f"  Daemon: {daemon_msg}")
        print()


def cmd_agents_add(name: str):
    """Add a new agent to the team interactively."""
    if not os.path.exists("config/agents.yaml"):
        print("No config found. Run `swarm configure` first.")
        return

    try:
        import questionary
        from core.onboard import (
            STYLE, PRESETS, PROVIDERS, C_OK, C_DIM, C_AGENT,
            _ask_provider, _ensure_api_key, _ask_model,
            _build_agent_entry,
        )
    except ImportError:
        print("ERROR: questionary is required.  pip3 install questionary")
        return

    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    import yaml

    # Load existing config
    with open("config/agents.yaml") as f:
        config = yaml.safe_load(f) or {}

    existing_ids = [a["id"] for a in config.get("agents", [])]
    if name in existing_ids:
        print(f"Agent '{name}' already exists. Choose a different name.")
        return

    print(f"\n  Adding agent: {name}\n")

    # Role selection
    preset_choices = [
        questionary.Choice(PRESETS[k]["label"], value=k)
        for k in PRESETS
    ] + [questionary.Choice("Custom (define your own)", value="custom")]

    preset = questionary.select(
        "Role:", choices=preset_choices, style=STYLE,
    ).ask()
    if preset is None:
        return

    if preset == "custom":
        role = questionary.text("Role description:", style=STYLE).ask()
        if not role:
            return
        skills = ["_base"]
    else:
        role = PRESETS[preset]["role"]
        skills = list(PRESETS[preset]["skills"])

    # Provider + key + model
    provider = _ask_provider()
    if provider is None:
        return

    api_key = _ensure_api_key(provider)
    if api_key is None:
        return

    model = _ask_model(provider, api_key)
    if model is None:
        return

    # Build and append
    entry = _build_agent_entry(name, role, model, skills, provider)
    config.setdefault("agents", []).append(entry)

    from core.config_manager import safe_write_yaml
    safe_write_yaml("config/agents.yaml", config, reason=f"add agent {name}")

    print(f"\n  ✓ Agent '{name}' added → {PROVIDERS[provider]['label']}/{model}")
    print(f"  Team: {', '.join(a['id'] for a in config['agents'])}\n")


def cmd_chain(action: str, agent_id: str = None, console=None):
    """Chain management CLI commands."""
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    import yaml
    if not os.path.exists("config/agents.yaml"):
        print("No config found. Run `swarm configure` first.")
        return

    with open("config/agents.yaml") as f:
        config = yaml.safe_load(f) or {}

    if not config.get("chain", {}).get("enabled", False):
        print("Chain is not enabled in config/agents.yaml")
        return

    from adapters.chain.chain_manager import ChainManager
    mgr = ChainManager(config)

    if action == "status":
        status = mgr.get_status()
        if console:
            console.print(f"\n  [bold]Chain Status[/bold]")
            console.print(f"  Network: {status['network']}  |  Lit: {status['lit_network']}")
            console.print(f"  x402: {'enabled' if status['x402_enabled'] else 'disabled'}")
            console.print()

            from rich.table import Table
            tbl = Table(box=None, padding=(0, 1), show_header=True)
            tbl.add_column("Agent", style="bold")
            tbl.add_column("Registered")
            tbl.add_column("PKP Address", style="dim")
            tbl.add_column("Chain ID")
            tbl.add_column("USDC")

            for aid, info in status.get("agents", {}).items():
                reg = "[green]\u2713[/green]" if info.get("registered") else "[red]\u2717[/red]"
                pkp = info.get("pkp_address", "")[:10] + "..." if info.get("pkp_address") else "-"
                cid = str(info.get("erc8004_agent_id") or "-")
                bal = info.get("usdc_balance", "0.00")
                tbl.add_row(aid, reg, pkp, cid, bal)
            console.print(tbl)
            console.print()
        else:
            print(json.dumps(status, indent=2))

    elif action == "balance":
        agents_list = config.get("agents", [])
        targets = [agent_id] if agent_id else [a["id"] for a in agents_list]
        for aid in targets:
            balance = mgr.get_balance(aid)
            print(f"  {aid}: {balance} USDC")

    elif action == "init":
        if not agent_id:
            print("Usage: swarm chain init <agent_id>")
            return
        agent_cfg = None
        for a in config.get("agents", []):
            if a["id"] == agent_id:
                agent_cfg = a
                break
        if console:
            console.print(f"\n  [bold]Initializing {agent_id} on-chain...[/bold]")
        result = mgr.initialize_agent(agent_id, agent_cfg)
        if console:
            console.print(f"  [green]\u2713[/green] PKP: {result.get('pkp_eth_address', '?')}")
            console.print(f"  [green]\u2713[/green] Registered: {result.get('registered', False)}")
            console.print()
        else:
            print(json.dumps(result, indent=2, default=str))

    elif action == "register":
        if not agent_id:
            print("Usage: swarm chain register <agent_id>")
            return
        tx_hash = mgr.register_agent(agent_id, {})
        print(f"  {agent_id}: tx={tx_hash}")

    elif action == "health":
        health = mgr.health_check()
        if console:
            console.print(f"\n  [bold]Chain Health[/bold]")
            for key, val in health.items():
                if isinstance(val, dict):
                    st = val.get("status", "?")
                    style = "green" if st == "ok" else "red"
                    console.print(f"  {key}: [{style}]{st}[/{style}]")
                else:
                    console.print(f"  {key}: {val}")
            console.print()
        else:
            print(json.dumps(health, indent=2, default=str))

    else:
        print(f"Unknown chain action: {action}")
        print("Available: status, balance, init, register, health")


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
        console.print(f"\n  [bold]Pending model swap for {agent_id}[/bold]")
        console.print(f"  New model : [cyan]{swap['new_model']}[/cyan]")
        console.print(f"  Reason    : [dim]{swap['reason']}[/dim]\n")
        ok = questionary.confirm("Apply model swap?", default=False, style=STYLE).ask()
        if ok:
            from reputation.evolution import EvolutionEngine
            from reputation.scorer import ScoreAggregator
            from core.task_board import TaskBoard
            eng = EvolutionEngine(ScoreAggregator(), TaskBoard())
            eng.apply_model_swap(agent_id)
            console.print(f"  [green]✓[/green] Model swap applied for {agent_id}.\n")
        else:
            console.print(f"  [dim]Cancelled.[/dim]\n")
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


def main():
    parser = argparse.ArgumentParser(prog="swarm")
    sub    = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", help="Full interactive setup wizard")
    sub.add_parser("configure", help="Full interactive setup wizard (alias)")
    sub.add_parser("chat", help="Interactive chat mode")

    p_run = sub.add_parser("run", help="Submit a task and run all agents")
    p_run.add_argument("task", help="Task description")

    sub.add_parser("status", help="Show task board")
    sub.add_parser("scores", help="Show reputation scores")
    sub.add_parser("doctor", help="System health check")
    p_gw = sub.add_parser("gateway", help="Gateway management")
    p_gw.add_argument("action", nargs="?", default="start",
                       choices=["start", "stop", "restart", "status",
                                "install", "uninstall"],
                       help="Gateway action (default: start)")
    p_gw.add_argument("-p", "--port", type=int, default=0,
                       help="Port (default: 19789 or SWARM_GATEWAY_PORT)")
    p_gw.add_argument("-t", "--token", default="",
                       help="Bearer token (default: auto-generate)")
    p_gw.add_argument("--force", action="store_true",
                       help="Kill existing process on port before starting")

    p_agents = sub.add_parser("agents", help="Agent management")
    agents_sub = p_agents.add_subparsers(dest="agents_cmd")
    p_add = agents_sub.add_parser("add", help="Add an agent to the team")
    p_add.add_argument("name", help="Agent ID/name")

    p_chain = sub.add_parser("chain", help="On-chain identity management")
    p_chain.add_argument("action", choices=["status", "balance", "init", "register", "health"],
                         help="Chain action")
    p_chain.add_argument("agent_id", nargs="?", default=None,
                         help="Agent ID (required for init/register)")

    p_ev = sub.add_parser("evolve", help="Manage evolution actions")
    p_ev.add_argument("agent_id")
    p_ev.add_argument("action", choices=["confirm"])

    args = parser.parse_args()
    if args.cmd in ("init", "configure"):
        cmd_init()
    elif args.cmd == "chat":
        interactive_main()
    elif args.cmd == "run":
        cmd_run(args.task)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "scores":
        cmd_scores()
    elif args.cmd == "doctor":
        cmd_doctor()
    elif args.cmd == "gateway":
        cmd_gateway(action=args.action, port=args.port,
                    token=args.token, force=args.force)
    elif args.cmd == "chain":
        cmd_chain(args.action, args.agent_id)
    elif args.cmd == "agents":
        if args.agents_cmd == "add":
            cmd_agents_add(args.name)
        else:
            p_agents.print_help()
    elif args.cmd == "evolve":
        if args.action == "confirm":
            cmd_evolve_confirm(args.agent_id)
    else:
        # Default: enter interactive chat mode (as documented)
        interactive_main()


if __name__ == "__main__":
    main()

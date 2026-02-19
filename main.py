#!/usr/bin/env python3
"""
main.py  —  Agent Stack CLI
Usage:
  swarm                       # interactive chat mode (default)
  swarm onboard               # interactive setup wizard
  swarm --setup               # setup wizard, then enter chat
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
  swarm agents create <name>  # create an agent (--template researcher|coder|debugger|doc_writer)
  swarm workflow list          # list available workflows
  swarm workflow run <name>    # run a workflow (e.g., code_review, bug_fix, brainstorm)
  swarm chain status          # on-chain identity status
  swarm chain init <agent>    # initialize agent on-chain
  swarm chain balance         # check USDC balances
  swarm chain health          # chain health check
  swarm install               # install from GitHub
  swarm uninstall             # remove CLI & daemon
  swarm update                # pull latest from GitHub

Chat commands:
  /configure  — re-run onboarding wizard
  /config     — show current agent team
  /status     — task board
  /scores     — reputation scores
  /gateway    — gateway status & control
  /doctor     — system health check
  /clear      — clear task history
  /install    — install from GitHub
  /uninstall  — remove CLI & daemon
  /update     — update from GitHub
  /help       — show commands
  exit        — quit
"""

import argparse
import json
import os
import sys


def _ensure_project_root():
    """Ensure we're running from the project root (needed for file-based state)."""
    # Find project root: directory containing this file
    root = os.path.dirname(os.path.abspath(__file__))
    if os.getcwd() != root:
        os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_project_root()

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
        from core.i18n import t as _t
        console.print(f"  [{_t('onboard.first_run_hint')}]")
        console.print()
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

    # ── Preflight check ──
    from core.doctor import run_preflight
    from core.i18n import t as _t
    issues = run_preflight()
    if issues:
        console.print()
        console.print(Panel(
            "\n".join(f"  [yellow]![/yellow] {issue}" for issue in issues)
            + f"\n\n  [dim]{_t('error.suggest_doctor')}[/dim]",
            title=f"[yellow]{_t('preflight.title')}[/yellow]",
            border_style="yellow",
            box=box.ROUNDED,
        ))
        try:
            from rich.prompt import Confirm
            if not Confirm.ask(f"  {_t('preflight.issues_found')}", default=True):
                return
        except (KeyboardInterrupt, EOFError):
            return

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

    # ── Session persistence: check for resume ──
    from core.orchestrator import Orchestrator
    from core.task_board import TaskBoard

    chat_history: list[dict] = []  # [{role: "user"/"assistant", content: str}]
    _session_dir = os.path.join("memory", "sessions")
    os.makedirs(_session_dir, exist_ok=True)

    # Check for recent session to resume
    _recent_session = _find_recent_session(_session_dir)
    if _recent_session:
        try:
            resume = Prompt.ask(
                f"  [dim]Resume previous session?[/dim] [bold]({_recent_session['task'][:40]})[/bold]",
                choices=["y", "n"],
                default="n",
            )
            if resume == "y":
                chat_history = _recent_session.get("history", [])
                console.print(f"  [dim]Restored {len(chat_history)} message(s)[/dim]")
                # Show last result
                for msg in reversed(chat_history):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        try:
                            console.print(Markdown(msg["content"][:500]))
                        except Exception:
                            console.print(msg["content"][:500])
                        break
        except (KeyboardInterrupt, EOFError):
            pass

    while True:
        try:
            task_text = Prompt.ask("[bold green]You[/bold green]")
        except (KeyboardInterrupt, EOFError):
            from core.i18n import t as _t
            console.print(f"\n[dim]{_t('cmd.bye')}[/dim]")
            # Save session on exit
            _save_session(_session_dir, chat_history)
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
            _save_session(_session_dir, chat_history)
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
                ("/save",            _t("help.save")),
                ("/templates",       _t("help.templates")),
                ("/export",          _t("help.export")),
                ("/config",          _t("help.config")),
                ("/config history",  _t("help.config_hist")),
                ("/config rollback", _t("help.config_roll")),
                ("/configure",       _t("help.configure")),
                ("/gateway",         _t("help.gateway")),
                ("/chain",           _t("help.chain")),
                ("/doctor",          _t("help.doctor")),
                ("/clear",           _t("help.clear")),
                ("/install",         _t("help.install")),
                ("/uninstall",       _t("help.uninstall")),
                ("/update",          _t("help.update")),
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

        if cmd == "install":
            cmd_install(console=console)
            continue

        if cmd == "uninstall":
            cmd_uninstall(console=console)
            continue

        if cmd == "update":
            cmd_update(console=console)
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

        if cmd == "save":
            # Save last task as template
            if chat_history:
                last_task = None
                for msg in reversed(chat_history):
                    if msg.get("role") == "user":
                        last_task = msg["content"]
                        break
                if last_task:
                    _save_template(last_task)
                    console.print(f"  [green]✓[/green] Saved template: {last_task[:40]}…\n")
                else:
                    console.print("  [dim]No task to save.[/dim]\n")
            else:
                console.print("  [dim]No task to save.[/dim]\n")
            continue

        if cmd == "templates":
            templates = _load_templates()
            if not templates:
                console.print("  [dim]No saved templates. Use /save after a task.[/dim]\n")
                continue
            try:
                import questionary
                from core.onboard import STYLE
                choices = [
                    questionary.Choice(t[:60], value=t)
                    for t in templates
                ]
                selected = questionary.select(
                    "Choose a template to run:",
                    choices=choices,
                    style=STYLE,
                ).ask()
                if selected:
                    task_text = selected
                    # Fall through to submit
                else:
                    continue
            except ImportError:
                for i, t in enumerate(templates):
                    console.print(f"  [{i}] {t[:60]}")
                console.print()
                continue

        if cmd == "export":
            # Export most recent task
            if os.path.exists(".task_board.json"):
                data = json.load(open(".task_board.json"))
                if data:
                    # Get most recent task (by creation order — first key)
                    first_tid = next(iter(data))
                    cmd_export(first_tid, fmt="md", console=console)
                else:
                    console.print("  [dim]No tasks to export.[/dim]\n")
            else:
                console.print("  [dim]No tasks to export.[/dim]\n")
            continue

        # If it looks like a slash command but isn't recognized, show help hint
        if cmd is not None:
            from core.i18n import t as _t
            console.print(f"  [yellow]{_t('cmd.unknown_cmd', cmd=task_text)}[/yellow]  /help\n")
            continue

        # ── Submit task to agents ──
        chat_history.append({"role": "user", "content": task_text})
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
                console.print(f"  [dim]{_t('error.suggest_doctor')}[/dim]")

            if result_text:
                console.print()
                try:
                    console.print(Markdown(result_text))
                except Exception:
                    console.print(result_text)
                console.print()
                chat_history.append({"role": "assistant", "content": result_text})
            elif not failures:
                from core.i18n import t as _t
                console.print(f"\n  [yellow]{_t('cmd.no_result')}[/yellow] /status\n")

        except Exception as e:
            console.print(f"\n  [red]Error: {e}[/red]\n")


# ── Rich status display ───────────────────────────────────────────────────────

# ── Session persistence helpers ──────────────────────────────────────────────

def _find_recent_session(session_dir: str) -> dict | None:
    """Find the most recent session file (< 24h old)."""
    import time
    if not os.path.isdir(session_dir):
        return None
    best = None
    best_ts = 0
    now = time.time()
    for fname in os.listdir(session_dir):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(session_dir, fname)
        try:
            mtime = os.path.getmtime(path)
            if (now - mtime) < 86400 and mtime > best_ts:  # < 24h
                best_ts = mtime
                best = path
        except Exception:
            continue
    if not best:
        return None
    try:
        with open(best) as f:
            return json.load(f)
    except Exception:
        return None


def _save_session(session_dir: str, history: list[dict]):
    """Save chat history to a session file."""
    if not history:
        return
    import time
    os.makedirs(session_dir, exist_ok=True)
    # Get first user task as label
    task_label = ""
    for msg in history:
        if msg.get("role") == "user":
            task_label = msg["content"][:60]
            break
    session = {
        "timestamp": time.time(),
        "task": task_label,
        "history": history,
    }
    filename = time.strftime("%Y%m%d_%H%M%S") + ".json"
    path = os.path.join(session_dir, filename)
    with open(path, "w") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)


# ── Task template helpers ────────────────────────────────────────────────────

_TEMPLATES_PATH = os.path.join("memory", "templates.json")


def _load_templates() -> list[str]:
    """Load saved task templates."""
    if not os.path.exists(_TEMPLATES_PATH):
        return []
    try:
        with open(_TEMPLATES_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_template(task: str):
    """Save a task description as a reusable template."""
    templates = _load_templates()
    if task not in templates:
        templates.append(task)
        # Keep last 20 templates
        templates = templates[-20:]
        os.makedirs(os.path.dirname(_TEMPLATES_PATH), exist_ok=True)
        with open(_TEMPLATES_PATH, "w") as f:
            json.dump(templates, f, ensure_ascii=False, indent=2)


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


def cmd_doctor(console=None, repair: bool = False, deep: bool = False):
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            pass

    if repair:
        from core.doctor import run_doctor_repair
        results = run_doctor_repair(rich_console=console)
    elif deep:
        from core.doctor import run_doctor_deep
        results = run_doctor_deep(rich_console=console)
    else:
        from core.doctor import run_doctor
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


def cmd_workflow_run(name: str, task_input: str = ""):
    """Run a named workflow with the given input."""
    import asyncio
    from core.workflow import list_workflows, load_workflow, WorkflowEngine

    # Find matching workflow
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

    # Load agents config
    if not os.path.exists("config/agents.yaml"):
        print("  No config found. Run `swarm onboard` first.")
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
        # Print last step result summary
        for step in reversed(result.steps):
            if step.result:
                preview = step.result[:200]
                print(f"\n  Final output ({step.id}):\n  {preview}{'…' if len(step.result) > 200 else ''}\n")
                break
    except Exception as e:
        print(f"\n  Workflow failed: {e}")


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
            console.print("  [dim]No task board found.[/dim]")
        else:
            print("  No task board found.")
        return

    data = json.load(open(".task_board.json"))

    # Find matching task (full or prefix match)
    match_id = None
    for tid in data:
        if tid == task_id or tid.startswith(task_id):
            match_id = tid
            break

    if not match_id:
        if console:
            console.print(f"  [red]Task not found: {task_id}[/red]")
        else:
            print(f"  Task not found: {task_id}")
        return

    task = data[match_id]

    # Collect subtasks (matching parent_id)
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
        # Markdown format
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

        # Total cost
        total_cost = cost + sum(t.get("cost_usd", 0) for _, t in subtasks)
        if total_cost:
            lines.append(f"---\n**Total Cost:** ~${total_cost:.4f}")

        output = "\n".join(lines)

    # Write to file
    ext = "json" if fmt == "json" else "md"
    filename = f"export_{match_id[:8]}.{ext}"
    with open(filename, "w") as f:
        f.write(output)

    if console:
        console.print(f"  [green]✓[/green] Exported to [bold]{filename}[/bold]")
        if fmt == "md":
            console.print(f"  [dim]{len(subtasks)} subtask(s) included[/dim]")
    else:
        print(f"  Exported to {filename}")


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


AGENT_TEMPLATES = {
    "researcher": {
        "role": "Research specialist — finds information, analyzes sources, synthesizes findings",
        "skills": ["_base", "web_search", "summarize"],
        "cognition": "# Researcher Cognition\n\n## Role\nYou are a meticulous researcher. Your job is to find accurate, relevant information and present it clearly.\n\n## Approach\n1. Understand the question scope\n2. Search multiple sources\n3. Cross-reference findings\n4. Synthesize into a clear summary\n\n## Quality Standards\n- Always cite sources\n- Distinguish facts from opinions\n- Flag conflicting information\n",
    },
    "coder": {
        "role": "Software engineer — writes, reviews, and debugs code",
        "skills": ["_base", "code_write", "code_review"],
        "cognition": "# Coder Cognition\n\n## Role\nYou are a pragmatic software engineer. Write clean, tested, maintainable code.\n\n## Approach\n1. Understand requirements before coding\n2. Follow existing code patterns\n3. Write minimal, focused changes\n4. Consider edge cases\n\n## Quality Standards\n- No unnecessary complexity\n- Clear variable/function names\n- Handle errors gracefully\n",
    },
    "debugger": {
        "role": "Debug specialist — diagnoses issues, traces root causes, proposes fixes",
        "skills": ["_base", "code_review", "code_write"],
        "cognition": "# Debugger Cognition\n\n## Role\nYou are a systematic debugger. Your job is to find and fix the root cause, not just the symptom.\n\n## Approach\n1. Reproduce the issue\n2. Form hypotheses\n3. Test each hypothesis\n4. Identify root cause\n5. Propose minimal fix\n\n## Quality Standards\n- Never guess — verify\n- Explain your reasoning chain\n- Prevent regression\n",
    },
    "doc_writer": {
        "role": "Documentation writer — creates clear, structured technical docs",
        "skills": ["_base", "summarize"],
        "cognition": "# Documentation Writer Cognition\n\n## Role\nYou write clear, user-friendly documentation. Make complex topics accessible.\n\n## Approach\n1. Identify the target audience\n2. Outline the structure\n3. Write concisely with examples\n4. Review for completeness\n\n## Quality Standards\n- Use simple language\n- Include practical examples\n- Keep sections focused\n",
    },
}


def cmd_agents_add(name: str, template: str | None = None):
    """Add a new agent to the team interactively."""
    if not os.path.exists("config/agents.yaml"):
        print("No config found. Run `swarm onboard` first.")
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

    print(f"\n  Creating agent: {name}\n")

    # Use template if provided via --template flag
    if template and template in AGENT_TEMPLATES:
        tmpl = AGENT_TEMPLATES[template]
        role = tmpl["role"]
        skills = list(tmpl["skills"])
        print(f"  Using template: {template}")
        print(f"  Role: {role}\n")
    else:
        # Interactive role selection
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

    # Auto-generate skill override and cognition files
    override_dir = os.path.join("skills", "agent_overrides")
    os.makedirs(override_dir, exist_ok=True)
    override_path = os.path.join(override_dir, f"{name}.md")
    if not os.path.exists(override_path):
        with open(override_path, "w") as f:
            f.write(f"# {name} — Skill Overrides\n\n"
                    f"<!-- Add agent-specific instructions here -->\n")

    cognition_dir = os.path.join("docs", name)
    os.makedirs(cognition_dir, exist_ok=True)
    cognition_path = os.path.join(cognition_dir, "cognition.md")
    if not os.path.exists(cognition_path):
        # Use template cognition if available
        if template and template in AGENT_TEMPLATES:
            content = AGENT_TEMPLATES[template]["cognition"]
        else:
            content = (f"# {name} Cognition\n\n"
                       f"## Role\n{role}\n\n"
                       f"## Approach\n<!-- Define how this agent should think -->\n\n"
                       f"## Quality Standards\n<!-- Define quality criteria -->\n")
        with open(cognition_path, "w") as f:
            f.write(content)

    print(f"\n  ✓ Agent '{name}' created → {PROVIDERS[provider]['label']}/{model}")
    print(f"  ✓ {override_path}")
    print(f"  ✓ {cognition_path}")
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


# ── Install / Uninstall / Update ─────────────────────────────────────────────

# Default GitHub repo — override with SWARM_REPO env var
_DEFAULT_REPO = "https://github.com/createpjf/swarm-dev.git"


def cmd_install(repo: str = "", target: str = "", console=None):
    """Clone from GitHub, set up venv, install deps, link CLI."""
    import subprocess
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    def _print(msg):
        if console:
            console.print(msg)
        else:
            print(msg)

    from core.i18n import t as _t
    repo_url = repo or os.environ.get("SWARM_REPO", _DEFAULT_REPO)
    install_dir = target or os.environ.get("SWARM_INSTALL_DIR", "")

    # If already installed (we're running from the project), just run setup
    project_root = os.path.dirname(os.path.abspath(__file__))
    if not install_dir and os.path.exists(os.path.join(project_root, "pyproject.toml")):
        _print(f"  [dim]{_t('install.already', path=project_root)}[/dim]")
        _print(f"  [dim]Running setup…[/dim]")
        setup_sh = os.path.join(project_root, "setup.sh")
        if os.path.exists(setup_sh):
            result = subprocess.run(["bash", setup_sh], cwd=project_root)
            if result.returncode == 0:
                _print(f"  [green]✓[/green] {_t('install.done')}")
            else:
                _print(f"  [red]✗[/red] {_t('install.failed', err='setup.sh failed')}")
        else:
            # Fallback: pip install directly
            venv_pip = os.path.join(project_root, ".venv", "bin", "pip")
            pip_cmd = venv_pip if os.path.exists(venv_pip) else "pip"
            result = subprocess.run(
                [pip_cmd, "install", "-e", ".[dev]"],
                cwd=project_root, capture_output=True, text=True,
            )
            if result.returncode == 0:
                _print(f"  [green]✓[/green] {_t('install.done')}")
            else:
                _print(f"  [red]✗[/red] {_t('install.failed', err=result.stderr[:200])}")
        return

    # Fresh install: clone from GitHub
    if not install_dir:
        install_dir = os.path.expanduser("~/swarm-dev")

    _print(f"  [dim]{_t('install.checking')}[/dim]")

    if os.path.exists(install_dir) and os.listdir(install_dir):
        _print(f"  [yellow]![/yellow] {_t('install.already', path=install_dir)}")
        _print(f"  [dim]Use 'swarm update' to pull latest changes.[/dim]")
        return

    _print(f"  [dim]{_t('install.cloning')}[/dim]  {repo_url}")
    result = subprocess.run(
        ["git", "clone", repo_url, install_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _print(f"  [red]✗[/red] {_t('install.failed', err=result.stderr[:200])}")
        return

    _print(f"  [dim]{_t('install.installing')}[/dim]")
    setup_sh = os.path.join(install_dir, "setup.sh")
    if os.path.exists(setup_sh):
        result = subprocess.run(["bash", setup_sh], cwd=install_dir)
    else:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
            cwd=install_dir, capture_output=True, text=True,
        )

    if result.returncode == 0:
        _print(f"  [green]✓[/green] {_t('install.done')}")
        _print(f"  [dim]Installed to: {install_dir}[/dim]")
        _print(f"  [bold]Quick start:[/bold]  cd {install_dir} && swarm")
    else:
        err = getattr(result, 'stderr', '') or ''
        _print(f"  [red]✗[/red] {_t('install.failed', err=err[:200])}")


def cmd_uninstall(console=None):
    """Remove swarm CLI symlink and daemon service. Source code stays."""
    import subprocess
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    def _print(msg):
        if console:
            console.print(msg)
        else:
            print(msg)

    from core.i18n import t as _t

    # Confirm
    try:
        import questionary
        from core.onboard import STYLE
        ok = questionary.confirm(
            _t("uninstall.confirm"), default=False, style=STYLE,
        ).ask()
        if not ok:
            _print(f"  [dim]{_t('uninstall.cancelled')}[/dim]")
            return
    except ImportError:
        answer = input(f"  {_t('uninstall.confirm')} [y/N] ").strip().lower()
        if answer != "y":
            _print(f"  [dim]{_t('uninstall.cancelled')}[/dim]")
            return

    removed = []

    # 1. Uninstall daemon if exists
    try:
        from core.daemon import uninstall_daemon
        ok, msg = uninstall_daemon()
        if ok:
            removed.append("daemon")
            _print(f"  [green]✓[/green] {msg}")
    except Exception:
        pass

    # 2. Remove /usr/local/bin/swarm symlink
    target = "/usr/local/bin/swarm"
    if os.path.islink(target):
        try:
            os.remove(target)
            removed.append("CLI symlink")
            _print(f"  [green]✓[/green] Removed {target}")
        except PermissionError:
            result = subprocess.run(["sudo", "rm", "-f", target],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                removed.append("CLI symlink")
                _print(f"  [green]✓[/green] Removed {target}")
            else:
                _print(f"  [yellow]![/yellow] Could not remove {target}")

    # 3. pip uninstall the package
    project_root = os.path.dirname(os.path.abspath(__file__))
    venv_pip = os.path.join(project_root, ".venv", "bin", "pip")
    pip_cmd = venv_pip if os.path.exists(venv_pip) else "pip"
    result = subprocess.run(
        [pip_cmd, "uninstall", "-y", "swarm-agent-stack"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        removed.append("pip package")
        _print(f"  [green]✓[/green] Uninstalled pip package")

    if removed:
        _print(f"\n  [green]✓[/green] {_t('uninstall.done')} ({', '.join(removed)})")
        _print(f"  [dim]Source code remains at: {project_root}[/dim]")
        _print(f"  [dim]To fully remove: rm -rf {project_root}[/dim]")
    else:
        _print(f"  [dim]Nothing to uninstall.[/dim]")


def cmd_update(branch: str = "", console=None):
    """Pull latest code from GitHub and reinstall dependencies."""
    import subprocess
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    def _print(msg):
        if console:
            console.print(msg)
        else:
            print(msg)

    from core.i18n import t as _t
    project_root = os.path.dirname(os.path.abspath(__file__))

    # Show current version info
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None

    if tomllib:
        try:
            with open(os.path.join(project_root, "pyproject.toml"), "rb") as f:
                pyproject = tomllib.load(f)
            version = pyproject.get("project", {}).get("version", "?")
            _print(f"  [dim]{_t('update.version', version=version)}[/dim]")
        except Exception:
            pass

    # Get current branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root, capture_output=True, text=True,
    )
    current_branch = result.stdout.strip() if result.returncode == 0 else "main"
    target_branch = branch or current_branch
    _print(f"  [dim]{_t('update.branch', branch=target_branch)}[/dim]")

    # Get remote name
    result = subprocess.run(
        ["git", "remote"],
        cwd=project_root, capture_output=True, text=True,
    )
    remote = result.stdout.strip().split("\n")[0] if result.returncode == 0 else "origin"

    _print(f"  [dim]{_t('update.checking')}[/dim]")

    # Fetch from remote
    _print(f"  [dim]{_t('update.fetching', remote=remote)}[/dim]")
    result = subprocess.run(
        ["git", "fetch", remote, target_branch],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        _print(f"  [red]✗[/red] {_t('update.failed', err=result.stderr[:200])}")
        return

    # Check if there are updates
    result = subprocess.run(
        ["git", "log", f"HEAD..{remote}/{target_branch}", "--oneline"],
        cwd=project_root, capture_output=True, text=True,
    )
    commits = result.stdout.strip()
    if not commits:
        _print(f"  [green]✓[/green] {_t('update.up_to_date')}")
        return

    commit_count = len(commits.split("\n"))
    _print(f"  [cyan]{commit_count} new commit(s) available[/cyan]")

    # Show what will change
    result = subprocess.run(
        ["git", "diff", "--stat", f"HEAD..{remote}/{target_branch}"],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n")[-3:]:
            _print(f"  [dim]{line.strip()}[/dim]")

    # Stash local changes if any
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root, capture_output=True, text=True,
    )
    has_local_changes = bool(result.stdout.strip())
    if has_local_changes:
        _print(f"  [yellow]![/yellow] Stashing local changes…")
        subprocess.run(
            ["git", "stash", "push", "-m", "swarm-update-auto-stash"],
            cwd=project_root, capture_output=True, text=True,
        )

    # Pull
    result = subprocess.run(
        ["git", "pull", remote, target_branch],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        _print(f"  [red]✗[/red] {_t('update.failed', err=result.stderr[:200])}")
        # Restore stash on failure
        if has_local_changes:
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=project_root, capture_output=True, text=True,
            )
        return

    # Count changed files
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~" + str(commit_count), "HEAD"],
        cwd=project_root, capture_output=True, text=True,
    )
    changed_files = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0

    # Reinstall dependencies
    _print(f"  [dim]{_t('update.deps')}[/dim]")
    venv_pip = os.path.join(project_root, ".venv", "bin", "pip")
    pip_cmd = venv_pip if os.path.exists(venv_pip) else "pip"
    subprocess.run(
        [pip_cmd, "install", "-e", ".[dev]", "-q"],
        cwd=project_root, capture_output=True, text=True,
    )

    # Pop stash if we stashed
    if has_local_changes:
        _print(f"  [dim]Restoring local changes…[/dim]")
        result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=project_root, capture_output=True, text=True,
        )
        if result.returncode != 0:
            _print(f"  [yellow]![/yellow] Stash pop had conflicts — check 'git stash list'")

    # Show updated version
    if tomllib:
        try:
            with open(os.path.join(project_root, "pyproject.toml"), "rb") as f:
                pyproject = tomllib.load(f)
            new_version = pyproject.get("project", {}).get("version", "?")
            _print(f"  [dim]{_t('update.version', version=new_version)}[/dim]")
        except Exception:
            pass

    summary = _t("update.changes", n=changed_files)
    _print(f"  [green]✓[/green] {_t('update.updated', summary=summary)}\n")


def main():
    parser = argparse.ArgumentParser(prog="swarm")
    sub    = parser.add_subparsers(dest="cmd")

    sub.add_parser("onboard", help="Interactive setup wizard")
    sub.add_parser("init", help="Interactive setup wizard (alias)")
    sub.add_parser("configure", help="Interactive setup wizard (alias)")
    sub.add_parser("chat", help="Interactive chat mode")

    p_run = sub.add_parser("run", help="Submit a task and run all agents")
    p_run.add_argument("task", help="Task description")

    sub.add_parser("status", help="Show task board")
    sub.add_parser("scores", help="Show reputation scores")
    p_doc = sub.add_parser("doctor", help="System health check")
    p_doc.add_argument("--repair", action="store_true",
                        help="Auto-fix common issues (missing .env, dirs, stale tasks)")
    p_doc.add_argument("--deep", action="store_true",
                        help="Deep diagnostics (disk, skills, workflows, Python version)")

    p_export = sub.add_parser("export", help="Export task results")
    p_export.add_argument("task_id", help="Task ID (full or prefix)")
    p_export.add_argument("--format", "-f", choices=["md", "json"], default="md",
                          help="Output format (default: md)")

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
    p_create = agents_sub.add_parser("create", help="Create a new agent")
    p_create.add_argument("name", help="Agent ID/name")
    p_create.add_argument("--template", choices=["researcher", "coder", "debugger", "doc_writer"],
                          default=None, help="Use a built-in role template")
    p_add = agents_sub.add_parser("add", help="Create a new agent (alias for create)")
    p_add.add_argument("name", help="Agent ID/name")
    p_add.add_argument("--template", choices=["researcher", "coder", "debugger", "doc_writer"],
                       default=None, help="Use a built-in role template")

    p_wf = sub.add_parser("workflow", help="Workflow management")
    wf_sub = p_wf.add_subparsers(dest="wf_cmd")
    wf_sub.add_parser("list", help="List available workflows")
    p_wf_run = wf_sub.add_parser("run", help="Run a workflow")
    p_wf_run.add_argument("name", help="Workflow name (e.g., code_review, bug_fix)")
    p_wf_run.add_argument("--input", "-i", default="", help="Task input for the workflow")

    p_chain = sub.add_parser("chain", help="On-chain identity management")
    p_chain.add_argument("action", choices=["status", "balance", "init", "register", "health"],
                         help="Chain action")
    p_chain.add_argument("agent_id", nargs="?", default=None,
                         help="Agent ID (required for init/register)")

    p_install = sub.add_parser("install", help="Install swarm from GitHub")
    p_install.add_argument("--repo", default="",
                           help="GitHub repo URL (default: SWARM_REPO env or built-in)")
    p_install.add_argument("--target", default="",
                           help="Install directory (default: ~/swarm-dev)")

    sub.add_parser("uninstall", help="Remove swarm CLI and daemon")

    p_update = sub.add_parser("update", help="Pull latest from GitHub and reinstall")
    p_update.add_argument("--branch", default="",
                          help="Branch to update from (default: current branch)")

    p_ev = sub.add_parser("evolve", help="Manage evolution actions")
    p_ev.add_argument("agent_id")
    p_ev.add_argument("action", choices=["confirm"])

    args = parser.parse_args()
    if args.cmd in ("onboard", "init", "configure"):
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
        cmd_doctor(repair=args.repair, deep=args.deep)
    elif args.cmd == "export":
        cmd_export(args.task_id, fmt=args.format)
    elif args.cmd == "gateway":
        cmd_gateway(action=args.action, port=args.port,
                    token=args.token, force=args.force)
    elif args.cmd == "chain":
        cmd_chain(args.action, args.agent_id)
    elif args.cmd == "workflow":
        if args.wf_cmd == "list":
            cmd_workflows()
        elif args.wf_cmd == "run":
            cmd_workflow_run(args.name, args.input)
        else:
            p_wf.print_help()
    elif args.cmd == "agents":
        if args.agents_cmd in ("create", "add"):
            cmd_agents_add(args.name, template=getattr(args, 'template', None))
        else:
            p_agents.print_help()
    elif args.cmd == "install":
        cmd_install(repo=args.repo, target=args.target)
    elif args.cmd == "uninstall":
        cmd_uninstall()
    elif args.cmd == "update":
        cmd_update(branch=args.branch)
    elif args.cmd == "evolve":
        if args.action == "confirm":
            cmd_evolve_confirm(args.agent_id)
    else:
        # Default: enter interactive chat mode (as documented)
        interactive_main()


if __name__ == "__main__":
    main()

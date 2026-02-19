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
  swarm gateway               # start HTTP gateway (foreground)
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
    try:
        from core.gateway import start_gateway, DEFAULT_PORT
        gw_port = int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
        gw_token = os.environ.get("SWARM_GATEWAY_TOKEN", "")
        gw_server = start_gateway(port=gw_port, token=gw_token, daemon=True)
        if gw_server:
            console.print(f"  [dim]Gateway: http://127.0.0.1:{gw_port}/[/dim]")
            if gw_token:
                console.print(f"  [dim]Token:   {gw_token}[/dim]")
        else:
            console.print(f"  [yellow]Gateway: failed to start on port {gw_port}[/yellow]")
    except Exception as e:
        console.print(f"  [yellow]Gateway: {e}[/yellow]")
    console.print()

    # ── Chat loop ──
    from core.orchestrator import Orchestrator
    from core.task_board import TaskBoard

    while True:
        try:
            task_text = Prompt.ask("[bold green]You[/bold green]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye![/dim]")
            break

        task_text = task_text.strip()
        if not task_text:
            continue

        # ── Commands ──
        # /command → slash command;  bare "exit"/"quit"/"configure" also recognized
        _lower = task_text.lower()
        cmd = _lower.lstrip("/") if _lower.startswith("/") else None

        if _lower in ("exit", "quit"):
            console.print("[dim]Bye![/dim]")
            break

        if cmd == "help":
            console.print(Panel(
                "[bold]/status[/bold]     — task board\n"
                "[bold]/scores[/bold]    — reputation scores\n"
                "[bold]/usage[/bold]     — token usage & cost stats\n"
                "[bold]/workflows[/bold] — list available workflows\n"
                "[bold]/config[/bold]    — show current config\n"
                "[bold]/configure[/bold] — re-run full setup wizard\n"
                "[bold]/chain[/bold]     — on-chain status (chain status/balance/init/health)\n"
                "[bold]/doctor[/bold]    — system health check\n"
                "[bold]/clear[/bold]     — clear task history\n"
                "[bold]exit[/bold]       — quit",
                title="Commands",
                border_style="dim",
            ))
            continue

        if cmd == "status":
            _show_status_rich(console)
            continue

        if cmd == "scores":
            cmd_scores()
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
            board = TaskBoard()
            board.clear()
            for fp in [".context_bus.json"]:
                if os.path.exists(fp):
                    os.remove(fp)
            import glob
            for fp in glob.glob(".mailboxes/*.jsonl"):
                os.remove(fp)
            console.print("  [dim]Cleared.[/dim]\n")
            continue

        if cmd and cmd.startswith("chain"):
            parts = cmd.split()
            action = parts[1] if len(parts) > 1 else "status"
            aid = parts[2] if len(parts) > 2 else None
            cmd_chain(action, aid, console)
            continue

        if cmd in ("setup", "configure") or _lower in ("configure", "swarm configure"):
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
            console.print(f"  [yellow]Unknown command: {task_text}[/yellow]  Type [bold]/help[/bold] for commands.\n")
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
                console.print()
                for desc, flags in failures:
                    reason = ""
                    for f in flags:
                        if f.startswith("failed:"):
                            err = f[7:]
                            if "401" in err:
                                reason = "API Key 无效或过期"
                            elif "429" in err:
                                reason = "请求频率超限"
                            elif "timeout" in err.lower():
                                reason = "请求超时"
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
                console.print(f"\n  [yellow]No result returned.[/yellow] Run [bold]/status[/bold] to check.\n")

        except Exception as e:
            console.print(f"\n  [red]Error: {e}[/red]\n")


# ── Rich status display ───────────────────────────────────────────────────────

def _show_status_rich(console):
    """Show task board with rich formatting."""
    if not os.path.exists(".task_board.json"):
        console.print("  [dim]No tasks yet.[/dim]\n")
        return

    data = json.load(open(".task_board.json"))
    if not data:
        console.print("  [dim]No tasks yet.[/dim]\n")
        return

    from rich.table import Table
    table = Table(box=None, padding=(0, 1))
    table.add_column("Status", style="bold")
    table.add_column("Agent")
    table.add_column("Description")

    status_style = {
        "completed": "green", "failed": "red", "pending": "yellow",
        "claimed": "cyan", "review": "magenta", "blocked": "dim",
    }

    for tid, t in data.items():
        st = t["status"]
        style = status_style.get(st, "")
        table.add_row(
            f"[{style}]{st}[/{style}]",
            t.get("agent_id") or "-",
            t["description"][:60],
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
    print(f"\n🚀 Submitting task: {task!r}\n")
    orch.run(task)
    print("\n✅ All agents finished.\n")
    cmd_status()


def cmd_status():
    data = json.load(open(".task_board.json")) if os.path.exists(".task_board.json") else {}
    print(f"\n{'ID':36}  {'STATUS':12}  {'AGENT':12}  DESCRIPTION")
    print("-" * 110)
    for tid, t in data.items():
        print(f"{tid:36}  {t['status']:12}  {(t.get('agent_id') or '-'):12}  "
              f"{t['description'][:40]}")
    print()


def cmd_scores():
    path = "memory/reputation_cache.json"
    if not os.path.exists(path):
        print("No scores yet.")
        return
    cache = json.load(open(path))
    print(f"\n{'AGENT':15}  {'SCORE':6}  TREND / STATUS")
    print("-" * 50)

    from reputation.scorer import ScoreAggregator
    sc = ScoreAggregator()
    for agent_id, data in cache.items():
        score  = data.get("composite", 0)
        trend  = sc.trend(agent_id)
        status = sc.threshold_status(agent_id)
        icon   = {"healthy": "✅", "watch": "👀", "warning": "⚠️", "evolve": "🔄"}.get(status, "")
        print(f"{agent_id:15}  {score:6.1f}  {trend:10}  {status} {icon}")
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


def cmd_gateway():
    from core.gateway import run_gateway_cli
    run_gateway_cli()


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

    with open("config/agents.yaml", "w") as f:
        f.write("# config/agents.yaml\n\n")
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

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
        print(f"✅ Model swap applied for {agent_id}.")
    else:
        print("Cancelled.")


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
    sub.add_parser("gateway", help="Start HTTP gateway (foreground)")

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
        cmd_gateway()
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
        # Default: show help for python3 main.py (no args)
        parser.print_help()


if __name__ == "__main__":
    main()

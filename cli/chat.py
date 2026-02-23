"""Interactive chat mode — the main REPL for Cleo CLI."""
from __future__ import annotations

import json
import os
import re
import sys

from core.theme import theme as _theme


# ── Setup / Onboard entry ──────────────────────────────────────────────────

def cmd_init(provider: str = "", api_key: str = "",
             model: str = "", non_interactive: bool = False,
             section: str = ""):
    if section:
        from core.onboard import run_onboard_section
        run_onboard_section(section)
    elif non_interactive or provider or api_key:
        from core.onboard import run_quick_setup
        run_quick_setup(
            provider_arg=provider, api_key_arg=api_key,
            model_arg=model, non_interactive=non_interactive or bool(provider),
        )
    else:
        from core.onboard import run_onboard
        run_onboard()


# ── Session persistence helpers ─────────────────────────────────────────────

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
            if (now - mtime) < 86400 and mtime > best_ts:
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
    if not os.path.exists(_TEMPLATES_PATH):
        return []
    try:
        with open(_TEMPLATES_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_template(task: str):
    templates = _load_templates()
    if task not in templates:
        templates.append(task)
        templates = templates[-20:]
        os.makedirs(os.path.dirname(_TEMPLATES_PATH), exist_ok=True)
        with open(_TEMPLATES_PATH, "w") as f:
            json.dump(templates, f, ensure_ascii=False, indent=2)


# ── Rich status display ─────────────────────────────────────────────────────

def _show_status_rich(console, agent_filter: str = "",
                      status_filter: str = "", since_filter: str = "",
                      search_filter: str = ""):
    """Show task board with rich formatting and optional filtering."""
    from core.i18n import t as _t
    if not os.path.exists(".task_board.json"):
        console.print(f"  [{_theme.muted}]{_t('cmd.no_tasks')}[/{_theme.muted}]\n")
        return

    data = json.load(open(".task_board.json"))
    if not data:
        console.print(f"  [{_theme.muted}]{_t('cmd.no_tasks')}[/{_theme.muted}]\n")
        return

    filtered = dict(data)

    if agent_filter:
        filtered = {k: v for k, v in filtered.items()
                    if v.get("agent_id", "") == agent_filter}

    if status_filter:
        allowed = set(s.strip() for s in status_filter.split(","))
        filtered = {k: v for k, v in filtered.items()
                    if v.get("status", "") in allowed}

    if since_filter:
        cutoff = _parse_time_range(since_filter)
        filtered = {k: v for k, v in filtered.items()
                    if v.get("created_at", 0) >= cutoff}

    if search_filter:
        _q = search_filter.lower()
        filtered = {k: v for k, v in filtered.items()
                    if _q in v.get("description", "").lower()}

    if not filtered:
        if agent_filter or status_filter or search_filter:
            console.print(f"  [{_theme.muted}]No tasks match the filter.[/{_theme.muted}]\n")
        else:
            console.print(f"  [{_theme.muted}]{_t('cmd.no_tasks')}[/{_theme.muted}]\n")
        return

    from rich.table import Table
    table = Table(box=None, padding=(0, 1), show_header=True)
    table.add_column("Status", style=_theme.heading, min_width=10)
    table.add_column("Agent", min_width=10)
    table.add_column("Description", min_width=30)
    table.add_column("ID", style=_theme.muted, max_width=8)

    status_style = {
        "completed": _theme.success, "failed": _theme.error,
        "pending": _theme.warning, "claimed": _theme.info,
        "review": _theme.accent_light, "blocked": _theme.muted,
        "cancelled": f"{_theme.muted} {_theme.warning}",
        "paused": f"bold {_theme.warning}",
    }

    sort_order = {"claimed": 0, "review": 1, "pending": 2, "paused": 3,
                  "completed": 4, "failed": 5, "cancelled": 6, "blocked": 7}
    sorted_items = sorted(filtered.items(),
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

    if agent_filter or status_filter or search_filter:
        total = len(data)
        shown = len(filtered)
        console.print(f"  [{_theme.muted}]Showing {shown}/{total} tasks[/{_theme.muted}]")
    console.print()


def _parse_time_range(since_str: str) -> float:
    """Parse '1h', '30m', '2d' into a Unix timestamp cutoff."""
    import time
    m = re.match(r"(\d+)\s*([smhd])", since_str.lower())
    if not m:
        return 0
    value = int(m.group(1))
    unit = m.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return time.time() - (value * multiplier.get(unit, 3600))


# ── Cost estimation ─────────────────────────────────────────────────────────

def _estimate_task_cost(task_text: str, num_agents: int) -> str:
    """Estimate token usage and cost for a task (rough heuristic)."""
    try:
        if os.path.exists("memory/usage.json"):
            with open("memory/usage.json") as f:
                usage = json.load(f)
            total_tokens = usage.get("total_tokens", 0)
            total_tasks = usage.get("total_tasks", 0)
            total_cost = usage.get("total_cost_usd", 0)

            if total_tasks >= 3:
                avg_tokens = total_tokens // total_tasks
                avg_cost = total_cost / total_tasks
                length_factor = max(0.5, min(2.0, len(task_text) / 100))
                est_tokens = int(avg_tokens * length_factor)
                est_cost = avg_cost * length_factor

                parts = [f"~{num_agents} agents"]
                if est_tokens > 0:
                    parts.append(f"~{est_tokens:,} tokens")
                if est_cost > 0.001:
                    parts.append(f"~${est_cost:.3f}")
                return "Estimated: " + " · ".join(parts)
    except Exception:
        pass

    est_tokens = num_agents * max(2000, len(task_text) * 10)
    return f"Estimated: ~{num_agents} agents · ~{est_tokens:,} tokens"


# ── Version check ───────────────────────────────────────────────────────────

def _check_version_async(console):
    """Non-blocking version check — cached, runs at most once per 24h."""
    import time
    from cli.helpers import get_version

    cache_path = os.path.join(".cache", "version_check.json")

    try:
        os.makedirs(".cache", exist_ok=True)
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                cache = json.load(f)
            if time.time() - cache.get("ts", 0) < 86400:
                if cache.get("newer"):
                    console.print(
                        f"  [{_theme.muted}]💡 Update available: v{cache['newer']} "
                        f"(current: v{get_version()}). "
                        f"Run [{_theme.heading}]cleo update[/{_theme.heading}][/{_theme.muted}]"
                    )
                return

        import threading

        def _do_check():
            try:
                import httpx
                resp = httpx.get(
                    "https://api.github.com/repos/user/cleo-dev/releases/latest",
                    timeout=3.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latest = data.get("tag_name", "").lstrip("v")
                    current = get_version()
                    newer = latest if latest > current else ""
                    with open(cache_path, "w") as f:
                        json.dump({"ts": time.time(), "newer": newer,
                                   "latest": latest}, f)
                    if newer:
                        console.print(
                            f"  [{_theme.muted}]💡 Update available: v{newer} "
                            f"(current: v{current}). "
                            f"Run [{_theme.heading}]cleo update[/{_theme.heading}][/{_theme.muted}]"
                        )
            except Exception:
                with open(cache_path, "w") as f:
                    json.dump({"ts": time.time(), "newer": ""}, f)

        t = threading.Thread(target=_do_check, daemon=True)
        t.start()
    except Exception:
        pass


# ── Interactive Chat Main Loop ──────────────────────────────────────────────

def interactive_main():
    """OpenClaw-style interactive CLI: onboard → chat loop."""
    from core.env_loader import load_dotenv

    if "--setup" in sys.argv:
        cmd_init()
        load_dotenv()

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
    console.print(f"""[{_theme.accent}]
    ██████╗██╗     ███████╗ ██████╗
   ██╔════╝██║     ██╔════╝██╔═══██╗
   ██║     ██║     █████╗  ██║   ██║
   ██║     ██║     ██╔══╝  ██║   ██║
   ╚██████╗███████╗███████╗╚██████╔╝
    ╚═════╝╚══════╝╚══════╝ ╚═════╝[/{_theme.accent}]
[{_theme.muted}]       type a task · /help · /config · exit[/{_theme.muted}]
""")

    # ── First-run check ──
    if not os.path.exists("config/agents.yaml"):
        from core.i18n import t as _t
        console.print(f"  [{_theme.muted}]{_t('onboard.first_run_hint')}[/{_theme.muted}]")
        console.print()
        from core.onboard import run_quick_setup
        ok = run_quick_setup()
        if not ok:
            return
        load_dotenv()

    # ── Load config & show team ──
    import yaml
    with open("config/agents.yaml") as f:
        config = yaml.safe_load(f)

    agents = config.get("agents", [])
    agent_names = ", ".join(a["id"] for a in agents)
    console.print(f"  [{_theme.muted}]Agents: {agent_names}[/{_theme.muted}]")

    # ── Version check ──
    _check_version_async(console)

    # ── Preflight check ──
    from core.doctor import run_preflight
    from core.i18n import t as _t
    issues = run_preflight()
    if issues:
        console.print()
        console.print(Panel(
            "\n".join(f"  [{_theme.warning}]![/{_theme.warning}] {issue}" for issue in issues)
            + f"\n\n  [{_theme.muted}]{_t('error.suggest_doctor')}[/{_theme.muted}]",
            title=f"[{_theme.warning}]{_t('preflight.title')}[/{_theme.warning}]",
            border_style=_theme.warning,
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
            gw_port = int(os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT)))
            gw_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
            gw_server = start_gateway(port=gw_port, token=gw_token, daemon=True)
            if gw_server:
                console.print(f"  [{_theme.muted}]Gateway:[/{_theme.muted}] [{_theme.heading}]http://127.0.0.1:{gw_port}/[/{_theme.heading}]  [{_theme.muted}]/gateway for status[/{_theme.muted}]")
                try:
                    from core.ws_gateway import _HAS_WEBSOCKETS
                    if _HAS_WEBSOCKETS:
                        console.print(f"  [{_theme.muted}]WebSocket:[/{_theme.muted}] [{_theme.heading}]ws://0.0.0.0:{gw_port + 1}/[/{_theme.heading}]  [{_theme.muted}]real-time push[/{_theme.muted}]")
                except ImportError:
                    pass
            else:
                console.print(f"  [{_theme.warning}]Gateway: port {gw_port} in use (use --force or /gateway restart)[/{_theme.warning}]")
        except Exception as e:
            console.print(f"  [{_theme.warning}]Gateway: {e}[/{_theme.warning}]")
    else:
        console.print(f"  [{_theme.muted}]Gateway: skipped (--no-gateway)[/{_theme.muted}]")
    console.print()

    # ── Session persistence ──
    from core.orchestrator import Orchestrator
    from core.task_board import TaskBoard

    chat_history: list[dict] = []
    _session_dir = os.path.join("memory", "sessions")
    os.makedirs(_session_dir, exist_ok=True)

    _recent_session = _find_recent_session(_session_dir)
    if _recent_session:
        try:
            resume = Prompt.ask(
                f"  [{_theme.muted}]Resume previous session?[/{_theme.muted}] [{_theme.heading}]({_recent_session['task'][:40]})[/{_theme.heading}]",
                choices=["y", "n"],
                default="n",
            )
            if resume == "y":
                chat_history = _recent_session.get("history", [])
                console.print(f"  [{_theme.muted}]Restored {len(chat_history)} message(s)[/{_theme.muted}]")
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
            task_text = Prompt.ask(f"[{_theme.success}]You[/{_theme.success}]")
        except (KeyboardInterrupt, EOFError):
            from core.i18n import t as _t
            console.print(f"\n[{_theme.muted}]{_t('cmd.bye')}[/{_theme.muted}]")
            _save_session(_session_dir, chat_history)
            break

        task_text = task_text.strip()
        if not task_text:
            continue

        # ── Commands ──
        _lower = task_text.lower()
        cmd = _lower.lstrip("/") if _lower.startswith("/") else None

        if _lower in ("exit", "quit"):
            from core.i18n import t as _t
            console.print(f"[{_theme.muted}]{_t('cmd.bye')}[/{_theme.muted}]")
            _save_session(_session_dir, chat_history)
            break

        if cmd == "help":
            _handle_help(console)
            continue

        if cmd == "status" or (cmd and cmd.startswith("status ")):
            _handle_status(cmd, console)
            continue

        if cmd == "scores":
            from cli.status_cmd import cmd_scores
            cmd_scores(console)
            continue

        if cmd == "doctor":
            from cli.doctor_cmd import cmd_doctor
            cmd_doctor(console)
            continue

        if cmd and cmd.startswith("doctor") and "--export" in cmd:
            from cli.doctor_cmd import cmd_doctor_export
            cmd_doctor_export(console)
            continue

        if cmd == "logs" or (cmd and cmd.startswith("logs ")):
            _handle_logs(cmd)
            continue

        if cmd == "plugins" or (cmd and cmd.startswith("plugins ")):
            _handle_plugins(cmd, console)
            continue

        if cmd == "pause" or (cmd and cmd.startswith("pause ")):
            _handle_pause(cmd, console)
            continue

        if cmd == "resume" or (cmd and cmd.startswith("resume ")):
            _handle_resume(cmd, console)
            continue

        if cmd == "usage":
            from cli.usage_cmd import cmd_usage
            cmd_usage(console)
            continue

        if cmd == "workflows":
            from cli.workflow_cmd import cmd_workflows
            cmd_workflows(console)
            continue

        if cmd == "cancel":
            _handle_cancel(console)
            continue

        if cmd == "budget":
            from cli.usage_cmd import cmd_budget
            cmd_budget(console)
            continue

        if cmd == "config":
            _handle_config(console, config, agents)
            continue

        if cmd == "clear":
            _handle_clear(console)
            continue

        if cmd == "config history":
            _handle_config_history(console)
            continue

        if cmd and cmd.startswith("config rollback"):
            _handle_config_rollback(cmd, console)
            # Reload config after rollback
            if os.path.exists("config/agents.yaml"):
                with open("config/agents.yaml") as f:
                    config = yaml.safe_load(f)
                agents = config.get("agents", [])
            continue

        if cmd and cmd.startswith("gateway"):
            _handle_gateway(cmd, console)
            continue

        if cmd and cmd.startswith("chain"):
            parts = cmd.split()
            action = parts[1] if len(parts) > 1 else "status"
            aid = parts[2] if len(parts) > 2 else None
            from cli.chain_cmd import cmd_chain
            cmd_chain(action, aid, console)
            continue

        if cmd == "install":
            from cli.install_cmd import cmd_install
            cmd_install(console=console)
            continue

        if cmd == "uninstall":
            from cli.install_cmd import cmd_uninstall
            cmd_uninstall(console=console)
            continue

        if cmd == "update":
            from cli.install_cmd import cmd_update
            cmd_update(console=console)
            continue

        if cmd in ("setup", "configure") or _lower in ("configure", "cleo configure"):
            from core.config_manager import snapshot_all
            snapshot_all(reason="pre-configure")
            cmd_init()
            load_dotenv()
            if os.path.exists("config/agents.yaml"):
                with open("config/agents.yaml") as f:
                    config = yaml.safe_load(f)
                agents = config.get("agents", [])
                agent_names = ", ".join(a["id"] for a in agents)
                console.print(f"  [{_theme.muted}]Agents: {agent_names}[/{_theme.muted}]\n")
            continue

        if cmd == "save":
            _handle_save(console, chat_history)
            continue

        if cmd == "templates":
            result = _handle_templates(console)
            if result:
                task_text = result
                # Fall through to submit
            else:
                continue

        if cmd == "export":
            _handle_export(console)
            continue

        # Unknown slash command
        if cmd is not None:
            from core.i18n import t as _t
            console.print(f"  [{_theme.warning}]{_t('cmd.unknown_cmd', cmd=task_text)}[/{_theme.warning}]  /help\n")
            continue

        # ── Submit task to agents ──
        chat_history.append({"role": "user", "content": task_text})

        if not os.environ.get("CLEO_AUTO_CONFIRM"):
            est = _estimate_task_cost(task_text, len(agents))
            if est:
                console.print(f"  [{_theme.muted}]{est}[/{_theme.muted}]")

        try:
            board = TaskBoard()
            board.clear()
            for fp in [".context_bus.json"]:
                if os.path.exists(fp):
                    os.remove(fp)
            import glob
            for fp in glob.glob(".mailboxes/*.jsonl"):
                os.remove(fp)

            orch = Orchestrator()
            task_id = orch.submit(task_text, required_role="leo")

            from core.live_status import LiveStatus
            import time as _time

            live = LiveStatus(console, config.get("agents", []))
            live.start()
            orch._launch_all()

            while any(p.is_alive() for p in orch.procs):
                live.poll(board)
                _time.sleep(0.5)

            live.poll(board)
            live.stop()

            result_text = board.collect_results(task_id)

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
                    console.print(f"  [{_theme.error}]✗ {desc[:50]}[/{_theme.error}]")
                    if reason:
                        console.print(f"    [{_theme.muted}]{reason}[/{_theme.muted}]")
                console.print(f"  [{_theme.muted}]{_t('error.suggest_doctor')}[/{_theme.muted}]")

            if result_text:
                from core.live_status import strip_think_tags
                result_text = strip_think_tags(result_text)
                console.print()
                try:
                    console.print(Markdown(result_text))
                except Exception:
                    console.print(result_text)
                console.print()
                chat_history.append({"role": "assistant", "content": result_text})
            elif not failures:
                from core.i18n import t as _t
                console.print(f"\n  [{_theme.warning}]{_t('cmd.no_result')}[/{_theme.warning}] /status\n")

        except Exception as e:
            console.print(f"\n  [{_theme.error}]Error: {e}[/{_theme.error}]\n")


# ── Slash command handlers (extracted for readability) ───────────────────────

def _handle_help(console):
    from core.i18n import t as _t
    from rich.table import Table as _HelpTbl
    from rich.panel import Panel
    htbl = _HelpTbl(
        show_header=False, show_edge=False, box=None,
        padding=(0, 1), expand=False,
    )
    htbl.add_column("cmd", style=_theme.heading, min_width=18)
    htbl.add_column("desc", style=_theme.muted)
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
        ("/logs",            _t("help.logs")),
        ("/plugins",         _t("help.plugins")),
        ("/pause",           _t("help.pause")),
        ("/resume",          _t("help.resume")),
        ("/doctor",          _t("help.doctor")),
        ("/doctor --export", _t("help.doctor_export")),
        ("/clear",           _t("help.clear")),
        ("/install",         _t("help.install")),
        ("/uninstall",       _t("help.uninstall")),
        ("/update",          _t("help.update")),
        ("exit",             _t("help.exit")),
    ]
    for c, d in _cmds:
        htbl.add_row(c, d)
    console.print(Panel(htbl, title="Commands", border_style=_theme.muted))


def _handle_status(cmd, console):
    _st_parts = cmd.split() if cmd else []
    _st_agent = ""
    _st_status = ""
    _st_search = ""
    i = 1
    while i < len(_st_parts):
        if _st_parts[i] in ("--agent", "-a") and i + 1 < len(_st_parts):
            _st_agent = _st_parts[i + 1]; i += 2
        elif _st_parts[i] in ("--status", "-s") and i + 1 < len(_st_parts):
            _st_status = _st_parts[i + 1]; i += 2
        elif _st_parts[i] in ("--search", "-q") and i + 1 < len(_st_parts):
            _st_search = _st_parts[i + 1]; i += 2
        elif not _st_parts[i].startswith("-"):
            _st_agent = _st_parts[i]; i += 1
        else:
            i += 1
    _show_status_rich(console, agent_filter=_st_agent,
                      status_filter=_st_status, search_filter=_st_search)


def _handle_logs(cmd):
    from cli.logs_cmd import cmd_logs
    _log_args = cmd.split() if cmd else []
    _log_follow = "-f" in _log_args or "--follow" in _log_args
    _log_agent = ""
    _log_level = ""
    for i, a in enumerate(_log_args):
        if a == "--agent" and i + 1 < len(_log_args):
            _log_agent = _log_args[i + 1]
        if a == "--level" and i + 1 < len(_log_args):
            _log_level = _log_args[i + 1]
    cmd_logs(follow=_log_follow, agent=_log_agent, level=_log_level)


def _handle_plugins(cmd, console):
    _pl_parts = cmd.split() if cmd else ["plugins"]
    _pl_action = _pl_parts[1] if len(_pl_parts) > 1 else "list"
    _pl_name = _pl_parts[2] if len(_pl_parts) > 2 else ""
    from core.plugin_cli import (
        cmd_plugins_list, cmd_plugins_install, cmd_plugins_remove,
        cmd_plugins_enable, cmd_plugins_disable, cmd_plugins_info,
    )
    if _pl_action == "list":
        cmd_plugins_list(console)
    elif _pl_action == "install" and _pl_name:
        cmd_plugins_install(_pl_name, console)
    elif _pl_action == "remove" and _pl_name:
        cmd_plugins_remove(_pl_name, console)
    elif _pl_action == "enable" and _pl_name:
        cmd_plugins_enable(_pl_name, console)
    elif _pl_action == "disable" and _pl_name:
        cmd_plugins_disable(_pl_name, console)
    elif _pl_action == "info" and _pl_name:
        cmd_plugins_info(_pl_name, console)
    else:
        console.print(f"  [{_theme.muted}]Usage: /plugins <list|install|remove|enable|disable|info> [name][/{_theme.muted}]\n")


def _handle_pause(cmd, console):
    _pause_parts = cmd.split() if cmd else []
    _pause_tid = _pause_parts[1] if len(_pause_parts) > 1 else ""
    from core.task_board import TaskBoard as _PauseBoard
    _pb = _PauseBoard()
    _pd = _pb._read()
    if _pause_tid:
        _match = None
        for tid in _pd:
            if tid.startswith(_pause_tid):
                _match = tid
                break
        if _match and _pd[_match].get("status") in ("pending", "claimed"):
            _pd[_match]["status"] = "paused"
            _pb._write(_pd)
            console.print(f"  [{_theme.warning}]⏸[/{_theme.warning}] Paused: {_pd[_match]['description'][:40]}\n")
        else:
            console.print(f"  [{_theme.muted}]Task not found or not pauseable.[/{_theme.muted}]\n")
    else:
        _count = 0
        for tid, t in _pd.items():
            if t.get("status") in ("pending", "claimed"):
                t["status"] = "paused"
                _count += 1
        if _count:
            _pb._write(_pd)
            console.print(f"  [{_theme.warning}]⏸[/{_theme.warning}] Paused {_count} task(s)\n")
        else:
            console.print(f"  [{_theme.muted}]No active tasks to pause.[/{_theme.muted}]\n")


def _handle_resume(cmd, console):
    _resume_parts = cmd.split() if cmd else []
    _resume_tid = _resume_parts[1] if len(_resume_parts) > 1 else ""
    from core.task_board import TaskBoard as _ResumeBoard
    _rb = _ResumeBoard()
    _rd = _rb._read()
    if _resume_tid:
        _match = None
        for tid in _rd:
            if tid.startswith(_resume_tid):
                _match = tid
                break
        if _match and _rd[_match].get("status") == "paused":
            _rd[_match]["status"] = "pending"
            _rb._write(_rd)
            console.print(f"  [{_theme.success}]▶[/{_theme.success}] Resumed: {_rd[_match]['description'][:40]}\n")
        else:
            console.print(f"  [{_theme.muted}]Task not found or not paused.[/{_theme.muted}]\n")
    else:
        _count = 0
        for tid, t in _rd.items():
            if t.get("status") == "paused":
                t["status"] = "pending"
                _count += 1
        if _count:
            _rb._write(_rd)
            console.print(f"  [{_theme.success}]▶[/{_theme.success}] Resumed {_count} task(s)\n")
        else:
            console.print(f"  [{_theme.muted}]No paused tasks.[/{_theme.muted}]\n")


def _handle_cancel(console):
    from core.i18n import t as _t
    from core.task_board import TaskBoard
    board = TaskBoard()
    data = board._read()
    active = {tid: t for tid, t in data.items()
              if t.get("status") in ("pending", "claimed", "review", "paused")}
    if not active:
        console.print(f"  [{_theme.muted}]{_t('cmd.no_active')}[/{_theme.muted}]\n")
        return
    if len(active) == 1:
        tid = next(iter(active))
        board.cancel(tid)
        console.print(f"  [{_theme.warning}]{_t('cancel.done', n=1)}[/{_theme.warning}]\n")
        return
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
            console.print(f"  [{_theme.muted}]{_t('cmd.cancelled')}[/{_theme.muted}]\n")
            return
        if not selected:
            console.print(f"  [{_theme.muted}]{_t('cancel.none_selected')}[/{_theme.muted}]\n")
            return
        for tid in selected:
            board.cancel(tid)
        console.print(f"  [{_theme.warning}]{_t('cancel.done', n=len(selected))}[/{_theme.warning}]\n")
    except ImportError:
        count = board.cancel_all()
        console.print(f"  [{_theme.warning}]{_t('cancel.done', n=count)}[/{_theme.warning}]\n")


def _handle_config(console, config, agents):
    from rich.table import Table as RichTable
    tbl = RichTable(box=None, padding=(0, 1), show_header=True)
    tbl.add_column("Agent", style=_theme.heading)
    tbl.add_column("Provider")
    tbl.add_column("Model")
    tbl.add_column("Skills", style=_theme.muted)
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
    console.print(f"  [{_theme.muted}]Memory: {mem}  |  Chain: {chain_on}[/{_theme.muted}]\n")


def _handle_clear(console):
    from core.i18n import t as _t
    from core.task_board import TaskBoard
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
            console.print(f"  [{_theme.muted}]{_t('cmd.cancelled')}[/{_theme.muted}]\n")
            return
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
            console.print(f"  [{_theme.muted}]{_t('cmd.cleared')}: {', '.join(cleared_parts)}[/{_theme.muted}]\n")
        else:
            console.print(f"  [{_theme.muted}]{_t('cmd.cancelled')}[/{_theme.muted}]\n")
    except ImportError:
        board = TaskBoard()
        result = board.clear(force=True)
        for fp in [".context_bus.json"]:
            if os.path.exists(fp):
                os.remove(fp)
        import glob as _glob
        for fp in _glob.glob(".mailboxes/*.jsonl"):
            os.remove(fp)
        from core.i18n import t as _t
        console.print(f"  [{_theme.muted}]{_t('cmd.cleared')} ({result})[/{_theme.muted}]\n")


def _handle_config_history(console):
    from core.i18n import t as _t
    from core.config_manager import history as config_history
    entries = config_history("config/agents.yaml")
    if not entries:
        console.print(f"  [{_theme.muted}]{_t('config.no_backups')}[/{_theme.muted}]\n")
    else:
        from rich.table import Table as RichTable
        tbl = RichTable(box=None, padding=(0, 1), show_header=True)
        tbl.add_column("#", justify="right", style=_theme.heading, width=3)
        tbl.add_column("Timestamp", min_width=19)
        tbl.add_column("Hash", style=_theme.muted, width=12)
        tbl.add_column("Reason")
        import time as _t2
        for i, e in enumerate(entries):
            ts = _t2.strftime("%Y-%m-%d %H:%M:%S", _t2.localtime(e["timestamp"]))
            tbl.add_row(str(i), ts, e["hash"], e.get("reason", ""))
        console.print(tbl)
        console.print()


def _handle_config_rollback(cmd, console):
    from core.i18n import t as _t
    from core.config_manager import rollback as config_rollback
    from core.config_manager import history as config_hist
    entries = config_hist("config/agents.yaml")
    if not entries:
        console.print(f"  [{_theme.error}]{_t('config.rollback_fail')}[/{_theme.error}]\n")
        return
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
            console.print(f"  [{_theme.muted}]{_t('cmd.cancelled')}[/{_theme.muted}]\n")
            return
        version = selected
    except ImportError:
        parts = cmd.split()
        version = int(parts[2]) if len(parts) > 2 else -1
    ok = config_rollback("config/agents.yaml", version=version)
    if ok:
        import yaml as _yaml
        with open("config/agents.yaml") as f:
            config = _yaml.safe_load(f)
        agents = config.get("agents", [])
        console.print(f"  [{_theme.success}]{_t('config.rolled_back')}[/{_theme.success}] Agents: {', '.join(a['id'] for a in agents)}\n")
    else:
        console.print(f"  [{_theme.error}]{_t('config.rollback_fail')}[/{_theme.error}]\n")


def _handle_gateway(cmd, console):
    from cli.gateway_cmd import show_gateway_status
    parts = cmd.split()
    gw_action = parts[1] if len(parts) > 1 else "status"
    if gw_action == "status":
        show_gateway_status(console)
    elif gw_action == "stop":
        from core.gateway import kill_port, DEFAULT_PORT
        gw_port = int(os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT)))
        from core.i18n import t as _t
        console.print(f"  [{_theme.muted}]{_t('gw.stopping')}[/{_theme.muted}]")
        killed = kill_port(gw_port)
        if killed:
            console.print(f"  [{_theme.success}]\u2713[/{_theme.success}] Gateway stopped\n")
        else:
            console.print(f"  [{_theme.muted}]{_t('gw.not_running')}[/{_theme.muted}]\n")
    elif gw_action == "restart":
        from core.gateway import kill_port, start_gateway, DEFAULT_PORT
        gw_port = int(os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT)))
        gw_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
        from core.i18n import t as _t
        console.print(f"  [{_theme.muted}]{_t('gw.restarting')}[/{_theme.muted}]")
        kill_port(gw_port)
        import time as _time
        _time.sleep(0.5)
        srv = start_gateway(port=gw_port, token=gw_token, daemon=True)
        if srv:
            console.print(f"  [{_theme.success}]\u2713[/{_theme.success}] {_t('gw.started')} — http://127.0.0.1:{gw_port}/\n")
        else:
            console.print(f"  [{_theme.error}]\u2717[/{_theme.error}] {_t('gw.port_in_use', port=gw_port)}\n")
    elif gw_action == "install":
        from core.gateway import generate_token, DEFAULT_PORT
        from core.daemon import install_daemon
        gw_port = int(os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT)))
        gw_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
        if not gw_token:
            gw_token = generate_token()
        ok, msg = install_daemon(gw_port, gw_token)
        icon = f"[{_theme.success}]\u2713[/{_theme.success}]" if ok else f"[{_theme.error}]\u2717[/{_theme.error}]"
        console.print(f"  {icon} {msg}\n")
    elif gw_action == "uninstall":
        from core.daemon import uninstall_daemon
        ok, msg = uninstall_daemon()
        icon = f"[{_theme.success}]\u2713[/{_theme.success}]" if ok else f"[{_theme.error}]\u2717[/{_theme.error}]"
        console.print(f"  {icon} {msg}\n")
    else:
        show_gateway_status(console)


def _handle_save(console, chat_history):
    if chat_history:
        last_task = None
        for msg in reversed(chat_history):
            if msg.get("role") == "user":
                last_task = msg["content"]
                break
        if last_task:
            _save_template(last_task)
            console.print(f"  [{_theme.success}]✓[/{_theme.success}] Saved template: {last_task[:40]}…\n")
        else:
            console.print(f"  [{_theme.muted}]No task to save.[/{_theme.muted}]\n")
    else:
        console.print(f"  [{_theme.muted}]No task to save.[/{_theme.muted}]\n")


def _handle_templates(console) -> str | None:
    """Returns selected template text, or None to skip."""
    templates = _load_templates()
    if not templates:
        console.print(f"  [{_theme.muted}]No saved templates. Use /save after a task.[/{_theme.muted}]\n")
        return None
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
            return selected
        return None
    except ImportError:
        for i, t in enumerate(templates):
            console.print(f"  [{i}] {t[:60]}")
        console.print()
        return None


def _handle_export(console):
    if os.path.exists(".task_board.json"):
        data = json.load(open(".task_board.json"))
        if data:
            first_tid = next(iter(data))
            from cli.export_cmd import cmd_export
            cmd_export(first_tid, fmt="md", console=console)
        else:
            console.print(f"  [{_theme.muted}]No tasks to export.[/{_theme.muted}]\n")
    else:
        console.print(f"  [{_theme.muted}]No tasks to export.[/{_theme.muted}]\n")

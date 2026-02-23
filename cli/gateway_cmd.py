"""Gateway lifecycle management CLI commands."""
from __future__ import annotations

import os

from core.theme import theme as _theme


def cmd_gateway(action: str = "start", port: int = 0, token: str = "",
                force: bool = False):
    """Gateway lifecycle management (OpenClaw-style subcommands)."""
    from core.gateway import DEFAULT_PORT
    from core.i18n import t as _t

    port = port or int(os.environ.get("CLEO_GATEWAY_PORT",
                       os.environ.get("PORT", str(DEFAULT_PORT))))
    token = token or os.environ.get("CLEO_GATEWAY_TOKEN", "")

    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    if action in ("start", "run"):
        if force:
            from core.gateway import kill_port
            if console:
                console.print(f"  [{_theme.muted}]{_t('gw.killing_port', port=port)}[/{_theme.muted}]")
            kill_port(port)

        from core.gateway import run_gateway_cli
        run_gateway_cli(port=port, token=token)

    elif action == "status":
        show_gateway_status(console, port)

    elif action == "stop":
        from core.gateway import kill_port
        if console:
            console.print(f"  [{_theme.muted}]{_t('gw.stopping')}[/{_theme.muted}]")
        killed = kill_port(port)
        if killed:
            if console:
                console.print(f"  [{_theme.success}]\u2713[/{_theme.success}] Gateway stopped on port {port}")
            else:
                print(f"  Gateway stopped on port {port}")
        else:
            if console:
                console.print(f"  [{_theme.muted}]{_t('gw.not_running')}[/{_theme.muted}]")
            else:
                print(f"  {_t('gw.not_running')}")

    elif action == "restart":
        from core.gateway import kill_port
        if console:
            console.print(f"  [{_theme.muted}]{_t('gw.restarting')}[/{_theme.muted}]")
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
            icon = f"[{_theme.success}]\u2713[/{_theme.success}]" if ok else f"[{_theme.error}]\u2717[/{_theme.error}]"
            console.print(f"  {icon} {msg}")
            if ok:
                console.print(f"  [{_theme.muted}]Port: {port}  Token: {token}[/{_theme.muted}]")
        else:
            print(f"  {'OK' if ok else 'FAIL'}: {msg}")

    elif action == "uninstall":
        from core.daemon import uninstall_daemon
        ok, msg = uninstall_daemon()
        if console:
            icon = f"[{_theme.success}]\u2713[/{_theme.success}]" if ok else f"[{_theme.error}]\u2717[/{_theme.error}]"
            console.print(f"  {icon} {msg}")
        else:
            print(f"  {'OK' if ok else 'FAIL'}: {msg}")

    else:
        print(f"Unknown gateway action: {action}")
        print("Available: start, stop, restart, status, install, uninstall")


def show_gateway_status(console, port: int = 0):
    """Rich gateway status display — service state + RPC probe + agents."""
    from core.gateway import DEFAULT_PORT, probe_gateway
    from core.daemon import daemon_status
    from core.i18n import t as _t

    port = port or int(os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT)))
    probe = probe_gateway(port)
    daemon_ok, daemon_msg = daemon_status()

    if console:
        from rich.panel import Panel
        from rich.table import Table
        from rich import box

        tbl = Table(show_header=False, show_edge=False, box=None,
                     padding=(0, 1), expand=False)
        tbl.add_column("key", style=_theme.heading, min_width=16)
        tbl.add_column("val")

        if probe["reachable"]:
            tbl.add_row(
                "Status",
                f"[{_theme.success}]{_t('gw.running')}[/{_theme.success}]",
            )
            tbl.add_row(_t("gw.url"), f"http://127.0.0.1:{port}/")

            token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
            if token:
                masked = token[:10] + "..." + token[-4:] if len(token) > 16 else "***"
                tbl.add_row(_t("gw.token"), f"[{_theme.muted}]{masked}[/{_theme.muted}]")

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

            online = probe.get("agents_online", 0)
            total = probe.get("agents_total", 0)
            if total > 0:
                tbl.add_row(
                    "Agents",
                    f"[{_theme.success}]{online}[/{_theme.success}]/{total} online",
                )
            else:
                tbl.add_row("Agents", f"[{_theme.muted}]{_t('gw.no_agents')}[/{_theme.muted}]")

            task_count = probe.get("task_count", 0)
            active = probe.get("active_tasks", 0)
            if task_count > 0:
                tbl.add_row("Tasks", f"{active} active / {task_count} total")

            for ag in probe.get("agents", []):
                aid = ag.get("agent_id", "?")
                online_flag = f"[{_theme.success}]●[/{_theme.success}]" if ag.get("online") else f"[{_theme.muted}]○[/{_theme.muted}]"
                status = ag.get("status", "idle")
                task_id = ag.get("task_id", "")
                detail = f"{status}"
                if task_id:
                    detail += f" ({task_id[:8]})"
                tbl.add_row(f"  {online_flag} {aid}", f"[{_theme.muted}]{detail}[/{_theme.muted}]")

        else:
            tbl.add_row(
                "Status",
                f"[{_theme.error}]{_t('gw.stopped')}[/{_theme.error}]",
            )
            tbl.add_row("Port", str(port))
            error = probe.get("error", "")
            if error:
                tbl.add_row("Error", f"[{_theme.muted}]{error}[/{_theme.muted}]")

        tbl.add_row("", "")
        if daemon_ok:
            tbl.add_row("Service", f"[{_theme.success}]{_t('gw.daemon_running')}[/{_theme.success}]")
        elif "Not installed" in daemon_msg:
            tbl.add_row("Service", f"[{_theme.muted}]{_t('gw.daemon_not_inst')}[/{_theme.muted}]")
        else:
            tbl.add_row("Service", f"[{_theme.warning}]{daemon_msg}[/{_theme.warning}]")

        tbl.add_row("", "")
        tbl.add_row(
            _t("gw.endpoints"),
            f"[{_theme.muted}]POST /v1/task · GET /v1/status · GET /v1/events[/{_theme.muted}]",
        )

        console.print()
        console.print(Panel(
            tbl,
            title=f"[{_theme.accent}]{_t('gw.title')}[/{_theme.accent}]",
            border_style=_theme.accent_light,
            box=box.ROUNDED,
        ))
        console.print()
    else:
        if probe["reachable"]:
            print(f"\n  Gateway: RUNNING on http://127.0.0.1:{port}/")
            print(f"  Uptime: {probe.get('uptime_seconds', 0):.0f}s")
            print(f"  Agents: {probe.get('agents_online', 0)}/{probe.get('agents_total', 0)} online")
        else:
            print(f"\n  Gateway: STOPPED (port {port})")
            print(f"  Error: {probe.get('error', '?')}")
        print(f"  Daemon: {daemon_msg}")
        print()

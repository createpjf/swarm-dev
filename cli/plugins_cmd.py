"""Plugin management CLI commands (argparse entry point)."""
from __future__ import annotations

from core.theme import theme as _theme


def cmd_plugins(args):
    """Plugin management commands — dispatches to core.plugin_cli."""
    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    from core.plugin_cli import (
        cmd_plugins_list, cmd_plugins_install, cmd_plugins_remove,
        cmd_plugins_enable, cmd_plugins_disable, cmd_plugins_info,
        cmd_plugins_update, cmd_plugins_doctor,
    )

    if args.plugins_cmd == "list" or args.plugins_cmd is None:
        cmd_plugins_list(console)
    elif args.plugins_cmd == "install":
        cmd_plugins_install(args.source, console)
    elif args.plugins_cmd == "remove":
        cmd_plugins_remove(args.name, console)
    elif args.plugins_cmd == "enable":
        cmd_plugins_enable(args.name, console)
    elif args.plugins_cmd == "disable":
        cmd_plugins_disable(args.name, console)
    elif args.plugins_cmd == "info":
        cmd_plugins_info(args.name, console)
    elif args.plugins_cmd == "update":
        cmd_plugins_update(getattr(args, "name", ""), console)
    elif args.plugins_cmd == "doctor":
        cmd_plugins_doctor(console)

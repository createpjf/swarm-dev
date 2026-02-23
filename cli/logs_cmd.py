"""Log viewer CLI commands."""
from __future__ import annotations

from core.theme import theme as _theme


def cmd_logs(follow: bool = False, agent: str = "",
             level: str = "", since: str = "", lines: int = 50):
    """View aggregated agent logs."""
    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    from core.log_viewer import LogViewer
    viewer = LogViewer()

    if follow:
        if console:
            console.print(f"[{_theme.muted}]Following logs (Ctrl+C to stop)...[/{_theme.muted}]\n")
        else:
            print("Following logs (Ctrl+C to stop)...\n")
        try:
            for entry in viewer.follow(agent=agent, level=level):
                line = viewer.format_entry(entry, color=console is not None)
                if console:
                    console.print(line)
                else:
                    print(line)
        except KeyboardInterrupt:
            if console:
                console.print(f"\n[{_theme.muted}]Stopped.[/{_theme.muted}]")
            else:
                print("\nStopped.")
    else:
        entries = viewer.tail(n=lines, agent=agent,
                              level=level, since=since)
        if not entries:
            if console:
                console.print(f"  [{_theme.muted}]No log entries found.[/{_theme.muted}]")
            else:
                print("  No log entries found.")
            return
        for entry in entries:
            line = viewer.format_entry(entry, color=console is not None)
            if console:
                console.print(line)
            else:
                print(line)

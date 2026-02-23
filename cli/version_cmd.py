"""Version subcommand — extended version info beyond -V flag."""
from __future__ import annotations

import os
import subprocess
import sys

from core.theme import theme as _theme


def cmd_version(json_output: bool = False):
    """Show version, git hash, Python version, and key dependency versions."""
    from cli.helpers import get_version

    version = get_version()
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Git short hash
    git_hash = "unknown"
    git_branch = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            git_hash = result.stdout.strip()
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            git_branch = result.stdout.strip()
    except Exception:
        pass

    # Python version
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # Key dependency versions
    deps: dict[str, str] = {}
    for pkg in ("chromadb", "httpx", "rich", "litellm", "fastapi"):
        try:
            mod = __import__(pkg)
            deps[pkg] = getattr(mod, "__version__", "installed")
        except ImportError:
            deps[pkg] = "not installed"

    if json_output:
        import json
        info = {
            "version": version,
            "git_hash": git_hash,
            "git_branch": git_branch,
            "python": py_version,
            "dependencies": deps,
            "install_path": project_root,
        }
        print(json.dumps(info, indent=2))
        return

    # Rich formatted output
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()

        console.print(f"\n  [{_theme.heading}]Cleo Agent Stack[/{_theme.heading}]  v{version}")
        console.print(f"  [{_theme.muted}]Git:[/{_theme.muted}]     {git_branch}@{git_hash}")
        console.print(f"  [{_theme.muted}]Python:[/{_theme.muted}]  {py_version}")
        console.print(f"  [{_theme.muted}]Path:[/{_theme.muted}]    {project_root}")

        table = Table(show_header=True, header_style=_theme.heading,
                      box=None, padding=(0, 2))
        table.add_column("Package", style=_theme.muted)
        table.add_column("Version")
        for pkg, ver in deps.items():
            style = _theme.success if ver != "not installed" else _theme.error
            table.add_row(pkg, f"[{style}]{ver}[/{style}]")
        console.print()
        console.print(table)
        console.print()

    except ImportError:
        print(f"\n  Cleo Agent Stack v{version}")
        print(f"  Git:     {git_branch}@{git_hash}")
        print(f"  Python:  {py_version}")
        print(f"  Path:    {project_root}")
        print()
        for pkg, ver in deps.items():
            print(f"  {pkg}: {ver}")
        print()

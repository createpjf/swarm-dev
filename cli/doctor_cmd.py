"""Doctor and diagnostics CLI commands."""
from __future__ import annotations

import json
import os

from core.theme import theme as _theme
from cli.helpers import get_version


def cmd_doctor(console=None, repair: bool = False, deep: bool = False,
               json_output: bool = False):
    if json_output:
        console = None

    if console is None and not json_output:
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

    if json_output:
        checks = [{"ok": ok, "label": label, "detail": detail}
                  for ok, label, detail in results]
        all_ok = all(c["ok"] for c in checks)
        print(json.dumps({"ok": all_ok, "checks": checks}, indent=2))
        return

    if console is None:
        for ok, label, detail in results:
            icon = "✓" if ok else "✗"
            print(f"  {icon} {label:14} {detail}")


def cmd_doctor_export(console=None):
    """Export a pasteable diagnostic report."""
    import platform
    import yaml

    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            pass

    lines = []
    lines.append(f"Cleo v{get_version()} | {platform.system()} {platform.release()} | Python {platform.python_version()}")

    if os.path.exists("config/agents.yaml"):
        with open("config/agents.yaml") as f:
            cfg = yaml.safe_load(f) or {}
        provider = cfg.get("llm", {}).get("provider", "?")
        model = cfg.get("llm", {}).get("model", "?")
        agents = [a["id"] for a in cfg.get("agents", [])]
        lines.append(f"Provider: {provider} | Model: {model} | Agents: {','.join(agents)}")
    else:
        lines.append("Config: not found (run `cleo onboard`)")

    try:
        import httpx
        gw_port = int(os.environ.get("CLEO_GATEWAY_PORT", "19789"))
        resp = httpx.get(f"http://127.0.0.1:{gw_port}/health", timeout=2.0)
        gw_status = "✓" if resp.status_code == 200 else "✗"
    except Exception:
        gw_port = 19789
        gw_status = "✗"
    lines.append(f"Gateway: {gw_status} :{gw_port}")

    if os.path.exists(".task_board.json"):
        with open(".task_board.json") as f:
            data = json.load(f)
        total = len(data)
        active = sum(1 for t in data.values() if t.get("status") in ("pending", "claimed", "review"))
        done = sum(1 for t in data.values() if t.get("status") == "completed")
        failed = sum(1 for t in data.values() if t.get("status") == "failed")
        lines.append(f"Tasks: {total} total ({active} active, {done} done, {failed} failed)")

    for dirname in ["memory", "workspace", ".logs"]:
        if os.path.isdir(dirname):
            total_size = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, filenames in os.walk(dirname)
                for f in filenames
            )
            size_mb = total_size / (1024 * 1024)
            lines.append(f"{dirname}/: {size_mb:.1f}MB")

    report = "\n".join(lines)

    if console:
        from rich.panel import Panel
        from rich import box
        console.print(Panel(report, title=f"[{_theme.heading}]Cleo Diagnostic Report[/{_theme.heading}]",
                           border_style=_theme.muted, box=box.ROUNDED))
        console.print(f"[{_theme.muted}]Copy the above for bug reports.[/{_theme.muted}]\n")
    else:
        print(report)

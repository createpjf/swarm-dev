"""Usage and budget CLI commands."""
from __future__ import annotations

import json

from core.theme import theme as _theme


def cmd_usage(console=None):
    """Show token usage and cost statistics."""
    from core.usage_tracker import UsageTracker
    tracker = UsageTracker()
    summary = tracker.get_summary()
    agg = summary.get("aggregate", {})

    if not agg.get("total_calls"):
        if console:
            console.print(f"  [{_theme.muted}]No usage data yet.[/{_theme.muted}]\n")
        else:
            print("  No usage data yet.")
        return

    if console:
        from rich.table import Table
        console.print()

        total_calls  = agg.get("total_calls", 0)
        total_tokens = agg.get("total_tokens", 0)
        total_cost   = agg.get("total_cost_usd", 0)
        successes    = agg.get("success_count", 0)
        failures     = agg.get("failure_count", 0)
        retries      = agg.get("total_retries", 0)
        failovers    = agg.get("total_failovers", 0)

        console.print(f"  [{_theme.heading}]Total Calls:[/{_theme.heading}] {total_calls}  "
                       f"([{_theme.success}]{successes} ok[/{_theme.success}]"
                       f"{f', [{_theme.error}]{failures} fail[/{_theme.error}]' if failures else ''})")
        console.print(f"  [{_theme.heading}]Total Tokens:[/{_theme.heading}] {total_tokens:,}  "
                       f"[{_theme.heading}]Est. Cost:[/{_theme.heading}] ${total_cost:.4f}")
        if retries or failovers:
            console.print(f"  [{_theme.muted}]Retries: {retries}  Failovers: {failovers}[/{_theme.muted}]")

        by_agent = summary.get("by_agent", {})
        if by_agent:
            console.print()
            tbl = Table(box=None, padding=(0, 1), show_header=True)
            tbl.add_column("Agent", style=_theme.heading)
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

        by_model = summary.get("by_model", {})
        if by_model:
            console.print()
            tbl2 = Table(box=None, padding=(0, 1), show_header=True)
            tbl2.add_column("Model", style=_theme.heading)
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
        print(f"\nUsage: {agg.get('total_calls', 0)} calls, "
              f"{agg.get('total_tokens', 0):,} tokens, "
              f"${agg.get('total_cost_usd', 0):.4f}")


def cmd_budget(console=None):
    """Show and manage budget limits."""
    from core.usage_tracker import UsageTracker
    budget = UsageTracker.get_budget()

    if console:
        console.print()
        enabled = budget.get("enabled", False)
        if not enabled:
            console.print(f"  [{_theme.muted}]Budget: not configured[/{_theme.muted}]")
            console.print(f"  [{_theme.muted}]Set via: POST /v1/budget or config/budget.json[/{_theme.muted}]\n")
        else:
            max_cost = budget.get("max_cost_usd", 0)
            current = budget.get("current_cost_usd", 0)
            pct = budget.get("percent_used", 0)
            tokens = budget.get("current_tokens", 0)

            if pct >= 90:
                style = _theme.error
            elif pct >= 70:
                style = _theme.warning
            else:
                style = _theme.success

            console.print(f"  [{_theme.heading}]Budget:[/{_theme.heading}]  ${max_cost:.2f}")
            console.print(f"  [{_theme.heading}]Spent:[/{_theme.heading}]   [{style}]${current:.4f}  ({pct:.0f}%)[/{style}]")
            console.print(f"  [{_theme.heading}]Tokens:[/{_theme.heading}]  {tokens:,}")
            max_tokens = budget.get("max_tokens", 0)
            if max_tokens:
                console.print(f"  [{_theme.heading}]Token Limit:[/{_theme.heading}] {max_tokens:,}")
            console.print()
    else:
        print(json.dumps(budget, indent=2))

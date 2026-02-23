"""On-chain identity management CLI commands."""
from __future__ import annotations

import json
import os

from core.theme import theme as _theme


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
        print("No config found. Run `cleo configure` first.")
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
            console.print(f"\n  [{_theme.heading}]Chain Status[/{_theme.heading}]")
            console.print(f"  Network: {status['network']}  |  Lit: {status['lit_network']}")
            console.print(f"  x402: {'enabled' if status['x402_enabled'] else 'disabled'}")
            console.print()

            from rich.table import Table
            tbl = Table(box=None, padding=(0, 1), show_header=True)
            tbl.add_column("Agent", style=_theme.heading)
            tbl.add_column("Registered")
            tbl.add_column("PKP Address", style=_theme.muted)
            tbl.add_column("Chain ID")
            tbl.add_column("USDC")

            for aid, info in status.get("agents", {}).items():
                reg = f"[{_theme.success}]\u2713[/{_theme.success}]" if info.get("registered") else f"[{_theme.error}]\u2717[/{_theme.error}]"
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
            print("Usage: cleo chain init <agent_id>")
            return
        agent_cfg = None
        for a in config.get("agents", []):
            if a["id"] == agent_id:
                agent_cfg = a
                break
        if console:
            console.print(f"\n  [{_theme.heading}]Initializing {agent_id} on-chain...[/{_theme.heading}]")
        result = mgr.initialize_agent(agent_id, agent_cfg)
        if console:
            console.print(f"  [{_theme.success}]\u2713[/{_theme.success}] PKP: {result.get('pkp_eth_address', '?')}")
            console.print(f"  [{_theme.success}]\u2713[/{_theme.success}] Registered: {result.get('registered', False)}")
            console.print()
        else:
            print(json.dumps(result, indent=2, default=str))

    elif action == "register":
        if not agent_id:
            print("Usage: cleo chain register <agent_id>")
            return
        tx_hash = mgr.register_agent(agent_id, {})
        print(f"  {agent_id}: tx={tx_hash}")

    elif action == "health":
        health = mgr.health_check()
        if console:
            console.print(f"\n  [{_theme.heading}]Chain Health[/{_theme.heading}]")
            for key, val in health.items():
                if isinstance(val, dict):
                    st = val.get("status", "?")
                    style = _theme.success if st == "ok" else _theme.error
                    console.print(f"  {key}: [{style}]{st}[/{style}]")
                else:
                    console.print(f"  {key}: {val}")
            console.print()
        else:
            print(json.dumps(health, indent=2, default=str))

    else:
        print(f"Unknown chain action: {action}")
        print("Available: status, balance, init, register, health")

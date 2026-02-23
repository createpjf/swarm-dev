"""Channel management CLI commands."""
from __future__ import annotations

import json
import os

from core.theme import theme as _theme


def cmd_channels(action: str = "list", channel: str = None,
                 json_output: bool = False):
    """Channel management CLI."""
    import yaml

    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        console = Console()
    except ImportError:
        console = None

    config_path = "config/agents.yaml"

    if action in ("list", "status"):
        from core.gateway import DEFAULT_PORT, probe_gateway
        port = int(os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT)))
        probe = probe_gateway(port)

        channels_data = []
        if probe.get("reachable"):
            try:
                import httpx
                token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp = httpx.get(f"http://127.0.0.1:{port}/v1/channels",
                                 headers=headers, timeout=5)
                data = resp.json()
                channels_data = data.get("channels", [])
            except Exception:
                pass

        if not channels_data:
            try:
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
                ch_cfg = cfg.get("channels", {})
                for name, c in ch_cfg.items():
                    channels_data.append({
                        "channel": name,
                        "enabled": c.get("enabled", False),
                        "running": False,
                        "token_configured": False,
                    })
            except Exception:
                pass

        if json_output:
            print(json.dumps(channels_data, indent=2, default=str))
            return

        if console:
            console.print()
            tbl = Table(title="Channel Status", box=box.ROUNDED,
                         show_header=True, padding=(0, 1))
            tbl.add_column("Channel", style=_theme.heading, min_width=10)
            tbl.add_column("Enabled", justify="center", min_width=8)
            tbl.add_column("Running", justify="center", min_width=8)
            tbl.add_column("Token", min_width=10)

            for ch in channels_data:
                enabled = f"[{_theme.success}]Yes[/{_theme.success}]" if ch.get("enabled") else f"[{_theme.muted}]No[/{_theme.muted}]"
                running = f"[{_theme.success}]Yes[/{_theme.success}]" if ch.get("running") else f"[{_theme.muted}]No[/{_theme.muted}]"
                token_ok = ch.get("token_configured", False)
                token_str = f"[{_theme.success}]Set[/{_theme.success}]" if token_ok else f"[{_theme.error}]Not Set[/{_theme.error}]"
                tbl.add_row(ch["channel"], enabled, running, token_str)

            console.print(tbl)
            console.print()
        else:
            print(f"\n{'Channel':12} {'Enabled':9} {'Running':9} Token")
            print("-" * 45)
            for ch in channels_data:
                e = "Yes" if ch.get("enabled") else "No"
                r = "Yes" if ch.get("running") else "No"
                t = "Set" if ch.get("token_configured") else "Not Set"
                print(f"{ch['channel']:12} {e:9} {r:9} {t}")
            print()

    elif action == "enable":
        if not channel:
            print("Error: specify a channel name. e.g. cleo channels enable slack")
            return
        _update_channel_config(config_path, channel, enabled=True)

    elif action == "disable":
        if not channel:
            print("Error: specify a channel name. e.g. cleo channels disable slack")
            return
        _update_channel_config(config_path, channel, enabled=False)

    elif action == "test":
        if not channel:
            print("Error: specify a channel name. e.g. cleo channels test slack")
            return
        print(f"Testing {channel} connection...")
        try:
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
            ch_cfg = cfg.get("channels", {}).get(channel, {})
            if not ch_cfg:
                print(f"Error: channel '{channel}' not found in config")
                return

            token_env_map = {
                "telegram": ["TELEGRAM_BOT_TOKEN"],
                "discord": ["DISCORD_BOT_TOKEN"],
                "feishu": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
                "slack": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
            }
            env_keys = token_env_map.get(channel, [])
            missing = [k for k in env_keys if not os.environ.get(k)]
            if missing:
                print(f"Missing env vars: {', '.join(missing)}")
                print("Set them in .env file first.")
            else:
                print(f"All required tokens for {channel} are set.")
                if not ch_cfg.get("enabled"):
                    print(f"Note: {channel} is disabled. "
                          f"Run 'cleo channels enable {channel}' to activate.")
        except Exception as e:
            print(f"Error: {e}")

    else:
        print(f"Unknown channels action: {action}")


def cmd_channels_pairing(action: str = "list", code_or_id: str = "",
                          json_output: bool = False):
    """Manage pairing: list trusted, generate codes, approve/revoke users."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        console = Console()
    except ImportError:
        console = None

    def _print(msg):
        if console:
            console.print(msg)
        else:
            print(msg)

    from core.user_auth import get_user_auth
    auth = get_user_auth("pairing")

    if action == "list":
        trusted = auth.list_trusted()
        if json_output:
            print(json.dumps(trusted, indent=2, default=str))
            return
        if not trusted:
            _print(f"  [{_theme.muted}]No paired users.[/{_theme.muted}]")
            _print(f"  [{_theme.muted}]Generate a code: cleo channels pairing generate[/{_theme.muted}]")
            return
        if console:
            tbl = Table(title="Paired Users", box=box.ROUNDED,
                        show_header=True, padding=(0, 1))
            tbl.add_column("Channel", style=_theme.heading)
            tbl.add_column("User ID")
            tbl.add_column("Name")
            tbl.add_column("Reason", style=_theme.muted)
            for u in trusted:
                tbl.add_row(
                    u.get("channel", "?"),
                    u.get("user_id", "?"),
                    u.get("user_name", ""),
                    u.get("reason", ""),
                )
            console.print(tbl)
        else:
            for u in trusted:
                print(f"  {u.get('channel', '?')}:{u.get('user_id', '?')} "
                      f"({u.get('user_name', '')})")

    elif action == "generate":
        label = code_or_id or ""
        code = auth.generate_pairing_code(label=label)
        _print(f"\n  [{_theme.success}]Pairing code:[/{_theme.success}] [{_theme.heading}]{code}[/{_theme.heading}]")
        _print(f"  [{_theme.muted}]Share this code with the user.[/{_theme.muted}]")
        _print(f"  [{_theme.muted}]Expires in 10 minutes.[/{_theme.muted}]\n")

    elif action == "approve":
        if not code_or_id:
            _print(f"  [{_theme.error}]Usage: cleo channels pairing approve <channel:user_id>[/{_theme.error}]")
            return
        parts = code_or_id.split(":", 1)
        if len(parts) != 2:
            _print(f"  [{_theme.error}]Format: channel:user_id (e.g. telegram:123456)[/{_theme.error}]")
            return
        channel, user_id = parts
        auth.trust_user(channel, user_id, reason="manual CLI approval")
        _print(f"  [{_theme.success}]✓[/{_theme.success}] Approved {channel}:{user_id}")

    elif action == "revoke":
        if not code_or_id:
            _print(f"  [{_theme.error}]Usage: cleo channels pairing revoke <channel:user_id>[/{_theme.error}]")
            return
        parts = code_or_id.split(":", 1)
        if len(parts) != 2:
            _print(f"  [{_theme.error}]Format: channel:user_id (e.g. telegram:123456)[/{_theme.error}]")
            return
        channel, user_id = parts
        ok = auth.revoke_user(channel, user_id)
        if ok:
            _print(f"  [{_theme.success}]✓[/{_theme.success}] Revoked {channel}:{user_id}")
        else:
            _print(f"  [{_theme.warning}]User not found in trusted list.[/{_theme.warning}]")

    else:
        _print(f"  [{_theme.muted}]Usage: cleo channels pairing <list|generate|approve|revoke> [id][/{_theme.muted}]")


def _update_channel_config(config_path: str, channel: str, enabled: bool):
    """Update channel enabled status in agents.yaml."""
    import yaml

    try:
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error loading config: {e}")
        return

    channels = cfg.get("channels", {})
    if channel not in channels:
        print(f"Error: channel '{channel}' not found in config")
        print(f"Available: {', '.join(channels.keys())}")
        return

    channels[channel]["enabled"] = enabled

    try:
        from core.config_manager import safe_write_yaml
        safe_write_yaml(config_path, cfg,
                        f"{'enable' if enabled else 'disable'} channel {channel}")
    except Exception as e:
        print(f"Error saving config: {e}")
        return

    status = "enabled" if enabled else "disabled"
    print(f"Channel '{channel}' {status}.")
    if enabled:
        print("Restart gateway to apply: cleo gateway restart")

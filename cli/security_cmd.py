"""Security audit command — checks for common security misconfigurations."""
from __future__ import annotations

import os
import stat
import subprocess

from rich.console import Console

console = Console()
C_OK = "green"
C_WARN = "yellow"
C_ERR = "red"
C_DIM = "dim"


def cmd_security_audit(deep: bool = False, fix: bool = False):
    """Run security audit checks."""
    console.print(f"\n  [bold]Security Audit[/bold]")
    console.print(f"  {'─' * 40}\n")

    issues: list[tuple[str, str, str]] = []  # (level, label, detail)

    # ── 1. .env file permissions ──
    env_path = ".env"
    if os.path.exists(env_path):
        mode = os.stat(env_path).st_mode
        world_read = mode & stat.S_IROTH
        if world_read:
            issues.append(("error", ".env file is world-readable",
                           f"chmod 600 {env_path}"))
            if fix:
                os.chmod(env_path, 0o600)
                _ok(".env permissions fixed (600)")
            else:
                _err(".env file is world-readable — run with --fix")
        else:
            _ok(".env permissions OK")
    else:
        _warn(".env file not found")

    # ── 2. Config does not contain raw API keys ──
    cfg_path = "config/agents.yaml"
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            content = f.read()
        key_patterns = ["sk-", "key-", "token-"]
        has_key = any(p in content.lower() for p in key_patterns)
        # More specific: check for long alphanumeric strings after key fields
        import re
        explicit_keys = re.findall(
            r'(?:api_key|token|secret)\s*:\s*["\']?([A-Za-z0-9_-]{20,})',
            content)
        if explicit_keys:
            _err(f"Config contains {len(explicit_keys)} potential API key(s) — "
                 f"move to .env and use ${{ENV}} syntax")
            issues.append(("error", "API keys in config", ""))
        else:
            _ok("No API keys detected in config")
    else:
        _warn("Config file not found")

    # ── 3. Gateway token ──
    token_path = ".gateway_token"
    gateway_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
    if not gateway_token and os.path.exists(token_path):
        with open(token_path) as f:
            gateway_token = f.read().strip()
    if gateway_token:
        _ok("Gateway token is set")
        # Check token file permissions
        if os.path.exists(token_path):
            mode = os.stat(token_path).st_mode
            if mode & stat.S_IROTH:
                _err(".gateway_token is world-readable")
                if fix:
                    os.chmod(token_path, 0o600)
                    _ok(".gateway_token permissions fixed (600)")
            else:
                _ok(".gateway_token permissions OK")
    else:
        _warn("No gateway token set — dashboard is unprotected")

    # ── 4. Channel pairing mode ──
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            content = f.read()
        if "auth_mode: open" in content:
            _warn("Channel auth_mode is 'open' — anyone can message the bot")
            issues.append(("warn", "Open channel auth", ""))
        elif "auth_mode: pairing" in content:
            _ok("Channel auth uses pairing mode")
        elif "auth_mode:" in content:
            _ok("Channel auth mode configured")

    # ── 5. .gitignore coverage ──
    gitignore_path = ".gitignore"
    sensitive_patterns = [".env", ".gateway_token", ".file_delivery/",
                          ".channel_session.json", "memory/"]
    if os.path.exists(gitignore_path):
        with open(gitignore_path) as f:
            gitignore = f.read()
        missing = [p for p in sensitive_patterns if p not in gitignore]
        if missing:
            _warn(f".gitignore missing: {', '.join(missing)}")
            if fix:
                with open(gitignore_path, "a") as f:
                    f.write("\n# Security — auto-added by cleo security audit\n")
                    for p in missing:
                        f.write(f"{p}\n")
                _ok(f"Added {len(missing)} entries to .gitignore")
        else:
            _ok(".gitignore covers sensitive files")
    else:
        _warn("No .gitignore found")
        if fix:
            with open(gitignore_path, "w") as f:
                f.write("# Cleo security defaults\n")
                for p in sensitive_patterns:
                    f.write(f"{p}\n")
            _ok("Created .gitignore with security defaults")

    # ── 6. Deep checks ──
    if deep:
        console.print(f"\n  [bold]Deep Audit[/bold]\n")

        # Check config/ directory permissions
        if os.path.isdir("config"):
            mode = os.stat("config").st_mode
            if mode & stat.S_IROTH:
                _err("config/ directory is world-readable")
                if fix:
                    os.chmod("config", 0o700)
                    _ok("config/ permissions fixed (700)")
            else:
                _ok("config/ directory permissions OK")

        # Check memory/ directory permissions
        if os.path.isdir("memory"):
            mode = os.stat("memory").st_mode
            if mode & stat.S_IROTH:
                _err("memory/ directory is world-readable")
                if fix:
                    os.chmod("memory", 0o700)
                    _ok("memory/ permissions fixed (700)")
            else:
                _ok("memory/ directory permissions OK")

        # Check git history for leaked keys
        try:
            result = subprocess.run(
                ["git", "log", "--all", "--oneline", "-20",
                 "--diff-filter=A", "--", ".env", ".gateway_token"],
                capture_output=True, text=True, timeout=10,
            )
            if result.stdout.strip():
                _warn("Sensitive files found in git history — "
                      "consider git filter-branch or BFG")
            else:
                _ok("No sensitive files in recent git history")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            _ok("Git history check skipped (not a git repo)")

    # ── Summary ──
    error_count = sum(1 for l, _, _ in issues if l == "error")
    warn_count = sum(1 for l, _, _ in issues if l == "warn")
    console.print()
    if error_count:
        console.print(f"  [{C_ERR}]{error_count} error(s)[/{C_ERR}], "
                      f"{warn_count} warning(s)")
        if not fix:
            console.print(f"  [{C_DIM}]Run `cleo security audit --fix` to auto-fix[/{C_DIM}]")
    elif warn_count:
        console.print(f"  [{C_WARN}]{warn_count} warning(s), no errors[/{C_WARN}]")
    else:
        console.print(f"  [{C_OK}]All checks passed[/{C_OK}]")
    console.print()


def _ok(msg: str):
    console.print(f"  [{C_OK}]✓[/{C_OK}] {msg}")


def _warn(msg: str):
    console.print(f"  [{C_WARN}]![/{C_WARN}] {msg}")


def _err(msg: str):
    console.print(f"  [{C_ERR}]✗[/{C_ERR}] {msg}")

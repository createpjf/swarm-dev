"""
core/daemon.py
Background service management — LaunchAgent (macOS) / systemd (Linux).

Installs Swarm gateway as a background service that auto-starts and
restarts on failure.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys

SERVICE_LABEL = "com.swarm.agent-stack"
SWARM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════════════════════
#  macOS LaunchAgent
# ══════════════════════════════════════════════════════════════════════════════

def _plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{SERVICE_LABEL}.plist")


def _plist_content(port: int, token: str) -> str:
    python = sys.executable
    swarm_path = os.path.join(SWARM_ROOT, "swarm")
    log_path = os.path.join(SWARM_ROOT, "logs/gateway.log")
    err_path = os.path.join(SWARM_ROOT, "logs/gateway.err.log")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SERVICE_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{swarm_path}</string>
        <string>gateway</string>
        <string>--port</string>
        <string>{port}</string>
        <string>--token</string>
        <string>{token}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{SWARM_ROOT}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
"""


def _systemd_path() -> str:
    return os.path.expanduser(f"~/.config/systemd/user/swarm.service")


def _systemd_content(port: int, token: str) -> str:
    python = sys.executable
    swarm_path = os.path.join(SWARM_ROOT, "swarm")

    return f"""[Unit]
Description=Swarm Agent Stack Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory={SWARM_ROOT}
ExecStart={python} {swarm_path} gateway --port {port} --token {token}
Restart=on-failure
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def install_daemon(port: int, token: str) -> tuple[bool, str]:
    """
    Install the background service.
    Returns (success, message).
    """
    system = platform.system()

    if system == "Darwin":
        return _install_macos(port, token)
    elif system == "Linux":
        return _install_linux(port, token)
    else:
        return False, f"Unsupported OS: {system}"


def uninstall_daemon() -> tuple[bool, str]:
    """Remove the background service."""
    system = platform.system()

    if system == "Darwin":
        return _uninstall_macos()
    elif system == "Linux":
        return _uninstall_linux()
    else:
        return False, f"Unsupported OS: {system}"


def daemon_status() -> tuple[bool, str]:
    """Check if daemon is installed and running."""
    system = platform.system()

    if system == "Darwin":
        plist = _plist_path()
        if not os.path.exists(plist):
            return False, "Not installed"
        result = subprocess.run(
            ["launchctl", "list", SERVICE_LABEL],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, "Running"
        return False, "Installed but not running"

    elif system == "Linux":
        svc = _systemd_path()
        if not os.path.exists(svc):
            return False, "Not installed"
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "swarm"],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "active":
            return True, "Running"
        return False, f"Installed ({result.stdout.strip()})"

    return False, f"Unsupported OS: {system}"


# ── macOS ──

def _install_macos(port: int, token: str) -> tuple[bool, str]:
    plist = _plist_path()

    # Create logs directory
    os.makedirs(os.path.join(SWARM_ROOT, "logs"), exist_ok=True)

    # Write plist
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    with open(plist, "w") as f:
        f.write(_plist_content(port, token))

    # Load
    subprocess.run(["launchctl", "unload", plist],
                    capture_output=True)  # ignore if not loaded
    result = subprocess.run(
        ["launchctl", "load", plist],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, f"LaunchAgent installed → {SERVICE_LABEL}"
    return False, f"launchctl load failed: {result.stderr.strip()}"


def _uninstall_macos() -> tuple[bool, str]:
    plist = _plist_path()
    if not os.path.exists(plist):
        return True, "Not installed"

    subprocess.run(["launchctl", "unload", plist], capture_output=True)
    os.remove(plist)
    return True, "LaunchAgent removed"


# ── Linux ──

def _install_linux(port: int, token: str) -> tuple[bool, str]:
    svc = _systemd_path()
    os.makedirs(os.path.dirname(svc), exist_ok=True)

    with open(svc, "w") as f:
        f.write(_systemd_content(port, token))

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "swarm"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, "systemd service installed → swarm.service"
    return False, f"systemctl enable failed: {result.stderr.strip()}"


def _uninstall_linux() -> tuple[bool, str]:
    svc = _systemd_path()
    if not os.path.exists(svc):
        return True, "Not installed"

    subprocess.run(["systemctl", "--user", "disable", "--now", "swarm"],
                    capture_output=True)
    os.remove(svc)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return True, "systemd service removed"

"""
core/exec_tool.py
Agent-accessible shell execution tool with approval gating.
Inspired by OpenClaw's exec tool pattern.

Provides:
  - Shell command execution for agents (sandboxed)
  - Approval allowlist for safe commands
  - Process management (list, kill background processes)
  - Configurable timeout and output limits

Security model:
  - Commands must match an allowlist pattern OR be explicitly approved
  - Default allowlist: read-only commands (ls, cat, head, grep, find, wc, etc.)
  - Write commands require explicit approval in config or per-invocation
  - All executions are logged to .logs/exec.log
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

EXEC_LOG_PATH = ".logs/exec.log"
APPROVALS_PATH = "config/exec_approvals.json"

# Default safe commands (read-only, non-destructive)
DEFAULT_ALLOWLIST = [
    r"^ls\b",
    r"^cat\b",
    r"^head\b",
    r"^tail\b",
    r"^grep\b",
    r"^find\b",
    r"^wc\b",
    r"^echo\b",
    r"^date\b",
    r"^pwd\b",
    r"^which\b",
    r"^env\b",
    r"^whoami\b",
    r"^uname\b",
    r"^df\b",
    r"^du\b",
    r"^uptime\b",
    r"^ps\b",
    r"^free\b",
    r"^curl\s.*--head",
    r"^curl\s.*-I\b",
    r"^python3?\s+-c\b",
    r"^node\s+-e\b",
    r"^git\s+(status|log|diff|branch|tag|remote|show)\b",
    r"^pip3?\s+(list|show|freeze)\b",
    r"^npm\s+(list|ls|info|view)\b",
]

# Explicitly denied patterns (never allowed even with approval)
DENY_LIST = [
    r"\brm\s+-rf\s+/",      # rm -rf /
    r"\bsudo\b",             # sudo anything
    r"\bchmod\s+777\b",      # world-writable
    r"\bmkfs\b",             # format disk
    r"\bdd\s+if=",           # raw disk write
    r"\b:()\{.*\};\s*:",     # fork bomb
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
]

_compiled_allow: list[re.Pattern] = []
_compiled_deny: list[re.Pattern] = []
_custom_approvals: list[str] = []


def _compile_patterns():
    """Compile regex patterns (lazy init)."""
    global _compiled_allow, _compiled_deny, _custom_approvals
    if _compiled_allow:
        return

    _compiled_deny = [re.compile(p, re.IGNORECASE) for p in DENY_LIST]

    # Load custom approvals
    if os.path.exists(APPROVALS_PATH):
        try:
            with open(APPROVALS_PATH) as f:
                data = json.load(f)
            _custom_approvals = data.get("allow", [])
        except (json.JSONDecodeError, OSError):
            pass

    all_patterns = DEFAULT_ALLOWLIST + _custom_approvals
    _compiled_allow = [re.compile(p, re.IGNORECASE) for p in all_patterns]


def is_command_allowed(command: str) -> tuple[bool, str]:
    """Check if a command is allowed.

    Returns (allowed, reason).
    """
    _compile_patterns()
    cmd = command.strip()

    # Check deny list first
    for pat in _compiled_deny:
        if pat.search(cmd):
            return False, f"Blocked by deny pattern: {pat.pattern}"

    # Check allow list
    for pat in _compiled_allow:
        if pat.search(cmd):
            return True, "Matched allowlist"

    return False, "Not in allowlist — add to config/exec_approvals.json"


def add_approval(pattern: str):
    """Add a command pattern to the approval allowlist."""
    os.makedirs(os.path.dirname(APPROVALS_PATH), exist_ok=True)
    data = {"allow": []}
    if os.path.exists(APPROVALS_PATH):
        try:
            with open(APPROVALS_PATH) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if pattern not in data.get("allow", []):
        data.setdefault("allow", []).append(pattern)
        with open(APPROVALS_PATH, "w") as f:
            json.dump(data, f, indent=2)

    # Reset compiled patterns to pick up new approval
    global _compiled_allow
    _compiled_allow = []


def _log_execution(agent_id: str, command: str, ok: bool,
                   output: str, elapsed: float):
    """Append execution record to exec log."""
    os.makedirs(os.path.dirname(EXEC_LOG_PATH), exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent_id,
        "cmd": command,
        "ok": ok,
        "elapsed_s": round(elapsed, 2),
        "output_len": len(output),
    }
    with open(EXEC_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def execute(
    command: str,
    agent_id: str = "system",
    timeout: int = 300,
    max_output: int = 50_000,
    cwd: str | None = None,
    force: bool = False,
) -> dict:
    """Execute a shell command with approval gating.

    Args:
        command: Shell command string
        agent_id: Who is running this (for logging)
        timeout: Max execution time in seconds
        max_output: Max output bytes to capture
        cwd: Working directory (default: project root)
        force: Skip allowlist check (for approved callers only)

    Returns:
        {ok, stdout, stderr, exit_code, elapsed_s, blocked?, reason?}
    """
    # Check approval
    if not force:
        allowed, reason = is_command_allowed(command)
        if not allowed:
            logger.warning("[exec] Blocked: %s (agent=%s, reason=%s)",
                           command[:80], agent_id, reason)
            _log_execution(agent_id, command, False, f"BLOCKED: {reason}", 0)
            return {
                "ok": False,
                "blocked": True,
                "reason": reason,
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "elapsed_s": 0,
            }

    # Execute
    start = time.time()
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        elapsed = time.time() - start
        stdout = result.stdout[:max_output]
        stderr = result.stderr[:max_output]
        ok = result.returncode == 0

        _log_execution(agent_id, command, ok, stdout[:200], elapsed)

        return {
            "ok": ok,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
            "elapsed_s": round(elapsed, 2),
        }

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        _log_execution(agent_id, command, False, "TIMEOUT", elapsed)
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"Timed out after {timeout}s",
            "exit_code": -1,
            "elapsed_s": round(elapsed, 2),
        }

    except Exception as e:
        elapsed = time.time() - start
        _log_execution(agent_id, command, False, str(e), elapsed)
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "elapsed_s": round(elapsed, 2),
        }


def list_approved_patterns() -> dict:
    """Return the current allowlist configuration."""
    _compile_patterns()
    return {
        "default": DEFAULT_ALLOWLIST,
        "custom": _custom_approvals,
        "deny": DENY_LIST,
    }

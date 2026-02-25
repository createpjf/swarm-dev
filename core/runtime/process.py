"""
core/runtime/process.py — ProcessRuntime: one OS process per agent.

This is the Phase 1 runtime — it wraps the EXACT same ``mp.Process``
logic that was previously inline in ``Orchestrator._launch_all()``.
Zero behaviour change from pre-Runtime code.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import signal
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

from core.protocols import FileLock  # shared fallback


def _build_agent_cfg_dict(agent_def: dict, config: dict) -> dict:
    """Build the flat config dict passed to ``_agent_process``.

    Extracted from the former ``Orchestrator._launch_all()`` body so
    it can be reused across runtimes (Process / InProcess / Lazy).
    """
    compact_cfg = config.get("compaction", {})
    return {
        "agent_id":             agent_def["id"],
        "role":                 agent_def["role"],
        "model":                agent_def["model"],
        "skills":               agent_def.get("skills", ["_base"]),
        "wallet_key":           os.getenv(
                                    agent_def.get("wallet", ""), ""),
        "short_term_turns":     agent_def.get("memory", {})
                                    .get("short_term_turns", 20),
        "long_term":            agent_def.get("memory", {})
                                    .get("long_term", True),
        "recall_top_k":         agent_def.get("memory", {})
                                    .get("recall_top_k", 3),
        "autonomy_level":       agent_def.get("autonomy_level", 1),
        # Compaction config
        "compaction_enabled":   compact_cfg.get("enabled", True),
        "max_context_tokens":   compact_cfg.get("max_context_tokens", 8000),
        "summary_target_tokens": compact_cfg.get("summary_target_tokens", 1500),
        "keep_recent_turns":    compact_cfg.get("keep_recent_turns", 4),
        # Episodic + KB memory config
        "episodic_recall_budget": agent_def.get("memory", {})
                                     .get("episodic_recall_budget", 1500),
        "kb_recall_budget":       agent_def.get("memory", {})
                                     .get("kb_recall_budget", 800),
        # Tool configuration (OpenClaw-inspired)
        "tools_config":         agent_def.get("tools", {}),
    }


class ProcessRuntime:
    """One mp.Process per agent — identical to pre-Runtime behaviour.

    Inherits default implementations (all_alive, start_all, ensure_running)
    from AgentRuntime via register() and provides concrete implementations
    of all abstract methods.
    """

    def __init__(self):
        self._procs: dict[str, mp.Process] = {}  # agent_id → Process

    # ── AgentRuntime interface ───────────────────────────────────────────

    def start(self, agent_def: dict, config: dict,
              wakeup: Any = None) -> None:
        from core.orchestrator import _agent_process

        agent_id = agent_def["id"]
        cfg_dict = _build_agent_cfg_dict(agent_def, config)

        p = mp.Process(
            target=_agent_process,
            args=(cfg_dict, agent_def, config, wakeup),
            name=agent_id,
            daemon=False,
        )
        p.start()
        self._procs[agent_id] = p
        logger.info("[runtime:process] launched '%s' (pid=%d)",
                    agent_id, p.pid)

    def is_alive(self, agent_id: str) -> bool:
        p = self._procs.get(agent_id)
        return p is not None and p.is_alive()

    def agent_ids(self) -> list[str]:
        return list(self._procs.keys())

    def start_all(self, config: dict, wakeup=None) -> None:
        """Launch every agent defined in config."""
        for agent_def in config.get("agents", []):
            self.start(agent_def, config, wakeup)

    def all_alive(self) -> dict[str, bool]:
        """Return {agent_id: is_alive} for every managed agent."""
        return {aid: self.is_alive(aid) for aid in self.agent_ids()}

    def stop(self, agent_id: str) -> None:
        """Graceful stop: mailbox shutdown → SIGTERM → join."""
        p = self._procs.get(agent_id)
        if p is None:
            return

        # Send shutdown message via mailbox (Agent Teams pattern)
        self._send_shutdown_mail(agent_id)

        # Give 5s for clean exit
        deadline = time.time() + 5
        while time.time() < deadline and p.is_alive():
            time.sleep(0.5)

        # Force SIGTERM
        if p.is_alive():
            try:
                os.kill(p.pid, signal.SIGTERM)
            except OSError:
                pass

        p.join(timeout=3)

    def stop_all(self) -> None:
        """Shut down all agents — mirrors old Orchestrator.shutdown()."""
        # Phase 1: mailbox shutdown messages
        for agent_id, p in self._procs.items():
            if p.is_alive():
                self._send_shutdown_mail(agent_id)

        # Phase 2: wait 5s for clean exit
        deadline = time.time() + 5
        while time.time() < deadline:
            if not any(p.is_alive() for p in self._procs.values()):
                break
            time.sleep(0.5)

        # Phase 3: SIGTERM remaining
        for p in self._procs.values():
            if p.is_alive():
                try:
                    os.kill(p.pid, signal.SIGTERM)
                except OSError:
                    pass

        # Phase 4: final join
        for p in self._procs.values():
            p.join(timeout=3)

        logger.info("[runtime:process] all agents stopped")

    def ensure_running(self, agent_id: str, config: dict = None,
                       wakeup: Any = None) -> None:
        """ProcessRuntime: agents are always-on; raise if dead."""
        if not self.is_alive(agent_id):
            raise RuntimeError(
                f"Agent '{agent_id}' is not running. "
                f"ProcessRuntime does not support on-demand restart."
            )

    # ── housekeeping ─────────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all agent tracking for pool restart."""
        self._procs.clear()

    def prune_dead(self) -> None:
        """Remove dead processes from internal tracking."""
        self._procs = {aid: p for aid, p in self._procs.items()
                       if p.is_alive()}

    # ── backward-compat ─────────────────────────────────────────────────

    @property
    def procs(self) -> list[mp.Process]:
        """Backward-compat: return list of mp.Process objects.

        ChannelManager accesses ``orch.procs`` — this property keeps
        that code working during the transition.
        """
        return list(self._procs.values())

    @procs.setter
    def procs(self, value: list):
        """Backward-compat setter: rebuild _procs dict from list."""
        self._procs = {p.name: p for p in value if hasattr(p, 'name')}

    # ── internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _send_shutdown_mail(agent_id: str):
        """Write a shutdown message to the agent's mailbox."""
        import json, time
        path = f".mailboxes/{agent_id}.jsonl"
        lock = FileLock(path + ".lock")
        msg = json.dumps({
            "from": "runtime", "type": "shutdown",
            "content": "shutdown requested", "ts": time.time(),
        })
        with lock:
            os.makedirs(".mailboxes", exist_ok=True)
            with open(path, "a") as f:
                f.write(msg + "\n")


# Register as proper subclass of AgentRuntime
from core.runtime.base import AgentRuntime
AgentRuntime.register(ProcessRuntime)

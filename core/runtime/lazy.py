"""
core/runtime/lazy.py — LazyRuntime: on-demand agent startup.

Wraps a delegate runtime (ProcessRuntime or InProcessRuntime) and adds:
  - **always_on**: agents that are started immediately and never stopped
  - **idle_shutdown**: seconds before idle agents auto-shutdown
  - **ensure_running()**: start an agent on demand if it's not running

This gives the best of both worlds:
  - DIRECT_ANSWER requests: only Leo runs (~600MB saved)
  - MAS_PIPELINE requests: Jerry+Alic launched on demand (~2-3s delay)
  - Idle agents auto-stop after configurable timeout

Usage::

    runtime = LazyRuntime(config={
        "runtime": {
            "mode": "lazy",
            "always_on": ["leo"],
            "idle_shutdown": 300,
        }
    })
    runtime.start_all(config, wakeup)   # only starts always_on agents
    runtime.ensure_running("jerry", config, wakeup)  # on demand
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LazyRuntime:
    """On-demand agent lifecycle with idle auto-shutdown.

    Delegates actual agent execution to a ProcessRuntime or
    InProcessRuntime under the hood.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        runtime_cfg = config.get("runtime", {})

        self._always_on: set[str] = set(runtime_cfg.get("always_on", ["leo"]))
        self._idle_shutdown: int = runtime_cfg.get("idle_shutdown", 300)

        # Choose the delegate runtime (process by default)
        delegate_mode = runtime_cfg.get("delegate", "process")
        if delegate_mode == "in_process":
            from core.runtime.in_process import InProcessRuntime
            self._delegate = InProcessRuntime()
        else:
            from core.runtime.process import ProcessRuntime
            self._delegate = ProcessRuntime()

        # Track agent definitions and last-activity times
        self._agent_defs: dict[str, dict] = {}
        self._last_activity: dict[str, float] = {}
        self._config: dict = config
        self._wakeup: Any = None

        # Idle monitor thread
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()

    # ── AgentRuntime interface ───────────────────────────────────────────

    def start(self, agent_def: dict, config: dict,
              wakeup: Any = None) -> None:
        """Start a single agent via the delegate runtime."""
        agent_id = agent_def["id"]
        self._agent_defs[agent_id] = agent_def
        self._config = config
        self._wakeup = wakeup

        # Only actually launch always_on agents
        if agent_id in self._always_on:
            self._delegate.start(agent_def, config, wakeup)
            self._last_activity[agent_id] = time.time()
            logger.info("[runtime:lazy] started always_on agent '%s'",
                        agent_id)
        else:
            logger.info("[runtime:lazy] registered '%s' (lazy, not started)",
                        agent_id)

    def start_all(self, config: dict, wakeup: Any = None) -> None:
        """Register all agents but only start always_on ones."""
        self._config = config
        self._wakeup = wakeup
        for agent_def in config.get("agents", []):
            self.start(agent_def, config, wakeup)
        # Start the idle monitor
        self._start_idle_monitor()

    def is_alive(self, agent_id: str) -> bool:
        return self._delegate.is_alive(agent_id)

    def agent_ids(self) -> list[str]:
        """Return ALL registered agents (not just running ones)."""
        return list(self._agent_defs.keys())

    def all_alive(self) -> dict[str, bool]:
        return {aid: self.is_alive(aid) for aid in self.agent_ids()}

    def stop(self, agent_id: str) -> None:
        self._delegate.stop(agent_id)

    def stop_all(self) -> None:
        self._stop_monitor.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3)
        self._delegate.stop_all()
        logger.info("[runtime:lazy] all agents stopped")

    def ensure_running(self, agent_id: str, config: dict = None,
                       wakeup: Any = None) -> None:
        """Start an agent on demand if it's not currently running.

        This is the key LazyRuntime method — called by the Orchestrator
        when MAS_PIPELINE is decided and Jerry/Alic need to be alive.
        """
        if self.is_alive(agent_id):
            self._last_activity[agent_id] = time.time()
            return

        agent_def = self._agent_defs.get(agent_id)
        if not agent_def:
            raise RuntimeError(
                f"Agent '{agent_id}' not registered with LazyRuntime.")

        cfg = config or self._config
        wk = wakeup or self._wakeup

        logger.info("[runtime:lazy] on-demand start for '%s'", agent_id)
        t0 = time.time()
        self._delegate.start(agent_def, cfg, wk)
        self._last_activity[agent_id] = time.time()
        elapsed = round(time.time() - t0, 2)
        logger.info("[runtime:lazy] '%s' started in %.2fs", agent_id, elapsed)

    # ── housekeeping ─────────────────────────────────────────────────────

    def clear(self) -> None:
        """Clear delegate runtime tracking."""
        self._delegate.clear()

    def prune_dead(self) -> None:
        """Prune dead agents in the delegate runtime."""
        self._delegate.prune_dead()

    # ── backward-compat ─────────────────────────────────────────────────

    @property
    def procs(self) -> list:
        return self._delegate.procs

    @procs.setter
    def procs(self, value: list):
        self._delegate.procs = value

    # ── monitors ──────────────────────────────────────────────────────────

    def _start_idle_monitor(self):
        """Background thread: idle shutdown + on-demand startup.

        Two responsibilities:
        1. Stop agents idle longer than ``idle_shutdown``.
        2. Start non-running agents when new subtasks appear on the TaskBoard
           (Leo creates subtasks → Jerry/Alic need to be alive to claim them).
        """

        def _monitor():
            while not self._stop_monitor.is_set():
                self._stop_monitor.wait(timeout=2)  # check every 2s
                if self._stop_monitor.is_set():
                    break
                self._check_pending_subtasks()
                # Idle check less frequently (every ~60s)
                if self._idle_shutdown > 0:
                    if int(time.time()) % 60 < 3:
                        self._check_idle_agents()

        self._monitor_thread = threading.Thread(
            target=_monitor, name="lazy-idle-monitor", daemon=True)
        self._monitor_thread.start()

    def _check_idle_agents(self):
        """Stop agents that have been idle longer than idle_shutdown."""
        now = time.time()
        for agent_id in list(self._last_activity.keys()):
            if agent_id in self._always_on:
                continue  # never stop always_on agents
            if not self.is_alive(agent_id):
                continue  # already stopped

            idle_secs = now - self._last_activity.get(agent_id, now)
            if idle_secs > self._idle_shutdown:
                logger.info(
                    "[runtime:lazy] stopping idle agent '%s' "
                    "(idle %.0fs > %ds threshold)",
                    agent_id, idle_secs, self._idle_shutdown)
                try:
                    self._delegate.stop(agent_id)
                except Exception as e:
                    logger.warning(
                        "[runtime:lazy] failed to stop '%s': %s",
                        agent_id, e)

    def _check_pending_subtasks(self):
        """Auto-start agents when pending subtasks need them.

        Reads the TaskBoard for PENDING tasks with ``required_role`` that
        maps to a non-running agent, and starts that agent on demand.
        """
        try:
            from core.task_board import TaskBoard, _ROLE_TO_AGENTS
            board = TaskBoard()
            data = board._read()
            if not data:
                return

            # Find pending tasks with specific role requirements
            needed_agents: set[str] = set()
            for tid, t in data.items():
                if t.get("status") != "pending":
                    continue
                role = t.get("required_role", "")
                if not role:
                    continue
                # Map role to agent IDs
                candidate_ids = _ROLE_TO_AGENTS.get(role, set())
                for cid in candidate_ids:
                    # Only care about agents we manage
                    if cid in self._agent_defs and not self.is_alive(cid):
                        needed_agents.add(cid)

            # Start needed agents
            for agent_id in needed_agents:
                logger.info(
                    "[runtime:lazy] pending tasks need '%s' — starting",
                    agent_id)
                self.ensure_running(agent_id)

        except Exception as e:
            logger.debug("[runtime:lazy] subtask check failed: %s", e)

    def touch(self, agent_id: str):
        """Update last-activity timestamp (called when agent does work)."""
        self._last_activity[agent_id] = time.time()

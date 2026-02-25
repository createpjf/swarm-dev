"""
core/runtime/in_process.py — InProcessRuntime: all agents as asyncio.Tasks.

Instead of spawning one OS process per agent (~600MB each), this runtime
runs every agent as a coroutine inside a single asyncio event loop.

Benefits:
  - **Memory**: 3 agents ≈ 600MB total vs ~1.8GB with mp.Process.
  - **Startup**: ~1s (no process fork) vs ~10s.
  - **Debugging**: single process → pdb/breakpoints work naturally.

Tradeoffs:
  - All agents share one GIL (CPU-bound LLM calls use threads anyway).
  - File-lock operations must go through AsyncTaskBoardWrapper.
  - Agent crash takes down the entire loop (mitigated by try/except).

Usage::

    runtime = InProcessRuntime()
    runtime.start_all(config, wakeup)
    await runtime.run_until_complete()  # or let gateway manage lifecycle
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class InProcessRuntime:
    """All agents as asyncio.Tasks in a single event loop.

    The runtime creates an event loop (or reuses the running one)
    and launches each agent as a Task via ``_run_agent_async()``.
    """

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}     # agent_id → Task
        self._agents: dict[str, Any] = {}              # agent_id → BaseAgent
        self._agent_defs: dict[str, dict] = {}         # agent_id → agent_def
        self._running = False

    # ── AgentRuntime interface ───────────────────────────────────────────

    def start(self, agent_def: dict, config: dict,
              wakeup: Any = None) -> None:
        """Launch a single agent as an asyncio.Task."""
        agent_id = agent_def["id"]
        self._agent_defs[agent_id] = agent_def

        loop = _get_or_create_loop()
        task = loop.create_task(
            self._run_agent_async(agent_def, config, wakeup),
            name=f"agent-{agent_id}",
        )
        self._tasks[agent_id] = task
        logger.info("[runtime:in_process] launched '%s' as asyncio.Task",
                    agent_id)

    def start_all(self, config: dict, wakeup: Any = None) -> None:
        """Launch every agent defined in config."""
        for agent_def in config.get("agents", []):
            self.start(agent_def, config, wakeup)
        self._running = True

    def is_alive(self, agent_id: str) -> bool:
        task = self._tasks.get(agent_id)
        return task is not None and not task.done()

    def agent_ids(self) -> list[str]:
        return list(self._tasks.keys())

    def all_alive(self) -> dict[str, bool]:
        return {aid: self.is_alive(aid) for aid in self.agent_ids()}

    def stop(self, agent_id: str) -> None:
        """Cancel a single agent's asyncio.Task."""
        task = self._tasks.get(agent_id)
        if task and not task.done():
            task.cancel()
            logger.info("[runtime:in_process] cancelled '%s'", agent_id)

    def stop_all(self) -> None:
        """Cancel all agent Tasks."""
        for agent_id in list(self._tasks.keys()):
            self.stop(agent_id)
        self._running = False
        logger.info("[runtime:in_process] all agents stopped")

    def ensure_running(self, agent_id: str, config: dict = None,
                       wakeup: Any = None) -> None:
        """Restart agent if its Task completed/crashed."""
        if self.is_alive(agent_id):
            return
        agent_def = self._agent_defs.get(agent_id)
        if agent_def and config:
            logger.info("[runtime:in_process] restarting '%s'", agent_id)
            self.start(agent_def, config, wakeup)
        else:
            raise RuntimeError(
                f"Agent '{agent_id}' is not running and cannot be restarted "
                f"(missing agent_def or config)."
            )

    # ── housekeeping ─────────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all agent tracking for pool restart."""
        self._tasks.clear()
        self._agents.clear()
        self._agent_defs.clear()

    def prune_dead(self) -> None:
        """Remove completed/crashed tasks from tracking."""
        self._tasks = {aid: t for aid, t in self._tasks.items()
                       if not t.done()}

    # ── backward-compat ─────────────────────────────────────────────────

    @property
    def procs(self) -> list:
        """Backward-compat: return lightweight stubs for ChannelManager.

        InProcessRuntime has no mp.Process objects.  We return stub objects
        with ``.is_alive()`` and ``.name`` so existing code that iterates
        ``orch.procs`` doesn't crash.
        """
        return [_TaskStub(aid, self.is_alive(aid))
                for aid in self.agent_ids()]

    @procs.setter
    def procs(self, value: list):
        """No-op setter for backward compat."""
        pass

    # ── core agent runner ────────────────────────────────────────────────

    async def _run_agent_async(self, agent_def: dict, config: dict,
                               wakeup: Any = None):
        """Run one agent as an async coroutine.

        Mirrors ``_agent_process()`` but without process isolation:
        - No fork / no subprocess
        - Uses AsyncTaskBoardWrapper instead of raw TaskBoard
        - Shares the event loop with other agents
        """
        from core.runtime.process import _build_agent_cfg_dict

        agent_id = agent_def["id"]
        cfg_dict = _build_agent_cfg_dict(agent_def, config)

        # Setup per-agent logging (to file, not stdout redirect)
        os.makedirs(".logs", exist_ok=True)
        agent_logger = logging.getLogger(f"agent.{agent_id}")
        fh = logging.FileHandler(f".logs/{agent_id}.log", mode="w")
        fh.setFormatter(logging.Formatter(
            "[%(asctime)s][%(name)s] %(message)s"))
        agent_logger.addHandler(fh)
        agent_logger.setLevel(logging.INFO)

        try:
            # Build adapters (same as _agent_process, but in-process)
            from core.orchestrator import (
                _build_llm_for_agent, _build_memory,
                _build_chain, _build_episodic_memory,
            )
            llm = _build_llm_for_agent(agent_def, config)
            memory = _build_memory(config, agent_id=agent_id)
            chain = _build_chain(config)
            episodic, kb = _build_episodic_memory(config, agent_id)

            from core.agent import AgentConfig, BaseAgent
            from core.skill_loader import SkillLoader
            from core.usage_tracker import UsageTracker

            cfg = AgentConfig(**cfg_dict)
            agent = BaseAgent(cfg, llm, memory, SkillLoader(), chain,
                              episodic=episodic, kb=kb)
            tracker = UsageTracker()
            self._agents[agent_id] = agent

            from core.context_bus import ContextBus
            from core.task_board import TaskBoard
            from core.async_wrappers import AsyncTaskBoardWrapper

            bus = ContextBus()
            raw_board = TaskBoard()
            board = AsyncTaskBoardWrapper(raw_board)

            from core.heartbeat import Heartbeat
            hb = Heartbeat(agent_id)

            # Run the agent loop (same _agent_loop from orchestrator)
            from core.orchestrator import _agent_loop
            try:
                await _agent_loop(agent, bus, board, config, tracker, hb,
                                  wakeup=wakeup)
            finally:
                hb.stop()

        except asyncio.CancelledError:
            logger.info("[runtime:in_process] '%s' cancelled", agent_id)
        except Exception as e:
            logger.error("[runtime:in_process] '%s' crashed: %s",
                         agent_id, e, exc_info=True)
        finally:
            self._agents.pop(agent_id, None)

    # ── async lifecycle helpers ──────────────────────────────────────────

    async def run_until_complete(self):
        """Wait for all agent tasks to complete (for CLI / testing)."""
        if self._tasks:
            await asyncio.gather(*self._tasks.values(),
                                 return_exceptions=True)

    async def wait_any_alive(self, poll_interval: float = 0.5):
        """Poll until no agents are alive (for ChannelManager)."""
        while any(self.is_alive(aid) for aid in self.agent_ids()):
            await asyncio.sleep(poll_interval)


class _TaskStub:
    """Lightweight stub mimicking mp.Process for backward compat."""

    def __init__(self, name: str, alive: bool):
        self.name = name
        self.pid = 0  # no real PID
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout=None):
        pass  # no-op for async tasks

    def terminate(self):
        pass  # use runtime.stop() instead


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Get the running event loop or create a new one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

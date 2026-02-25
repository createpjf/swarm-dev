"""
core/runtime/wakeup.py — Dual-mode WakeupBus.

Supports two backends transparently:
  - **process mode** (default): uses ``multiprocessing.Event``
    for cross-process wakeup (same as original ``core/wakeup.py``).
  - **async mode**: uses ``asyncio.Event`` for in-process
    coroutine wakeup (InProcessRuntime).

The agent loop code (``_agent_loop``) calls ``wakeup.async_wait()``
and ``wakeup.wake_all()`` identically regardless of backend.

Usage::

    # Process mode (Orchestrator default)
    bus = DualWakeupBus(mode="process")
    bus.register("jerry")
    # ... pass to child processes

    # Async mode (InProcessRuntime)
    bus = DualWakeupBus(mode="async")
    bus.register("jerry")
    # ... pass to asyncio.Tasks
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from typing import Optional

logger = logging.getLogger(__name__)


class DualWakeupBus:
    """Event bus that works in both mp.Process and asyncio.Task modes."""

    def __init__(self, mode: str = "process"):
        """
        Args:
            mode: "process" for multiprocessing.Event,
                  "async" for asyncio.Event.
        """
        if mode not in ("process", "async"):
            raise ValueError(f"Invalid wakeup mode: {mode}")
        self._mode = mode
        self._mp_events: dict[str, multiprocessing.Event] = {}
        self._async_events: dict[str, asyncio.Event] = {}

    @property
    def mode(self) -> str:
        return self._mode

    # ── registration ─────────────────────────────────────────────────────

    def register(self, agent_id: str):
        """Register an agent — creates the appropriate Event type.

        For async mode, the asyncio.Event is created lazily on first use
        (inside the running event loop) to avoid "attached to different loop"
        errors in Python 3.9.
        """
        if self._mode == "process":
            self._mp_events[agent_id] = multiprocessing.Event()
        else:
            # Mark as registered; actual asyncio.Event created on first access
            self._async_events[agent_id] = None  # type: ignore

    # ── internal: lazy asyncio.Event creation ───────────────────────────

    def _get_async_event(self, agent_id: str) -> Optional[asyncio.Event]:
        """Get or create the asyncio.Event for an agent.

        Events are created lazily inside the running loop to avoid
        the "attached to different loop" error in Python < 3.10.
        """
        if agent_id not in self._async_events:
            return None
        ev = self._async_events[agent_id]
        if ev is None:
            ev = asyncio.Event()
            self._async_events[agent_id] = ev
        return ev

    # ── wake (called by producer: Leo creating subtasks) ─────────────────

    def wake(self, agent_id: str):
        """Wake a specific agent."""
        if self._mode == "process":
            ev = self._mp_events.get(agent_id)
            if ev:
                ev.set()
        else:
            ev = self._get_async_event(agent_id)
            if ev:
                ev.set()

    def wake_all(self):
        """Wake every registered agent."""
        if self._mode == "process":
            for ev in self._mp_events.values():
                ev.set()
        else:
            for agent_id in self._async_events:
                ev = self._get_async_event(agent_id)
                if ev:
                    ev.set()

    # ── wait (called by consumer: agent idle loop) ───────────────────────

    async def async_wait(self, agent_id: str, timeout: float = 2.0) -> bool:
        """Async-compatible wait — works in both modes.

        Returns True if woken by signal, False if timed out.
        """
        if self._mode == "process":
            # mp.Event.wait() blocks — run in thread pool
            ev = self._mp_events.get(agent_id)
            if not ev:
                await asyncio.sleep(timeout)
                return False
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, ev.wait, timeout)
            if result:
                ev.clear()
            return result
        else:
            # asyncio.Event — native async wait with lazy creation
            ev = self._get_async_event(agent_id)
            if not ev:
                await asyncio.sleep(timeout)
                return False
            try:
                await asyncio.wait_for(ev.wait(), timeout=timeout)
                ev.clear()
                return True
            except asyncio.TimeoutError:
                return False

    # ── query ────────────────────────────────────────────────────────────

    def get_event(self, agent_id: str):
        """Get raw event for an agent (for passing to child process)."""
        if self._mode == "process":
            return self._mp_events.get(agent_id)
        return self._async_events.get(agent_id)

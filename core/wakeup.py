"""
WakeupBus — event-driven agent wakeup (inspired by OpenAI Swarm's synchronous run loop).

Instead of agents polling the TaskBoard every 1s, they block on a
multiprocessing.Event and get woken instantly when work is available.

Usage:
    # In parent process (Orchestrator.__init__):
    wakeup = WakeupBus()
    ev = wakeup.register("jerry")
    # Pass wakeup to child processes

    # In child process (agent loop):
    await wakeup.async_wait("jerry", timeout=2.0)

    # When Leo creates subtasks:
    wakeup.wake("jerry")
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from typing import Optional

logger = logging.getLogger(__name__)


class WakeupBus:
    """Cross-process event bus for zero-delay agent wakeup."""

    def __init__(self):
        self._events: dict[str, multiprocessing.Event] = {}

    def register(self, agent_id: str) -> multiprocessing.Event:
        """Register an agent and return its Event (call in parent process)."""
        ev = multiprocessing.Event()
        self._events[agent_id] = ev
        return ev

    def wake(self, agent_id: str):
        """Wake a specific agent (e.g., after creating a subtask for it)."""
        ev = self._events.get(agent_id)
        if ev:
            ev.set()

    def wake_all(self):
        """Wake all registered agents."""
        for ev in self._events.values():
            ev.set()

    def get_event(self, agent_id: str) -> Optional[multiprocessing.Event]:
        """Get the Event for an agent (for passing to child process)."""
        return self._events.get(agent_id)

    async def async_wait(self, agent_id: str, timeout: float = 2.0) -> bool:
        """Async-compatible wait — runs Event.wait() in a thread executor.

        Returns True if woken by signal, False if timed out.
        """
        ev = self._events.get(agent_id)
        if not ev:
            await asyncio.sleep(timeout)
            return False
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, ev.wait, timeout)
        if result:
            ev.clear()  # Reset for next wait
        return result

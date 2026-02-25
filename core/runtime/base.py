"""
core/runtime/base.py — AgentRuntime abstract base class.

Defines the contract that all runtime backends must implement.
The Orchestrator delegates ALL agent lifecycle operations to this interface.
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AgentRuntime(abc.ABC):
    """Abstract interface for agent lifecycle management.

    Subclasses decide HOW agents run (process, coroutine, lazy, etc.)
    while the Orchestrator only cares WHAT to do (start, stop, check).
    """

    # ── launch ───────────────────────────────────────────────────────────

    @abc.abstractmethod
    def start(self, agent_def: dict, config: dict,
              wakeup: Any = None) -> None:
        """Launch a single agent.

        Args:
            agent_def:  one entry from ``config["agents"]``
            config:     full agents.yaml dict (for compaction etc.)
            wakeup:     WakeupBus instance (or None)
        """

    def start_all(self, config: dict, wakeup: Any = None) -> None:
        """Launch every agent defined in *config*.

        Default implementation iterates ``config["agents"]`` and calls
        :meth:`start` for each.  Subclasses may override for bulk
        optimisation (e.g. a single event-loop in InProcessRuntime).
        """
        for agent_def in config.get("agents", []):
            self.start(agent_def, config, wakeup)

    # ── query ────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def is_alive(self, agent_id: str) -> bool:
        """Return True if the agent is currently running."""

    def all_alive(self) -> dict[str, bool]:
        """Return ``{agent_id: is_alive}`` for every managed agent."""
        return {aid: self.is_alive(aid) for aid in self.agent_ids()}

    @abc.abstractmethod
    def agent_ids(self) -> list[str]:
        """Return the list of agent IDs managed by this runtime."""

    # ── lifecycle ────────────────────────────────────────────────────────

    @abc.abstractmethod
    def stop(self, agent_id: str) -> None:
        """Stop a single agent (graceful)."""

    @abc.abstractmethod
    def stop_all(self) -> None:
        """Stop every managed agent (graceful shutdown)."""

    def ensure_running(self, agent_id: str, config: dict,
                       wakeup: Any = None) -> None:
        """Ensure an agent is alive; start it if not.

        Default: no-op if alive, otherwise raises.
        LazyRuntime overrides this to start on demand.
        """
        if not self.is_alive(agent_id):
            raise RuntimeError(
                f"Agent '{agent_id}' is not running and this runtime "
                f"does not support on-demand start."
            )

    # ── housekeeping ─────────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all agent tracking (for pool restart scenarios).

        After clear(), agent_ids() returns [].  Call start_all() to
        re-populate.
        """

    def prune_dead(self) -> None:
        """Remove dead/finished agents from internal tracking.

        Keeps alive agents untouched.  Default: no-op (subclasses override).
        """

    # ── compatibility helpers (Phase 1 bridge) ───────────────────────────

    @property
    def procs(self) -> list:
        """Backward-compat: return a list of process-like objects.

        ChannelManager accesses ``orch.procs`` directly in several places.
        ProcessRuntime returns actual ``mp.Process`` objects; other runtimes
        return lightweight stubs so existing code doesn't break.
        """
        return []

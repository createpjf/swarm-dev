"""
core/subagent.py
Dynamic subagent spawning framework — allows agents to create child agents
at runtime for parallel or specialized work.

Inspired by OpenClaw's subagent-registry + subagent-spawn patterns.

Key features:
  - SubagentRegistry: tracks parent→child relationships and lifecycle
  - spawn(): create a child agent process with inherited/overridden config
  - Spawn modes: "run" (one-shot) vs "session" (persistent, bound to thread)
  - Depth limit: default max_depth=3 prevents infinite recursion
  - Auto-announce: child notifies parent on completion via mailbox
  - Deferred cleanup: parent confirms receipt before child process exits

Usage from a tool or agent:
    from core.subagent import SubagentRegistry
    registry = SubagentRegistry()
    child_id = registry.spawn(
        parent_id="jerry",
        task="Research Python async patterns",
        model="gpt-4o-mini",
        mode="run",
    )
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.protocols import FileLock  # shared fallback

logger = logging.getLogger(__name__)

REGISTRY_FILE = ".subagent_registry.json"
REGISTRY_LOCK = ".subagent_registry.lock"
MAX_DEPTH = 3
MAX_CHILDREN_PER_PARENT = 5
DEFAULT_SPAWN_TIMEOUT = 300  # 5 minutes default timeout for subagent tasks


class SpawnMode(str, Enum):
    RUN = "run"          # One-shot: execute task and exit
    SESSION = "session"  # Persistent: stays alive for follow-up interactions


@dataclass
class SubagentEntry:
    """Registry entry for a spawned subagent."""
    subagent_id: str
    parent_id: str
    task: str
    model: str
    mode: str = "run"
    status: str = "pending"  # pending, running, completed, failed
    depth: int = 1
    result: str = ""
    created_at: float = 0.0
    completed_at: float = 0.0
    task_id: str = ""  # Associated TaskBoard task ID
    timeout: int = DEFAULT_SPAWN_TIMEOUT  # watchdog timeout in seconds

    def to_dict(self) -> dict:
        return {
            "subagent_id": self.subagent_id,
            "parent_id": self.parent_id,
            "task": self.task,
            "model": self.model,
            "mode": self.mode,
            "status": self.status,
            "depth": self.depth,
            "result": self.result,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "task_id": self.task_id,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SubagentEntry":
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


class SubagentRegistry:
    """File-backed registry tracking all spawned subagents.

    Process-safe via FileLock. Designed to be used from tool handlers
    and agent loops alike.
    """

    def __init__(self):
        self.lock = FileLock(REGISTRY_LOCK)

    def _read(self) -> dict[str, dict]:
        try:
            with open(REGISTRY_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        with open(REGISTRY_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def spawn(self, parent_id: str, task: str,
              model: str = "", skills: list[str] | None = None,
              mode: str = "run", depth: int = 0,
              timeout: int = DEFAULT_SPAWN_TIMEOUT) -> str:
        """Spawn a new subagent and submit its task to the TaskBoard.

        Args:
            parent_id: The spawning agent's ID
            task: Task description for the subagent
            model: LLM model override (empty = inherit from parent agent config)
            skills: Skill list override (None = inherit)
            mode: "run" (one-shot) or "session" (persistent)
            depth: Current depth in the subagent tree (0 = top-level agent)

        Returns:
            The subagent's unique ID

        Raises:
            RuntimeError: If max depth or max children exceeded
        """
        # Check depth limit
        if depth >= MAX_DEPTH:
            raise RuntimeError(
                f"Subagent depth limit reached ({MAX_DEPTH}). "
                f"Cannot spawn child from depth {depth}.")

        # Check children count
        with self.lock:
            data = self._read()
            active_children = sum(
                1 for e in data.values()
                if e.get("parent_id") == parent_id
                and e.get("status") in ("pending", "running"))
            if active_children >= MAX_CHILDREN_PER_PARENT:
                raise RuntimeError(
                    f"Max children per parent reached ({MAX_CHILDREN_PER_PARENT}). "
                    f"Wait for existing subagents to complete.")

        subagent_id = f"sub_{parent_id}_{uuid.uuid4().hex[:8]}"

        # Submit task to TaskBoard
        try:
            from core.task_board import TaskBoard
            board = TaskBoard()
            # Subagent tasks are routed to executors (required_role=execute)
            new_task = board.create(
                description=(
                    f"[SUBAGENT from {parent_id}] {task}\n\n"
                    f"---\n"
                    f"This task was dynamically spawned by agent '{parent_id}'. "
                    f"Complete it independently and report results."
                ),
                required_role="execute",
            )
            task_id = new_task.task_id
        except Exception as e:
            logger.error("Failed to create subagent task: %s", e)
            raise

        # Register in registry
        entry = SubagentEntry(
            subagent_id=subagent_id,
            parent_id=parent_id,
            task=task[:500],
            model=model,
            mode=mode,
            status="pending",
            depth=depth + 1,
            created_at=time.time(),
            task_id=task_id,
            timeout=timeout,
        )

        with self.lock:
            data = self._read()
            data[subagent_id] = entry.to_dict()
            self._write(data)

        logger.info("[subagent] spawned %s (parent=%s, depth=%d, task_id=%s)",
                    subagent_id, parent_id, depth + 1, task_id)
        return subagent_id

    def get(self, subagent_id: str) -> Optional[SubagentEntry]:
        """Get a subagent entry by ID."""
        with self.lock:
            data = self._read()
        entry = data.get(subagent_id)
        if entry:
            return SubagentEntry.from_dict(entry)
        return None

    def list_children(self, parent_id: str,
                      include_completed: bool = False) -> list[SubagentEntry]:
        """List all subagents spawned by a parent."""
        with self.lock:
            data = self._read()
        results = []
        for entry_dict in data.values():
            if entry_dict.get("parent_id") != parent_id:
                continue
            if not include_completed and entry_dict.get("status") in ("completed", "failed"):
                continue
            results.append(SubagentEntry.from_dict(entry_dict))
        return sorted(results, key=lambda e: e.created_at, reverse=True)

    def update_status(self, subagent_id: str, status: str,
                      result: str = ""):
        """Update subagent status and optionally store result."""
        with self.lock:
            data = self._read()
            entry = data.get(subagent_id)
            if not entry:
                return
            entry["status"] = status
            if result:
                entry["result"] = result[:5000]
            if status in ("completed", "failed"):
                entry["completed_at"] = time.time()
            self._write(data)

    def auto_announce(self, subagent_id: str, result: str):
        """Notify parent agent that a subagent has completed.

        Sends a mailbox message to the parent with the result.
        """
        with self.lock:
            data = self._read()
        entry = data.get(subagent_id)
        if not entry:
            return

        parent_id = entry["parent_id"]
        task_desc = entry.get("task", "")[:200]

        # Send completion notification to parent's mailbox
        try:
            from core.agent import BaseAgent, MAILBOX_DIR
            path = os.path.join(MAILBOX_DIR, f"{parent_id}.jsonl")
            lock = FileLock(path + ".lock")
            msg = json.dumps({
                "from": subagent_id,
                "type": "subagent_complete",
                "content": (
                    f"Subagent '{subagent_id}' completed task:\n"
                    f"{task_desc}\n\n"
                    f"Result:\n{result[:2000]}"
                ),
                "task_id": entry.get("task_id", ""),
                "ts": time.time(),
            }, ensure_ascii=False)
            with lock:
                os.makedirs(MAILBOX_DIR, exist_ok=True)
                with open(path, "a") as f:
                    f.write(msg + "\n")
            logger.info("[subagent] auto-announced %s → parent %s",
                        subagent_id, parent_id)
        except Exception as e:
            logger.error("[subagent] auto-announce failed: %s", e)

        # Update status
        self.update_status(subagent_id, "completed", result)

    def check_timeouts(self) -> list[str]:
        """Check for timed-out subagents and mark them as failed.

        Called periodically by the scheduler or orchestrator loop.
        Returns list of subagent IDs that were timed out.
        """
        timed_out = []
        now = time.time()
        with self.lock:
            data = self._read()
            changed = False
            for sid, entry in data.items():
                if entry.get("status") not in ("pending", "running"):
                    continue
                timeout = entry.get("timeout", DEFAULT_SPAWN_TIMEOUT)
                created = entry.get("created_at", 0)
                if created and (now - created) > timeout:
                    entry["status"] = "failed"
                    entry["result"] = (
                        f"Timed out after {timeout}s "
                        f"(elapsed: {now - created:.0f}s)")
                    entry["completed_at"] = now
                    timed_out.append(sid)
                    changed = True
                    logger.warning(
                        "[subagent] %s timed out after %ds", sid, timeout)
            if changed:
                self._write(data)

        # Notify parents of timed-out subagents
        for sid in timed_out:
            self.auto_announce(sid, f"TIMEOUT: subagent {sid} exceeded {DEFAULT_SPAWN_TIMEOUT}s limit")

        return timed_out

    def cleanup_old(self, max_age_hours: int = 24):
        """Remove completed/failed entries older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        with self.lock:
            data = self._read()
            to_remove = [
                sid for sid, entry in data.items()
                if entry.get("status") in ("completed", "failed")
                and entry.get("completed_at", 0) < cutoff
            ]
            for sid in to_remove:
                del data[sid]
            if to_remove:
                self._write(data)
                logger.info("[subagent] cleaned up %d old entries", len(to_remove))

    def get_tree(self, root_id: str = "") -> list[dict]:
        """Get the full subagent tree (for dashboard visualization)."""
        with self.lock:
            data = self._read()

        if root_id:
            # Filter to a specific parent's subtree
            relevant = {}
            def _collect(pid):
                for sid, entry in data.items():
                    if entry.get("parent_id") == pid:
                        relevant[sid] = entry
                        _collect(sid)
            _collect(root_id)
            return list(relevant.values())

        return list(data.values())

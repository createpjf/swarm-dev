"""
core/task_board.py
File-locked task lifecycle manager.
Supports Agent Teams-style self-claim:
  each agent process independently claims the next available task.
Dependency graph: tasks can be blocked_by other task IDs.
Role-based routing: tasks can require a specific agent role.
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

# Phase 8: loud warning on missing filelock
try:
    from filelock import FileLock
except ImportError:
    import warnings
    warnings.warn(
        "filelock package not installed. TaskBoard is NOT process-safe. "
        "Install with: pip install filelock",
        RuntimeWarning, stacklevel=2,
    )

    class FileLock:  # type: ignore
        def __init__(self, path):
            logging.getLogger(__name__).warning(
                "FileLock unavailable — concurrent access to %s is UNSAFE",
                path)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

logger = logging.getLogger(__name__)

BOARD_FILE = ".task_board.json"
BOARD_LOCK = ".task_board.lock"

# ── Role matching ────────────────────────────────────────────────────────────
# Maps required_role keywords → which agent_id(s) can claim them.
# This avoids false positives from substring matching
# (e.g. planner role says "Do not implement" which contains "implement").

_ROLE_TO_AGENTS = {
    "planner":    {"planner"},
    "implement":  {"executor", "coder", "developer", "builder"},
    "review":     {"reviewer", "auditor"},
}

def _role_matches(required_role: str, agent_id: str, agent_role: str | None) -> bool:
    """Check if an agent qualifies for a required_role."""
    req = required_role.lower()
    aid = agent_id.lower()

    # 1. Direct match: agent_id matches the required_role
    if req == aid:
        return True

    # 2. Map-based match: check if agent_id is in the allowed set
    allowed = _ROLE_TO_AGENTS.get(req)
    if allowed and aid in allowed:
        return True

    # 3. Fallback: agent_id contains the required_role keyword
    #    (but only the first word of agent_role, not the full description)
    if req in aid:
        return True

    return False


class TaskStatus(str, Enum):
    PENDING   = "pending"
    CLAIMED   = "claimed"
    REVIEW    = "review"      # waiting for peer review
    COMPLETED = "completed"
    FAILED    = "failed"
    BLOCKED   = "blocked"     # waiting for dependency


@dataclass
class Task:
    task_id:         str
    description:     str
    status:          TaskStatus = TaskStatus.PENDING
    agent_id:        Optional[str] = None
    result:          Optional[str] = None
    blocked_by:      list[str] = field(default_factory=list)
    min_reputation:  int = 0
    required_role:   Optional[str] = None       # Phase 6: role-based routing
    created_at:      float = field(default_factory=time.time)
    claimed_at:      Optional[float] = None
    completed_at:    Optional[float] = None
    review_scores:   list[dict] = field(default_factory=list)   # [{reviewer, score, comment}]
    evolution_flags: list[str] = field(default_factory=list)    # error tags

    def to_dict(self) -> dict:
        return {
            "task_id":        self.task_id,
            "description":    self.description,
            "status":         self.status.value,
            "agent_id":       self.agent_id,
            "result":         self.result,
            "blocked_by":     self.blocked_by,
            "min_reputation": self.min_reputation,
            "required_role":  self.required_role,
            "created_at":     self.created_at,
            "claimed_at":     self.claimed_at,
            "completed_at":   self.completed_at,
            "review_scores":  self.review_scores,
            "evolution_flags":self.evolution_flags,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["status"] = TaskStatus(d.get("status", "pending"))
        # Handle older task dicts that may lack required_role
        d.setdefault("required_role", None)
        return cls(**d)


class TaskBoard:
    """
    File-backed task store.
    All mutating methods acquire a file lock — safe for concurrent agent processes.
    """

    def __init__(self, path: str = BOARD_FILE):
        self.path = path
        self.lock = FileLock(BOARD_LOCK)
        if not os.path.exists(path):
            self._write({})

    # ── Create ───────────────────────────────────────────────────────────────

    def create(self, description: str,
               blocked_by: list[str] | None = None,
               min_reputation: int = 0,
               required_role: str | None = None) -> Task:
        # Phase 8: full UUID instead of [:8] to prevent collisions
        task = Task(
            task_id=str(uuid.uuid4()),
            description=description,
            blocked_by=blocked_by or [],
            min_reputation=min_reputation,
            required_role=required_role,
        )
        with self.lock:
            data = self._read()
            data[task.task_id] = task.to_dict()
            self._write(data)
        return task

    # ── Self-claim (Agent Teams pattern) ────────────────────────────────────

    def claim_next(self, agent_id: str, agent_reputation: int = 100,
                   agent_role: str | None = None) -> Optional[Task]:
        """
        Atomically grab the next available unblocked task this agent qualifies for.
        Returns None if nothing is available.
        File lock prevents two agents claiming the same task.
        """
        with self.lock:
            data = self._read()
            completed_ids = {tid for tid, t in data.items()
                             if t["status"] == TaskStatus.COMPLETED.value}

            for tid, t in data.items():
                if t["status"] != TaskStatus.PENDING.value:
                    continue
                if t["min_reputation"] > agent_reputation:
                    continue
                # check all blockers are done
                if any(b not in completed_ids for b in t.get("blocked_by", [])):
                    continue

                # Phase 6: role-based routing
                req_role = t.get("required_role")
                if req_role:
                    if not _role_matches(req_role, agent_id, agent_role):
                        continue

                # claim it
                t["status"]     = TaskStatus.CLAIMED.value
                t["agent_id"]   = agent_id
                t["claimed_at"] = time.time()
                self._write(data)
                return Task.from_dict(t)

        return None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def submit_for_review(self, task_id: str, result: str):
        with self.lock:
            data = self._read()
            t = data[task_id]
            t["status"] = TaskStatus.REVIEW.value
            t["result"] = result
            self._write(data)

    def add_review(self, task_id: str, reviewer_id: str,
                   score: int, comment: str):
        with self.lock:
            data  = self._read()
            t     = data[task_id]
            t["review_scores"].append({
                "reviewer": reviewer_id,
                "score":    score,
                "comment":  comment,
                "ts":       time.time(),
            })
            self._write(data)

    def complete(self, task_id: str) -> Task:
        """Hook: TaskCompleted — only mark done if review passed."""
        with self.lock:
            data = self._read()
            t    = data[task_id]
            avg  = self._avg_review_score(t)
            if avg < 60:
                # Phase 4 fix: send back to PENDING (not CLAIMED)
                # so task re-enters the claimable pool
                t["status"]    = TaskStatus.PENDING.value
                t["agent_id"]  = None
                t["claimed_at"] = None
                t["evolution_flags"].append("review_failed")
                self._write(data)
                return Task.from_dict(t)
            t["status"]       = TaskStatus.COMPLETED.value
            t["completed_at"] = time.time()
            self._write(data)
            return Task.from_dict(t)

    def fail(self, task_id: str, reason: str = ""):
        with self.lock:
            data = self._read()
            t    = data[task_id]
            t["status"] = TaskStatus.FAILED.value
            t["evolution_flags"].append(f"failed:{reason}")
            self._write(data)

    def flag(self, task_id: str, tag: str):
        with self.lock:
            data = self._read()
            data[task_id]["evolution_flags"].append(tag)
            self._write(data)

    # ── Query ────────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[Task]:
        data = self._read()
        raw  = data.get(task_id)
        return Task.from_dict(raw) if raw else None

    def list_by_agent(self, agent_id: str) -> list[Task]:
        return [Task.from_dict(t) for t in self._read().values()
                if t.get("agent_id") == agent_id]

    def pending_count(self) -> int:
        return sum(1 for t in self._read().values()
                   if t["status"] == TaskStatus.PENDING.value)

    def collect_results(self, root_task_id: str) -> str:
        """Collect all completed results for a task tree (root + all subtasks).

        Gathers results from:
        1. All non-planner completed tasks (executor output)
        2. Falls back to planner output if no executor results exist
        """
        data = self._read()
        planner_result = None
        executor_results = []

        for tid, t in data.items():
            if not t.get("result"):
                continue
            agent = t.get("agent_id", "")
            if "planner" in agent.lower():
                planner_result = t["result"]
            else:
                executor_results.append(t["result"])

        # Prefer executor results (the actual implementation)
        if executor_results:
            return "\n\n---\n\n".join(executor_results)

        # If no executor results, fall back to planner output
        if planner_result:
            return planner_result

        # Last resort: any result from root task
        root = data.get(root_task_id)
        if root and root.get("result"):
            return root["result"]

        return ""

    def clear(self):
        """Remove all tasks. Used between chat turns."""
        with self.lock:
            self._write({})

    def history(self, agent_id: str, last: int = 50) -> list[Task]:
        tasks = [Task.from_dict(t) for t in self._read().values()
                 if t.get("agent_id") == agent_id]
        tasks.sort(key=lambda t: t.created_at or 0, reverse=True)
        return tasks[:last]

    # ── Internal ─────────────────────────────────────────────────────────────

    def _read(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _avg_review_score(t: dict) -> float:
        scores = [r["score"] for r in t.get("review_scores", [])]
        return sum(scores) / len(scores) if scores else 100.0  # no review = pass

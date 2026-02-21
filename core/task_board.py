"""
core/task_board.py
File-locked task lifecycle manager.
Supports Agent Teams-style self-claim:
  each agent process independently claims the next available task.
Dependency graph: tasks can be blocked_by other task IDs.
Role-based routing: tasks can require a specific agent role.
Timeout recovery: stale CLAIMED/REVIEW tasks auto-recover to PENDING.
Cancel/Pause/Retry: user-controllable task lifecycle.
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

# Timeout thresholds (seconds)
CLAIMED_TIMEOUT = 600   # 10 min — agent crashed if no progress
REVIEW_TIMEOUT  = 300   # 5 min — reviewer crashed

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
    "planner":    {"leo", "planner"},
    "plan":       {"leo", "planner"},
    "implement":  {"jerry", "executor", "coder", "developer", "builder"},
    "execute":    {"jerry", "executor", "coder", "developer", "builder"},
    "code":       {"jerry", "executor", "coder", "developer", "builder"},
    "review":     {"alic", "reviewer", "auditor"},
    "critique":   {"alic", "reviewer", "auditor"},
}

# Strict role guard: these roles can ONLY be claimed by their designated agents.
# Prevents executor from stealing planner tasks or vice versa.
_STRICT_ROLES = {"planner", "plan", "review", "critique"}

# Agent claim restrictions: certain agents can ONLY claim specific role types.
# This prevents reviewer from stealing executor/planner tasks when required_role=None.
_AGENT_CLAIM_RESTRICTIONS: dict[str, set[str]] = {
    "alic":     {"review", "critique"},
    "reviewer": {"review", "critique"},
    "auditor":  {"review", "critique"},
}


def _agent_may_claim(agent_id: str, required_role: str | None) -> bool:
    """Check if agent is allowed to claim a task based on agent-level restrictions.

    Restricted agents (reviewer, auditor) can ONLY claim tasks whose
    required_role matches their allowed set.  If required_role is None
    (generic task), restricted agents are blocked.

    Non-restricted agents (executor, planner) are always allowed.
    """
    aid = agent_id.lower()
    for restricted_keyword, allowed_roles in _AGENT_CLAIM_RESTRICTIONS.items():
        if restricted_keyword in aid:
            if required_role is None:
                return False
            return required_role.lower() in allowed_roles
    return True


def _role_matches(required_role: str, agent_id: str, agent_role: str | None) -> bool:
    """Check if an agent qualifies for a required_role.

    For strict roles (planner, review), only mapped agents can claim.
    For other roles, a loose fallback is allowed.
    """
    req = required_role.lower()
    aid = agent_id.lower()

    # 1. Direct match: agent_id matches the required_role
    if req == aid:
        return True

    # 2. Map-based match: check if agent_id is in the allowed set
    allowed = _ROLE_TO_AGENTS.get(req)
    if allowed and aid in allowed:
        return True

    # 3. For strict roles, NO fallback — only mapped agents qualify
    if req in _STRICT_ROLES:
        return False

    # 4. Fallback for non-strict roles: agent_id contains the keyword
    if req in aid:
        return True

    return False


class TaskStatus(str, Enum):
    PENDING   = "pending"
    CLAIMED   = "claimed"
    REVIEW    = "review"      # waiting for peer review
    CRITIQUE  = "critique"    # advisor gave fix suggestions, awaiting executor revision
    COMPLETED = "completed"
    FAILED    = "failed"
    BLOCKED   = "blocked"     # waiting for dependency
    CANCELLED = "cancelled"   # user-cancelled
    PAUSED    = "paused"      # user-paused (resumable)


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
    review_submitted_at: Optional[float] = None  # when sent to review
    retry_count:     int = 0                      # number of retries
    review_scores:   list[dict] = field(default_factory=list)   # [{reviewer, score, comment}]
    evolution_flags: list[str] = field(default_factory=list)    # error tags
    complexity:      str = "normal"                             # "simple" | "normal" | "complex"
    critique:        dict | None = None                         # {reviewer, passed, suggestions, comment, ts}
    critique_round:  int = 0                                    # current revision round (max=1)
    parent_id:       Optional[str] = None                       # parent task ID for subtask tree

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
            "review_submitted_at": self.review_submitted_at,
            "retry_count":    self.retry_count,
            "review_scores":  self.review_scores,
            "evolution_flags":self.evolution_flags,
            "complexity":     self.complexity,
            "critique":       self.critique,
            "critique_round": self.critique_round,
            "parent_id":      self.parent_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        raw_status = d.get("status", "pending")
        # Handle unknown status values from older data gracefully
        try:
            d["status"] = TaskStatus(raw_status)
        except ValueError:
            d["status"] = TaskStatus.PENDING
        # Handle older task dicts that may lack newer fields
        d.setdefault("required_role", None)
        d.setdefault("review_submitted_at", None)
        d.setdefault("retry_count", 0)
        d.setdefault("complexity", "normal")
        d.setdefault("critique", None)
        d.setdefault("critique_round", 0)
        d.setdefault("parent_id", None)
        # Remove internal bookkeeping fields not in dataclass
        d.pop("_paused_from", None)
        d.pop("partial_result", None)
        d.pop("cost_usd", None)
        # Remove any other unknown keys to prevent __init__ errors
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        for key in list(d.keys()):
            if key not in known_fields:
                d.pop(key)
        return cls(**d)


class TaskBoard:
    """
    File-backed task store.
    All mutating methods acquire a file lock — safe for concurrent agent processes.
    Includes timeout recovery: stale CLAIMED/REVIEW tasks auto-return to PENDING.
    """

    def __init__(self, path: str = BOARD_FILE):
        self.path = path
        self.lock = FileLock(BOARD_LOCK)
        # Fix TOCTOU: init under lock
        with self.lock:
            if not os.path.exists(path):
                self._write({})

    # ── Create ───────────────────────────────────────────────────────────────

    def create(self, description: str,
               blocked_by: list[str] | None = None,
               min_reputation: int = 0,
               required_role: str | None = None,
               parent_id: str | None = None) -> Task:
        # Phase 8: full UUID instead of [:8] to prevent collisions
        task = Task(
            task_id=str(uuid.uuid4()),
            description=description,
            blocked_by=blocked_by or [],
            min_reputation=min_reputation,
            required_role=required_role,
            parent_id=parent_id,
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

                # Agent claim restrictions (prevents reviewer stealing executor tasks)
                req_role = t.get("required_role")
                if not _agent_may_claim(agent_id, req_role):
                    continue

                # Phase 6: role-based routing
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
            t = data.get(task_id)
            if not t:
                logger.warning("submit_for_review: task %s not found", task_id)
                return
            t["status"] = TaskStatus.REVIEW.value
            t["result"] = result
            t["review_submitted_at"] = time.time()
            self._write(data)

    def add_review(self, task_id: str, reviewer_id: str,
                   score: int, comment: str):
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                logger.warning("add_review: task %s not found", task_id)
                return
            t.setdefault("review_scores", []).append({
                "reviewer": reviewer_id,
                "score":    score,
                "comment":  comment,
                "ts":       time.time(),
            })
            self._write(data)

    def add_critique(self, task_id: str, reviewer_id: str,
                     passed: bool, suggestions: list[str], comment: str,
                     score: int = 7):
        """Advisor submits structured critique with quality score.

        IMPORTANT: Reviewer is an ADVISOR, not a gatekeeper.
        Tasks are ALWAYS marked completed regardless of score.
        The planner reads scores/suggestions during final synthesis.
        """
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                logger.warning("add_critique: task %s not found", task_id)
                return
            t["critique"] = {
                "reviewer": reviewer_id,
                "passed": True,       # Always pass — reviewer is advisor, not gatekeeper
                "score": score,
                "suggestions": suggestions or [],
                "comment": comment,
                "ts": time.time(),
            }
            # Always complete — reviewer never blocks tasks
            t["status"] = TaskStatus.COMPLETED.value
            t["completed_at"] = time.time()
            self._write(data)

    def claim_critique(self, agent_id: str,
                       agent_role: str | None = None) -> Optional[Task]:
        """Executor claims a CRITIQUE task for targeted revision.
        Only the original executor can claim their own critique tasks."""
        with self.lock:
            data = self._read()
            for tid, t in data.items():
                if t["status"] != TaskStatus.CRITIQUE.value:
                    continue
                # Only the original executor can fix their own work
                if t.get("agent_id") != agent_id:
                    continue
                t["status"] = TaskStatus.CLAIMED.value
                t["claimed_at"] = time.time()
                self._write(data)
                return Task.from_dict(t)
        return None

    def complete(self, task_id: str) -> Optional[Task]:
        """Mark task as completed. Simplified: no score-based rejection."""
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                logger.warning("complete: task %s not found", task_id)
                return None
            t["status"]       = TaskStatus.COMPLETED.value
            t["completed_at"] = time.time()
            self._write(data)
            return Task.from_dict(t)

    def fail(self, task_id: str, reason: str = ""):
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                logger.warning("fail: task %s not found", task_id)
                return
            t["status"] = TaskStatus.FAILED.value
            t.setdefault("evolution_flags", []).append(f"failed:{reason}")
            self._write(data)

    def flag(self, task_id: str, tag: str):
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                return
            t.setdefault("evolution_flags", []).append(tag)
            self._write(data)

    # ── Streaming partial results ──────────────────────────────────────────

    def update_partial(self, task_id: str, partial: str):
        """Update partial result for a task (for streaming output to dashboard)."""
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                return
            t["partial_result"] = partial
            self._write(data)

    def set_cost(self, task_id: str, cost_usd: float):
        """Record estimated cost for a task (displayed in dashboard)."""
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                return
            t["cost_usd"] = round(
                t.get("cost_usd", 0) + cost_usd, 6
            )
            self._write(data)

    # ── Cancel / Pause / Resume / Retry ───────────────────────────────────

    def cancel(self, task_id: str) -> bool:
        """Cancel a task. Returns True if cancelled, False if not cancellable."""
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                return False
            # Can only cancel non-terminal tasks
            if t["status"] in (TaskStatus.COMPLETED.value,
                               TaskStatus.CANCELLED.value):
                return False
            t["status"] = TaskStatus.CANCELLED.value
            t["completed_at"] = time.time()
            t.setdefault("evolution_flags", []).append("user_cancelled")
            self._write(data)
            return True

    def pause(self, task_id: str) -> bool:
        """Pause a pending/claimed task. Returns True if paused."""
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                return False
            if t["status"] not in (TaskStatus.PENDING.value,
                                   TaskStatus.CLAIMED.value):
                return False
            t["_paused_from"] = t["status"]  # remember original state
            t["status"] = TaskStatus.PAUSED.value
            self._write(data)
            return True

    def resume(self, task_id: str) -> bool:
        """Resume a paused task back to PENDING."""
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                return False
            if t["status"] != TaskStatus.PAUSED.value:
                return False
            t["status"]    = TaskStatus.PENDING.value
            t["agent_id"]  = None
            t["claimed_at"] = None
            t.pop("_paused_from", None)
            self._write(data)
            return True

    def retry(self, task_id: str) -> bool:
        """Retry a failed/cancelled task. Resets it to PENDING."""
        with self.lock:
            data = self._read()
            t = data.get(task_id)
            if not t:
                return False
            if t["status"] not in (TaskStatus.FAILED.value,
                                   TaskStatus.CANCELLED.value):
                return False
            t["status"]    = TaskStatus.PENDING.value
            t["agent_id"]  = None
            t["claimed_at"] = None
            t["completed_at"] = None
            t["review_submitted_at"] = None
            t["result"]    = None
            t["review_scores"] = []
            t.setdefault("retry_count", 0)
            t["retry_count"] += 1
            self._write(data)
            return True

    # ── Timeout Recovery ──────────────────────────────────────────────────

    def recover_stale_tasks(self) -> list[str]:
        """
        Recover stale CLAIMED/REVIEW tasks back to PENDING.
        Called periodically by the orchestrator or agent loop.
        Returns list of recovered task IDs.
        """
        recovered = []
        now = time.time()
        with self.lock:
            data = self._read()
            changed = False
            for tid, t in data.items():
                status = t.get("status")
                # Stale CLAIMED: agent crashed or hung
                if status == TaskStatus.CLAIMED.value:
                    claimed_at = t.get("claimed_at", 0)
                    if claimed_at and (now - claimed_at) > CLAIMED_TIMEOUT:
                        t["status"]    = TaskStatus.PENDING.value
                        t["agent_id"]  = None
                        t["claimed_at"] = None
                        t.setdefault("retry_count", 0)
                        t["retry_count"] += 1
                        t.setdefault("evolution_flags", []).append(
                            "timeout_recovered:claimed")
                        recovered.append(tid)
                        changed = True
                        logger.warning(
                            "Recovered stale CLAIMED task %s (age=%.0fs)",
                            tid, now - claimed_at)
                # Stale REVIEW: reviewer/advisor crashed
                elif status == TaskStatus.REVIEW.value:
                    review_at = t.get("review_submitted_at") or t.get("claimed_at", 0)
                    if review_at and (now - review_at) > REVIEW_TIMEOUT:
                        # No critique arrived — auto-complete with existing result
                        t["status"]       = TaskStatus.COMPLETED.value
                        t["completed_at"] = time.time()
                        t.setdefault("evolution_flags", []).append(
                            "timeout_recovered:review")
                        recovered.append(tid)
                        changed = True
                        logger.warning(
                            "Recovered stale REVIEW task %s (age=%.0fs)",
                            tid, now - review_at)
                # Stale CRITIQUE: executor didn't pick up revision
                elif status == TaskStatus.CRITIQUE.value:
                    critique_ts = (t.get("critique") or {}).get("ts", 0)
                    if critique_ts and (now - critique_ts) > CLAIMED_TIMEOUT:
                        # Force complete with original result
                        t["status"]       = TaskStatus.COMPLETED.value
                        t["completed_at"] = time.time()
                        t.setdefault("evolution_flags", []).append(
                            "timeout_recovered:critique")
                        recovered.append(tid)
                        changed = True
                        logger.warning(
                            "Recovered stale CRITIQUE task %s (age=%.0fs)",
                            tid, now - critique_ts)
            if changed:
                self._write(data)
        return recovered

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
        1. All non-planner completed tasks (executor output) — with attribution
        2. Falls back to planner output if no executor results exist

        Each result section includes the producing agent for traceability.
        """
        data = self._read()
        planner_result = None
        planner_agent = ""
        executor_results: list[dict] = []

        for tid, t in data.items():
            if not t.get("result"):
                continue
            agent = t.get("agent_id", "")
            desc  = t.get("description", "")[:80]
            if agent.lower() in ("leo", "planner") or "planner" in agent.lower():
                planner_result = t["result"]
                planner_agent = agent
            else:
                executor_results.append({
                    "agent_id": agent,
                    "description": desc,
                    "result": t["result"],
                    "task_id": tid,
                })

        # Prefer executor results (the actual implementation) — with attribution
        if executor_results:
            parts = []
            for r in executor_results:
                header = f"<!-- agent:{r['agent_id']} task:{r['task_id'][:8]} -->"
                parts.append(f"{header}\n{r['result']}")
            return "\n\n---\n\n".join(parts)

        # If no executor results, fall back to planner output
        if planner_result:
            return planner_result

        # Last resort: any result from root task
        root = data.get(root_task_id)
        if root and root.get("result"):
            return root["result"]

        return ""

    def collect_results_with_critiques(self, root_task_id: str,
                                        subtask_ids: list[str] | None = None,
                                        ) -> tuple[str, str]:
        """Collect executor results AND reviewer critiques for planner close-out.

        Returns (results_text, critique_text) tuple:
          - results_text: executor outputs formatted as markdown
          - critique_text: reviewer scores + suggestions formatted as markdown

        The planner uses both during final synthesis to produce a polished answer.
        """
        data = self._read()
        results_parts: list[str] = []
        critique_parts: list[str] = []

        # Determine which tasks to collect from
        target_ids = subtask_ids or []
        if not target_ids:
            # Fallback: all tasks with results (except planner's own)
            target_ids = list(data.keys())

        for tid in target_ids:
            t = data.get(tid)
            if not t or not t.get("result"):
                continue
            agent = t.get("agent_id", "")
            # Skip planner's own decomposition output
            if agent.lower() in ("leo", "planner") or "planner" in agent.lower():
                continue

            desc = t.get("description", "")[:100]

            # Executor result
            results_parts.append(
                f"### Subtask: {desc}\n"
                f"**Agent:** {agent}\n\n"
                f"{t['result']}"
            )

            # Reviewer critique (if exists)
            critique = t.get("critique")
            if critique:
                score = critique.get("score", "N/A")
                comment = critique.get("comment", "")
                suggestions = critique.get("suggestions", [])
                critique_entry = (
                    f"### Subtask: {desc}\n"
                    f"**Score:** {score}/10 | **Reviewer:** {critique.get('reviewer', 'unknown')}\n"
                )
                if comment:
                    critique_entry += f"**Comment:** {comment}\n"
                if suggestions:
                    critique_entry += "**Suggestions:**\n"
                    for s in suggestions:
                        critique_entry += f"- {s}\n"
                critique_parts.append(critique_entry)

        results_text = "\n\n---\n\n".join(results_parts) if results_parts else "(no executor results)"
        critique_text = "\n\n".join(critique_parts) if critique_parts else "(no reviewer feedback)"

        return results_text, critique_text

    def clear(self, force: bool = False) -> int:
        """Remove all tasks. Returns count of removed tasks.
        If force=False and there are active tasks, does NOT clear and returns -1.
        """
        with self.lock:
            data = self._read()
            if not force:
                active_states = {"pending", "claimed", "review", "critique", "blocked", "paused"}
                active = sum(1 for t in data.values()
                             if t.get("status") in active_states)
                if active > 0:
                    return -1  # signal: active tasks exist, need confirmation
            count = len(data)
            self._write({})
            return count

    def cancel_all(self) -> int:
        """Cancel all non-terminal tasks. Returns count cancelled."""
        cancelled = 0
        with self.lock:
            data = self._read()
            terminal = {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value,
                        TaskStatus.FAILED.value}
            for tid, t in data.items():
                if t.get("status") not in terminal:
                    t["status"] = TaskStatus.CANCELLED.value
                    t["completed_at"] = time.time()
                    t.setdefault("evolution_flags", []).append("user_cancelled")
                    cancelled += 1
            if cancelled:
                self._write(data)
        return cancelled

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

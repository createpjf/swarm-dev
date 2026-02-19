"""
reputation/evolution.py
Evolution Engine: triggered when agent reputation drops below threshold.
Three paths:
  A — Prompt upgrade   (automated)
  B — Model swap       (leader confirmation)
  C — Role restructure (team vote)
"""

from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from reputation.scorer import ScoreAggregator
    from core.task_board import TaskBoard

# Phase 7: file locks for all shared state
try:
    from filelock import FileLock
except ImportError:
    class FileLock:  # type: ignore
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

logger = logging.getLogger(__name__)

EVOLUTION_LOG    = "memory/evolution_log.jsonl"
AGENT_CONFIG_DIR = "config"
PENDING_DIR      = "memory/pending_evolution"


@dataclass
class EvolutionPlan:
    agent_id:             str
    root_cause:           str
    error_patterns:       list[str]
    recommended_path:     str        # "prompt" | "model" | "role"
    prompt_upgrade:       Optional[dict] = None   # {new_prompt, changelog}
    model_swap:           Optional[dict] = None   # {new_model, reason}
    role_restructure:     Optional[dict] = None   # {proposal}
    confidence:           float = 0.0
    expected_improvement: str = ""


class EvolutionEngine:

    def __init__(self, scorer: "ScoreAggregator", board: "TaskBoard"):
        self.scorer = scorer
        self.board  = board
        os.makedirs("memory", exist_ok=True)
        os.makedirs(PENDING_DIR, exist_ok=True)

    # ── File-backed pending state (Phase 8 fix) ──────────────────────────────

    def _is_pending(self, agent_id: str) -> bool:
        return os.path.exists(os.path.join(PENDING_DIR, f"{agent_id}.json"))

    def _mark_pending(self, agent_id: str, plan: EvolutionPlan):
        path = os.path.join(PENDING_DIR, f"{agent_id}.json")
        with open(path, "w") as f:
            json.dump({
                "agent_id": agent_id,
                "path":     plan.recommended_path,
                "ts":       time.time(),
            }, f, indent=2)

    def _clear_pending(self, agent_id: str):
        path = os.path.join(PENDING_DIR, f"{agent_id}.json")
        if os.path.exists(path):
            os.remove(path)

    def _get_lock(self, path: str) -> FileLock:
        return FileLock(path + ".lock")

    # ── Trigger ───────────────────────────────────────────────────────────────

    async def maybe_trigger(self, agent_id: str, status: str):
        """
        Called by ReputationScheduler when threshold is breached.
        Uses file-backed pending state with lock to prevent cross-process
        re-triggering (Phase 8 TOCTOU fix).
        """
        pending_path = os.path.join(PENDING_DIR, f"{agent_id}.json")
        lock = self._get_lock(pending_path)

        if status == "evolve":
            with lock:
                if self._is_pending(agent_id):
                    return
                plan = await self._diagnose(agent_id)
                self._mark_pending(agent_id, plan)
            # Execute outside the lock (may involve file I/O)
            await self._execute(agent_id, plan)

        elif status == "warning":
            logger.warning(
                "[evolution] WARNING %s score=%.1f trend=%s — monitoring",
                agent_id,
                self.scorer.get(agent_id),
                self.scorer.trend(agent_id),
            )

    # ── Diagnosis ────────────────────────────────────────────────────────────

    async def _diagnose(self, agent_id: str) -> EvolutionPlan:
        history  = self.board.history(agent_id, last=50)
        errors   = [t for t in history
                     if any("failed" in f for f in (t.evolution_flags or []))]
        reworks  = [t for t in history
                     if "review_failed" in (t.evolution_flags or [])]
        score    = self.scorer.get(agent_id)
        all_dims = self.scorer.get_all(agent_id)
        trend    = self.scorer.trend(agent_id)

        # Pattern classification (simple heuristics)
        error_patterns = []
        if len(errors) / max(len(history), 1) > 0.3:
            error_patterns.append("high_failure_rate")
        if len(reworks) / max(len(history), 1) > 0.2:
            error_patterns.append("frequent_rework")
        if all_dims.get("output_quality", 70) < 45:
            error_patterns.append("low_output_quality")
        if all_dims.get("consistency", 70) < 45:
            error_patterns.append("inconsistent_output")
        if all_dims.get("improvement_rate", 70) < 40:
            error_patterns.append("not_improving")

        # Select path based on patterns
        if "not_improving" in error_patterns and len(error_patterns) >= 2:
            path = "model"
            root = "Agent is not responding to feedback. Model capability ceiling reached."
        elif "inconsistent_output" in error_patterns:
            path = "prompt"
            root = "Output inconsistency suggests unclear role definition in system prompt."
        elif "high_failure_rate" in error_patterns:
            path = "prompt"
            root = "High failure rate on task completion — prompt constraints may be too loose."
        else:
            path = "prompt"
            root = f"General underperformance. Score: {score:.1f}, trend: {trend}."

        plan = EvolutionPlan(
            agent_id=agent_id,
            root_cause=root,
            error_patterns=error_patterns,
            recommended_path=path,
            confidence=0.75 if len(error_patterns) >= 2 else 0.5,
            expected_improvement="Score should recover above 60 within 10 tasks.",
        )

        if path == "prompt":
            plan.prompt_upgrade = self._generate_prompt_upgrade(agent_id, error_patterns)
        elif path == "model":
            plan.model_swap = {
                "new_model": "flock/qwen3-235b-thinking",
                "reason":    root,
            }
        elif path == "role":
            plan.role_restructure = {"proposal": root}

        self._log_plan(plan)
        return plan

    # ── Execution ────────────────────────────────────────────────────────────

    async def _execute(self, agent_id: str, plan: EvolutionPlan):
        if plan.recommended_path == "prompt" and plan.prompt_upgrade:
            self._apply_prompt_upgrade(agent_id, plan.prompt_upgrade)
            logger.info("[evolution] PATH A applied prompt upgrade for %s", agent_id)
            self._clear_pending(agent_id)

        elif plan.recommended_path == "model" and plan.model_swap:
            self._write_pending_swap(agent_id, plan.model_swap)
            logger.warning(
                "[evolution] PATH B pending: %s → %s  (awaiting leader confirmation)",
                agent_id, plan.model_swap["new_model"],
            )

        elif plan.recommended_path == "role" and plan.role_restructure:
            self._write_vote_request(agent_id, plan.role_restructure)
            logger.warning(
                "[evolution] PATH C pending: role restructure for %s (vote required)",
                agent_id,
            )

    # ── Path A: Prompt Upgrade ───────────────────────────────────────────────

    def _generate_prompt_upgrade(self, agent_id: str,
                                  patterns: list[str]) -> dict:
        additions = []
        if "high_failure_rate" in patterns:
            additions.append(
                "- Before starting any task, state your approach in one sentence.\n"
                "- If a task is ambiguous, ask for clarification instead of guessing."
            )
        if "inconsistent_output" in patterns:
            additions.append(
                "- Always follow the output format specified in your skills.\n"
                "- End every response with CONFIDENCE: [0-100] and NEXT: [one sentence]."
            )
        if "low_output_quality" in patterns:
            additions.append(
                "- Your recent output quality scores have been low.\n"
                "- Focus on correctness and completeness. Quality over speed."
            )
        changelog = f"Auto-upgraded by Evolution Engine. Patterns: {', '.join(patterns)}"
        return {
            "additions": "\n".join(additions),
            "changelog": changelog,
        }

    def _apply_prompt_upgrade(self, agent_id: str, upgrade: dict):
        """Append new constraints to the agent's skill document."""
        skill_path = f"skills/agent_overrides/{agent_id}.md"
        os.makedirs(os.path.dirname(skill_path), exist_ok=True)
        header = f"\n\n## Evolution Engine Override ({time.strftime('%Y-%m-%d')})\n"
        with open(skill_path, "a") as f:
            f.write(header + upgrade["additions"] + "\n")
        logger.info("[evolution] wrote override skill to %s", skill_path)

    # ── Path B: Model Swap ──────────────────────────────────────────────────

    def _write_pending_swap(self, agent_id: str, swap: dict):
        path = f"memory/pending_swaps/{agent_id}.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Phase 7: file-locked write
        lock = self._get_lock(path)
        with lock:
            with open(path, "w") as f:
                json.dump({**swap, "agent_id": agent_id, "ts": time.time()},
                          f, indent=2)

    def apply_model_swap(self, agent_id: str):
        """Called by leader/human after confirmation."""
        path = f"memory/pending_swaps/{agent_id}.json"
        if not os.path.exists(path):
            return
        lock = self._get_lock(path)
        with lock:
            with open(path, "r") as f:
                swap = json.load(f)
        cfg = self._load_agent_config(agent_id)
        old = cfg.get("model", "")
        cfg["model"] = swap["new_model"]
        self._save_agent_config(agent_id, cfg)
        os.remove(path)
        self._clear_pending(agent_id)
        logger.info("[evolution] PATH B swapped %s: %s → %s",
                    agent_id, old, swap["new_model"])

    # ── Path C: Role Vote ────────────────────────────────────────────────────

    def _write_vote_request(self, agent_id: str, restructure: dict):
        path = f"memory/pending_votes/{agent_id}.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lock = self._get_lock(path)
        with lock:
            with open(path, "w") as f:
                json.dump({**restructure, "agent_id": agent_id,
                           "ts": time.time(),
                           "votes_for": [], "votes_against": []},
                          f, indent=2)

    def cast_vote(self, agent_id: str, voter_id: str, approve: bool) -> dict:
        """Cast a vote on a Path C role restructure request."""
        path = f"memory/pending_votes/{agent_id}.json"
        lock = self._get_lock(path)
        with lock:
            if not os.path.exists(path):
                return {"error": "no pending vote", "agent_id": agent_id}

            with open(path, "r") as f:
                vote_data = json.load(f)

            all_voters = vote_data.get("votes_for", []) + vote_data.get("votes_against", [])
            if voter_id in all_voters:
                return {"error": "already voted", "voter_id": voter_id}

            if approve:
                vote_data.setdefault("votes_for", []).append(voter_id)
            else:
                vote_data.setdefault("votes_against", []).append(voter_id)

            with open(path, "w") as f:
                json.dump(vote_data, f, indent=2)

        return self._check_vote_result(agent_id, vote_data)

    def _check_vote_result(self, agent_id: str, vote_data: dict) -> dict:
        """Check if the vote has reached threshold and execute if so."""
        import yaml
        try:
            with open(os.path.join(AGENT_CONFIG_DIR, "agents.yaml"), "r") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}

        threshold = cfg.get("reputation", {}).get("role_vote_threshold", 0.6)
        total_agents = len(cfg.get("agents", []))
        if total_agents == 0:
            total_agents = 1

        votes_for = len(vote_data.get("votes_for", []))
        votes_against = len(vote_data.get("votes_against", []))
        total_votes = votes_for + votes_against
        approval_ratio = votes_for / max(total_votes, 1)

        result = {
            "agent_id": agent_id,
            "votes_for": votes_for,
            "votes_against": votes_against,
            "total_agents": total_agents,
            "threshold": threshold,
            "approval_ratio": round(approval_ratio, 2),
            "status": "pending",
        }

        if total_votes < (total_agents // 2 + 1):
            result["status"] = "waiting_for_quorum"
            return result

        if approval_ratio >= threshold:
            result["status"] = "approved"
            self._execute_role_restructure(agent_id, vote_data)
        else:
            result["status"] = "rejected"
            self._clear_vote(agent_id)
            self._clear_pending(agent_id)

        return result

    def _execute_role_restructure(self, agent_id: str, vote_data: dict):
        """Execute an approved role restructure."""
        proposal = vote_data.get("proposal", "")
        logger.info("[evolution] PATH C approved for %s: %s", agent_id, proposal)

        skill_path = f"skills/agent_overrides/{agent_id}.md"
        os.makedirs(os.path.dirname(skill_path), exist_ok=True)
        header = f"\n\n## Role Restructure ({time.strftime('%Y-%m-%d')})\n"
        content = (
            f"**Team vote approved role change.**\n"
            f"Reason: {proposal}\n\n"
            f"- Focus on simpler, well-defined tasks only.\n"
            f"- Avoid tasks requiring complex reasoning or multi-step planning.\n"
            f"- Request help from other agents when facing ambiguity.\n"
        )
        with open(skill_path, "a") as f:
            f.write(header + content)

        self._clear_vote(agent_id)
        self._clear_pending(agent_id)
        logger.info("[evolution] PATH C executed for %s", agent_id)

    def _clear_vote(self, agent_id: str):
        path = f"memory/pending_votes/{agent_id}.json"
        if os.path.exists(path):
            os.remove(path)

    def get_pending_votes(self) -> list[dict]:
        """List all pending vote requests (for dashboard)."""
        vote_dir = "memory/pending_votes"
        if not os.path.isdir(vote_dir):
            return []
        results = []
        for fname in os.listdir(vote_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(vote_dir, fname)) as f:
                    results.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
        return results

    # ── Config helpers ───────────────────────────────────────────────────────

    def _load_agent_config(self, agent_id: str) -> dict:
        import yaml
        path = os.path.join(AGENT_CONFIG_DIR, "agents.yaml")
        lock = self._get_lock(path)
        with lock:
            with open(path, "r") as f:
                cfg = yaml.safe_load(f)
        for a in cfg.get("agents", []):
            if a["id"] == agent_id:
                return a
        return {}

    def _save_agent_config(self, agent_id: str, agent_data: dict):
        import yaml
        path = os.path.join(AGENT_CONFIG_DIR, "agents.yaml")
        # Phase 7: file-locked config write
        lock = self._get_lock(path)
        with lock:
            with open(path, "r") as f:
                cfg = yaml.safe_load(f)
            for i, a in enumerate(cfg.get("agents", [])):
                if a["id"] == agent_id:
                    cfg["agents"][i] = agent_data
            with open(path, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True)

    def _log_plan(self, plan: EvolutionPlan):
        entry = json.dumps({
            "agent_id":          plan.agent_id,
            "root_cause":        plan.root_cause,
            "error_patterns":    plan.error_patterns,
            "recommended_path":  plan.recommended_path,
            "confidence":        plan.confidence,
            "ts":                time.time(),
        })
        try:
            with open(EVOLUTION_LOG, "a") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning("Failed to write evolution log: %s", e)

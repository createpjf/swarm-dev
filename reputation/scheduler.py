"""
reputation/scheduler.py
Event hooks connecting task lifecycle to reputation scoring and evolution triggers.
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.task_board import Task, TaskBoard

from reputation.scorer import ScoreAggregator
from reputation.evolution import EvolutionEngine

logger = logging.getLogger(__name__)


class ReputationScheduler:
    """
    Called by the agent loop on task lifecycle events.
    Updates reputation scores and triggers evolution when thresholds are breached.
    """

    def __init__(self, board: "TaskBoard"):
        self.board  = board
        self.scorer = ScoreAggregator()
        self.engine = EvolutionEngine(self.scorer, board)

    def get_score(self, agent_id: str) -> float:
        """Return current composite reputation score."""
        return self.scorer.get(agent_id)

    async def on_task_complete(self, agent_id: str, task: "Task",
                                result: str):
        """
        Called when an agent finishes executing a task (before review).
        Updates task_completion and output_quality dimensions.
        """
        # Task completion: 100 for normal, 70 if this is a rework
        is_rework = any("review_failed" in f for f in (task.evolution_flags or []))
        completion_signal = 70.0 if is_rework else 100.0
        self.scorer.update(agent_id, "task_completion", completion_signal)

        # Output quality: heuristic based on result length and structure
        # In production, this would be replaced by peer review scores
        quality_signal = self._heuristic_quality(result)
        self.scorer.update(agent_id, "output_quality", quality_signal)

        # Improvement rate: higher if rework succeeded
        if is_rework:
            self.scorer.update(agent_id, "improvement_rate", 85.0)
        else:
            self.scorer.update(agent_id, "improvement_rate", 70.0)

        # Check threshold and maybe trigger evolution
        await self._check_threshold(agent_id)

    async def on_error(self, agent_id: str, task_id: str, error: str):
        """
        Called when a task fails with an exception.
        Penalizes task_completion and consistency.
        """
        self.scorer.update(agent_id, "task_completion", 0.0)
        self.scorer.update(agent_id, "consistency", 30.0)

        await self._check_threshold(agent_id)

    async def on_review(self, reviewer_id: str, score: int):
        """
        Called after a reviewer submits a review (legacy).
        Updates the reviewer's review_accuracy dimension.
        """
        if 40 <= score <= 80:
            accuracy_signal = 85.0
        elif 20 <= score <= 90:
            accuracy_signal = 70.0
        else:
            accuracy_signal = 55.0
        self.scorer.update(reviewer_id, "review_accuracy", accuracy_signal)

    async def on_review_score(self, agent_id: str, review_score: int):
        """
        Called when an agent's output receives a peer review score (legacy).
        Updates the agent's output_quality dimension with the actual review.
        """
        self.scorer.update(agent_id, "output_quality", float(review_score))

    async def on_critique(self, reviewer_id: str, passed: bool,
                           score: int = 7):
        """
        Called after an advisor submits a quality score.
        Updates the reviewer's review_accuracy dimension based on score.
        Higher scores with differentiation → better calibrated reviewer.
        """
        # Score-based accuracy: moderate scores (4-8) indicate careful review
        # Extreme scores (1-2 or 9-10) are fine but less differentiating
        accuracy_signal = min(90.0, 50.0 + score * 5)
        self.scorer.update(reviewer_id, "review_accuracy", accuracy_signal)

    async def on_critique_result(self, agent_id: str, passed_first_time: bool,
                                  had_revision: bool):
        """
        Called when an executor's task is critiqued.
        Updates the agent's output_quality dimension.
        """
        if passed_first_time:
            quality_signal = 90.0
        elif had_revision:
            quality_signal = 70.0
        else:
            quality_signal = 50.0
        self.scorer.update(agent_id, "output_quality", quality_signal)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _check_threshold(self, agent_id: str):
        """Check reputation threshold and trigger evolution if needed."""
        status = self.scorer.threshold_status(agent_id)
        if status in ("warning", "evolve"):
            await self.engine.maybe_trigger(agent_id, status)

    @staticmethod
    def _heuristic_quality(result: str) -> float:
        """
        Simple heuristic for output quality.
        Production systems should use peer review scores instead.
        """
        if not result or len(result.strip()) < 10:
            return 20.0

        score = 60.0  # baseline

        # Longer, more detailed responses tend to be better
        if len(result) > 200:
            score += 10.0
        if len(result) > 500:
            score += 5.0

        # Structured output (has headers, lists, code blocks)
        if any(marker in result for marker in ["#", "- ", "```", "1."]):
            score += 10.0

        return min(score, 95.0)

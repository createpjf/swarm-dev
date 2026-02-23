"""
tests/test_scheduler.py
Sprint 5.2 — Tests for reputation/scheduler.py

Covers:
  - Rework detection with actual flags ("failed:", "timeout_recovered:")
  - Score signals for task completion and errors
  - Recovery cleanup (clear overrides when score >= 80)
  - Heuristic quality scoring
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass, field

from reputation.scheduler import ReputationScheduler


# ── Fixtures ──────────────────────────────────────────────────────────────────

@dataclass
class MockTask:
    task_id: str = "t-1"
    description: str = "test task"
    agent_id: str = "leo"
    status: str = "completed"
    evolution_flags: list = field(default_factory=list)


class MockBoard:
    def history(self, agent_id, last=50):
        return []


@pytest.fixture
def scheduler(tmp_path):
    """Create scheduler with isolated temp scorer paths.

    Note: We can't patch CACHE_FILE because Python default args are
    bound at definition time, not call time. Instead, construct manually.
    """
    from reputation.scorer import ScoreAggregator
    from reputation.evolution import EvolutionEngine

    board = MockBoard()
    scorer = ScoreAggregator(
        cache_path=str(tmp_path / "cache.json"),
        log_path=str(tmp_path / "log.jsonl"),
    )
    sched = ReputationScheduler.__new__(ReputationScheduler)
    sched.board = board
    sched.scorer = scorer
    sched.engine = EvolutionEngine(scorer, board)
    return sched


# ══════════════════════════════════════════════════════════════════════════════
#  REWORK DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestReworkDetection:

    @pytest.mark.asyncio
    async def test_failed_prefix_is_rework(self, scheduler):
        """Task with 'failed:reason' flag should get rework score (70)."""
        task = MockTask(evolution_flags=["failed:timeout"])

        with patch.object(scheduler, '_check_threshold', new_callable=AsyncMock):
            await scheduler.on_task_complete("leo", task, "result text")

        # task_completion should be 70.0 (rework), not 100.0
        dims = scheduler.scorer.get_all("leo")
        # After 1 update: EMA from 70 with signal 70 = 70 (no change)
        assert dims["task_completion"] == 70.0

    @pytest.mark.asyncio
    async def test_timeout_recovered_is_rework(self, scheduler):
        """Task with 'timeout_recovered:state' flag should get rework score."""
        task = MockTask(evolution_flags=["timeout_recovered:claimed"])

        with patch.object(scheduler, '_check_threshold', new_callable=AsyncMock):
            await scheduler.on_task_complete("leo", task, "result text")

        dims = scheduler.scorer.get_all("leo")
        assert dims["task_completion"] == 70.0

    @pytest.mark.asyncio
    async def test_normal_task_is_not_rework(self, scheduler):
        """Task with no failure flags should get full score (100)."""
        task = MockTask(evolution_flags=[])

        with patch.object(scheduler, '_check_threshold', new_callable=AsyncMock):
            await scheduler.on_task_complete("leo", task, "result text")

        dims = scheduler.scorer.get_all("leo")
        # EMA: 0.3 * 100 + 0.7 * 70 = 79
        expected = 0.3 * 100.0 + 0.7 * 70.0
        assert abs(dims["task_completion"] - expected) < 0.1

    @pytest.mark.asyncio
    async def test_old_review_failed_is_not_rework(self, scheduler):
        """Old 'review_failed' flag should NOT trigger rework scoring."""
        task = MockTask(evolution_flags=["review_failed"])

        with patch.object(scheduler, '_check_threshold', new_callable=AsyncMock):
            await scheduler.on_task_complete("leo", task, "result text")

        dims = scheduler.scorer.get_all("leo")
        # Should be 79 (normal), not 70 (rework)
        expected = 0.3 * 100.0 + 0.7 * 70.0
        assert abs(dims["task_completion"] - expected) < 0.1


# ══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLING
# ══════════════════════════════════════════════════════════════════════════════

class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_error_penalizes_completion(self, scheduler):
        """on_error should set task_completion to 0."""
        with patch.object(scheduler, '_check_threshold', new_callable=AsyncMock):
            await scheduler.on_error("leo", "t-1", "LLM timeout")

        dims = scheduler.scorer.get_all("leo")
        # EMA: 0.3 * 0 + 0.7 * 70 = 49
        expected = 0.3 * 0.0 + 0.7 * 70.0
        assert abs(dims["task_completion"] - expected) < 0.1

    @pytest.mark.asyncio
    async def test_error_penalizes_consistency(self, scheduler):
        """on_error should also penalize consistency."""
        with patch.object(scheduler, '_check_threshold', new_callable=AsyncMock):
            await scheduler.on_error("leo", "t-1", "Error")

        dims = scheduler.scorer.get_all("leo")
        # EMA: 0.3 * 30 + 0.7 * 70 = 58
        expected = 0.3 * 30.0 + 0.7 * 70.0
        assert abs(dims["consistency"] - expected) < 0.1


# ══════════════════════════════════════════════════════════════════════════════
#  RECOVERY CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

class TestRecoveryCleanup:

    @pytest.mark.asyncio
    async def test_clears_overrides_when_recovered(self, scheduler):
        """When score >= 80, should clear evolution overrides."""
        # Push score above 80
        for _ in range(30):
            scheduler.scorer.update("leo", "task_completion", 100.0)
            scheduler.scorer.update("leo", "output_quality", 100.0)
            scheduler.scorer.update("leo", "improvement_rate", 100.0)
            scheduler.scorer.update("leo", "consistency", 100.0)
            scheduler.scorer.update("leo", "review_accuracy", 100.0)

        with patch.object(scheduler.engine, 'clear_overrides') as mock_clear, \
             patch.object(scheduler.engine, 'maybe_trigger', new_callable=AsyncMock):
            await scheduler._check_threshold("leo")

        mock_clear.assert_called_once_with("leo")


# ══════════════════════════════════════════════════════════════════════════════
#  HEURISTIC QUALITY
# ══════════════════════════════════════════════════════════════════════════════

class TestHeuristicQuality:

    def test_empty_result_low_score(self):
        assert ReputationScheduler._heuristic_quality("") == 20.0
        assert ReputationScheduler._heuristic_quality("hi") == 20.0

    def test_short_result_baseline(self):
        result = "A reasonable response with enough content."
        score = ReputationScheduler._heuristic_quality(result)
        assert score >= 60.0

    def test_long_structured_result_high_score(self):
        result = "# Heading\n" + "- Item\n" * 20 + "```code block```\n" + "x" * 500
        score = ReputationScheduler._heuristic_quality(result)
        assert score >= 80.0

    def test_score_capped_at_95(self):
        result = "# " + "x" * 1000 + "\n- list\n```code```\n1. numbered"
        score = ReputationScheduler._heuristic_quality(result)
        assert score <= 95.0

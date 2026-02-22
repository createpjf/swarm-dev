"""
tests/test_scorer.py
Sprint 5.2 — Tests for reputation/scorer.py

Covers:
  - EMA calculation correctness
  - Composite score weights
  - Threshold status boundaries
  - Trend detection (improving/declining/stable)
  - Default scores for new agents
"""

import os
import json
import tempfile
import pytest

from reputation.scorer import ScoreAggregator, WEIGHTS, ALPHA, DEFAULT_SCORE, DIMENSIONS


@pytest.fixture
def scorer(tmp_path):
    """Create a scorer with temp files to avoid polluting real data."""
    cache_path = str(tmp_path / "test_cache.json")
    log_path = str(tmp_path / "test_log.jsonl")
    return ScoreAggregator(cache_path=cache_path, log_path=log_path)


# ══════════════════════════════════════════════════════════════════════════════
#  EMA CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

class TestEMACalculation:

    def test_first_update_applies_ema_from_default(self, scorer):
        """First update: EMA from DEFAULT_SCORE (70) with signal."""
        scorer.update("agent-1", "task_completion", 100.0)
        dims = scorer.get_all("agent-1")
        expected = ALPHA * 100.0 + (1 - ALPHA) * DEFAULT_SCORE  # 0.3*100 + 0.7*70 = 79
        assert abs(dims["task_completion"] - expected) < 0.1

    def test_multiple_updates_converge(self, scorer):
        """Repeated high signals should push score toward 100."""
        for _ in range(20):
            scorer.update("agent-1", "task_completion", 100.0)
        dims = scorer.get_all("agent-1")
        # After 20 updates of 100.0 with alpha=0.3, should be very close to 100
        assert dims["task_completion"] > 98.0

    def test_ema_decay_on_low_signal(self, scorer):
        """After good scores, bad signals should pull score down."""
        # Build up
        for _ in range(10):
            scorer.update("agent-1", "output_quality", 95.0)
        high = scorer.get_all("agent-1")["output_quality"]

        # Pull down
        for _ in range(5):
            scorer.update("agent-1", "output_quality", 20.0)
        low = scorer.get_all("agent-1")["output_quality"]

        assert low < high
        assert low < 70  # Should be significantly lower

    def test_unknown_dimension_ignored(self, scorer):
        """Unknown dimension should be silently ignored."""
        scorer.update("agent-1", "nonexistent_dim", 50.0)
        dims = scorer.get_all("agent-1")
        assert "nonexistent_dim" not in dims


# ══════════════════════════════════════════════════════════════════════════════
#  COMPOSITE SCORE
# ══════════════════════════════════════════════════════════════════════════════

class TestCompositeScore:

    def test_default_composite_is_70(self, scorer):
        """New agent should have default score of 70."""
        assert scorer.get("unknown-agent") == DEFAULT_SCORE

    def test_composite_reflects_weights(self, scorer):
        """Composite should be weighted sum of dimensions."""
        # Set all dimensions to specific values
        scorer.update("agent-1", "task_completion", 100.0)
        scorer.update("agent-1", "output_quality", 100.0)
        scorer.update("agent-1", "improvement_rate", 100.0)
        scorer.update("agent-1", "consistency", 100.0)
        scorer.update("agent-1", "review_accuracy", 100.0)

        # After one update each: new = 0.3*100 + 0.7*70 = 79 per dim
        # Composite = 79 * (0.25+0.30+0.25+0.10+0.10) = 79 * 1.0 = 79
        score = scorer.get("agent-1")
        assert 78.0 < score < 80.0  # Close to 79

    def test_weights_sum_to_one(self):
        """Sanity check: all weights should sum to 1.0."""
        assert abs(sum(WEIGHTS.values()) - 1.0) < 0.001

    def test_all_five_dimensions_present(self):
        assert len(DIMENSIONS) == 5
        assert set(DIMENSIONS) == {
            "task_completion", "output_quality", "improvement_rate",
            "consistency", "review_accuracy"
        }


# ══════════════════════════════════════════════════════════════════════════════
#  THRESHOLD STATUS
# ══════════════════════════════════════════════════════════════════════════════

class TestThresholdStatus:

    def test_default_is_watch(self, scorer):
        """Default 70 → watch status."""
        assert scorer.threshold_status("new-agent") == "watch"

    def test_healthy_above_80(self, scorer):
        """Score >= 80 → healthy."""
        # Push score above 80
        for _ in range(30):
            scorer.update("agent-1", "task_completion", 100.0)
            scorer.update("agent-1", "output_quality", 100.0)
            scorer.update("agent-1", "improvement_rate", 100.0)
            scorer.update("agent-1", "consistency", 100.0)
            scorer.update("agent-1", "review_accuracy", 100.0)
        assert scorer.threshold_status("agent-1") == "healthy"

    def test_evolve_below_40(self, scorer):
        """Score < 40 → evolve."""
        for _ in range(30):
            scorer.update("agent-1", "task_completion", 0.0)
            scorer.update("agent-1", "output_quality", 0.0)
            scorer.update("agent-1", "improvement_rate", 0.0)
            scorer.update("agent-1", "consistency", 0.0)
            scorer.update("agent-1", "review_accuracy", 0.0)
        assert scorer.threshold_status("agent-1") == "evolve"


# ══════════════════════════════════════════════════════════════════════════════
#  TREND DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestTrendDetection:

    def test_stable_with_no_history(self, scorer):
        assert scorer.trend("unknown-agent") == "stable"

    def test_improving_trend(self, scorer):
        """Rising scores should show 'improving'."""
        # First, add low scores
        for _ in range(5):
            scorer.update("agent-1", "task_completion", 30.0)
        # Then add high scores
        for _ in range(5):
            scorer.update("agent-1", "task_completion", 95.0)
        trend = scorer.trend("agent-1")
        assert trend == "improving"

    def test_declining_trend(self, scorer):
        """Falling scores should show 'declining'."""
        for _ in range(5):
            scorer.update("agent-1", "task_completion", 95.0)
        for _ in range(5):
            scorer.update("agent-1", "task_completion", 20.0)
        trend = scorer.trend("agent-1")
        assert trend == "declining"


# ══════════════════════════════════════════════════════════════════════════════
#  PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

class TestPersistence:

    def test_scores_persist_across_instances(self, tmp_path):
        """Scores should survive recreating the scorer instance."""
        cache_path = str(tmp_path / "cache.json")
        log_path = str(tmp_path / "log.jsonl")

        s1 = ScoreAggregator(cache_path=cache_path, log_path=log_path)
        s1.update("agent-1", "task_completion", 100.0)
        score_before = s1.get("agent-1")

        s2 = ScoreAggregator(cache_path=cache_path, log_path=log_path)
        score_after = s2.get("agent-1")

        assert abs(score_before - score_after) < 0.01

    def test_log_file_written(self, tmp_path):
        """Score updates should be logged to JSONL file."""
        cache_path = str(tmp_path / "cache.json")
        log_path = str(tmp_path / "log.jsonl")

        s = ScoreAggregator(cache_path=cache_path, log_path=log_path)
        s.update("agent-1", "output_quality", 80.0)

        assert os.path.exists(log_path)
        with open(log_path) as f:
            line = f.readline()
        entry = json.loads(line)
        assert entry["agent_id"] == "agent-1"
        assert entry["dimension"] == "output_quality"

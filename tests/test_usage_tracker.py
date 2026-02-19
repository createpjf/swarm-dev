"""
tests/test_usage_tracker.py
Usage tracking and budget enforcement tests.
"""

import json
import os
import pytest
from core.usage_tracker import UsageTracker, BudgetExceeded


class TestUsageTracking:
    """Basic usage recording and aggregation."""

    def test_record_and_summary(self, tmp_workdir):
        tracker = UsageTracker()
        tracker.record("executor", "test-model",
                        prompt_tokens=100, completion_tokens=50)

        summary = tracker.get_summary()
        agg = summary["aggregate"]
        assert agg["total_calls"] == 1
        assert agg["total_tokens"] == 150
        assert agg["success_count"] == 1

    def test_per_agent_breakdown(self, tmp_workdir):
        tracker = UsageTracker()
        tracker.record("planner", "model-a", prompt_tokens=100, completion_tokens=50)
        tracker.record("executor", "model-b", prompt_tokens=200, completion_tokens=100)

        summary = tracker.get_summary()
        assert "planner" in summary["by_agent"]
        assert "executor" in summary["by_agent"]
        assert summary["by_agent"]["planner"]["tokens"] == 150
        assert summary["by_agent"]["executor"]["tokens"] == 300

    def test_clear(self, tmp_workdir):
        tracker = UsageTracker()
        tracker.record("executor", "model", prompt_tokens=100, completion_tokens=50)
        tracker.clear()
        summary = tracker.get_summary()
        assert summary["aggregate"] == {}


class TestBudgetEnforcement:
    """Budget limits and alerts."""

    def test_no_budget_by_default(self, tmp_workdir):
        tracker = UsageTracker()
        budget = UsageTracker.get_budget()
        assert budget.get("enabled", False) is False

    def test_set_budget(self, tmp_workdir):
        budget = UsageTracker.set_budget(
            max_cost_usd=1.00, max_tokens=100000, warn_at_percent=80)
        assert budget["enabled"] is True
        assert budget["max_cost_usd"] == 1.00

        # Verify persisted
        budget2 = UsageTracker.get_budget()
        assert budget2["max_cost_usd"] == 1.00

    def test_budget_exceeded_raises(self, tmp_workdir):
        UsageTracker.set_budget(max_cost_usd=0.0001, enabled=True)
        tracker = UsageTracker()

        with pytest.raises(BudgetExceeded):
            # Record a call that pushes cost over limit
            tracker.record("executor", "qwen3-235b-thinking",
                           prompt_tokens=100000, completion_tokens=50000)

    def test_budget_warning_creates_alert(self, tmp_workdir):
        UsageTracker.set_budget(max_cost_usd=10.0, warn_at_percent=10,
                                enabled=True)
        tracker = UsageTracker()
        # Record enough to trigger warning (>10% of $10 = $1)
        tracker.record("executor", "qwen3-235b-thinking",
                        prompt_tokens=500000, completion_tokens=200000)

        alerts = UsageTracker.get_alerts()
        assert any(a["type"] == "budget_warning" for a in alerts)

    def test_budget_percent_used(self, tmp_workdir):
        # Set budget high enough to not raise, but check percent > 0
        UsageTracker.set_budget(max_cost_usd=10.0, warn_at_percent=99,
                                enabled=True)
        tracker = UsageTracker()
        # Use big token count to ensure measurable cost
        tracker.record("executor", "qwen3-235b-thinking",
                        prompt_tokens=1000000, completion_tokens=500000)

        budget = UsageTracker.get_budget()
        assert budget["current_cost_usd"] > 0
        assert budget["percent_used"] > 0

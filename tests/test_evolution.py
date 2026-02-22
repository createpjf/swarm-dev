"""
tests/test_evolution.py
Sprint 5.2 — Tests for reputation/evolution.py

Covers:
  - Ghost flag fix: _diagnose detects "failed:" prefix (not "review_failed")
  - Path B model swap reads fallback_models from config
  - Override dedup, cap (MAX_OVERRIDES=3), and cleanup
  - Path selection logic
"""

import os
import json
import tempfile
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass, field

from reputation.evolution import EvolutionEngine, EvolutionPlan


# ── Fixtures ──────────────────────────────────────────────────────────────────

@dataclass
class MockTask:
    task_id: str = "t-1"
    description: str = "test task"
    agent_id: str = "leo"
    status: str = "completed"
    evolution_flags: list = field(default_factory=list)


class MockBoard:
    def __init__(self, tasks=None):
        self._tasks = tasks or []

    def history(self, agent_id, last=50):
        return self._tasks[:last]


class MockScorer:
    def __init__(self, score=70.0, dims=None, trend="stable"):
        self._score = score
        self._dims = dims or {}
        self._trend = trend

    def get(self, agent_id):
        return self._score

    def get_all(self, agent_id):
        return dict(self._dims)

    def trend(self, agent_id):
        return self._trend


# ══════════════════════════════════════════════════════════════════════════════
#  GHOST FLAG FIX (YELLOW-1)
# ══════════════════════════════════════════════════════════════════════════════

class TestGhostFlagFix:
    """Verify _diagnose correctly detects actual flags, not "review_failed"."""

    @pytest.mark.asyncio
    async def test_detects_failed_prefix(self):
        """Tasks with 'failed:reason' should be counted as reworks."""
        tasks = [
            MockTask(evolution_flags=["failed:timeout"]),
            MockTask(evolution_flags=["failed:llm_error"]),
            MockTask(evolution_flags=[]),  # normal
            MockTask(evolution_flags=[]),
            MockTask(evolution_flags=[]),
        ]
        scorer = MockScorer(score=35.0, dims={"output_quality": 40, "consistency": 40,
                                               "improvement_rate": 35})
        board = MockBoard(tasks)
        engine = EvolutionEngine(scorer, board)

        plan = await engine._diagnose("leo")
        # 2/5 = 40% rework rate > 20% threshold → should detect frequent_rework
        assert "frequent_rework" in plan.error_patterns

    @pytest.mark.asyncio
    async def test_detects_timeout_recovered(self):
        """Tasks with 'timeout_recovered:state' should be counted as reworks."""
        tasks = [
            MockTask(evolution_flags=["timeout_recovered:claimed"]),
            MockTask(evolution_flags=["timeout_recovered:review"]),
            MockTask(evolution_flags=[]),
            MockTask(evolution_flags=[]),
        ]
        scorer = MockScorer(score=35.0, dims={"output_quality": 40, "consistency": 40,
                                               "improvement_rate": 35})
        board = MockBoard(tasks)
        engine = EvolutionEngine(scorer, board)

        plan = await engine._diagnose("leo")
        assert "frequent_rework" in plan.error_patterns

    @pytest.mark.asyncio
    async def test_old_review_failed_not_counted(self):
        """The old 'review_failed' flag should NOT be counted."""
        tasks = [
            MockTask(evolution_flags=["review_failed"]),  # old ghost flag
            MockTask(evolution_flags=[]),
            MockTask(evolution_flags=[]),
            MockTask(evolution_flags=[]),
            MockTask(evolution_flags=[]),
        ]
        scorer = MockScorer(score=65.0, dims={})
        board = MockBoard(tasks)
        engine = EvolutionEngine(scorer, board)

        plan = await engine._diagnose("leo")
        # 1/5 = 20% — exactly at threshold, but "review_failed" doesn't match
        # startswith("failed:") or startswith("timeout_recovered:")
        assert "frequent_rework" not in plan.error_patterns


# ══════════════════════════════════════════════════════════════════════════════
#  PATH B MODEL SWAP (YELLOW-2)
# ══════════════════════════════════════════════════════════════════════════════

class TestPathBModelSwap:

    def test_pick_fallback_model_from_config(self, tmp_path):
        """Should read fallback_models from agents.yaml."""
        import yaml
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        agents_yaml = {
            "agents": [
                {
                    "id": "leo",
                    "model": "MiniMax-M2.5-highspeed",
                    "fallback_models": ["MiniMax-M2.5", "MiniMax-M2.1"],
                }
            ]
        }
        with open(config_dir / "agents.yaml", "w") as f:
            yaml.dump(agents_yaml, f)

        scorer = MockScorer()
        board = MockBoard()
        engine = EvolutionEngine(scorer, board)

        # Patch AGENT_CONFIG_DIR
        import reputation.evolution as evo
        old_dir = evo.AGENT_CONFIG_DIR
        evo.AGENT_CONFIG_DIR = str(config_dir)
        try:
            model = engine._pick_fallback_model("leo")
            # Should pick first fallback != current model
            assert model == "MiniMax-M2.5"
        finally:
            evo.AGENT_CONFIG_DIR = old_dir

    def test_pick_fallback_skips_current_model(self, tmp_path):
        """Should skip fallback models that match current model."""
        import yaml
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        agents_yaml = {
            "agents": [
                {
                    "id": "jerry",
                    "model": "minimax-m2.5",
                    "fallback_models": ["minimax-m2.5", "gpt-4o-mini"],
                }
            ]
        }
        with open(config_dir / "agents.yaml", "w") as f:
            yaml.dump(agents_yaml, f)

        scorer = MockScorer()
        board = MockBoard()
        engine = EvolutionEngine(scorer, board)

        import reputation.evolution as evo
        old_dir = evo.AGENT_CONFIG_DIR
        evo.AGENT_CONFIG_DIR = str(config_dir)
        try:
            model = engine._pick_fallback_model("jerry")
            assert model == "gpt-4o-mini"
        finally:
            evo.AGENT_CONFIG_DIR = old_dir

    def test_fallback_default_when_no_config(self):
        """Should return default model when config is missing."""
        scorer = MockScorer()
        board = MockBoard()
        engine = EvolutionEngine(scorer, board)

        import reputation.evolution as evo
        old_dir = evo.AGENT_CONFIG_DIR
        evo.AGENT_CONFIG_DIR = "/nonexistent/path"
        try:
            model = engine._pick_fallback_model("unknown-agent")
            assert model == "minimax-m2.5"
        finally:
            evo.AGENT_CONFIG_DIR = old_dir


# ══════════════════════════════════════════════════════════════════════════════
#  OVERRIDE DEDUP + CAP + CLEANUP (YELLOW-3)
# ══════════════════════════════════════════════════════════════════════════════

class TestOverrideManagement:

    def test_dedup_skips_identical_override(self, tmp_path):
        """Identical override additions should not be written twice."""
        scorer = MockScorer()
        board = MockBoard()
        engine = EvolutionEngine(scorer, board)

        skill_dir = tmp_path / "skills" / "agent_overrides"
        skill_dir.mkdir(parents=True)
        skill_path = str(skill_dir / "leo.md")

        # Patch the skill path
        upgrade = {"additions": "- Focus on quality over speed."}

        # Write first time
        with patch.object(engine, '_apply_prompt_upgrade') as mock:
            mock.side_effect = lambda aid, up: EvolutionEngine._apply_prompt_upgrade(engine, aid, up)

        # Write directly
        import reputation.evolution as evo
        # Temporarily override skill path construction
        original_apply = engine._apply_prompt_upgrade

        def patched_apply(agent_id, upgrade):
            sp = str(skill_dir / f"{agent_id}.md")
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            # Use the same logic but with custom path
            additions = upgrade["additions"]
            existing = ""
            if os.path.exists(sp):
                with open(sp, "r") as f:
                    existing = f.read()
            if additions.strip() in existing:
                return  # dedup
            header = f"\n\n## Evolution Engine Override ({time.strftime('%Y-%m-%d')})\n"
            with open(sp, "a") as f:
                f.write(header + additions + "\n")

        # Simulate two calls with same content
        engine._apply_prompt_upgrade("leo", upgrade)
        engine._apply_prompt_upgrade("leo", upgrade)

        # Read the override file
        path = f"skills/agent_overrides/leo.md"
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            # Count override blocks
            count = content.count("## Evolution Engine Override")
            assert count == 1, f"Expected 1 override block, got {count}"

    def test_cap_limits_override_blocks(self):
        """Should keep at most MAX_OVERRIDES blocks."""
        scorer = MockScorer()
        board = MockBoard()
        engine = EvolutionEngine(scorer, board)

        # Write 5 different overrides
        for i in range(5):
            engine._apply_prompt_upgrade("test-agent", {
                "additions": f"- Override rule #{i}: unique content {i}",
            })

        path = "skills/agent_overrides/test-agent.md"
        try:
            assert os.path.exists(path)
            with open(path) as f:
                content = f.read()
            count = content.count("## Evolution Engine Override")
            assert count <= engine.MAX_OVERRIDES, \
                f"Expected at most {engine.MAX_OVERRIDES} blocks, got {count}"
            # Should keep the most recent ones
            assert "unique content 4" in content  # Latest should be kept
        finally:
            if os.path.exists(path):
                os.remove(path)
            # Clean up directory
            dir_path = "skills/agent_overrides"
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                os.rmdir(dir_path)

    def test_clear_overrides_removes_file(self):
        """clear_overrides should delete the override file."""
        scorer = MockScorer()
        board = MockBoard()
        engine = EvolutionEngine(scorer, board)

        # Create an override
        engine._apply_prompt_upgrade("clear-test", {
            "additions": "- Test rule",
        })

        path = "skills/agent_overrides/clear-test.md"
        try:
            assert os.path.exists(path)
            engine.clear_overrides("clear-test")
            assert not os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_clear_overrides_noop_when_no_file(self):
        """clear_overrides should silently do nothing if no file."""
        scorer = MockScorer()
        board = MockBoard()
        engine = EvolutionEngine(scorer, board)
        # Should not raise
        engine.clear_overrides("nonexistent-agent")


# ══════════════════════════════════════════════════════════════════════════════
#  PATH SELECTION LOGIC
# ══════════════════════════════════════════════════════════════════════════════

class TestPathSelection:

    @pytest.mark.asyncio
    async def test_model_path_when_not_improving(self):
        """Path B (model) should be selected when 'not_improving' + other issues."""
        tasks = [MockTask(evolution_flags=["failed:err"]) for _ in range(20)]
        tasks += [MockTask() for _ in range(30)]
        scorer = MockScorer(
            score=35.0,
            dims={"improvement_rate": 30, "output_quality": 35,
                  "consistency": 35, "task_completion": 30},
            trend="declining"
        )
        board = MockBoard(tasks)
        engine = EvolutionEngine(scorer, board)

        plan = await engine._diagnose("test-agent")
        assert plan.recommended_path == "model"

    @pytest.mark.asyncio
    async def test_prompt_path_for_single_issue(self):
        """Path A (prompt) for quality issues without 'not_improving'."""
        tasks = [MockTask() for _ in range(10)]
        scorer = MockScorer(
            score=50.0,
            dims={"output_quality": 30, "improvement_rate": 60,
                  "consistency": 60, "task_completion": 60},
            trend="stable"
        )
        board = MockBoard(tasks)
        engine = EvolutionEngine(scorer, board)

        plan = await engine._diagnose("test-agent")
        assert plan.recommended_path == "prompt"

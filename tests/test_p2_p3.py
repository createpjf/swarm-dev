"""
tests/test_p2_p3.py
Tests for P2 (chain completeness) and P3 (robustness) features.
"""

import json
import os
import time
import pytest


# ── P2-3: Anti-cheating (peer review) ────────────────────────────────────────

class TestPeerReviewAntiCheating:

    def test_compute_weight_normal(self, tmp_workdir):
        from reputation.peer_review import PeerReviewAggregator
        pr = PeerReviewAggregator()
        # Normal reviewer with rep=70 should get weight = 70/100 = 0.7
        weight = pr.compute_weight("reviewer", "target", 70.0)
        assert 0.6 <= weight <= 0.8

    def test_mutual_inflation_penalty(self, tmp_workdir):
        from reputation.peer_review import PeerReviewAggregator
        pr = PeerReviewAggregator()
        # Simulate mutual high scoring between two agents
        for _ in range(5):
            pr.record_review("agent_a", "agent_b", 95)
            pr.record_review("agent_b", "agent_a", 92)

        # Both should be detected as mutual inflators
        history = pr._read_history()
        detected = pr._detect_mutual_inflation("agent_a", "agent_b", history)
        assert detected is True

    def test_extreme_bias_detection(self, tmp_workdir):
        from reputation.peer_review import PeerReviewAggregator
        pr = PeerReviewAggregator()
        # Record many extreme-low scores
        for i in range(10):
            pr.record_review("harsh_reviewer", f"target_{i}", 5)

        history = pr._read_history()
        detected = pr._detect_extreme_bias("harsh_reviewer", history)
        assert detected is True

    def test_no_bias_for_normal_scores(self, tmp_workdir):
        from reputation.peer_review import PeerReviewAggregator
        pr = PeerReviewAggregator()
        for i in range(10):
            pr.record_review("fair_reviewer", f"target_{i}", 50 + i)

        history = pr._read_history()
        detected = pr._detect_extreme_bias("fair_reviewer", history)
        assert detected is False

    def test_reviewer_stats(self, tmp_workdir):
        from reputation.peer_review import PeerReviewAggregator
        pr = PeerReviewAggregator()
        pr.record_review("r1", "t1", 80)
        pr.record_review("r1", "t2", 70)
        stats = pr.get_reviewer_stats("r1")
        assert stats["total_reviews"] == 2
        assert stats["avg_score"] == 75.0

    def test_aggregate_with_weights(self, tmp_workdir):
        from reputation.peer_review import PeerReviewAggregator
        pr = PeerReviewAggregator()
        reviews = [
            {"reviewer": "a", "target": "x", "score": 80},
            {"reviewer": "b", "target": "x", "score": 60},
        ]
        result = pr.aggregate(reviews, {"a": 90.0, "b": 50.0})
        # Higher-rep reviewer's score should pull average upward
        assert result > 65  # weighted toward reviewer a's 80


# ── P2-5: Evolution Path C voting ────────────────────────────────────────────

class TestPathCVoting:

    def test_cast_vote_and_check(self, tmp_workdir):
        from reputation.evolution import EvolutionEngine
        from reputation.scorer import ScoreAggregator
        from core.task_board import TaskBoard

        eng = EvolutionEngine(ScoreAggregator(), TaskBoard())
        # First create a vote request
        eng._write_vote_request("executor", {"proposal": "test restructure"})

        result = eng.cast_vote("executor", "planner", approve=True)
        assert result.get("error") != "no pending vote", "Vote file should exist"

        pending = eng.get_pending_votes()
        # After one vote, either still pending or already executed
        assert isinstance(pending, list)

    def test_double_vote_rejected(self, tmp_workdir):
        from reputation.evolution import EvolutionEngine
        from reputation.scorer import ScoreAggregator
        from core.task_board import TaskBoard

        eng = EvolutionEngine(ScoreAggregator(), TaskBoard())
        eng._write_vote_request("executor", {"proposal": "test restructure"})
        eng.cast_vote("executor", "reviewer", approve=True)
        result = eng.cast_vote("executor", "reviewer", approve=False)
        assert result.get("error") == "already voted"


# ── P3-1: Structured logging ─────────────────────────────────────────────────

class TestStructuredLogging:

    def test_correlation_id(self, tmp_workdir):
        from core.logging_config import set_correlation_id, get_correlation_id
        set_correlation_id("test123")
        assert get_correlation_id() == "test123"

    def test_structured_formatter(self, tmp_workdir):
        import logging
        from core.logging_config import StructuredFormatter, set_correlation_id
        set_correlation_id("abc")
        fmt = StructuredFormatter()
        record = logging.LogRecord(
            name="agent.planner", level=logging.INFO,
            pathname="", lineno=0, msg="test msg",
            args=None, exc_info=None,
        )
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["msg"] == "test msg"
        assert parsed["agent"] == "planner"
        assert parsed["cid"] == "abc"

    def test_setup_logging(self, tmp_workdir):
        from core.logging_config import setup_logging
        root = setup_logging(level="DEBUG", structured=True)
        assert root is not None
        assert os.path.exists(".logs")


# ── P3-2: Memory lifecycle ───────────────────────────────────────────────────

class TestMemoryLifecycle:

    def test_get_storage_size(self, tmp_workdir):
        from adapters.memory.episodic import EpisodicMemory
        mem = EpisodicMemory(agent_id="test_agent", base_dir="memory/agents")
        size = mem.get_storage_size()
        assert "total_bytes" in size
        assert "file_count" in size

    def test_cleanup_empty(self, tmp_workdir):
        from adapters.memory.episodic import EpisodicMemory
        mem = EpisodicMemory(agent_id="test_agent", base_dir="memory/agents")
        result = mem.cleanup(max_age_days=1)
        assert result["archived"] == 0


# ── P3-3: BM25 persistence ──────────────────────────────────────────────────

class TestBM25Persistence:

    def test_save_and_load(self, tmp_workdir):
        from adapters.memory.hybrid import BM25Index
        idx = BM25Index()
        idx.add("doc1", "hello world foo bar", {"type": "test"})
        idx.add("doc2", "another document about foo", {"type": "test"})

        path = "memory/bm25_test.json"
        idx.save(path)
        assert os.path.exists(path)

        idx2 = BM25Index.load(path)
        assert len(idx2.docs) == 2
        assert idx2.doc_ids == ["doc1", "doc2"]

        # Search should work on loaded index
        results = idx2.search("foo", n_results=2)
        assert len(results) >= 1

    def test_load_missing_returns_empty(self, tmp_workdir):
        from adapters.memory.hybrid import BM25Index
        idx = BM25Index.load("nonexistent.json")
        assert len(idx.docs) == 0

    def test_reciprocal_rank_fusion(self, tmp_workdir):
        from adapters.memory.hybrid import reciprocal_rank_fusion
        list1 = [("a", 10), ("b", 8), ("c", 5)]
        list2 = [("b", 9), ("d", 7), ("a", 3)]
        fused = reciprocal_rank_fusion(list1, list2)
        # Both a and b appear in both lists, should rank high
        ids = [doc_id for doc_id, _ in fused]
        assert "a" in ids
        assert "b" in ids
        # b appears at rank 1+1 in both, a at 1+3 — b should rank higher
        assert ids.index("b") < ids.index("a")


# ── P3-4: Config version control ────────────────────────────────────────────

class TestConfigVersionControl:

    def test_snapshot_and_history(self, tmp_workdir):
        from core.config_manager import snapshot, history
        name = snapshot("config/agents.yaml", reason="test backup")
        assert name is not None

        entries = history("config/agents.yaml")
        assert len(entries) == 1
        assert entries[0]["reason"] == "test backup"

    def test_snapshot_dedup(self, tmp_workdir):
        from core.config_manager import snapshot, history
        snapshot("config/agents.yaml", reason="first")
        name2 = snapshot("config/agents.yaml", reason="duplicate")
        assert name2 is None  # unchanged file, should skip

        entries = history("config/agents.yaml")
        assert len(entries) == 1

    def test_rollback(self, tmp_workdir):
        import yaml
        from core.config_manager import snapshot, rollback

        # Snapshot original
        snapshot("config/agents.yaml", reason="original")

        # Modify config
        with open("config/agents.yaml") as f:
            cfg = yaml.safe_load(f)
        cfg["agents"].append({"id": "new_agent", "role": "test", "model": "x", "skills": []})
        with open("config/agents.yaml", "w") as f:
            yaml.dump(cfg, f)

        # Snapshot modified
        snapshot("config/agents.yaml", reason="modified")

        # Rollback to version 0 (original)
        ok = rollback("config/agents.yaml", version=0)
        assert ok is True

        # Verify rollback
        with open("config/agents.yaml") as f:
            restored = yaml.safe_load(f)
        agent_ids = [a["id"] for a in restored.get("agents", [])]
        assert "new_agent" not in agent_ids

    def test_snapshot_all(self, tmp_workdir):
        from core.config_manager import snapshot_all
        results = snapshot_all(reason="batch test")
        # Should snapshot at least agents.yaml
        assert len(results) >= 1

    def test_safe_write_yaml(self, tmp_workdir):
        import yaml
        from core.config_manager import safe_write_yaml, history
        data = {"test": True, "agents": []}
        safe_write_yaml("config/agents.yaml", data, reason="safe write test")

        # Should have created a backup of the original
        entries = history("config/agents.yaml")
        assert len(entries) >= 1

        # New content should be written
        with open("config/agents.yaml") as f:
            written = yaml.safe_load(f)
        assert written["test"] is True

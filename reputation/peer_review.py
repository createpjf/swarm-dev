"""
reputation/peer_review.py
Weighted peer review aggregation with anti-gaming mechanisms.
Detects:
  1. Mutual inflation: reviewer<->target pairs that consistently trade high scores
  2. Consensus deviation: reviewers who systematically deviate from peer consensus
  3. Extreme score bias: reviewers who always give near-0 or near-100
"""

from __future__ import annotations
import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional

try:
    from filelock import FileLock
except ImportError:
    class FileLock:  # type: ignore
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

logger = logging.getLogger(__name__)

REVIEW_HISTORY_FILE = "memory/review_history.json"
REVIEW_HISTORY_LOCK = "memory/review_history.lock"


class PeerReviewAggregator:
    """
    Computes weighted review scores with anti-gaming mechanisms.
    Tracks historical review patterns for fraud detection.
    """

    def __init__(self):
        self._lock = FileLock(REVIEW_HISTORY_LOCK)
        os.makedirs("memory", exist_ok=True)

    # ── Core Weight Computation ───────────────────────────────────────────

    def compute_weight(self, reviewer_id: str, target_id: str,
                       reviewer_reputation: float) -> float:
        """
        Compute review weight with anti-gaming penalties.
        Base weight = reviewer_reputation / 100, then apply penalties:
          - Mutual inflation: halve weight
          - Consensus deviation: reduce by up to 30%
          - Extreme bias: reduce by 40%
        """
        weight = max(0.1, reviewer_reputation / 100.0)

        history = self._read_history()

        # Anti-gaming 1: Mutual inflation detection
        if self._detect_mutual_inflation(reviewer_id, target_id, history):
            weight *= 0.5
            logger.warning("[anti-gaming] Mutual inflation: %s <-> %s",
                          reviewer_id, target_id)

        # Anti-gaming 2: Consensus deviation
        deviation = self._get_consensus_deviation(reviewer_id, history)
        if deviation > 25:
            penalty = min(0.3, deviation / 100.0)
            weight *= (1.0 - penalty)

        # Anti-gaming 3: Extreme score bias
        if self._detect_extreme_bias(reviewer_id, history):
            weight *= 0.6

        return max(0.05, weight)

    def aggregate(self, review_scores: list[dict],
                  reviewer_reputations: dict[str, float] | None = None) -> float:
        """
        Weighted average of review scores.
        If no reputations provided, uses equal weights.
        Returns 100.0 if no reviews (pass by default).
        """
        if not review_scores:
            return 100.0

        if reviewer_reputations is None:
            scores = [r["score"] for r in review_scores]
            return sum(scores) / len(scores)

        total_weight = 0.0
        weighted_sum = 0.0

        for r in review_scores:
            reviewer = r.get("reviewer", "")
            target   = r.get("target", "")
            score    = r["score"]
            rep      = reviewer_reputations.get(reviewer, 70.0)
            weight   = self.compute_weight(reviewer, target, rep)

            weighted_sum += score * weight
            total_weight += weight

        if total_weight == 0:
            return 100.0

        return weighted_sum / total_weight

    # ── History Tracking ──────────────────────────────────────────────────

    def record_review(self, reviewer_id: str, target_id: str, score: int):
        """Record a review for anti-gaming analysis."""
        score = max(0, min(100, score))
        with self._lock:
            history = self._read_history()

            pair_key = f"{reviewer_id}->{target_id}"
            pairs = history.setdefault("pairs", {})
            pairs.setdefault(pair_key, []).append({
                "score": score, "ts": time.time()})
            if len(pairs[pair_key]) > 50:
                pairs[pair_key] = pairs[pair_key][-50:]

            reviewers = history.setdefault("reviewers", {})
            reviewers.setdefault(reviewer_id, []).append({
                "target": target_id, "score": score, "ts": time.time()})
            if len(reviewers[reviewer_id]) > 100:
                reviewers[reviewer_id] = reviewers[reviewer_id][-100:]

            targets = history.setdefault("targets", {})
            targets.setdefault(target_id, []).append({
                "reviewer": reviewer_id, "score": score, "ts": time.time()})
            if len(targets[target_id]) > 100:
                targets[target_id] = targets[target_id][-100:]

            self._write_history(history)

    # ── Anti-Gaming Detection ─────────────────────────────────────────────

    def _detect_mutual_inflation(self, reviewer_id: str, target_id: str,
                                  history: dict) -> bool:
        """
        True if reviewer<->target pair consistently trade high scores.
        Both directions must average > 85 with >= 3 reviews each.
        """
        pairs = history.get("pairs", {})
        fwd_key = f"{reviewer_id}->{target_id}"
        rev_key = f"{target_id}->{reviewer_id}"

        fwd_scores = [r["score"] for r in pairs.get(fwd_key, [])]
        rev_scores = [r["score"] for r in pairs.get(rev_key, [])]

        if len(fwd_scores) >= 3 and len(rev_scores) >= 3:
            fwd_avg = sum(fwd_scores) / len(fwd_scores)
            rev_avg = sum(rev_scores) / len(rev_scores)
            if fwd_avg > 85 and rev_avg > 85:
                return True
        return False

    def _get_consensus_deviation(self, reviewer_id: str,
                                  history: dict) -> float:
        """
        Measure how much a reviewer deviates from peer consensus.
        Returns average |reviewer_score - target_avg| across recent reviews.
        """
        reviewer_reviews = history.get("reviewers", {}).get(reviewer_id, [])
        if len(reviewer_reviews) < 5:
            return 0.0

        targets = history.get("targets", {})
        deviations = []

        for review in reviewer_reviews[-20:]:
            target = review["target"]
            target_scores = [r["score"] for r in targets.get(target, [])
                            if r["reviewer"] != reviewer_id]
            if len(target_scores) >= 2:
                consensus = sum(target_scores) / len(target_scores)
                deviations.append(abs(review["score"] - consensus))

        if not deviations:
            return 0.0
        return sum(deviations) / len(deviations)

    def _detect_extreme_bias(self, reviewer_id: str, history: dict) -> bool:
        """
        True if >70% of recent scores are <10 or >90.
        """
        reviews = history.get("reviewers", {}).get(reviewer_id, [])
        if len(reviews) < 5:
            return False

        recent = reviews[-20:]
        extreme = sum(1 for r in recent if r["score"] < 10 or r["score"] > 90)
        return extreme / len(recent) > 0.7

    def get_reviewer_stats(self, reviewer_id: str) -> dict:
        """Get anti-gaming stats for a reviewer (for dashboard/API)."""
        history = self._read_history()
        reviews = history.get("reviewers", {}).get(reviewer_id, [])

        if not reviews:
            return {"reviewer_id": reviewer_id, "total_reviews": 0}

        scores = [r["score"] for r in reviews]
        mean = sum(scores) / len(scores)
        variance = (sum((x - mean) ** 2 for x in scores) / max(len(scores) - 1, 1))

        return {
            "reviewer_id": reviewer_id,
            "total_reviews": len(reviews),
            "avg_score": round(mean, 1),
            "score_stddev": round(variance ** 0.5, 1),
            "consensus_deviation": round(
                self._get_consensus_deviation(reviewer_id, history), 1),
            "extreme_bias": self._detect_extreme_bias(reviewer_id, history),
        }

    # ── Persistence ───────────────────────────────────────────────────────

    def _read_history(self) -> dict:
        try:
            with open(REVIEW_HISTORY_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"pairs": {}, "reviewers": {}, "targets": {}}

    def _write_history(self, history: dict):
        with open(REVIEW_HISTORY_FILE, "w") as f:
            json.dump(history, f, ensure_ascii=False)

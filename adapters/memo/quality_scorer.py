"""
adapters/memo/quality_scorer.py — Memo 3-dimension quality scoring.

Dimensions (matching Memo Protocol spec):
    completeness  35 %  — executable without additional context?
    utility       35 %  — solves real problems? immediately reusable?
    uniqueness    30 %  — novel approach? unique insights?

Minimum threshold: **0.6** (enforced at export time).

Reuses ``core/protocols.classify_density()`` as a uniqueness signal.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── signal word lists ─────────────────────────────────────────────────────

_UTILITY_SIGNALS: list[str] = [
    "solution", "解决", "fix", "修复", "workaround", "步骤",
    "implementation", "实现", "code", "command", "命令",
    "install", "安装", "配置", "config", "deploy", "部署",
    "answer", "回答", "result", "结果", "output", "输出",
]

_EDGE_CASE_SIGNALS: list[str] = [
    "edge case", "边界", "exception", "异常", "workaround",
    "pitfall", "陷阱", "caveat", "注意", "warning", "警告",
    "gotcha", "trap", "limitation", "限制", "trade-off", "权衡",
    "lesson", "教训", "root cause", "根因",
]


# ══════════════════════════════════════════════════════════════════════════════
#  Scoring functions
# ══════════════════════════════════════════════════════════════════════════════

def _score_completeness(content: str, source_type: str,
                        meta: dict) -> float:
    """Content completeness — can it be understood/executed standalone?"""
    score = 0.45  # baseline

    # Length signals
    length = len(content)
    if length > 500:
        score += 0.08
    if length > 1500:
        score += 0.08
    if length > 3000:
        score += 0.04

    # Structure signals
    if "## " in content or "# " in content:
        score += 0.05      # has heading structure
    if "```" in content:
        score += 0.08      # has code blocks
    if "- " in content or "1. " in content:
        score += 0.04      # has lists/steps

    # Procedural completeness: does it have steps?
    if source_type in ("case", "procedural"):
        cl = content.lower()
        if "step" in cl or "步骤" in cl or "step 1" in cl:
            score += 0.08
        if "result" in cl or "结果" in cl:
            score += 0.04

    # Cleo-specific: episode score
    cleo_score = meta.get("score")
    if cleo_score is not None:
        if cleo_score >= 7:
            score += 0.08
        if cleo_score >= 9:
            score += 0.04

    return min(score, 1.0)


def _score_utility(content: str, source_type: str,
                   meta: dict) -> float:
    """Practical utility — does it solve real problems?"""
    score = 0.40  # baseline

    # Case type is naturally high utility (problem→solution)
    if source_type == "case":
        score += 0.15
        use_count = meta.get("use_count", 0)
        if use_count >= 2:
            score += 0.08
        if use_count >= 5:
            score += 0.08

    # Utility signal words
    cl = content.lower()
    hit = sum(1 for s in _UTILITY_SIGNALS if s in cl)
    score += min(hit * 0.04, 0.16)

    # Episode outcome=success
    if meta.get("outcome") == "success":
        score += 0.08

    # KB note with multiple contributors / updates = validated knowledge
    if source_type == "kb_note":
        if meta.get("update_count", 1) >= 3:
            score += 0.10
        if len(meta.get("contributors", [])) >= 2:
            score += 0.06

    # Pattern with high occurrences = repeatedly validated
    if source_type == "pattern":
        occ = meta.get("occurrences", 0)
        if occ >= 5:
            score += 0.10
        elif occ >= 3:
            score += 0.06

    return min(score, 1.0)


def _score_uniqueness(content: str, meta: dict) -> float:
    """Uniqueness — novel approach or insight?"""
    score = 0.50  # baseline

    # DensityTag signal (reuse core/protocols.classify_density)
    try:
        from core.protocols import classify_density, DensityLevel
        density = classify_density(content, meta.get("tags", []))
        if density == DensityLevel.HIGH:
            score += 0.18
        elif density == DensityLevel.LOW:
            score -= 0.18
    except ImportError:
        pass  # fallback: no density signal

    # Edge case / pitfall signals = rarer, more unique knowledge
    cl = content.lower()
    hit = sum(1 for s in _EDGE_CASE_SIGNALS if s in cl)
    score += min(hit * 0.04, 0.15)

    # Pattern occurrences = repeatedly validated = higher value
    if meta.get("occurrences", 0) >= 5:
        score += 0.08

    return min(max(score, 0.0), 1.0)


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

def score_memory(content: str, source_type: str,
                 metadata: dict) -> dict:
    """Score a memory candidate across 3 Memo dimensions.

    Args:
        content: deidentified content text
        source_type: one of episode / summary / case / pattern / kb_note
        metadata: original Cleo metadata dict (score, tags, use_count, etc.)

    Returns:
        dict with keys: completeness, utility, uniqueness, composite, passed
    """
    completeness = _score_completeness(content, source_type, metadata)
    utility = _score_utility(content, source_type, metadata)
    uniqueness = _score_uniqueness(content, metadata)

    composite = (completeness * 0.35
                 + utility * 0.35
                 + uniqueness * 0.30)

    return {
        "completeness": round(completeness, 3),
        "utility": round(utility, 3),
        "uniqueness": round(uniqueness, 3),
        "composite": round(composite, 3),
        "passed": composite >= 0.6,
    }

"""
adapters/memo/transformer.py — Cleo memory → Memo MemoryObject conversion.

Handles 5 source types:
    episode        → Memo EPISODIC    (full task execution journey)
    summary        → Memo SEMANTIC    (consolidated multi-episode summary)
    case           → Memo PROCEDURAL  (problem → solution = executable skill)
    pattern        → Memo SEMANTIC    (recurring generalizable observation)
    kb_note        → Memo SEMANTIC    (cross-agent distilled knowledge)

Each converter returns a ``MemoObject`` dataclass ready for
``to_api_payload()`` serialization.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adapters.memo.config import MemoConfig


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

_NANOID_ALPHABET = (
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
)


def _nanoid(prefix: str = "mem_", length: int = 21) -> str:
    """Generate a Memo-compatible nanoid."""
    return prefix + "".join(secrets.choice(_NANOID_ALPHABET)
                            for _ in range(length))


def _content_hash(content: str) -> str:
    """SHA-256 hash of content (Memo requirement)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, max_len: int = 280) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# ══════════════════════════════════════════════════════════════════════════════
#  MemoObject
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MemoObject:
    """Python representation of a Memo MemoryObject (v1.0)."""

    memo_version: str = "1.0"
    id: str = ""
    type: str = "episodic"          # episodic | semantic | procedural
    status: str = "draft"
    content: str = ""
    content_hash: str = ""
    title: str = ""
    summary: str = ""               # max 280 chars
    tags: list[str] = field(default_factory=list)
    domain: str = "python"
    language: str = "zh"
    author: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    signals: dict = field(default_factory=lambda: {
        "quality_score": 0.0,
        "community_score": 1.0,
        "call_count": 0,
        "helpful_count": 0,
        "not_helpful_count": 0,
        "freshness_score": 1.0,
    })
    access: dict = field(default_factory=lambda: {
        "tier": "developer",
        "price_usdc": 0.0,
        "subscription_bypass": True,
    })
    created_at: str = ""
    updated_at: str = ""

    # ── Cleo-internal tracking (not sent to API) ─────────────────────────
    _cleo_source_type: str = ""
    _cleo_source_id: str = ""

    def to_api_payload(self) -> dict:
        """Serialize to Memo API POST /memories request body."""
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if k.startswith("_"):
                continue
            d[k] = v
        return d


# ══════════════════════════════════════════════════════════════════════════════
#  Content builders
# ══════════════════════════════════════════════════════════════════════════════

def _build_episode_content(episode: dict) -> str:
    """Assemble L2 episode into structured Markdown for Memo."""
    parts: list[str] = []

    title = episode.get("title", "Untitled Task")
    parts.append(f"# Task: {title}")

    desc = episode.get("description", "")
    if desc:
        parts.append(f"\n## Description\n{desc}")

    # Execution context
    ctx = episode.get("context", {})
    if ctx:
        try:
            ctx_str = json.dumps(ctx, indent=2, ensure_ascii=False)
            parts.append(f"\n## Context\n```json\n{ctx_str}\n```")
        except (TypeError, ValueError):
            pass

    # Result
    result = episode.get("result_full",
                         episode.get("result_preview", ""))
    if result:
        parts.append(f"\n## Result\n{result}")

    # Metadata
    parts.append("\n## Metadata")
    parts.append(f"- Outcome: {episode.get('outcome', 'unknown')}")
    score = episode.get("score")
    if score is not None:
        parts.append(f"- Score: {score}")
    parts.append(f"- Model: {episode.get('model', 'unknown')}")
    err = episode.get("error_type")
    if err:
        parts.append(f"- Error Type: {err}")

    return "\n".join(parts)


def _build_case_content(case: dict) -> str:
    """Convert Case (problem→solution) into Skill Document format."""
    problem = case.get("problem", "")
    solution = case.get("solution", "")
    tags = case.get("tags", [])

    return f"""# SKILL: {problem[:80]}

## Trigger Conditions
- {problem}

## Solution Steps
{solution}

## Metadata
- Agent: {case.get('agent_id', 'unknown')}
- Usage Count: {case.get('use_count', 0)}
- Tags: {', '.join(tags)}
"""


def _build_pattern_content(pattern: dict) -> str:
    """Convert Pattern into semantic knowledge text."""
    desc = pattern.get("description", "")
    evidence = pattern.get("evidence", [])

    parts = [f"# Pattern: {desc[:100]}"]
    parts.append(f"\n## Description\n{desc}")

    if evidence:
        parts.append("\n## Evidence")
        for i, ev in enumerate(evidence[:10], 1):
            if isinstance(ev, str):
                parts.append(f"{i}. {ev}")
            elif isinstance(ev, dict):
                parts.append(f"{i}. {ev.get('text', str(ev))}")

    parts.append(f"\n## Occurrences: {pattern.get('occurrences', 0)}")
    return "\n".join(parts)


def _build_kb_note_content(note: dict) -> str:
    """Convert KB Note into semantic knowledge text."""
    topic = note.get("topic", "")
    content = note.get("content", "")
    links = note.get("links", [])

    parts = [f"# {topic}"]
    parts.append(f"\n{content}")

    if links:
        parts.append("\n## Related Notes")
        for link in links[:10]:
            parts.append(f"- [[{link}]]")

    contributors = note.get("contributors", [])
    if contributors:
        parts.append(f"\n## Contributors: {', '.join(contributors)}")

    return "\n".join(parts)


def _build_summary_content(summary: dict) -> str:
    """Convert Summary Episode into semantic knowledge text."""
    parts = [f"# Consolidated Summary"]

    titles = summary.get("titles", [])
    if titles:
        parts.append("\n## Source Tasks")
        for t in titles[:10]:
            parts.append(f"- {t}")

    content = summary.get("content_summary", "")
    if content:
        parts.append(f"\n## Summary\n{content}")

    dist = summary.get("outcome_distribution", {})
    if dist:
        parts.append("\n## Outcome Distribution")
        for k, v in dist.items():
            parts.append(f"- {k}: {v}")

    parts.append(f"\n## Source Count: {summary.get('source_count', 0)}")
    avg = summary.get("avg_score")
    if avg is not None:
        parts.append(f"## Average Score: {avg:.1f}")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  Converters (Cleo → MemoObject)
# ══════════════════════════════════════════════════════════════════════════════

def episode_to_memo(episode: dict, config: "MemoConfig",
                    deidentified_content: str) -> MemoObject:
    """Convert Cleo Episode → Memo EPISODIC MemoryObject."""
    title = episode.get("title", "Untitled")[:120]
    desc = episode.get("description", "")
    now = _iso_now()

    return MemoObject(
        id=_nanoid(),
        type="episodic",
        content=deidentified_content,
        content_hash=_content_hash(deidentified_content),
        title=title,
        summary=_truncate(desc, 280),
        tags=episode.get("tags", [])[:10],
        domain=config.default_domain,
        language=config.default_language,
        author=config.author_info,
        access=config.default_access,
        created_at=now,
        updated_at=now,
        _cleo_source_type="episode",
        _cleo_source_id=episode.get("task_id", ""),
    )


def summary_to_memo(summary: dict, config: "MemoConfig",
                    deidentified_content: str) -> MemoObject:
    """Convert Cleo Summary Episode → Memo SEMANTIC MemoryObject."""
    titles = summary.get("titles", [])
    title = f"Summary: {titles[0][:80]}" if titles else "Consolidated Summary"
    now = _iso_now()

    return MemoObject(
        id=_nanoid(),
        type="semantic",
        content=deidentified_content,
        content_hash=_content_hash(deidentified_content),
        title=title[:120],
        summary=_truncate(summary.get("content_summary", ""), 280),
        tags=list(set(summary.get("tags", [])))[:10],
        domain=config.default_domain,
        language=config.default_language,
        author=config.author_info,
        access=config.default_access,
        created_at=now,
        updated_at=now,
        _cleo_source_type="summary",
        _cleo_source_id=summary.get("task_id",
                                     f"summary_{int(summary.get('created_at', 0))}"),
    )


def case_to_memo(case: dict, config: "MemoConfig",
                 deidentified_content: str) -> MemoObject:
    """Convert Cleo Case → Memo PROCEDURAL MemoryObject."""
    problem = case.get("problem", "")
    title = f"Case: {problem[:100]}"
    now = _iso_now()

    return MemoObject(
        id=_nanoid(),
        type="procedural",
        content=deidentified_content,
        content_hash=_content_hash(deidentified_content),
        title=title[:120],
        summary=_truncate(problem, 280),
        tags=case.get("tags", [])[:10],
        domain=config.default_domain,
        language=config.default_language,
        author=config.author_info,
        access=config.default_access,
        created_at=now,
        updated_at=now,
        _cleo_source_type="case",
        _cleo_source_id=case.get("id", ""),
    )


def pattern_to_memo(pattern: dict, config: "MemoConfig",
                    deidentified_content: str) -> MemoObject:
    """Convert Cleo Pattern → Memo SEMANTIC MemoryObject."""
    desc = pattern.get("description", "")
    title = f"Pattern: {desc[:100]}"
    now = _iso_now()

    return MemoObject(
        id=_nanoid(),
        type="semantic",
        content=deidentified_content,
        content_hash=_content_hash(deidentified_content),
        title=title[:120],
        summary=_truncate(desc, 280),
        tags=pattern.get("tags", [])[:10],
        domain=config.default_domain,
        language=config.default_language,
        author=config.author_info,
        access=config.default_access,
        created_at=now,
        updated_at=now,
        _cleo_source_type="pattern",
        _cleo_source_id=pattern.get("id", ""),
    )


def kb_note_to_memo(note: dict, config: "MemoConfig",
                    deidentified_content: str) -> MemoObject:
    """Convert Cleo KB Note → Memo SEMANTIC MemoryObject."""
    topic = note.get("topic", "")
    now = _iso_now()

    return MemoObject(
        id=_nanoid(),
        type="semantic",
        content=deidentified_content,
        content_hash=_content_hash(deidentified_content),
        title=topic[:120] or "KB Note",
        summary=_truncate(note.get("content", ""), 280),
        tags=note.get("tags", [])[:10],
        domain=config.default_domain,
        language=config.default_language,
        author=config.author_info,
        access=config.default_access,
        created_at=now,
        updated_at=now,
        _cleo_source_type="kb_note",
        _cleo_source_id=note.get("slug", ""),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Content builder dispatch
# ══════════════════════════════════════════════════════════════════════════════

CONTENT_BUILDERS: dict[str, callable] = {
    "episode":  _build_episode_content,
    "summary":  _build_summary_content,
    "case":     _build_case_content,
    "pattern":  _build_pattern_content,
    "kb_note":  _build_kb_note_content,
}

CONVERTERS: dict[str, callable] = {
    "episode":  episode_to_memo,
    "summary":  summary_to_memo,
    "case":     case_to_memo,
    "pattern":  pattern_to_memo,
    "kb_note":  kb_note_to_memo,
}

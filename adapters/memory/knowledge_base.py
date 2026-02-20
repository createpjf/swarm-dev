"""
adapters/memory/knowledge_base.py
Shared knowledge base — cross-agent learning via atomic notes (Zettelkasten).

This implements the user's Zettelkasten-inspired structure:
  memory/
    shared/
      atomic/         # Atomic notes: one concept per file
      moc.md          # Map of Content — navigational index
      insights.jsonl  # Cross-agent insight feed

Design principles:
  - Atomic notes are topic-specific, shared across all agents
  - MOC (Map of Content) provides navigational structure
  - Any agent can CREATE or MERGE notes (OpenViking dedup logic)
  - Insights feed captures cross-agent learnings

Unlike per-agent EpisodicMemory, KnowledgeBase is SHARED.
All agents read it; any agent can write to it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from filelock import FileLock
except ImportError:
    class FileLock:  # type: ignore
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

logger = logging.getLogger(__name__)

SHARED_DIR = os.path.join("memory", "shared")
ATOMIC_DIR = os.path.join(SHARED_DIR, "atomic")
MOC_PATH   = os.path.join(SHARED_DIR, "moc.md")
INSIGHTS_PATH = os.path.join(SHARED_DIR, "insights.jsonl")
LOCK_PATH  = os.path.join(SHARED_DIR, ".kb.lock")


def _slug(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    s = text.lower().strip()
    s = re.sub(r'[^a-z0-9\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff_\- ]+', '', s)
    s = re.sub(r'\s+', '-', s)
    return s[:80] or hashlib.sha256(text.encode()).hexdigest()[:12]


class KnowledgeBase:
    """
    Shared Zettelkasten-style knowledge base.

    All agents share this store. Each atomic note is a JSON file
    containing a single concept, linked by tags.

    Thread-safe via file locks.
    """

    def __init__(self, base_dir: str = SHARED_DIR):
        self.base = base_dir
        self.atomic_dir = os.path.join(base_dir, "atomic")
        self.moc_path = os.path.join(base_dir, "moc.md")
        self.insights_path = os.path.join(base_dir, "insights.jsonl")
        self.lock = FileLock(os.path.join(base_dir, ".kb.lock"))

        for d in [self.base, self.atomic_dir]:
            os.makedirs(d, exist_ok=True)

    # ── Atomic Notes ──────────────────────────────────────────────────────

    def create_note(self, topic: str, content: str,
                    tags: Optional[list[str]] = None,
                    author: str = "system",
                    links: Optional[list[str]] = None) -> str:
        """
        Create or update an atomic note.
        If a note with the same topic slug exists, MERGE content.
        Returns the note slug.
        """
        slug = _slug(topic)
        path = os.path.join(self.atomic_dir, f"{slug}.json")

        with self.lock:
            existing = None
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            if existing:
                # MERGE strategy (OpenViking dedup: CREATE/MERGE/SKIP)
                # Append new content if substantially different
                old_content = existing.get("content", "")
                if content.strip() not in old_content:
                    existing["content"] = (
                        old_content.rstrip() + "\n\n---\n\n" + content.strip()
                    )
                # Merge tags
                old_tags = set(existing.get("tags", []))
                old_tags.update(tags or [])
                existing["tags"] = sorted(old_tags)
                # Merge links
                old_links = set(existing.get("links", []))
                old_links.update(links or [])
                existing["links"] = sorted(old_links)
                existing["updated_at"] = time.time()
                existing["update_count"] = existing.get("update_count", 1) + 1
                existing["contributors"] = list(
                    set(existing.get("contributors", []) + [author]))
                note = existing
            else:
                note = {
                    "slug": slug,
                    "topic": topic,
                    "content": content.strip(),
                    "tags": sorted(set(tags or [])),
                    "links": sorted(set(links or [])),
                    "author": author,
                    "contributors": [author],
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "update_count": 1,
                }

            with open(path, "w") as f:
                json.dump(note, f, ensure_ascii=False, indent=2)

        logger.debug("KB note saved: %s (by %s)", slug, author)
        return slug

    def get_note(self, topic_or_slug: str) -> Optional[dict]:
        """Read an atomic note by topic or slug."""
        slug = _slug(topic_or_slug)
        path = os.path.join(self.atomic_dir, f"{slug}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def search_notes(self, query: str, limit: int = 5) -> list[dict]:
        """
        Search atomic notes by keyword.
        Returns notes sorted by relevance score.
        """
        results = []
        query_lower = query.lower()
        query_words = query_lower.split()

        for fname in os.listdir(self.atomic_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(self.atomic_dir, fname)) as f:
                    note = json.load(f)
                text = (note.get("topic", "") + " " +
                        note.get("content", "") + " " +
                        " ".join(note.get("tags", []))).lower()
                score = sum(1 for w in query_words if w in text)
                if score > 0:
                    note["_relevance"] = score
                    results.append(note)
            except (json.JSONDecodeError, OSError):
                continue

        results.sort(key=lambda x: x.get("_relevance", 0), reverse=True)
        return results[:limit]

    def list_notes(self, limit: int = 50) -> list[dict]:
        """List all notes, newest first."""
        notes = []
        for fname in os.listdir(self.atomic_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(self.atomic_dir, fname)) as f:
                    note = json.load(f)
                notes.append(note)
            except (json.JSONDecodeError, OSError):
                continue
        notes.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return notes[:limit]

    def list_notes_compact(self) -> list[dict]:
        """List all notes in compact form (slug, topic, tags only) for MOC."""
        notes = []
        for fname in os.listdir(self.atomic_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(self.atomic_dir, fname)) as f:
                    note = json.load(f)
                notes.append({
                    "slug": note.get("slug", fname.replace(".json", "")),
                    "topic": note.get("topic", ""),
                    "tags": note.get("tags", []),
                    "contributors": note.get("contributors", []),
                    "update_count": note.get("update_count", 1),
                    "links": note.get("links", []),
                })
            except (json.JSONDecodeError, OSError):
                continue
        notes.sort(key=lambda x: x.get("topic", ""))
        return notes

    # ── Map of Content (MOC) ──────────────────────────────────────────────

    def rebuild_moc(self) -> str:
        """
        Regenerate the Map of Content from all atomic notes.
        Groups notes by tags.
        """
        notes = self.list_notes_compact()
        if not notes:
            moc = "# Map of Content\n\n_No knowledge notes yet._\n"
            with self.lock:
                with open(self.moc_path, "w") as f:
                    f.write(moc)
            return moc

        # Group by first tag
        by_tag: dict[str, list[dict]] = {}
        for n in notes:
            tag = n["tags"][0] if n["tags"] else "uncategorized"
            by_tag.setdefault(tag, []).append(n)

        lines = ["# Map of Content\n",
                 f"_Total notes: {len(notes)}_\n"]

        for tag in sorted(by_tag.keys()):
            lines.append(f"\n## {tag.title()}\n")
            for n in sorted(by_tag[tag], key=lambda x: x["topic"]):
                contributors = ", ".join(n.get("contributors", []))
                link_count = len(n.get("links", []))
                links_str = f", {link_count} links" if link_count else ""
                lines.append(
                    f"- **{n['topic']}** "
                    f"({n.get('update_count', 1)} updates{links_str}, "
                    f"by {contributors})")

        moc = "\n".join(lines)
        with self.lock:
            with open(self.moc_path, "w") as f:
                f.write(moc)
        return moc

    def get_moc(self) -> str:
        """Read the Map of Content."""
        if os.path.exists(self.moc_path):
            with open(self.moc_path) as f:
                return f.read()
        return self.rebuild_moc()

    # ── Insights Feed (cross-agent learning) ──────────────────────────────

    def add_insight(self, agent_id: str, insight: str,
                    tags: Optional[list[str]] = None):
        """
        Publish a cross-agent insight.
        Other agents can read the feed for collective learning.
        """
        entry = {
            "agent_id": agent_id,
            "insight": insight,
            "tags": tags or [],
            "ts": time.time(),
        }
        with self.lock:
            with open(self.insights_path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent_insights(self, limit: int = 20,
                        exclude_agent: Optional[str] = None) -> list[dict]:
        """
        Read recent insights from the feed.
        Optionally exclude the calling agent's own insights.
        """
        if not os.path.exists(self.insights_path):
            return []
        entries = []
        try:
            with open(self.insights_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            if exclude_agent and entry.get("agent_id") == exclude_agent:
                                continue
                            entries.append(entry)
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return []

        # Return most recent
        return entries[-limit:]

    # ── Recall for System Prompt Injection ────────────────────────────────

    def recall(self, query: str, agent_id: str,
               token_budget: int = 800) -> str:
        """
        Build a knowledge base context block for injection into
        the agent's system prompt.

        Includes:
          1. Relevant notes (from search)
          2. Recent cross-agent insights
        """
        CHARS_PER_TOKEN = 3
        budget_chars = token_budget * CHARS_PER_TOKEN
        parts = []
        used = 0

        # Relevant notes
        notes = self.search_notes(query, limit=3)
        if notes:
            section = "### Team Knowledge\n"
            for n in notes:
                content_preview = n.get("content", "")[:300]
                entry = (f"- **{n['topic']}**: {content_preview}\n")
                if used + len(entry) > budget_chars:
                    break
                section += entry
                used += len(entry)
            if len(section) > 25:
                parts.append(section)

        # Cross-agent insights (from other agents)
        insights = self.recent_insights(limit=5, exclude_agent=agent_id)
        if insights:
            section = "### Team Insights\n"
            for ins in insights[-3:]:
                entry = (f"- [{ins['agent_id']}] {ins['insight'][:200]}\n")
                if used + len(entry) > budget_chars:
                    break
                section += entry
                used += len(entry)
            if len(section) > 25:
                parts.append(section)

        if not parts:
            return ""
        return "## Shared Knowledge\n" + "\n".join(parts)

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        note_count = len([f for f in os.listdir(self.atomic_dir)
                          if f.endswith(".json")])
        insight_count = 0
        if os.path.exists(self.insights_path):
            with open(self.insights_path) as f:
                insight_count = sum(1 for line in f if line.strip())

        return {
            "notes": note_count,
            "insights": insight_count,
            "moc_exists": os.path.exists(self.moc_path),
        }

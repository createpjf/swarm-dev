"""
core/search/memory_search.py
Unified memory search interface — wraps QMD FTS5 for agent-facing queries.

Usage:
    from core.search import MemorySearch
    ms = MemorySearch(agent_id="executor")
    results = ms.search("error handling patterns")
    grouped = ms.search_all("authentication")
"""

from __future__ import annotations

import logging
from typing import Optional

from .qmd import QMD

logger = logging.getLogger(__name__)

# Collections that hold memory-related data
MEMORY_COLLECTIONS = ("memory", "knowledge")
ALL_COLLECTIONS = ("memory", "knowledge", "workspace", "docs")


class MemorySearch:
    """Unified search over memory and knowledge collections."""

    def __init__(self, agent_id: str = "", db_path: str = "search.db"):
        self.agent_id = agent_id
        self._qmd = QMD(db_path)

    # ── Single-collection search ─────────────────────────────────────────

    def search(self, query: str, *,
               collection: str = "memory",
               limit: int = 10) -> list[dict]:
        """Search a single collection.

        Args:
            query: Natural language search query.
            collection: Which collection to search (memory/knowledge/workspace/docs).
            limit: Max results.

        Returns:
            List of result dicts with id, title, snippet, path, collection, rank.
        """
        if not query or not query.strip():
            return []
        return self._qmd.search(query, collection=collection, limit=limit)

    # ── Cross-collection search ──────────────────────────────────────────

    def search_all(self, query: str, *,
                   limit: int = 10) -> dict[str, list[dict]]:
        """Search ALL collections, return results grouped by source.

        Returns:
            {"memory": [...], "knowledge": [...], "workspace": [...], "docs": [...]}
        """
        if not query or not query.strip():
            return {c: [] for c in ALL_COLLECTIONS}

        results: dict[str, list[dict]] = {}
        for col in ALL_COLLECTIONS:
            results[col] = self._qmd.search(
                query, collection=col, limit=limit)
        return results

    # ── Memory-focused search ────────────────────────────────────────────

    def search_memory(self, query: str, *,
                      limit: int = 10) -> list[dict]:
        """Search only memory + knowledge collections (most relevant for agents).

        Results are merged and re-sorted by rank.
        """
        if not query or not query.strip():
            return []

        merged: list[dict] = []
        for col in MEMORY_COLLECTIONS:
            merged.extend(
                self._qmd.search(query, collection=col, limit=limit))

        # Sort by rank (lower = better in BM25)
        merged.sort(key=lambda r: r.get("rank", 0))
        return merged[:limit]

    # ── Agent-scoped search ──────────────────────────────────────────────

    def search_agent_memory(self, query: str, *,
                            agent_id: Optional[str] = None,
                            limit: int = 10) -> list[dict]:
        """Search memory collection filtered to a specific agent.

        Falls back to self.agent_id if agent_id not provided.
        """
        aid = agent_id or self.agent_id
        if not query or not query.strip():
            return []

        results = self._qmd.search(
            query, collection="memory", limit=limit * 3)

        if aid:
            results = [r for r in results
                       if r.get("agent_id", "") == aid]

        return results[:limit]

    # ── Stats ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return index statistics."""
        return self._qmd.stats()

    def close(self):
        """Close underlying QMD connection."""
        self._qmd.close()

"""
core/search/indexer.py
Document indexer — indexes episodic memory, knowledge base,
workspace files, and docs into the QMD FTS5 engine.

Usage:
    from core.search import QMD, Indexer
    indexer = Indexer(QMD())
    indexer.reindex_all()
    indexer.index_single("title", "content", "memory", agent_id="executor")
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .qmd import QMD

logger = logging.getLogger(__name__)


class Indexer:
    """Indexes various data sources into QMD FTS5."""

    def __init__(self, qmd: QMD):
        self.qmd = qmd

    # ── Episodic Memory ───────────────────────────────────────────────────

    def index_episodes(self, agent_id: str,
                       base_dir: str = "memory/agents") -> int:
        """Index all episodes for an agent.
        Scans memory/agents/{agent_id}/episodes/{date}/{task_id}.json
        Returns count of indexed documents.
        """
        count = 0
        episodes_dir = os.path.join(base_dir, agent_id, "episodes")
        if not os.path.isdir(episodes_dir):
            return 0

        for date_dir in os.listdir(episodes_dir):
            day_path = os.path.join(episodes_dir, date_dir)
            if not os.path.isdir(day_path):
                continue
            for fname in os.listdir(day_path):
                if not fname.endswith(".json") or fname.startswith("."):
                    continue
                fpath = os.path.join(day_path, fname)
                try:
                    with open(fpath) as f:
                        ep = json.load(f)
                    title = ep.get("title", ep.get("description", "")[:120])
                    content = ep.get("result_preview",
                                     ep.get("result_full", ""))[:2000]
                    tags = " ".join(ep.get("tags", []))
                    self.qmd.index(
                        title=title,
                        content=content,
                        collection="memory",
                        path=fpath,
                        tags=tags,
                        agent_id=agent_id,
                        source_type="episode",
                        metadata={"task_id": ep.get("task_id", ""),
                                  "date": ep.get("date", "")},
                    )
                    count += 1
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Skip episode %s: %s", fpath, e)

        logger.info("Indexed %d episodes for agent %s", count, agent_id)
        return count

    def index_cases(self, agent_id: str,
                    base_dir: str = "memory/agents") -> int:
        """Index all cases for an agent.
        Scans memory/agents/{agent_id}/cases/{hash}.json
        """
        count = 0
        cases_dir = os.path.join(base_dir, agent_id, "cases")
        if not os.path.isdir(cases_dir):
            return 0

        for fname in os.listdir(cases_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            fpath = os.path.join(cases_dir, fname)
            try:
                with open(fpath) as f:
                    case = json.load(f)
                self.qmd.index(
                    title=case.get("problem", "")[:200],
                    content=case.get("solution", "")[:2000],
                    collection="memory",
                    path=fpath,
                    tags=" ".join(case.get("tags", [])),
                    agent_id=agent_id,
                    source_type="case",
                    metadata={"use_count": case.get("use_count", 0)},
                )
                count += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("Skip case %s: %s", fpath, e)

        logger.info("Indexed %d cases for agent %s", count, agent_id)
        return count

    # ── Knowledge Base ────────────────────────────────────────────────────

    def index_knowledge_base(self,
                             base_dir: str = "memory/shared") -> int:
        """Index all atomic notes from shared knowledge base.
        Scans memory/shared/atomic/{slug}.json
        """
        count = 0
        atomic_dir = os.path.join(base_dir, "atomic")
        if not os.path.isdir(atomic_dir):
            return 0

        for fname in os.listdir(atomic_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            fpath = os.path.join(atomic_dir, fname)
            try:
                with open(fpath) as f:
                    note = json.load(f)
                self.qmd.index(
                    title=note.get("topic", ""),
                    content=note.get("content", "")[:3000],
                    collection="knowledge",
                    path=fpath,
                    tags=" ".join(note.get("tags", [])),
                    agent_id=", ".join(note.get("contributors", [])),
                    source_type="note",
                )
                count += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("Skip note %s: %s", fpath, e)

        logger.info("Indexed %d knowledge base notes", count)
        return count

    # ── Workspace Files ───────────────────────────────────────────────────

    def index_workspace(self, ws_path: str = "workspace") -> int:
        """Index text files in the shared workspace."""
        count = 0
        if not os.path.isdir(ws_path):
            return 0

        text_exts = {".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml",
                     ".yml", ".toml", ".csv", ".html", ".css", ".sh"}

        for root, _, files in os.walk(ws_path):
            for fname in files:
                if fname.startswith("."):
                    continue
                ext = Path(fname).suffix.lower()
                if ext not in text_exts:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    content = Path(fpath).read_text(
                        encoding="utf-8", errors="replace")[:5000]
                    self.qmd.index(
                        title=fname,
                        content=content,
                        collection="workspace",
                        path=fpath,
                        source_type="file",
                    )
                    count += 1
                except OSError as e:
                    logger.debug("Skip workspace file %s: %s", fpath, e)

        logger.info("Indexed %d workspace files", count)
        return count

    # ── Docs ──────────────────────────────────────────────────────────────

    def index_docs(self, docs_path: str = "docs") -> int:
        """Index markdown documentation files."""
        count = 0
        if not os.path.isdir(docs_path):
            return 0

        for root, _, files in os.walk(docs_path):
            for fname in files:
                if not fname.endswith(".md") or fname.startswith("."):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    content = Path(fpath).read_text(
                        encoding="utf-8", errors="replace")[:5000]
                    self.qmd.index(
                        title=fname.replace(".md", "").replace("-", " "),
                        content=content,
                        collection="docs",
                        path=fpath,
                        source_type="doc",
                    )
                    count += 1
                except OSError as e:
                    logger.debug("Skip doc %s: %s", fpath, e)

        logger.info("Indexed %d doc files", count)
        return count

    # ── Full Reindex ──────────────────────────────────────────────────────

    def reindex_all(self, agent_ids: list[str] | None = None) -> dict:
        """Full reindex: clear all collections, re-scan all data sources.

        Args:
            agent_ids: List of agent IDs to index episodes/cases for.
                       If None, auto-detect from memory/agents/ directory.

        Returns dict with counts per collection.
        """
        # Clear everything
        for collection in ["memory", "knowledge", "workspace", "docs"]:
            self.qmd.delete_collection(collection)

        # Auto-detect agents
        if agent_ids is None:
            agents_dir = "memory/agents"
            if os.path.isdir(agents_dir):
                agent_ids = [d for d in os.listdir(agents_dir)
                             if os.path.isdir(os.path.join(agents_dir, d))
                             and not d.startswith(".")]
            else:
                agent_ids = []

        counts = {"memory": 0, "knowledge": 0, "workspace": 0, "docs": 0}

        # Memory: episodes + cases per agent
        for aid in agent_ids:
            counts["memory"] += self.index_episodes(aid)
            counts["memory"] += self.index_cases(aid)

        # Knowledge base
        counts["knowledge"] = self.index_knowledge_base()

        # Workspace
        counts["workspace"] = self.index_workspace()

        # Docs
        counts["docs"] = self.index_docs()

        total = sum(counts.values())
        logger.info("Reindex complete: %d total docs %s", total, counts)
        return counts

    # ── Incremental ───────────────────────────────────────────────────────

    def index_single(self, title: str, content: str,
                     collection: str, **kwargs) -> int:
        """Convenience: index a single document."""
        return self.qmd.index(title=title, content=content,
                              collection=collection, **kwargs)

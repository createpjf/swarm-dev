"""
adapters/memo/exporter.py — Batch / selective Memo export pipeline.

Full pipeline:
    [Cleo raw memories]
    → filter (agent / date / type / score)
    → content assembly (L2 expand)
    → deidentify (regex + optional LLM)
    → quality score (≥ 0.6 gate)
    → MemoObject transform
    → idempotent tracking
    → output (JSON files / Memo API upload)

Usage::

    exporter = MemoExporter(config)
    result = await exporter.export_batch(
        ExportFilter(agents=["jerry"], min_score=7),
        output_dir="memo_export/",
    )
    print(result)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from adapters.memo.tracking import ExportTracker
from adapters.memo.deidentifier import deidentify
from adapters.memo.quality_scorer import score_memory
from adapters.memo.transformer import (
    MemoObject,
    CONTENT_BUILDERS,
    CONVERTERS,
)

if TYPE_CHECKING:
    from adapters.memo.config import MemoConfig

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Filter & Result
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ExportFilter:
    """Criteria for selecting memories to export."""
    agents: list[str] = field(default_factory=list)       # empty = all
    types: list[str] = field(default_factory=list)         # episodic/semantic/procedural
    date_from: str = ""            # YYYY-MM-DD inclusive
    date_to: str = ""              # YYYY-MM-DD inclusive
    min_score: int = 0             # Cleo episode score minimum
    min_quality: float = 0.6       # Memo quality composite minimum
    include_kb: bool = True
    include_patterns: bool = True
    exclude_archived: bool = True


@dataclass
class ExportResult:
    """Statistics from an export run."""
    total_scanned: int = 0
    total_eligible: int = 0
    total_exported: int = 0
    skipped_quality: int = 0
    skipped_duplicate: int = 0
    skipped_error: int = 0
    by_type: dict = field(default_factory=dict)
    output_path: str = ""
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  Exporter
# ══════════════════════════════════════════════════════════════════════════════

class MemoExporter:
    """Batch export pipeline: Cleo memories → Memo MemoryObjects."""

    def __init__(self, config: "MemoConfig",
                 tracker: Optional[ExportTracker] = None):
        self.config = config
        self.tracker = tracker or ExportTracker()

    # ── main entry ────────────────────────────────────────────────────────

    async def export_batch(
        self,
        filt: ExportFilter,
        output_dir: str = "memo_export",
        upload: bool = False,
        dry_run: bool = False,
    ) -> ExportResult:
        """Run the full export pipeline.

        Args:
            filt:       filter criteria
            output_dir: where to write JSON files
            upload:     also upload to Memo API (requires client)
            dry_run:    preview only — no writes, no uploads

        Returns:
            ExportResult with statistics
        """
        t0 = time.monotonic()
        result = ExportResult()

        if not dry_run:
            os.makedirs(output_dir, exist_ok=True)

        # Phase 1 — collect candidates
        candidates = self._collect_candidates(filt)
        result.total_scanned = len(candidates)

        # Phase 2 — process each candidate
        memo_objects: list[MemoObject] = []
        for candidate in candidates:
            src_type = candidate["_source_type"]
            src_id = candidate["_source_id"]

            # Idempotent check
            if self.tracker.is_exported(src_type, src_id):
                result.skipped_duplicate += 1
                continue

            try:
                obj = await self._process_one(candidate, filt)
            except Exception as e:
                result.skipped_error += 1
                result.errors.append(f"{src_type}:{src_id}: {e}")
                continue

            if obj is None:
                result.skipped_quality += 1
                continue

            memo_objects.append(obj)

        result.total_eligible = len(memo_objects)

        # Phase 3 — output
        if not dry_run:
            for obj in memo_objects:
                # Write JSON file
                path = os.path.join(output_dir, f"{obj.id}.json")
                with open(path, "w") as f:
                    json.dump(obj.to_api_payload(), f,
                              ensure_ascii=False, indent=2)
                result.total_exported += 1
                result.by_type[obj.type] = result.by_type.get(obj.type, 0) + 1

                # Track
                self.tracker.record(obj._cleo_source_type,
                                    obj._cleo_source_id, obj.id)

                # Optional upload
                if upload:
                    await self._upload(obj, result)

            self.tracker.save()
        else:
            # Dry run — just count
            for obj in memo_objects:
                result.total_exported += 1
                result.by_type[obj.type] = result.by_type.get(obj.type, 0) + 1

        result.output_path = output_dir
        result.duration_seconds = round(time.monotonic() - t0, 2)
        return result

    # ── single-item processing ────────────────────────────────────────────

    async def _process_one(self, candidate: dict,
                           filt: ExportFilter) -> Optional[MemoObject]:
        """Process one candidate: content → deident → score → transform.

        Returns MemoObject if quality passes, else None.
        """
        src_type = candidate["_source_type"]

        # Build raw content
        builder = CONTENT_BUILDERS.get(src_type)
        if not builder:
            return None
        raw_content = builder(candidate)

        if not raw_content or len(raw_content.strip()) < 50:
            return None  # too short, skip

        # Deidentify
        deidentified, _stats = await deidentify(raw_content, self.config)
        if not deidentified or len(deidentified.strip()) < 30:
            return None

        # Quality score
        quality = score_memory(deidentified, src_type, candidate)
        if not quality["passed"] or quality["composite"] < filt.min_quality:
            return None

        # Transform
        converter = CONVERTERS.get(src_type)
        if not converter:
            return None
        memo_obj = converter(candidate, self.config, deidentified)

        # Set quality score
        memo_obj.signals["quality_score"] = quality["composite"]

        # Type filter (if specified)
        if filt.types and memo_obj.type not in filt.types:
            return None

        return memo_obj

    # ── candidate collection ──────────────────────────────────────────────

    def _collect_candidates(self, filt: ExportFilter) -> list[dict]:
        """Gather all candidate memories from Cleo storage."""
        candidates: list[dict] = []

        agent_ids = filt.agents or self._list_agents()

        for agent_id in agent_ids:
            try:
                self._collect_agent_memories(
                    agent_id, filt, candidates)
            except Exception as e:
                logger.debug("[memo-export] agent %s scan error: %s",
                             agent_id, e)

        # KB Notes (shared, not per-agent)
        if filt.include_kb:
            self._collect_kb_notes(filt, candidates)

        return candidates

    def _collect_agent_memories(self, agent_id: str,
                                filt: ExportFilter,
                                out: list[dict]):
        """Collect episodes, cases, patterns for one agent."""
        try:
            from adapters.memory.episodic import EpisodicMemory
        except ImportError:
            logger.debug("[memo-export] episodic module not available")
            return

        ep = EpisodicMemory(agent_id)

        # ── Episodes ─────────────────────────────────────────────────
        for episode in ep.list_episodes(limit=500, level=2):
            # Date filter
            date = episode.get("date", "")
            if filt.date_from and date < filt.date_from:
                continue
            if filt.date_to and date > filt.date_to:
                continue

            if filt.exclude_archived and episode.get("archived"):
                continue

            # Summary episodes → semantic
            if episode.get("type") == "summary_episode":
                if episode.get("source_count", 0) >= 2:
                    episode["_source_type"] = "summary"
                    episode["_source_id"] = episode.get(
                        "task_id",
                        f"summary_{int(episode.get('created_at', 0))}")
                    out.append(episode)
                continue

            # Regular episodes → episodic (success + high score)
            outcome = episode.get("outcome", "")
            score = episode.get("score")
            if outcome != "success":
                continue
            if score is not None and score < max(filt.min_score, 7):
                continue

            episode["_source_type"] = "episode"
            episode["_source_id"] = episode.get("task_id", "")
            out.append(episode)

        # ── Cases ────────────────────────────────────────────────────
        for case in ep.list_cases(limit=200):
            if len(case.get("solution", "")) <= 100:
                continue
            case["_source_type"] = "case"
            case["_source_id"] = case.get("id", "")
            out.append(case)

        # ── Patterns ─────────────────────────────────────────────────
        if filt.include_patterns:
            for pattern in ep.list_patterns(limit=100):
                if pattern.get("occurrences", 0) < 3:
                    continue
                pattern["_source_type"] = "pattern"
                pattern["_source_id"] = pattern.get("id", "")
                out.append(pattern)

    def _collect_kb_notes(self, filt: ExportFilter, out: list[dict]):
        """Collect shared KB notes."""
        try:
            from adapters.memory.knowledge_base import KnowledgeBase
        except ImportError:
            logger.debug("[memo-export] knowledge_base module not available")
            return

        kb = KnowledgeBase()
        for note in kb.list_notes(limit=200):
            density = note.get("density", "NORMAL")
            update_count = note.get("update_count", 1)
            if density != "HIGH" and update_count < 2:
                continue
            note["_source_type"] = "kb_note"
            note["_source_id"] = note.get("slug", "")
            out.append(note)

    # ── helpers ───────────────────────────────────────────────────────────

    def _list_agents(self) -> list[str]:
        """Discover agent IDs from memory directory."""
        agents_dir = os.path.join("memory", "agents")
        if not os.path.isdir(agents_dir):
            return []
        return [d for d in os.listdir(agents_dir)
                if os.path.isdir(os.path.join(agents_dir, d))
                and not d.startswith(".")]

    async def _upload(self, obj: MemoObject, result: ExportResult):
        """Upload a MemoObject to the Memo API."""
        try:
            from adapters.memo.client import MemoClient
            client = MemoClient(self.config)
            await client.upload_memory(obj.to_api_payload())
            logger.info("[memo] uploaded %s (%s)", obj.id, obj.type)
        except Exception as e:
            result.errors.append(f"upload {obj.id}: {e}")

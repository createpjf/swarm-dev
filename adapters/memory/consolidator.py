"""
adapters/memory/consolidator.py — V0.02 改进 7: MemoryConsolidator

Three-phase pipeline for episodic memory lifecycle management:

  Phase 1 — Cluster: Group old episodes (>7 days) by tag overlap
  Phase 2 — Compress: Merge each cluster into a single SummaryEpisode
  Phase 3 — Promote: High-value summaries (source_count ≥ 3) → KB atomic notes

Safety rules:
  - Only processes episodes older than 7 days
  - Original episodes are marked 'archived' (not deleted)
  - Each compression records provenance (source task IDs)
  - Consolidation log written to memory/consolidation_log.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))  # → project root
CONSOLIDATION_LOG = os.path.join(_PROJECT_ROOT, "memory", "consolidation_log.jsonl")
_MIN_AGE_DAYS = 3       # was 7 — allow faster consolidation
_MIN_PROMOTE_SOURCES = 2  # was 3 — lower bar for KB promotion


class MemoryConsolidator:
    """
    Periodic consolidation pipeline for episodic memories.

    Designed to run as a background task in the orchestrator
    (non-blocking, failure-tolerant).
    """

    def __init__(self, episodic_memory, knowledge_base=None):
        """
        Args:
            episodic_memory: EpisodicMemory instance for the agent.
            knowledge_base: Optional KnowledgeBase instance for KB promotion.
        """
        self.episodic = episodic_memory
        self.kb = knowledge_base
        self._last_run: float = 0.0

    def should_run(self, interval_seconds: int = 86400) -> bool:
        """Check if enough time has passed since the last run."""
        return (time.time() - self._last_run) >= interval_seconds

    def run(self) -> dict:
        """Execute the full consolidation pipeline (sync — use asyncio.to_thread).

        Returns:
            Stats dict: {clustered, compressed, promoted, errors}
        """
        self._last_run = time.time()
        stats = {"clustered": 0, "compressed": 0, "promoted": 0, "errors": 0}

        try:
            # Phase 1: Cluster old episodes
            clusters = self._cluster_episodes()
            stats["clustered"] = len(clusters)

            if not clusters:
                self._log_consolidation(stats)
                return stats

            # Phase 2: Compress each cluster into a summary
            summaries = []
            for cluster in clusters:
                try:
                    summary = self._compress_cluster(cluster)
                    if summary:
                        summaries.append(summary)
                        stats["compressed"] += 1
                except Exception as e:
                    logger.debug("Compress failed for cluster: %s", e)
                    stats["errors"] += 1

            # Phase 3: Promote high-value summaries to KB
            for summary in summaries:
                source_count = summary.get("source_count", 0)
                if source_count >= _MIN_PROMOTE_SOURCES and self.kb:
                    try:
                        self._promote_to_kb(summary)
                        stats["promoted"] += 1
                    except Exception as e:
                        logger.debug("Promote failed: %s", e)
                        stats["errors"] += 1

            # Phase 4: Dedup insights feed
            if self.kb:
                try:
                    dedup_stats = self.kb.dedup_insights()
                    removed = dedup_stats.get("removed", 0)
                    if removed > 0:
                        logger.debug("[%s] insight dedup: %s",
                                     self.episodic.agent_id, dedup_stats)
                        stats["insights_deduped"] = removed
                except Exception as e:
                    logger.debug("Insight dedup failed: %s", e)

            # Log consolidation run
            self._log_consolidation(stats)

        except Exception as e:
            logger.debug("Consolidation pipeline error: %s", e)
            stats["errors"] += 1

        return stats

    # ── Phase 1: Cluster ─────────────────────────────────────────────────

    def _cluster_episodes(self) -> list[list[dict]]:
        """Group old episodes by tag overlap.

        Returns list of clusters, each cluster is a list of episodes.
        """
        cutoff_ts = time.time() - (_MIN_AGE_DAYS * 86400)
        old_episodes = []

        # Collect episodes older than 7 days
        for date_str in self.episodic._list_dates():
            try:
                day_ts = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue

            if day_ts >= cutoff_ts:
                continue  # Too recent

            day_dir = os.path.join(self.episodic.episodes_dir, date_str)
            if not os.path.isdir(day_dir):
                continue

            for fname in os.listdir(day_dir):
                if not fname.endswith(".json") or fname.startswith("."):
                    continue
                path = os.path.join(day_dir, fname)
                try:
                    with open(path) as f:
                        ep = json.load(f)
                    # Skip already archived episodes
                    if ep.get("archived"):
                        continue
                    ep["_path"] = path
                    old_episodes.append(ep)
                except (json.JSONDecodeError, OSError):
                    continue

        if not old_episodes:
            return []

        # Cluster by tag overlap (simple greedy approach)
        clusters: list[list[dict]] = []
        used = set()

        for i, ep in enumerate(old_episodes):
            if i in used:
                continue
            cluster = [ep]
            used.add(i)
            ep_tags = set(ep.get("tags", []))

            for j, other in enumerate(old_episodes):
                if j in used:
                    continue
                other_tags = set(other.get("tags", []))
                # Require at least 1 tag overlap, or same date
                if (ep_tags & other_tags
                        or ep.get("date") == other.get("date")):
                    cluster.append(other)
                    used.add(j)

            clusters.append(cluster)

        return clusters

    # ── Phase 2: Compress ────────────────────────────────────────────────

    def _compress_cluster(self, cluster: list[dict]) -> dict | None:
        """Merge a cluster of episodes into a single SummaryEpisode.

        Returns the summary dict, or None if cluster is too small.
        """
        if len(cluster) < 2:
            # Single episode — just mark as archived, no summary needed
            if cluster:
                self._archive_episode(cluster[0])
            return None

        # Build summary from cluster
        all_tags: set[str] = set()
        all_titles: list[str] = []
        all_task_ids: list[str] = []
        total_score = 0
        score_count = 0
        outcomes: dict[str, int] = defaultdict(int)
        earliest_ts = float("inf")
        latest_ts = 0.0
        content_pieces: list[str] = []

        for ep in cluster:
            all_tags.update(ep.get("tags", []))
            title = ep.get("title", "")
            if title:
                all_titles.append(title)
            task_id = ep.get("task_id", "")
            if task_id:
                all_task_ids.append(task_id)
            score = ep.get("score")
            if score is not None:
                total_score += score
                score_count += 1
            outcome = ep.get("outcome", "unknown")
            outcomes[outcome] += 1
            ts = ep.get("ts", 0)
            earliest_ts = min(earliest_ts, ts)
            latest_ts = max(latest_ts, ts)
            # Collect brief result previews for summary content
            preview = ep.get("result_preview", "")[:200]
            if preview:
                content_pieces.append(f"- {title}: {preview}")

        avg_score = (total_score / score_count) if score_count else None
        dominant_outcome = max(outcomes, key=outcomes.get) if outcomes else "unknown"

        summary = {
            "type": "summary_episode",
            "agent_id": self.episodic.agent_id,
            "source_task_ids": all_task_ids,
            "source_count": len(cluster),
            "tags": sorted(all_tags),
            "titles": all_titles[:10],  # Keep up to 10 titles
            "avg_score": avg_score,
            "dominant_outcome": dominant_outcome,
            "outcome_distribution": dict(outcomes),
            "earliest_ts": earliest_ts,
            "latest_ts": latest_ts,
            "content_summary": "\n".join(content_pieces[:10]),
            "created_at": time.time(),
        }

        # Save summary as a special episode
        summary_id = f"summary_{int(earliest_ts)}_{int(latest_ts)}"
        summary["task_id"] = summary_id

        # Use earliest date for directory
        summary_date = datetime.fromtimestamp(
            earliest_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        summary["date"] = summary_date

        self.episodic.save_episode(summary)

        # Archive original episodes
        for ep in cluster:
            self._archive_episode(ep)

        logger.debug("[%s] compressed %d episodes → %s",
                     self.episodic.agent_id, len(cluster), summary_id)
        return summary

    def _archive_episode(self, episode: dict):
        """Mark an episode as archived (don't delete)."""
        path = episode.get("_path")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            data["archived"] = True
            data["archived_at"] = time.time()
            with open(path, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Archive failed for %s: %s", path, e)

    # ── Phase 3: Promote to KB ───────────────────────────────────────────

    def _promote_to_kb(self, summary: dict):
        """Promote a high-value summary to a KB atomic note.

        Only promotes summaries with source_count ≥ 3.
        Tags the note with density=HIGH.
        """
        if not self.kb:
            return

        topic = f"[{summary['agent_id']}] " + ", ".join(
            summary.get("titles", [])[:3])
        if not topic.strip("[] "):
            topic = f"[{summary['agent_id']}] consolidated episodes"

        content_parts = [
            f"**Sources:** {summary.get('source_count', 0)} episodes",
            f"**Period:** {summary.get('date', 'unknown')}",
            f"**Avg Score:** {summary.get('avg_score', 'N/A')}",
            f"**Outcome:** {summary.get('dominant_outcome', 'unknown')}",
            "",
            summary.get("content_summary", ""),
        ]

        self.kb.create_note(
            topic=topic[:120],
            content="\n".join(content_parts),
            tags=summary.get("tags", []) + ["consolidated", "auto-promoted"],
            author=summary.get("agent_id", "system"),
            density="HIGH",
        )

        logger.debug("[%s] promoted summary to KB: %s",
                     summary.get("agent_id"), topic[:60])

    # ── Logging ──────────────────────────────────────────────────────────

    def _log_consolidation(self, stats: dict):
        """Append consolidation stats to the log file."""
        entry = {
            "ts": time.time(),
            "agent_id": self.episodic.agent_id,
            **stats,
        }
        try:
            os.makedirs(os.path.dirname(CONSOLIDATION_LOG) or ".", exist_ok=True)
            with open(CONSOLIDATION_LOG, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.debug("Failed to write consolidation log: %s", e)

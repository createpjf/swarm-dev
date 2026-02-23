"""
core/doc_updater.py
Automatic document updater — monitors evolution triggers and consolidates
lessons into per-agent documentation.

Triggered by:
  - Evolution engine execution (writes lessons.md)
  - Repeated failure patterns (same error_type 3+ times)

Outputs:
  - docs/{agent_id}/lessons.md    — per-evolution-trigger lessons
  - memory/agents/{agent_id}/MEMORY.md — P1 section with consolidated lessons
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Threshold: how many same-type failures before auto-documenting
ERROR_REPEAT_THRESHOLD = 3


class DocUpdater:
    """Monitors agent episodes and updates documentation automatically."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.docs_dir = f"docs/{agent_id}"
        self.memory_dir = f"memory/agents/{agent_id}"
        os.makedirs(self.docs_dir, exist_ok=True)
        os.makedirs(self.memory_dir, exist_ok=True)

    def check_and_update(self):
        """Run all auto-update checks. Called periodically or after task completion."""
        self._check_error_patterns()
        self._consolidate_lessons()

    def _check_error_patterns(self):
        """Scan recent episodes for repeated error patterns → auto-document."""
        episodes_dir = os.path.join(self.memory_dir, "episodes")
        if not os.path.isdir(episodes_dir):
            return

        # Count error types across recent episodes
        error_counts: dict[str, list[dict]] = {}
        dates = sorted(
            [d for d in os.listdir(episodes_dir)
             if os.path.isdir(os.path.join(episodes_dir, d))],
            reverse=True)[:7]  # Last 7 days

        for date_str in dates:
            day_dir = os.path.join(episodes_dir, date_str)
            for fname in os.listdir(day_dir):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(day_dir, fname)) as f:
                        ep = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue

                error_type = ep.get("error_type")
                outcome = ep.get("outcome", "")
                if error_type and outcome in ("failure", "needs_improvement"):
                    error_counts.setdefault(error_type, []).append(ep)

        # Document patterns that exceed threshold
        lessons_path = os.path.join(self.docs_dir, "lessons.md")
        existing = ""
        if os.path.exists(lessons_path):
            with open(lessons_path) as f:
                existing = f.read()

        new_entries = []
        for error_type, episodes in error_counts.items():
            if len(episodes) < ERROR_REPEAT_THRESHOLD:
                continue
            # Check if already documented
            marker = f"[auto:pattern:{error_type}]"
            if marker in existing:
                continue

            # Generate lesson from pattern
            sample_descs = [ep.get("title", "?")[:80] for ep in episodes[:3]]
            entry = (
                f"\n## {time.strftime('%Y-%m-%d')} — Recurring error: {error_type} "
                f"{marker}\n"
                f"- **Occurrences**: {len(episodes)} in last 7 days\n"
                f"- **Sample tasks**:\n"
                + "".join(f"  - {d}\n" for d in sample_descs)
                + f"- **Action**: Investigate root cause of {error_type} errors. "
                f"Consider prompt adjustments or tool configuration changes.\n"
            )
            new_entries.append(entry)

        if new_entries:
            with open(lessons_path, "a") as f:
                for entry in new_entries:
                    f.write(entry)
            logger.info("[doc_updater] wrote %d new error pattern lessons for %s",
                        len(new_entries), self.agent_id)

    def _consolidate_lessons(self):
        """Merge lessons into MEMORY.md P1 section (daily, max once per day)."""
        lessons_path = os.path.join(self.docs_dir, "lessons.md")
        memory_path = os.path.join(self.memory_dir, "MEMORY.md")

        if not os.path.exists(lessons_path):
            return

        # Check if already consolidated today
        marker_file = os.path.join(self.memory_dir, ".last_consolidation")
        today = time.strftime("%Y-%m-%d")
        if os.path.exists(marker_file):
            try:
                with open(marker_file) as f:
                    if f.read().strip() == today:
                        return  # Already done today
            except OSError:
                pass

        # Read lessons
        try:
            with open(lessons_path) as f:
                lessons_content = f.read()
        except OSError:
            return

        if not lessons_content.strip():
            return

        # Count lessons
        lesson_count = lessons_content.count("## ")

        # Build P1 summary
        p1_section = (
            f"\n## P1 — Lessons Learned ({today})\n"
            f"Total evolution events: {lesson_count}\n"
            f"See `docs/{self.agent_id}/lessons.md` for full details.\n"
        )

        # Extract most recent 3 lessons as summary
        sections = lessons_content.split("## ")[1:]  # Skip header
        recent = sections[-3:] if len(sections) > 3 else sections
        for sec in recent:
            first_line = sec.split("\n")[0].strip()
            if first_line:
                p1_section += f"- {first_line}\n"

        # Update MEMORY.md
        existing = ""
        if os.path.exists(memory_path):
            with open(memory_path) as f:
                existing = f.read()

        # Replace existing P1 section or append
        p1_marker = "## P1 — Lessons Learned"
        if p1_marker in existing:
            # Find and replace existing P1 section
            start = existing.index(p1_marker)
            # Find next ## or end of file
            next_section = existing.find("\n## ", start + len(p1_marker))
            if next_section > 0:
                existing = existing[:start] + p1_section.strip() + "\n" + existing[next_section:]
            else:
                existing = existing[:start] + p1_section.strip() + "\n"
        else:
            existing = existing.rstrip() + "\n" + p1_section

        with open(memory_path, "w") as f:
            f.write(existing.strip() + "\n")

        # Mark as done today
        with open(marker_file, "w") as f:
            f.write(today)

        logger.info("[doc_updater] consolidated lessons into MEMORY.md for %s", self.agent_id)

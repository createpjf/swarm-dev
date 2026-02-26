"""
reputation/textgrad.py — V0.02 改进 8: TextGrad Pipeline

Four-step feedback loop that converts accumulated CritiqueSpec reviews
into auto-injected skill improvements for each agent.

Pipeline steps:
  1. Accumulate  — CritiqueSpec → critique_log.jsonl (done by orchestrator)
  2. Aggregate   — Every 20 entries: extract recurring issues (≥3 occurrences)
  3. Inject      — Write improvement patches to skills/agent_overrides/{id}_textgrad.md
  4. Decay       — Remove patches for issues that no longer recur in recent reviews

The output files are hot-loaded by SkillLoader on each agent.run() call.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

CRITIQUE_LOG_FILE = os.path.join("memory", "critique_log.jsonl")
OVERRIDES_DIR = os.path.join("skills", "agent_overrides")
_AGGREGATE_THRESHOLD = 20   # Trigger aggregation every N entries
_RECURRENCE_MIN = 3         # Issue must appear ≥3 times to become a patch
_DECAY_WINDOW = 40          # Look at last N entries for decay check
_DECAY_THRESHOLD = 2        # Issue appears <2 times in window → decayed


class TextGradPipeline:
    """
    Converts repeated critique feedback into agent skill patches.

    Designed to run as a periodic background task (non-blocking).
    """

    def __init__(self):
        self._last_line_count: int = 0
        self._last_run: float = 0.0

    def should_run(self, interval_seconds: int = 60) -> bool:
        """Check if enough time has passed and new entries exist."""
        if (time.time() - self._last_run) < interval_seconds:
            return False
        # Quick check: count lines in critique_log
        try:
            count = _count_lines(CRITIQUE_LOG_FILE)
            return count >= self._last_line_count + _AGGREGATE_THRESHOLD
        except Exception:
            return False

    def run(self) -> dict:
        """Execute the full TextGrad pipeline (sync — use asyncio.to_thread).

        Returns:
            Stats dict: {entries_processed, agents_patched, issues_found, decayed}
        """
        self._last_run = time.time()
        stats = {
            "entries_processed": 0,
            "agents_patched": 0,
            "issues_found": 0,
            "decayed": 0,
        }

        try:
            # Load all critique entries
            entries = _load_critique_log()
            stats["entries_processed"] = len(entries)
            self._last_line_count = len(entries)

            if len(entries) < _AGGREGATE_THRESHOLD:
                return stats

            # Group entries by agent_id
            by_agent: dict[str, list[dict]] = defaultdict(list)
            for entry in entries:
                agent_id = entry.get("agent_id")
                if agent_id:
                    by_agent[agent_id].append(entry)

            # Process each agent
            for agent_id, agent_entries in by_agent.items():
                result = self._process_agent(agent_id, agent_entries)
                if result.get("patched"):
                    stats["agents_patched"] += 1
                stats["issues_found"] += result.get("issues", 0)
                stats["decayed"] += result.get("decayed", 0)

        except Exception as e:
            logger.debug("TextGrad pipeline error: %s", e)

        return stats

    def _process_agent(self, agent_id: str,
                       entries: list[dict]) -> dict:
        """Aggregate + Inject + Decay for one agent.

        Returns: {patched: bool, issues: int, decayed: int}
        """
        result = {"patched": False, "issues": 0, "decayed": 0}

        # Step 2: Aggregate — find recurring issues
        issue_counter: Counter = Counter()
        for entry in entries:
            items = entry.get("items", [])
            for item in items:
                issue_text = item.get("issue", "").strip()
                if issue_text:
                    # Normalize: lowercase first 60 chars as key
                    key = issue_text[:60].lower()
                    issue_counter[key] += 1

        # Find issues that recur ≥ _RECURRENCE_MIN times
        recurring = {
            issue: count
            for issue, count in issue_counter.items()
            if count >= _RECURRENCE_MIN
        }
        result["issues"] = len(recurring)

        if not recurring:
            return result

        # Step 4: Decay — check if issues still appear in recent window
        recent = entries[-_DECAY_WINDOW:] if len(entries) > _DECAY_WINDOW else entries
        recent_issues: Counter = Counter()
        for entry in recent:
            for item in entry.get("items", []):
                issue_text = item.get("issue", "").strip()
                if issue_text:
                    key = issue_text[:60].lower()
                    recent_issues[key] += 1

        # Separate active vs decayed
        active_issues: dict[str, int] = {}
        decayed_issues: list[str] = []
        for issue, total_count in recurring.items():
            recent_count = recent_issues.get(issue, 0)
            if recent_count >= _DECAY_THRESHOLD:
                active_issues[issue] = total_count
            else:
                decayed_issues.append(issue)
                result["decayed"] += 1

        # Step 3: Inject — write skill patch file
        if active_issues:
            self._write_patch(agent_id, active_issues)
            result["patched"] = True

            # Also write GradientSignal for tracking
            self._write_gradient_signal(
                agent_id, active_issues, decayed_issues, entries)
        else:
            # All issues decayed — remove patch file
            self._remove_patch(agent_id)

        return result

    def _write_patch(self, agent_id: str,
                     active_issues: dict[str, int]):
        """Write improvement patches to the agent override file."""
        os.makedirs(OVERRIDES_DIR, exist_ok=True)
        path = os.path.join(OVERRIDES_DIR, f"{agent_id}_textgrad.md")

        lines = [
            "# TextGrad Auto-Improvements",
            "",
            f"_Auto-generated from {sum(active_issues.values())} "
            f"critique observations. Updated: "
            f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime())}_",
            "",
            "## Known Issues to Avoid",
            "",
        ]

        for issue, count in sorted(
                active_issues.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- **[{count}x]** {issue}")

        lines.extend([
            "",
            "## Improvement Guidelines",
            "",
            "Based on recurring feedback, pay special attention to:",
        ])

        for i, (issue, count) in enumerate(
                sorted(active_issues.items(),
                       key=lambda x: x[1], reverse=True)[:5], 1):
            lines.append(f"{i}. Address: {issue}")

        content = "\n".join(lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("[textgrad] wrote patch for %s: %d active issues",
                    agent_id, len(active_issues))

    def _remove_patch(self, agent_id: str):
        """Remove the textgrad patch file when all issues have decayed."""
        path = os.path.join(OVERRIDES_DIR, f"{agent_id}_textgrad.md")
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info("[textgrad] removed decayed patch for %s",
                            agent_id)
            except OSError:
                pass

    def _write_gradient_signal(self, agent_id: str,
                               active_issues: dict[str, int],
                               decayed_issues: list[str],
                               entries: list[dict]):
        """Write a GradientSignal record for tracking/debugging."""
        try:
            from core.protocols import GradientSignal
            signal = GradientSignal(
                agent_id=agent_id,
                recurring_issues=list(active_issues.keys()),
                improvement_patches=[
                    f"Avoid: {issue}" for issue in active_issues],
                source_critique_ids=[
                    e.get("task_id", "") for e in entries[-10:]],
                generated_at=time.time(),
                decayed_issues=decayed_issues,
            )
            # Save to memory dir
            signal_path = os.path.join(
                "memory", f"gradient_signal_{agent_id}.json")
            os.makedirs(os.path.dirname(signal_path), exist_ok=True)
            with open(signal_path, "w", encoding="utf-8") as f:
                f.write(signal.to_json())
        except Exception as e:
            logger.debug("[textgrad] gradient signal write failed: %s", e)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _count_lines(path: str) -> int:
    """Count lines in a file efficiently."""
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, "rb") as f:
        for _ in f:
            count += 1
    return count


def _load_critique_log() -> list[dict]:
    """Load all entries from the critique log."""
    if not os.path.exists(CRITIQUE_LOG_FILE):
        return []
    entries = []
    with open(CRITIQUE_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries

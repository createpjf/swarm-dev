"""
adapters/memory/episodic.py
Three-layer episodic memory inspired by OpenViking's progressive loading.

Layer 0 (L0) — Atomic Index:  ~100 tokens per entry (title + tags + score)
Layer 1 (L1) — Overview:      ~500 tokens (summary + key decisions + outcome)
Layer 2 (L2) — Full Detail:   Complete task input/output (stored but loaded on demand)

Storage layout:
  memory/agents/{agent_id}/
    episodes/
      {date}/
        {task_id}.json          # Full L2 episode
    daily/
      {date}.md                 # Daily learning log (auto-generated)
    cases/
      {case_hash}.json          # Extracted problem→solution cases
    patterns/
      {pattern_hash}.json       # Recurring patterns across tasks

OpenViking concepts adapted:
  - L0/L1/L2 progressive loading (token-budget-aware)
  - 6-category extraction: profile, preferences, entities, events (user-owned);
    cases, patterns (agent-owned)
  - Session commit as crystallization point
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

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _episode_id(task_id: str) -> str:
    return task_id


def _hash_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# ── Episode data structure ─────────────────────────────────────────────────

def make_episode(
    agent_id: str,
    task_id: str,
    task_description: str,
    result: str,
    score: Optional[int] = None,
    tags: Optional[list[str]] = None,
    context: Optional[dict] = None,
) -> dict:
    """Create a structured episode from a completed task."""
    now = time.time()
    # L0: compact index entry (~100 tokens)
    title = task_description[:120]
    l0 = {
        "task_id": task_id,
        "agent_id": agent_id,
        "title": title,
        "tags": tags or [],
        "score": score,
        "ts": now,
        "date": _today(),
    }
    # L1: overview (~500 tokens)
    # Truncate result for overview
    result_preview = result[:1500] if len(result) > 1500 else result
    l1 = {
        **l0,
        "description": task_description,
        "result_preview": result_preview,
        "outcome": "success" if (score is None or score >= 50) else "needs_improvement",
    }
    # L2: full detail
    l2 = {
        **l1,
        "result_full": result,
        "context": context or {},
        "result_length": len(result),
    }
    return l2


# ── Episodic Memory Store ──────────────────────────────────────────────────

class EpisodicMemory:
    """
    Per-agent episodic memory with progressive loading.

    Stores task episodes, daily learning logs, extracted cases and patterns.
    Integrates with the existing HybridMemory for vector+keyword retrieval.
    """

    def __init__(self, agent_id: str, base_dir: str = "memory/agents"):
        self.agent_id = agent_id
        self.base = os.path.join(base_dir, agent_id)
        self.episodes_dir = os.path.join(self.base, "episodes")
        self.daily_dir = os.path.join(self.base, "daily")
        self.cases_dir = os.path.join(self.base, "cases")
        self.patterns_dir = os.path.join(self.base, "patterns")

        for d in [self.episodes_dir, self.daily_dir,
                  self.cases_dir, self.patterns_dir]:
            os.makedirs(d, exist_ok=True)

    # ── Episode CRUD ──────────────────────────────────────────────────────

    def save_episode(self, episode: dict) -> str:
        """Save a full L2 episode. Returns file path."""
        date = episode.get("date", _today())
        task_id = episode["task_id"]
        day_dir = os.path.join(self.episodes_dir, date)
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, f"{task_id}.json")
        with open(path, "w") as f:
            json.dump(episode, f, ensure_ascii=False, indent=2)
        logger.debug("[%s] saved episode %s", self.agent_id, task_id)
        return path

    def load_episode(self, task_id: str, date: Optional[str] = None,
                     level: int = 1) -> Optional[dict]:
        """
        Load an episode at specified level.
        level=0: index only (title, tags, score, ts)
        level=1: overview (+ description, result_preview, outcome)
        level=2: full detail (+ result_full, context)
        """
        # Search in date dir or scan all dates
        dates_to_check = [date] if date else self._list_dates()
        for d in dates_to_check:
            path = os.path.join(self.episodes_dir, d, f"{task_id}.json")
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        ep = json.load(f)
                    return self._trim_to_level(ep, level)
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    def _trim_to_level(self, episode: dict, level: int) -> dict:
        """Return episode trimmed to the requested level."""
        if level == 0:
            return {k: episode.get(k) for k in
                    ["task_id", "agent_id", "title", "tags", "score", "ts", "date"]}
        elif level == 1:
            keys = ["task_id", "agent_id", "title", "tags", "score", "ts",
                    "date", "description", "result_preview", "outcome"]
            return {k: episode.get(k) for k in keys}
        else:
            return episode

    def list_episodes(self, limit: int = 50, level: int = 0) -> list[dict]:
        """List recent episodes at specified level, newest first."""
        episodes = []
        for date_str in sorted(self._list_dates(), reverse=True):
            day_dir = os.path.join(self.episodes_dir, date_str)
            for fname in sorted(os.listdir(day_dir), reverse=True):
                if not fname.endswith(".json") or fname.startswith("."):
                    continue
                path = os.path.join(day_dir, fname)
                try:
                    with open(path) as f:
                        ep = json.load(f)
                    episodes.append(self._trim_to_level(ep, level))
                except (json.JSONDecodeError, OSError):
                    continue
                if len(episodes) >= limit:
                    return episodes
        return episodes

    def _list_dates(self) -> list[str]:
        """List all date directories in episodes/."""
        if not os.path.isdir(self.episodes_dir):
            return []
        return [d for d in os.listdir(self.episodes_dir)
                if os.path.isdir(os.path.join(self.episodes_dir, d))
                and re.match(r"\d{4}-\d{2}-\d{2}", d)]

    # ── Daily Learning Log ────────────────────────────────────────────────

    def get_daily_log(self, date: Optional[str] = None) -> str:
        """Read today's daily learning log."""
        date = date or _today()
        path = os.path.join(self.daily_dir, f"{date}.md")
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
        return ""

    def append_daily_log(self, entry: str, date: Optional[str] = None):
        """Append an entry to today's daily log."""
        date = date or _today()
        path = os.path.join(self.daily_dir, f"{date}.md")
        header = f"# {self.agent_id} — {date}\n\n"
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(header)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with open(path, "a") as f:
            f.write(f"## [{ts}]\n{entry}\n\n")

    def generate_daily_summary(self, date: Optional[str] = None) -> str:
        """
        Generate a daily summary from all episodes of the day.
        Returns markdown text suitable for the daily log.
        """
        date = date or _today()
        day_dir = os.path.join(self.episodes_dir, date)
        if not os.path.isdir(day_dir):
            return ""

        episodes = []
        for fname in sorted(os.listdir(day_dir)):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(day_dir, fname)) as f:
                    ep = json.load(f)
                episodes.append(ep)
            except (json.JSONDecodeError, OSError):
                continue

        if not episodes:
            return ""

        lines = [f"# Daily Summary — {self.agent_id} — {date}\n"]
        lines.append(f"**Tasks completed:** {len(episodes)}\n")

        scores = [ep["score"] for ep in episodes if ep.get("score") is not None]
        if scores:
            lines.append(f"**Average score:** {sum(scores)/len(scores):.0f}\n")

        lines.append("\n## Tasks\n")
        for ep in episodes:
            score_str = f" (score: {ep['score']})" if ep.get("score") else ""
            lines.append(f"- **{ep.get('title', 'untitled')}**{score_str}")
            if ep.get("outcome"):
                lines.append(f"  - Outcome: {ep['outcome']}")

        return "\n".join(lines)

    # ── Cases (problem → solution) ────────────────────────────────────────

    def save_case(self, problem: str, solution: str,
                  tags: Optional[list[str]] = None,
                  source_task_id: Optional[str] = None) -> str:
        """
        Save a reusable case (problem→solution pair).
        OpenViking 'cases' category — agent-owned knowledge.
        """
        key = _hash_key(problem)
        case = {
            "id": key,
            "problem": problem,
            "solution": solution,
            "tags": tags or [],
            "source_task_id": source_task_id,
            "agent_id": self.agent_id,
            "created_at": time.time(),
            "use_count": 0,
        }
        path = os.path.join(self.cases_dir, f"{key}.json")
        with open(path, "w") as f:
            json.dump(case, f, ensure_ascii=False, indent=2)
        logger.debug("[%s] saved case %s", self.agent_id, key)
        return key

    def search_cases(self, query: str, limit: int = 5) -> list[dict]:
        """Simple keyword search over cases."""
        results = []
        query_lower = query.lower()
        for fname in os.listdir(self.cases_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(self.cases_dir, fname)) as f:
                    case = json.load(f)
                # Score by keyword overlap
                text = (case.get("problem", "") + " " +
                        case.get("solution", "") + " " +
                        " ".join(case.get("tags", []))).lower()
                score = sum(1 for word in query_lower.split() if word in text)
                if score > 0:
                    case["_match_score"] = score
                    results.append(case)
            except (json.JSONDecodeError, OSError):
                continue
        results.sort(key=lambda x: x.get("_match_score", 0), reverse=True)
        return results[:limit]

    def list_cases(self, limit: int = 20) -> list[dict]:
        """List all cases, newest first."""
        cases = []
        for fname in os.listdir(self.cases_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(self.cases_dir, fname)) as f:
                    cases.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
        cases.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return cases[:limit]

    # ── Patterns (recurring observations) ─────────────────────────────────

    def save_pattern(self, pattern: str, evidence: list[str],
                     tags: Optional[list[str]] = None) -> str:
        """
        Save a recurring pattern observed across tasks.
        OpenViking 'patterns' category — agent-owned meta-knowledge.
        """
        key = _hash_key(pattern)
        path = os.path.join(self.patterns_dir, f"{key}.json")

        # Merge with existing if same pattern
        existing = None
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        if existing:
            existing["evidence"].extend(evidence)
            existing["evidence"] = existing["evidence"][-20:]  # Keep last 20
            existing["occurrences"] = existing.get("occurrences", 1) + 1
            existing["updated_at"] = time.time()
            data = existing
        else:
            data = {
                "id": key,
                "pattern": pattern,
                "evidence": evidence[-20:],
                "tags": tags or [],
                "agent_id": self.agent_id,
                "occurrences": 1,
                "created_at": time.time(),
                "updated_at": time.time(),
            }

        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return key

    def list_patterns(self, limit: int = 10) -> list[dict]:
        """List patterns sorted by occurrence frequency."""
        patterns = []
        for fname in os.listdir(self.patterns_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            try:
                with open(os.path.join(self.patterns_dir, fname)) as f:
                    patterns.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
        patterns.sort(key=lambda x: x.get("occurrences", 0), reverse=True)
        return patterns[:limit]

    # ── Progressive Recall (L0→L1→L2 budget-aware) ────────────────────────

    def recall(self, query: str, token_budget: int = 2000,
               max_episodes: int = 5) -> str:
        """
        Progressive recall: fit as much relevant context as possible
        within the token budget.

        Strategy (OpenViking L0/L1/L2):
        1. Load L0 index of recent episodes → pick top-N relevant
        2. Load L1 overviews for top matches
        3. If budget remains, load matching cases
        4. If budget remains, load relevant patterns

        Returns formatted markdown text for injection into system prompt.
        """
        CHARS_PER_TOKEN = 3
        budget_chars = token_budget * CHARS_PER_TOKEN
        parts = []
        used = 0

        # 1. Search cases first (most actionable)
        cases = self.search_cases(query, limit=3)
        if cases:
            case_section = "### Relevant Cases\n"
            for c in cases:
                entry = (f"- **Problem:** {c['problem'][:200]}\n"
                         f"  **Solution:** {c['solution'][:300]}\n")
                if used + len(entry) > budget_chars:
                    break
                case_section += entry
                used += len(entry)
            if len(case_section) > 25:
                parts.append(case_section)

        # 2. Recent episodes (L1 level)
        episodes = self.list_episodes(limit=max_episodes * 2, level=1)
        if episodes:
            # Score episodes by query relevance
            query_lower = query.lower()
            scored = []
            for ep in episodes:
                text = (ep.get("title", "") + " " +
                        ep.get("description", "") + " " +
                        " ".join(ep.get("tags", []))).lower()
                score = sum(1 for w in query_lower.split() if w in text)
                # Recency bonus
                age_days = (time.time() - ep.get("ts", 0)) / 86400
                recency = max(0, 1.0 - age_days / 30)  # decay over 30 days
                scored.append((ep, score + recency))
            scored.sort(key=lambda x: x[1], reverse=True)

            ep_section = "### Past Experiences\n"
            count = 0
            for ep, _score in scored[:max_episodes]:
                entry = (f"- [{ep.get('date','')}] **{ep.get('title','')}** "
                         f"({ep.get('outcome','?')})\n")
                preview = ep.get("result_preview", "")
                if preview:
                    # Truncate preview to fit budget
                    max_preview = min(400, budget_chars - used - len(entry) - 50)
                    if max_preview > 50:
                        entry += f"  > {preview[:max_preview]}...\n"
                if used + len(entry) > budget_chars:
                    break
                ep_section += entry
                used += len(entry)
                count += 1
            if count > 0:
                parts.append(ep_section)

        # 3. Patterns
        patterns = self.list_patterns(limit=3)
        if patterns:
            pat_section = "### Learned Patterns\n"
            for p in patterns:
                entry = (f"- {p['pattern']} "
                         f"(seen {p.get('occurrences',1)}x)\n")
                if used + len(entry) > budget_chars:
                    break
                pat_section += entry
                used += len(entry)
            if len(pat_section) > 25:
                parts.append(pat_section)

        if not parts:
            return ""

        return "## Long-Term Memory Recall\n" + "\n".join(parts)

    # ── Lifecycle Management (TTL / Cleanup / Archival) ───────────────────

    def cleanup(self, max_age_days: int = 90, max_episodes: int = 500) -> dict:
        """
        Clean up old episodes beyond TTL or count limit.
        Keeps daily logs and patterns (they're already compact).
        Returns: {archived: int, deleted_dates: []}
        """
        archived = 0
        deleted_dates = []
        now_ts = time.time()
        cutoff = now_ts - (max_age_days * 86400)

        all_dates = sorted(self._list_dates())
        total_episodes = 0

        # Count total episodes
        for d in all_dates:
            day_dir = os.path.join(self.episodes_dir, d)
            total_episodes += len([f for f in os.listdir(day_dir)
                                   if f.endswith(".json")])

        # Archive old dates (beyond TTL)
        for d in all_dates:
            day_dir = os.path.join(self.episodes_dir, d)
            try:
                # Parse date to timestamp
                from datetime import datetime as _dt, timezone as _tz
                day_ts = _dt.strptime(d, "%Y-%m-%d").replace(
                    tzinfo=_tz.utc).timestamp()
            except ValueError:
                continue

            if day_ts < cutoff or total_episodes > max_episodes:
                # Archive: compress to daily summary, then delete episodes
                summary = self.generate_daily_summary(d)
                if summary:
                    self.append_daily_log(
                        f"[ARCHIVED] {summary[:500]}", date=d)

                # Delete individual episode files
                for fname in os.listdir(day_dir):
                    if fname.endswith(".json"):
                        os.remove(os.path.join(day_dir, fname))
                        archived += 1
                        total_episodes -= 1

                # Remove empty directory
                try:
                    os.rmdir(day_dir)
                except OSError:
                    pass
                deleted_dates.append(d)

        if archived:
            logger.info("[%s] cleaned up %d episodes from %d dates",
                       self.agent_id, archived, len(deleted_dates))

        return {"archived": archived, "deleted_dates": deleted_dates}

    def get_storage_size(self) -> dict:
        """Get storage usage stats for this agent's memory."""
        total_bytes = 0
        file_count = 0
        for dirpath, _, filenames in os.walk(self.base):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total_bytes += os.path.getsize(fp)
                    file_count += 1
                except OSError:
                    pass
        return {
            "agent_id": self.agent_id,
            "total_bytes": total_bytes,
            "total_kb": round(total_bytes / 1024, 1),
            "file_count": file_count,
        }

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return memory statistics."""
        episode_count = 0
        for d in self._list_dates():
            day_dir = os.path.join(self.episodes_dir, d)
            episode_count += len([f for f in os.listdir(day_dir)
                                  if f.endswith(".json") and not f.startswith(".")])

        case_count = len([f for f in os.listdir(self.cases_dir)
                          if f.endswith(".json") and not f.startswith(".")])
        pattern_count = len([f for f in os.listdir(self.patterns_dir)
                             if f.endswith(".json") and not f.startswith(".")])
        daily_count = len([f for f in os.listdir(self.daily_dir)
                           if f.endswith(".md") and not f.startswith(".")])

        return {
            "agent_id": self.agent_id,
            "episodes": episode_count,
            "cases": case_count,
            "patterns": pattern_count,
            "daily_logs": daily_count,
            "dates": sorted(self._list_dates()),
        }

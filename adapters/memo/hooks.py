"""
adapters/memo/hooks.py — Runtime hooks for Memo Protocol integration.

Post-task hook:
    Called after task completion in orchestrator._extract_and_store_memories().
    Auto-uploads successful, high-quality episodes to Memo.

Pre-task hook (future):
    Searches skills/memo/ for relevant skills to inject into task context.

Both hooks are non-blocking and fault-tolerant — Memo failures never
affect core task execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapters.memo.config import MemoConfig

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Post-task auto-upload hook
# ══════════════════════════════════════════════════════════════════════════════

async def post_task_memo_hook(
    agent_id: str,
    task_id: str,
    outcome: str,
    score: int | None,
    config: "MemoConfig",
) -> None:
    """Auto-upload hook called after task completion.

    Injection point: ``core/orchestrator.py`` after
    ``_extract_and_store_memories()``.

    Conditions for upload:
        - config.memo.enabled AND config.memo.auto_upload.enabled
        - outcome == "success"
        - score is None or score >= 7
        - quality composite >= config.auto_upload_min_quality

    This function is meant to be wrapped in ``asyncio.create_task()``
    so it never blocks the main pipeline.
    """
    if not config.enabled or not config.auto_upload_enabled:
        return

    try:
        from adapters.memo.tracking import ExportTracker
        tracker = ExportTracker()

        # Already exported?
        if tracker.is_exported("episode", task_id):
            return

        # Load full episode (L2)
        from adapters.memory.episodic import EpisodicMemory
        ep = EpisodicMemory(agent_id)
        episode = ep.load_episode(task_id, level=2)
        if not episode:
            return

        # Outcome / score gate
        ep_outcome = episode.get("outcome", "")
        ep_score = episode.get("score")
        if ep_outcome != "success":
            return
        if ep_score is not None and ep_score < 7:
            return

        # Build content
        from adapters.memo.transformer import _build_episode_content, episode_to_memo
        raw_content = _build_episode_content(episode)
        if len(raw_content.strip()) < 50:
            return

        # Deidentify
        from adapters.memo.deidentifier import deidentify
        deidentified, _stats = await deidentify(raw_content, config)
        if len(deidentified.strip()) < 30:
            return

        # Quality score
        from adapters.memo.quality_scorer import score_memory
        quality = score_memory(deidentified, "episode", episode)
        if not quality["passed"]:
            return
        if quality["composite"] < config.auto_upload_min_quality:
            return

        # Transform
        memo_obj = episode_to_memo(episode, config, deidentified)
        memo_obj.signals["quality_score"] = quality["composite"]

        # Upload
        from adapters.memo.client import MemoClient
        client = MemoClient(config)
        resp = await client.upload_memory(memo_obj.to_api_payload())

        # Track
        tracker.record("episode", task_id, memo_obj.id)
        tracker.save()

        logger.info("[memo] auto-uploaded %s → %s (quality=%.2f)",
                    task_id, memo_obj.id, quality["composite"])

    except ImportError as e:
        logger.debug("[memo] hook skipped (missing dep): %s", e)
    except Exception as e:
        # Never let Memo failures affect core pipeline
        logger.debug("[memo] auto-upload failed (non-critical): %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  Pre-task skill injection hook (future enhancement)
# ══════════════════════════════════════════════════════════════════════════════

def find_relevant_memo_skills(task_description: str,
                              max_skills: int = 3) -> list[str]:
    """Search local ``skills/memo/`` for relevant skills.

    Returns list of skill file paths (most relevant first).
    This is a simple keyword-match implementation; a future version
    could use embedding similarity.
    """
    import os

    skills_dir = os.path.join("skills", "memo")
    if not os.path.isdir(skills_dir):
        return []

    desc_lower = task_description.lower()
    scored: list[tuple[float, str]] = []

    for fname in os.listdir(skills_dir):
        if not fname.endswith(".md") or fname.startswith("."):
            continue
        path = os.path.join(skills_dir, fname)
        try:
            with open(path) as f:
                content = f.read(2000)  # read head only
        except OSError:
            continue

        # Simple keyword overlap score
        content_lower = content.lower()
        words = set(desc_lower.split())
        matches = sum(1 for w in words if w in content_lower and len(w) > 3)
        if matches > 0:
            scored.append((matches, path))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [path for _, path in scored[:max_skills]]

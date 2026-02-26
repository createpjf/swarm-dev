"""
adapters/memo/importer.py — Import Memo Skills into Cleo's skill system.

Purchased Memo Skills are written to ``skills/memo/`` as Markdown files
with YAML frontmatter, compatible with Cleo's ``SkillLoader``.

An optional symlink mechanism lets skills be shared to specific agents
under ``skills/agents/{agent_id}/``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from adapters.memo.client import MemoClient
    from adapters.memo.config import MemoConfig

logger = logging.getLogger(__name__)

MEMO_SKILLS_DIR = os.path.join("skills", "memo")


class MemoImporter:
    """Pull Memo Skills and inject them into Cleo's skill directory."""

    def __init__(self, config: "MemoConfig", client: "MemoClient"):
        self.config = config
        self.client = client
        os.makedirs(MEMO_SKILLS_DIR, exist_ok=True)

    # ── public API ────────────────────────────────────────────────────────

    async def sync_skills(
        self,
        memory_ids: Optional[list[str]] = None,
    ) -> dict:
        """Synchronize Memo Skills to local directory.

        If ``memory_ids`` is None, uses the export tracker to get
        previously exported IDs.

        Returns stats dict: ``{fetched, written, updated, errors}``.
        """
        stats = {"fetched": 0, "written": 0, "updated": 0, "errors": 0}

        if not memory_ids:
            try:
                from adapters.memo.tracking import ExportTracker
                tracker = ExportTracker()
                memory_ids = tracker.all_memo_ids()
            except Exception:
                memory_ids = []

        if not memory_ids:
            logger.debug("[memo-import] no memory IDs to sync")
            return stats

        try:
            skills = await self.client.sync_skills(memory_ids)
            stats["fetched"] = len(skills)
        except Exception as e:
            stats["errors"] += 1
            logger.error("[memo-import] skill sync failed: %s", e)
            return stats

        for skill in skills:
            try:
                written = self._write_skill_file(skill)
                if written == "new":
                    stats["written"] += 1
                elif written == "updated":
                    stats["updated"] += 1
            except Exception as e:
                stats["errors"] += 1
                logger.debug("[memo-import] skill write failed: %s", e)

        return stats

    def inject_skill_to_agent(self, skill_filename: str, agent_id: str):
        """Create a symlink from ``skills/memo/`` to an agent's skill dir.

        This makes the Memo skill visible to a specific agent without
        duplicating the file.
        """
        agent_skills_dir = os.path.join("skills", "agents", agent_id)
        os.makedirs(agent_skills_dir, exist_ok=True)

        source = os.path.abspath(
            os.path.join(MEMO_SKILLS_DIR, skill_filename))
        target = os.path.join(agent_skills_dir, skill_filename)

        if not os.path.exists(source):
            logger.warning("[memo-import] source not found: %s", source)
            return

        if os.path.exists(target):
            return  # already linked

        try:
            os.symlink(source, target)
            logger.info("[memo-import] linked %s → %s", source, target)
        except OSError as e:
            # Fallback: copy instead of symlink (Windows compat)
            import shutil
            shutil.copy2(source, target)
            logger.info("[memo-import] copied %s → %s", source, target)

    # ── internal ──────────────────────────────────────────────────────────

    def _write_skill_file(self, skill: dict) -> str:
        """Write a Memo Skill as a Markdown file with YAML frontmatter.

        Returns "new", "updated", or "skipped".
        """
        skill_id = skill.get("id", "unknown")
        title = skill.get("title", "Memo Skill")
        content = skill.get("content", "")
        tags = skill.get("tags", [])
        source_memory = skill.get("source_memory_id", "")
        version = skill.get("source_version", 1)
        quality = skill.get("quality_score", 0.0)

        # YAML frontmatter (compatible with SkillLoader)
        frontmatter = (
            f"---\n"
            f"name: \"{title}\"\n"
            f"description: \"Imported from Memo Protocol\"\n"
            f"tags: {json.dumps(tags)}\n"
            f"source: memo\n"
            f"memo_skill_id: \"{skill_id}\"\n"
            f"memo_memory_id: \"{source_memory}\"\n"
            f"memo_version: {version}\n"
            f"quality_score: {quality}\n"
            f"---\n\n"
        )

        md_content = frontmatter + content

        # Filename: memo_{skill_id_prefix}.md
        safe_id = skill_id.replace("/", "_")[:30]
        filename = f"memo_{safe_id}.md"
        path = os.path.join(MEMO_SKILLS_DIR, filename)

        # Check if exists and needs update
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = f.read()
                if f"memo_version: {version}" in existing:
                    return "skipped"  # same version
            except OSError:
                pass
            # Version changed → update
            with open(path, "w") as f:
                f.write(md_content)
            logger.info("[memo-import] updated skill: %s", path)
            return "updated"

        with open(path, "w") as f:
            f.write(md_content)
        logger.info("[memo-import] new skill: %s", path)
        return "new"

    def list_local_skills(self) -> list[dict]:
        """List all locally stored Memo skills with metadata."""
        skills = []
        if not os.path.isdir(MEMO_SKILLS_DIR):
            return skills

        for fname in os.listdir(MEMO_SKILLS_DIR):
            if not fname.endswith(".md") or fname.startswith("."):
                continue
            path = os.path.join(MEMO_SKILLS_DIR, fname)
            try:
                with open(path) as f:
                    head = f.read(500)
                # Parse basic frontmatter
                if head.startswith("---"):
                    end = head.find("---", 3)
                    if end > 0:
                        fm = head[3:end].strip()
                        info = {"filename": fname}
                        for line in fm.split("\n"):
                            if ": " in line:
                                k, v = line.split(": ", 1)
                                info[k.strip()] = v.strip().strip('"')
                        skills.append(info)
            except OSError:
                continue

        return skills

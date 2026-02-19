"""
core/skill_loader.py
Hot-reload markdown skill documents from skills/ directory.
Reads from disk on every call — no caching.
This allows Evolution Engine Path A to patch skills at runtime.

Features:
  - YAML frontmatter parsing (Claude Code skill compatible)
  - Shared skills (skills/*.md)
  - Auto-injected team skill (skills/_team.md)
  - Per-agent private skills (skills/agents/{agent_id}/*.md)
  - Agent overrides (skills/agent_overrides/{agent_id}.md)
  - Per-agent reference documents (docs/_shared/ + docs/{agent_id}/)
  - Skill inventory listing for configure UI
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

SKILLS_DIR = "skills"
DOCS_DIR = "docs"


# ── Frontmatter Parser ────────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    Parse optional YAML frontmatter from markdown skill file.
    Compatible with Claude Code skill format:

        ---
        name: My Skill
        description: Does something
        tags: [coding, debug]
        ---
        # Skill content...

    Returns (metadata_dict, body_text).
    """
    if not content or not content.startswith("---"):
        return {}, content

    match = re.match(r'^---\s*\n(.*?)\n---\s*\n?', content, re.DOTALL)
    if not match:
        return {}, content

    try:
        import yaml
        meta = yaml.safe_load(match.group(1)) or {}
    except Exception:
        meta = {}

    body = content[match.end():]
    return meta, body


# ── Skill Loader ──────────────────────────────────────────────────────────────

class SkillLoader:
    """
    Loads markdown skill files and agent-specific overrides.
    Hot-reload: reads from disk every call (no cache).

    Load order (all injected into system prompt):
      1. Shared skills (skills/{name}.md) — from agent's skill list
      2. Team skill (skills/_team.md) — auto-injected unless already listed
      3. Per-agent private skills (skills/agents/{agent_id}/*.md)
      4. Agent overrides (skills/agent_overrides/{agent_id}.md)
    """

    def __init__(self, skills_dir: str = SKILLS_DIR, docs_dir: str = DOCS_DIR):
        self.skills_dir = skills_dir
        self.docs_dir = docs_dir

    def load(self, skill_names: list[str],
             agent_id: str | None = None) -> str:
        """
        Load and concatenate skill documents.

        1. For each name in skill_names, read skills/{name}.md
        2. Auto-inject skills/_team.md (team roster)
        3. Load per-agent private skills from skills/agents/{agent_id}/
        4. If agent_id provided, also read skills/agent_overrides/{agent_id}.md
        5. Return concatenated string
        """
        parts = []

        # ── 1. Shared skills ──
        for name in skill_names:
            path = os.path.join(self.skills_dir, f"{name}.md")
            content = self._read_file(path)
            if content:
                _meta, body = _parse_frontmatter(content)
                display_name = _meta.get("name", name)
                parts.append(f"### Skill: {display_name}\n{body}")
            else:
                logger.debug("Skill file not found: %s", path)

        # ── 2. Team skill (auto-inject if not already in skill_names) ──
        if "_team" not in skill_names:
            team_path = os.path.join(self.skills_dir, "_team.md")
            team_content = self._read_file(team_path)
            if team_content:
                parts.append(f"### Skill: Team Roster\n{team_content}")

        # ── 3. Per-agent private skills ──
        if agent_id:
            agent_skills_dir = os.path.join(
                self.skills_dir, "agents", agent_id)
            if os.path.isdir(agent_skills_dir):
                for fname in sorted(os.listdir(agent_skills_dir)):
                    if fname.endswith(".md") and not fname.startswith("."):
                        path = os.path.join(agent_skills_dir, fname)
                        content = self._read_file(path)
                        if content:
                            _meta, body = _parse_frontmatter(content)
                            skill_name = _meta.get(
                                "name", fname.replace(".md", ""))
                            parts.append(
                                f"### Skill: {skill_name} (private)\n{body}")

        # ── 4. Agent-specific overrides (written by Evolution Engine Path A) ──
        if agent_id:
            override_path = os.path.join(
                self.skills_dir, "agent_overrides", f"{agent_id}.md")
            override = self._read_file(override_path)
            if override:
                parts.append(f"### Agent Override ({agent_id})\n{override}")

        return "\n\n".join(parts) if parts else "(no skills loaded)"

    def load_docs(self, agent_id: str) -> str:
        """
        Load reference documents for an agent.

        Scans:
          1. docs/_shared/ — shared reference docs for all agents
          2. docs/{agent_id}/ — agent-specific reference docs

        Returns concatenated markdown string, or empty string if no docs.
        """
        parts = []

        # ── Shared docs ──
        shared_dir = os.path.join(self.docs_dir, "_shared")
        if os.path.isdir(shared_dir):
            for fname in sorted(os.listdir(shared_dir)):
                if fname.endswith((".md", ".txt")) and not fname.startswith("."):
                    path = os.path.join(shared_dir, fname)
                    content = self._read_file(path)
                    if content:
                        parts.append(
                            f"### Doc: {fname} (shared)\n{content}")

        # ── Agent-specific docs ──
        agent_dir = os.path.join(self.docs_dir, agent_id)
        if os.path.isdir(agent_dir):
            for fname in sorted(os.listdir(agent_dir)):
                if fname.endswith((".md", ".txt")) and not fname.startswith("."):
                    path = os.path.join(agent_dir, fname)
                    content = self._read_file(path)
                    if content:
                        parts.append(f"### Doc: {fname}\n{content}")

        return "\n\n".join(parts) if parts else ""

    def list_skills(self) -> dict:
        """
        List all installed skills with metadata.

        Returns:
            {
                "shared": [
                    {"name": "planning", "file": "planning.md",
                     "description": "...", "tags": [...]},
                    ...
                ],
                "agents": {
                    "planner": [
                        {"name": "debug_tips", "file": "debug_tips.md",
                         "description": "..."},
                    ],
                    ...
                }
            }
        """
        result: dict = {"shared": [], "agents": {}}

        # ── Shared skills ──
        if os.path.isdir(self.skills_dir):
            for fname in sorted(os.listdir(self.skills_dir)):
                if (fname.endswith(".md") and fname != "_team.md"
                        and not fname.startswith(".")):
                    path = os.path.join(self.skills_dir, fname)
                    content = self._read_file(path)
                    meta = {}
                    if content:
                        meta, _ = _parse_frontmatter(content)
                    result["shared"].append({
                        "name": meta.get("name",
                                         fname.replace(".md", "")),
                        "file": fname,
                        "description": meta.get("description", ""),
                        "tags": meta.get("tags", []),
                    })

        # ── Per-agent private skills ──
        agents_dir = os.path.join(self.skills_dir, "agents")
        if os.path.isdir(agents_dir):
            for agent_id in sorted(os.listdir(agents_dir)):
                agent_path = os.path.join(agents_dir, agent_id)
                if not os.path.isdir(agent_path):
                    continue
                # Skip hidden files/dirs
                if agent_id.startswith("."):
                    continue
                skills = []
                for fname in sorted(os.listdir(agent_path)):
                    if fname.endswith(".md") and not fname.startswith("."):
                        path = os.path.join(agent_path, fname)
                        content = self._read_file(path)
                        meta = {}
                        if content:
                            meta, _ = _parse_frontmatter(content)
                        skills.append({
                            "name": meta.get("name",
                                             fname.replace(".md", "")),
                            "file": fname,
                            "description": meta.get("description", ""),
                        })
                if skills:
                    result["agents"][agent_id] = skills

        return result

    @staticmethod
    def _read_file(path: str) -> str:
        """Read a file, returning empty string if not found or unreadable."""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
        except (FileNotFoundError, OSError):
            return ""

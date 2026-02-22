"""
core/skill_registry.py — Remote skill registry for dynamic discovery and installation.

Inspired by OpenClaw's ClawHub: agents can search for skills they don't have,
install them at runtime, and use them immediately (hot-reload).

Architecture:
  - Registry index hosted on GitHub (JSON manifest)
  - Skills downloaded as individual .md files or directory packs (.tar.gz)
  - Local tracking via skills/_registry.json
  - Integrates with SkillLoader hot-reload (no restart needed)
  - Agent tools: search_skills, install_remote_skill

Registry Index Format (remote registry.json):
  {
    "version": "1.0",
    "updated_at": "2025-01-01T00:00:00Z",
    "skills": [
      {
        "slug": "pdf-rotate",
        "name": "PDF Rotate",
        "description": "Rotate PDF pages by 90/180/270 degrees",
        "version": "0.2.0",
        "author": "cleo-community",
        "tags": ["pdf", "document", "utility"],
        "requires": {"bins": ["qpdf"]},
        "install": [{"kind": "brew", "formula": "qpdf"}],
        "download_url": "https://raw.githubusercontent.com/.../pdf-rotate.md",
        "pack": false
      },
      ...
    ]
  }
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/punkpeye/cleo-skills/main/registry.json"
)
LOCAL_INDEX_FILE = "skills/_registry.json"
SKILLS_DIR = "skills"
INDEX_CACHE_TTL = 3600  # 1 hour cache for remote index
HTTP_TIMEOUT = 15       # seconds


# ── Local Index (tracks installed remote skills) ─────────────────────────────

def _load_local_index() -> dict:
    """Load the local registry tracking file."""
    try:
        with open(LOCAL_INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"installed": {}, "last_sync": 0}


def _save_local_index(data: dict):
    """Save the local registry tracking file."""
    os.makedirs(os.path.dirname(LOCAL_INDEX_FILE) or ".", exist_ok=True)
    with open(LOCAL_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── HTTP Utilities ────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    """Fetch a URL with basic error handling."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Cleo-SkillRegistry/1.0",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        logger.warning("HTTP %d fetching %s: %s", e.code, url, e.reason)
        raise
    except urllib.error.URLError as e:
        logger.warning("URL error fetching %s: %s", url, e.reason)
        raise
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        raise


# ── Skill Registry ────────────────────────────────────────────────────────────

class SkillRegistry:
    """
    Remote skill registry client.

    Fetches a JSON index from a remote URL (default: GitHub),
    allows searching by name/description/tags, and installs
    skills to the local skills/ directory.

    Skills are installed as:
      - Flat file: skills/{slug}.md
      - Directory pack: skills/{slug}/SKILL.md + resources
    """

    def __init__(self, registry_url: str = "",
                 skills_dir: str = SKILLS_DIR):
        self.registry_url = registry_url or self._get_config_url()
        self.skills_dir = skills_dir
        self._index_cache: dict | None = None
        self._cache_ts: float = 0

    @staticmethod
    def _get_config_url() -> str:
        """Read registry URL from config/agents.yaml if available."""
        try:
            import yaml
            with open("config/agents.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("skill_registry", {}).get("url", DEFAULT_REGISTRY_URL)
        except Exception:
            return DEFAULT_REGISTRY_URL

    # ── Index Management ─────────────────────────────────────────────────

    def fetch_index(self, force: bool = False) -> dict:
        """Fetch the remote registry index (with caching).

        Returns:
            {"version": str, "skills": [skill_entry, ...]}
        """
        now = time.time()
        if (not force and self._index_cache
                and now - self._cache_ts < INDEX_CACHE_TTL):
            return self._index_cache

        try:
            raw = _http_get(self.registry_url)
            index = json.loads(raw.decode("utf-8"))
            self._index_cache = index
            self._cache_ts = now

            # Update local sync timestamp
            local = _load_local_index()
            local["last_sync"] = now
            _save_local_index(local)

            logger.info("[registry] Fetched index: %d skills available",
                        len(index.get("skills", [])))
            return index

        except Exception as e:
            logger.warning("[registry] Failed to fetch index: %s", e)
            # Return cached or empty
            if self._index_cache:
                return self._index_cache
            return {"version": "0", "skills": []}

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search the registry for skills matching a query.

        Searches across name, description, slug, and tags.
        Returns ranked results (name match > tag match > description match).

        Args:
            query: Search query string
            limit: Max results to return

        Returns:
            List of skill entries with added 'relevance' score
        """
        index = self.fetch_index()
        skills = index.get("skills", [])
        if not skills or not query:
            return skills[:limit]

        query_lower = query.lower()
        query_words = set(query_lower.split())
        scored = []

        for skill in skills:
            score = 0
            slug = skill.get("slug", "").lower()
            name = skill.get("name", "").lower()
            desc = skill.get("description", "").lower()
            tags = [t.lower() for t in skill.get("tags", [])]

            # Exact slug match (highest)
            if query_lower == slug:
                score += 100

            # Slug contains query
            if query_lower in slug:
                score += 50

            # Name contains query
            if query_lower in name:
                score += 40

            # Tag exact match
            for word in query_words:
                if word in tags:
                    score += 30

            # Description contains query
            if query_lower in desc:
                score += 15

            # Individual word matches
            for word in query_words:
                if word in slug:
                    score += 10
                if word in name:
                    score += 8
                if word in desc:
                    score += 3
                for tag in tags:
                    if word in tag:
                        score += 5

            if score > 0:
                entry = dict(skill)
                entry["_score"] = score
                scored.append(entry)

        scored.sort(key=lambda x: x["_score"], reverse=True)

        # Add installed status
        local = _load_local_index()
        installed = local.get("installed", {})
        for entry in scored:
            slug = entry.get("slug", "")
            if slug in installed:
                entry["installed"] = True
                entry["installed_version"] = installed[slug].get("version", "?")
            else:
                # Check if exists locally (manually installed)
                if self._is_locally_available(slug):
                    entry["installed"] = True
                    entry["installed_version"] = "local"
                else:
                    entry["installed"] = False

        return scored[:limit]

    def list_all(self) -> list[dict]:
        """List all available remote skills."""
        index = self.fetch_index()
        return index.get("skills", [])

    def get_info(self, slug: str) -> dict | None:
        """Get detailed info about a specific remote skill."""
        index = self.fetch_index()
        for skill in index.get("skills", []):
            if skill.get("slug") == slug:
                return skill
        return None

    # ── Installation ─────────────────────────────────────────────────────

    def install(self, slug: str) -> dict:
        """Download and install a skill from the registry.

        Args:
            slug: Skill slug (e.g. "pdf-rotate")

        Returns:
            {"ok": bool, "message": str, "path": str, ...}
        """
        skill_info = self.get_info(slug)
        if not skill_info:
            return {"ok": False, "error": f"Skill '{slug}' not found in registry"}

        download_url = skill_info.get("download_url", "")
        if not download_url:
            return {"ok": False, "error": f"No download URL for skill '{slug}'"}

        is_pack = skill_info.get("pack", False)

        try:
            if is_pack:
                result = self._install_pack(slug, download_url, skill_info)
            else:
                result = self._install_flat(slug, download_url, skill_info)

            if result.get("ok"):
                # Track in local index
                self._track_install(slug, skill_info)

                # Install CLI dependencies if any
                dep_result = self._install_deps(slug, skill_info)
                if dep_result:
                    result["deps"] = dep_result

            return result

        except Exception as e:
            logger.error("[registry] Failed to install '%s': %s", slug, e)
            return {"ok": False, "error": str(e)}

    def _install_flat(self, slug: str, url: str, info: dict) -> dict:
        """Install a flat .md skill file."""
        try:
            raw = _http_get(url)
            content = raw.decode("utf-8")
        except Exception as e:
            return {"ok": False, "error": f"Download failed: {e}"}

        # Validate it looks like a skill file
        if not content.strip():
            return {"ok": False, "error": "Downloaded file is empty"}

        # Write to skills directory
        path = os.path.join(self.skills_dir, f"{slug}.md")
        os.makedirs(self.skills_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("[registry] Installed skill '%s' → %s (%d bytes)",
                    slug, path, len(content))
        return {
            "ok": True,
            "message": f"Installed '{info.get('name', slug)}' v{info.get('version', '?')}",
            "path": path,
            "version": info.get("version", ""),
        }

    def _install_pack(self, slug: str, url: str, info: dict) -> dict:
        """Install a directory-style skill pack (.tar.gz)."""
        try:
            raw = _http_get(url)
        except Exception as e:
            return {"ok": False, "error": f"Download failed: {e}"}

        target_dir = os.path.join(self.skills_dir, slug)

        try:
            # Extract to temp first, then move
            with tempfile.TemporaryDirectory() as tmpdir:
                tar_path = os.path.join(tmpdir, f"{slug}.tar.gz")
                with open(tar_path, "wb") as f:
                    f.write(raw)

                with tarfile.open(tar_path, "r:gz") as tar:
                    # Security: check for path traversal
                    for member in tar.getmembers():
                        if member.name.startswith("/") or ".." in member.name:
                            return {"ok": False,
                                    "error": "Security: path traversal in archive"}
                    tar.extractall(tmpdir)

                # Find the extracted skill directory
                extracted = [d for d in os.listdir(tmpdir)
                             if os.path.isdir(os.path.join(tmpdir, d))]
                if not extracted:
                    return {"ok": False,
                            "error": "Archive contained no directories"}

                source = os.path.join(tmpdir, extracted[0])

                # Remove existing if present
                if os.path.exists(target_dir):
                    shutil.rmtree(target_dir)

                shutil.copytree(source, target_dir)

        except tarfile.TarError as e:
            return {"ok": False, "error": f"Archive extraction failed: {e}"}

        logger.info("[registry] Installed skill pack '%s' → %s", slug, target_dir)
        return {
            "ok": True,
            "message": f"Installed pack '{info.get('name', slug)}' v{info.get('version', '?')}",
            "path": target_dir,
            "version": info.get("version", ""),
        }

    def _track_install(self, slug: str, info: dict):
        """Record the installation in local index."""
        local = _load_local_index()
        local["installed"][slug] = {
            "version": info.get("version", ""),
            "installed_at": time.time(),
            "source": self.registry_url,
            "name": info.get("name", slug),
        }
        _save_local_index(local)

    def _install_deps(self, slug: str, info: dict) -> dict | None:
        """Auto-install CLI dependencies for a newly installed skill."""
        requires = info.get("requires", {})
        bins = requires.get("bins", [])
        if not bins:
            return None

        # Check if already installed
        missing = [b for b in bins if not shutil.which(b)]
        if not missing:
            return {"deps_ok": True, "message": "All deps already installed"}

        # Try to install via skill_deps
        install_entries = info.get("install", [])
        if not install_entries:
            return {"deps_ok": False, "missing": missing,
                    "message": f"Missing: {', '.join(missing)} (no auto-install available)"}

        try:
            from core.skill_deps import pick_best_installer, install_dep
            best = pick_best_installer(install_entries)
            if best:
                success = install_dep(best, quiet=True)
                if success:
                    return {"deps_ok": True,
                            "message": f"Installed deps: {', '.join(bins)}"}
                else:
                    return {"deps_ok": False, "missing": missing,
                            "message": "Auto-install failed, install manually"}
        except ImportError:
            pass

        return {"deps_ok": False, "missing": missing,
                "message": f"Install manually: {', '.join(missing)}"}

    # ── Update ───────────────────────────────────────────────────────────

    def check_updates(self) -> list[dict]:
        """Check for available updates to installed skills.

        Returns list of skills with available updates:
            [{"slug": str, "current": str, "available": str, "name": str}, ...]
        """
        local = _load_local_index()
        installed = local.get("installed", {})
        if not installed:
            return []

        index = self.fetch_index()
        updates = []

        for skill in index.get("skills", []):
            slug = skill.get("slug", "")
            if slug in installed:
                local_ver = installed[slug].get("version", "0")
                remote_ver = skill.get("version", "0")
                if self._version_newer(remote_ver, local_ver):
                    updates.append({
                        "slug": slug,
                        "name": skill.get("name", slug),
                        "current": local_ver,
                        "available": remote_ver,
                    })

        return updates

    def update(self, slug: str) -> dict:
        """Update an installed skill to the latest version."""
        local = _load_local_index()
        if slug not in local.get("installed", {}):
            return {"ok": False, "error": f"Skill '{slug}' not tracked as installed"}
        return self.install(slug)

    # ── Uninstall ────────────────────────────────────────────────────────

    def uninstall(self, slug: str) -> dict:
        """Remove an installed remote skill.

        Args:
            slug: Skill slug to remove

        Returns:
            {"ok": bool, "message": str}
        """
        # Remove file/directory
        flat_path = os.path.join(self.skills_dir, f"{slug}.md")
        dir_path = os.path.join(self.skills_dir, slug)

        removed = False
        if os.path.isfile(flat_path):
            os.remove(flat_path)
            removed = True
        if os.path.isdir(dir_path):
            shutil.rmtree(dir_path)
            removed = True

        if not removed:
            return {"ok": False, "error": f"Skill '{slug}' not found locally"}

        # Remove from tracking
        local = _load_local_index()
        local.get("installed", {}).pop(slug, None)
        _save_local_index(local)

        logger.info("[registry] Uninstalled skill '%s'", slug)
        return {"ok": True, "message": f"Uninstalled '{slug}'"}

    # ── Helpers ──────────────────────────────────────────────────────────

    def _is_locally_available(self, slug: str) -> bool:
        """Check if a skill exists locally (regardless of registry tracking)."""
        flat = os.path.join(self.skills_dir, f"{slug}.md")
        dirp = os.path.join(self.skills_dir, slug, "SKILL.md")
        return os.path.exists(flat) or os.path.exists(dirp)

    @staticmethod
    def _version_newer(a: str, b: str) -> bool:
        """Check if version a is newer than version b (semver comparison)."""
        def parse(v: str) -> tuple[int, ...]:
            nums = re.findall(r'\d+', v)
            return tuple(int(n) for n in nums) if nums else (0,)
        return parse(a) > parse(b)

    # ── Agent Config Integration ─────────────────────────────────────────

    def add_to_agent(self, slug: str, agent_id: str) -> dict:
        """Add an installed skill to an agent's skill list in agents.yaml.

        This makes the skill active for the specified agent on next task.

        Args:
            slug: Skill slug
            agent_id: Agent ID (e.g. "leo", "jerry")

        Returns:
            {"ok": bool, "message": str}
        """
        try:
            import yaml
            config_path = "config/agents.yaml"

            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f)

            agents = cfg.get("agents", [])
            agent = None
            for a in agents:
                if a.get("id") == agent_id:
                    agent = a
                    break

            if not agent:
                return {"ok": False, "error": f"Agent '{agent_id}' not found"}

            skills = agent.get("skills", [])
            if slug in skills:
                return {"ok": True,
                        "message": f"'{slug}' already in {agent_id}'s skill list"}

            skills.append(slug)
            agent["skills"] = skills

            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

            logger.info("[registry] Added '%s' to agent '%s' skill list",
                        slug, agent_id)
            return {"ok": True,
                    "message": f"Added '{slug}' to {agent_id}'s skills"}

        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_to_all_agents(self, slug: str) -> dict:
        """Add a skill to all agents' skill lists."""
        try:
            import yaml
            config_path = "config/agents.yaml"

            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f)

            agents = cfg.get("agents", [])
            added_to = []

            for agent in agents:
                agent_id = agent.get("id", "")
                skills = agent.get("skills", [])
                if slug not in skills:
                    skills.append(slug)
                    agent["skills"] = skills
                    added_to.append(agent_id)

            with open(config_path, "w") as f:
                yaml.dump(cfg, default_flow_style=False, allow_unicode=True)

            if added_to:
                return {"ok": True,
                        "message": f"Added '{slug}' to agents: {', '.join(added_to)}"}
            return {"ok": True, "message": f"'{slug}' already in all agents"}

        except Exception as e:
            return {"ok": False, "error": str(e)}


# ── Factory ───────────────────────────────────────────────────────────────────

_singleton: SkillRegistry | None = None


def get_registry(registry_url: str = "") -> SkillRegistry:
    """Get or create the singleton SkillRegistry instance."""
    global _singleton
    if _singleton is None:
        _singleton = SkillRegistry(registry_url=registry_url)
    return _singleton

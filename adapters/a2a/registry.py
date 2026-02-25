"""
adapters/a2a/registry.py — A2A Agent Registry: discovery + capability matching.

Responsibilities:
  - Static registry:   pre-registered agents from config (a2a.client.remotes)
  - Dynamic discovery:  fetch Agent Cards from remote registries
  - Capability match:   find agents by required_skills tags
  - Cache:             Agent Card cache with configurable TTL

The registry is used by the A2A Client to resolve "auto" agent URLs
and by Jerry's a2a_delegate tool to find the best agent for a skill.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from adapters.a2a.models import AgentCard, AgentSkill
from adapters.a2a.security import TrustLevel, resolve_trust_level

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Registry Entry
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentEntry:
    """A registered external agent with resolved metadata."""
    url: str = ""                           # Agent A2A endpoint URL
    name: str = ""                          # Agent name (from Agent Card)
    description: str = ""                   # Agent description
    skills: list[str] = field(default_factory=list)   # Skill tags
    trust_level: str = TrustLevel.UNTRUSTED
    card: Optional[AgentCard] = None        # Full Agent Card (if fetched)
    last_seen: float = 0.0                  # Last successful contact
    failure_count: int = 0                  # Consecutive failures
    auth: dict[str, Any] = field(default_factory=dict)  # Auth config

    @property
    def is_healthy(self) -> bool:
        """Agent considered healthy if < 3 consecutive failures."""
        return self.failure_count < 3

    def record_success(self):
        self.last_seen = time.time()
        self.failure_count = 0

    def record_failure(self):
        self.failure_count += 1


# ══════════════════════════════════════════════════════════════════════════════
#  Agent Registry
# ══════════════════════════════════════════════════════════════════════════════

class AgentRegistry:
    """Agent discovery and capability matching.

    Usage::

        registry = AgentRegistry(config)
        # Find agent by skills
        entry = registry.find_by_skills(["chart-generation"])
        # Find agent by URL
        entry = registry.get("https://chart-agent.example.com")
        # Resolve "auto" to best matching agent
        entry = registry.resolve("auto", required_skills=["chart-generation"])
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: Full Cleo config dict (reads config["a2a"]["client"]).
        """
        config = config or {}
        client_cfg = config.get("a2a", {}).get("client", {})

        self._remotes: list[dict] = client_cfg.get("remotes", [])
        self._registries: list[dict] = client_cfg.get("registries", [])
        self._entries: dict[str, AgentEntry] = {}  # url → AgentEntry
        self._card_cache: dict[str, tuple[float, dict]] = {}  # url → (ts, card_dict)
        self._card_cache_ttl: int = 3600  # 1 hour

        # Load static remotes
        self._load_static_remotes()

        logger.info("[a2a:registry] initialized: %d static agents, %d registries",
                    len(self._entries), len(self._registries))

    # ── Static remotes ────────────────────────────────────────────────────

    def _load_static_remotes(self):
        """Load pre-registered agents from config."""
        for remote in self._remotes:
            url = remote.get("url", "").rstrip("/")
            if not url:
                continue

            entry = AgentEntry(
                url=url,
                name=remote.get("name", urlparse(url).hostname or "unknown"),
                description=remote.get("description", ""),
                skills=remote.get("skills", []),
                trust_level=remote.get("trust_level", TrustLevel.VERIFIED),
                auth=remote.get("auth", {}),
            )
            self._entries[url] = entry
            logger.debug("[a2a:registry] static agent: %s (%s)",
                         entry.name, entry.trust_level)

    # ── Lookup ────────────────────────────────────────────────────────────

    def get(self, url: str) -> Optional[AgentEntry]:
        """Get agent entry by URL (exact match)."""
        normalized = url.rstrip("/")
        return self._entries.get(normalized)

    def list_all(self) -> list[AgentEntry]:
        """List all known agents."""
        return list(self._entries.values())

    def list_healthy(self) -> list[AgentEntry]:
        """List all healthy agents."""
        return [e for e in self._entries.values() if e.is_healthy]

    # ── Capability matching ───────────────────────────────────────────────

    def find_by_skills(self, required_skills: list[str],
                       trust_min: str = TrustLevel.UNTRUSTED
                       ) -> list[AgentEntry]:
        """Find agents that match required skills.

        Args:
            required_skills: List of skill tags to match.
            trust_min: Minimum trust level (verified > community > untrusted).

        Returns:
            List of matching AgentEntry objects, sorted by relevance.
        """
        if not required_skills:
            return self.list_healthy()

        trust_order = [TrustLevel.VERIFIED, TrustLevel.COMMUNITY,
                       TrustLevel.UNTRUSTED]
        min_idx = trust_order.index(trust_min) if trust_min in trust_order else 2

        matches: list[tuple[int, AgentEntry]] = []
        req_set = {s.lower() for s in required_skills}

        for entry in self._entries.values():
            if not entry.is_healthy:
                continue

            # Trust level filter
            entry_idx = trust_order.index(entry.trust_level) \
                if entry.trust_level in trust_order else 2
            if entry_idx > min_idx:
                continue

            # Skill matching — count overlapping skills
            entry_skills = {s.lower() for s in entry.skills}
            overlap = len(req_set & entry_skills)
            if overlap > 0:
                # Score: overlap count * trust bonus
                trust_bonus = (3 - entry_idx)  # verified=3, community=2, untrusted=1
                score = overlap * 10 + trust_bonus
                matches.append((score, entry))

        # Sort by score descending
        matches.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in matches]

    def resolve(self, agent_url: str,
                required_skills: list[str] = None) -> Optional[AgentEntry]:
        """Resolve agent URL — supports "auto" for capability-based matching.

        Args:
            agent_url: Agent URL or "auto" for automatic matching.
            required_skills: Required skills (used when agent_url is "auto").

        Returns:
            Resolved AgentEntry or None.
        """
        if agent_url.lower() == "auto":
            # Auto-resolve: find best matching agent
            matches = self.find_by_skills(required_skills or [])
            if matches:
                logger.info("[a2a:registry] auto-resolved to: %s (%s)",
                            matches[0].name, matches[0].url)
                return matches[0]
            logger.warning("[a2a:registry] no agent found for skills: %s",
                           required_skills)
            return None

        # Explicit URL — find or create entry
        normalized = agent_url.rstrip("/")
        entry = self._entries.get(normalized)
        if entry:
            return entry

        # Unknown agent — create entry with untrusted level
        trust = resolve_trust_level(
            normalized, self._remotes, self._registries)
        entry = AgentEntry(
            url=normalized,
            name=urlparse(normalized).hostname or "unknown",
            trust_level=trust,
        )
        self._entries[normalized] = entry
        logger.info("[a2a:registry] registered new agent: %s (trust=%s)",
                    entry.name, trust)
        return entry

    # ── Agent Card discovery ──────────────────────────────────────────────

    def fetch_agent_card(self, base_url: str,
                         timeout: float = 10.0) -> Optional[dict]:
        """Fetch an Agent Card from /.well-known/agent.json.

        Args:
            base_url: The agent's base URL.
            timeout: HTTP timeout seconds.

        Returns:
            Agent Card dict or None.
        """
        normalized = base_url.rstrip("/")

        # Check cache
        cached = self._card_cache.get(normalized)
        if cached:
            ts, card_dict = cached
            if time.time() - ts < self._card_cache_ttl:
                return card_dict

        # Fetch
        card_url = f"{normalized}/.well-known/agent.json"
        try:
            req = urllib.request.Request(
                card_url,
                headers={"Accept": "application/json",
                          "User-Agent": "Cleo-A2A-Client/0.2.0"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    card_dict = json.loads(resp.read().decode("utf-8"))
                    self._card_cache[normalized] = (time.time(), card_dict)
                    logger.info("[a2a:registry] fetched agent card: %s",
                                card_dict.get("name", "unknown"))

                    # Update entry with card info
                    entry = self._entries.get(normalized)
                    if entry:
                        entry.name = card_dict.get("name", entry.name)
                        entry.description = card_dict.get("description", "")
                        entry.skills = [
                            tag
                            for skill in card_dict.get("skills", [])
                            for tag in skill.get("tags", [])
                        ]
                        entry.record_success()

                    return card_dict

        except Exception as e:
            logger.warning("[a2a:registry] failed to fetch card from %s: %s",
                           card_url, e)
            # Record failure
            entry = self._entries.get(normalized)
            if entry:
                entry.record_failure()

        return None

    def refresh_remote_agents(self):
        """Fetch Agent Cards for all known agents to refresh metadata."""
        for url in list(self._entries.keys()):
            self.fetch_agent_card(url)

    # ── Registry discovery ────────────────────────────────────────────────

    def discover_from_registries(self) -> int:
        """Fetch agent lists from configured registries.

        Returns:
            Number of newly discovered agents.
        """
        discovered = 0
        for registry in self._registries:
            registry_url = registry.get("url", "")
            if not registry_url:
                continue

            trust_level = registry.get("trust_level", TrustLevel.COMMUNITY)

            try:
                req = urllib.request.Request(
                    registry_url,
                    headers={"Accept": "application/json",
                              "User-Agent": "Cleo-A2A-Client/0.2.0"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        agents = data if isinstance(data, list) else data.get("agents", [])

                        for agent_info in agents:
                            url = agent_info.get("url", "").rstrip("/")
                            if not url or url in self._entries:
                                continue

                            entry = AgentEntry(
                                url=url,
                                name=agent_info.get("name", ""),
                                description=agent_info.get("description", ""),
                                skills=[
                                    tag
                                    for skill in agent_info.get("skills", [])
                                    for tag in (skill.get("tags", [])
                                                if isinstance(skill, dict)
                                                else [skill])
                                ],
                                trust_level=trust_level,
                            )
                            self._entries[url] = entry
                            discovered += 1

                        logger.info(
                            "[a2a:registry] discovered %d agents from %s",
                            discovered, registry_url)

            except Exception as e:
                logger.warning("[a2a:registry] registry fetch failed: %s: %s",
                               registry_url, e)

        return discovered

    # ── Auth helper ───────────────────────────────────────────────────────

    def get_auth_headers(self, agent_url: str) -> dict[str, str]:
        """Get authentication headers for an agent.

        Reads auth config from the entry and resolves env var tokens.

        Args:
            agent_url: The agent URL.

        Returns:
            Dict of HTTP headers (e.g. {"Authorization": "Bearer xxx"}).
        """
        entry = self.get(agent_url.rstrip("/"))
        if not entry or not entry.auth:
            return {}

        scheme = entry.auth.get("scheme", "").lower()
        if scheme == "bearer":
            token_env = entry.auth.get("token_env", "")
            token = os.environ.get(token_env, "") if token_env else ""
            if token:
                return {"Authorization": f"Bearer {token}"}

        return {}

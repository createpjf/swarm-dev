"""
adapters/memo/client.py — Memo Platform REST API client.

Wraps the Memo v1 API endpoints:
    POST   /memories           Upload new memory
    GET    /memories/search    Semantic search
    GET    /memories/{id}      Get full content (paid)
    POST   /skills/sync        Bulk pull purchased skills
    POST   /memories/similarity  Provenance similarity check

Uses ``httpx.AsyncClient`` for async HTTP.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from adapters.memo.config import MemoConfig


class MemoClient:
    """HTTP client for the Memo Protocol REST API."""

    def __init__(self, config: "MemoConfig"):
        self.base_url = config.api_base_url.rstrip("/")
        self.api_key = config.api_key
        self.wallet = config.wallet_address
        self.agent_id = config.erc8004_agent_id

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "X-Agent-ID": self.agent_id,
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.wallet:
            h["X-Wallet-Address"] = self.wallet
        return h

    # ── memories ──────────────────────────────────────────────────────────

    async def upload_memory(self, payload: dict) -> dict:
        """POST /memories — upload a new MemoryObject.

        Returns API response (id, status, quality_score, etc.).
        """
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/v1/memories",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def search_memories(
        self,
        query: str,
        type: str = "",
        min_quality: float = 0.6,
        domain: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict]:
        """GET /memories/search — semantic search."""
        import httpx
        params: dict = {
            "q": query,
            "min_quality": min_quality,
            "limit": limit,
        }
        if type:
            params["type"] = type
        if domain:
            params["domain"] = domain
        if offset:
            params["offset"] = offset

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.base_url}/v1/memories/search",
                headers=self._headers(),
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("results", [])

    async def get_memory(self, memory_id: str,
                         subscription_token: str = "") -> dict:
        """GET /memories/{id} — get full content (may require payment)."""
        import httpx
        headers = self._headers()
        if subscription_token:
            headers["X-Subscription-Token"] = subscription_token

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.base_url}/v1/memories/{memory_id}",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    # ── skills ────────────────────────────────────────────────────────────

    async def sync_skills(self, memory_ids: list[str]) -> list[dict]:
        """POST /skills/sync — bulk pull purchased skills."""
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/v1/skills/sync",
                headers=self._headers(),
                json={"memory_ids": memory_ids},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("skills", [])

    # ── provenance ────────────────────────────────────────────────────────

    async def check_similarity(self, content: str) -> dict:
        """Check content similarity against existing memories.

        Returns ``{max_similarity, most_similar_id, root_id, generation}``.
        If max_similarity > 0.85, the upload MUST declare parent_id.
        """
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/v1/memories/similarity",
                headers=self._headers(),
                json={"content": content[:4000]},
            )
            resp.raise_for_status()
            return resp.json()

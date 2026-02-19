"""
adapters/llm/ollama.py
Local Ollama adapter — /api/chat endpoint.
"""

from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)


class OllamaAdapter:

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        # api_key accepted for interface consistency but Ollama doesn't use auth
        self.base_url = base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")

    async def chat(self, messages: list[dict], model: str) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]

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

    async def chat_stream(self, messages: list[dict], model: str):
        """Yield content chunks from Ollama streaming response."""
        import httpx

        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()
                import json as _json
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = _json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            yield content
                    except _json.JSONDecodeError:
                        continue

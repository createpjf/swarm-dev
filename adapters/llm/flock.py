"""
adapters/llm/flock.py
FLock API adapter — OpenAI-compatible /chat/completions endpoint.
Supports both blocking and streaming chat.
"""

from __future__ import annotations
import json
import logging
import os

logger = logging.getLogger(__name__)


class FLockAdapter:

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key  = api_key  or os.getenv("FLOCK_API_KEY", "")
        self.base_url = base_url or os.getenv("FLOCK_BASE_URL", "https://api.flock.io/v1")
        if not self.api_key:
            logger.warning("FLOCK_API_KEY not set — LLM calls will fail")

    async def chat(self, messages: list[dict], model: str) -> str:
        """Blocking chat — returns full response text."""
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def chat_stream(self, messages: list[dict], model: str):
        """
        Streaming chat — yields content chunks as they arrive.
        Uses SSE (server-sent events) format.
        """
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    # SSE format: "data: {...}"
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            return
                        try:
                            chunk = json.loads(payload)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue

    async def chat_with_usage(self, messages: list[dict], model: str) -> tuple[str, dict]:
        """
        Chat that also returns token usage info.
        Returns (content, usage_dict) where usage_dict has:
          prompt_tokens, completion_tokens, total_tokens
        """
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return content, {
                "prompt_tokens":     usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens":      usage.get("total_tokens", 0),
            }

"""
adapters/llm/openai.py
OpenAI / OpenAI-compatible API adapter.
Supports both blocking and streaming chat.
"""

from __future__ import annotations
import json
import logging
import os

logger = logging.getLogger(__name__)


class OpenAIAdapter:

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key  = api_key  or os.getenv("OPENAI_API_KEY", "")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not self.api_key:
            logger.warning("OPENAI_API_KEY not set — LLM calls will fail")

    async def chat(self, messages: list[dict], model: str) -> str:
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
        """Streaming chat — yields content chunks."""
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

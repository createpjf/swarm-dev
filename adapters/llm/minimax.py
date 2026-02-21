"""
adapters/llm/minimax.py
Minimax API adapter — OpenAI-compatible /chat/completions endpoint.

Supports models: MiniMax-M2.5, MiniMax-M2.1, MiniMax-M2, and their
highspeed variants. Full streaming, tool/function calling, and
interleaved thinking support.

API docs: https://platform.minimax.io/docs/guides/models-intro
Base URL: https://api.minimax.io/v1

Minimax is fully OpenAI-compatible, so this adapter follows the same
SSE streaming protocol as FLockAdapter/OpenAIAdapter.
"""

from __future__ import annotations
import json
import logging
import os

logger = logging.getLogger(__name__)

# Default base URL for Minimax OpenAI-compatible API
MINIMAX_BASE_URL = "https://api.minimax.io/v1"


class MinimaxAdapter:

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key  = api_key  or os.getenv("MINIMAX_API_KEY", "")
        self.base_url = base_url or os.getenv("MINIMAX_BASE_URL", MINIMAX_BASE_URL)
        if not self.api_key:
            logger.warning("MINIMAX_API_KEY not set — LLM calls will fail")

    async def chat(self, messages: list[dict], model: str) -> str:
        """Blocking chat — returns full response text."""
        import httpx

        try:
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
                choices = data.get("choices")
                if not choices:
                    raise ValueError(
                        f"Empty choices in Minimax response: {list(data.keys())}")
                return choices[0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            body = ""
            try:
                body = e.response.text[:500]
            except Exception:
                pass
            logger.error("[minimax] HTTP %d — %s", code, body)
            if code == 401:
                raise RuntimeError("Minimax API key invalid (401)") from e
            elif code == 429:
                raise RuntimeError("Minimax rate limited (429)") from e
            raise RuntimeError(f"Minimax API error ({code}): {body[:200]}") from e
        except httpx.ConnectError as e:
            raise RuntimeError(
                f"Cannot connect to Minimax API: {self.base_url}") from e
        except httpx.TimeoutException:
            raise RuntimeError("Minimax API timeout (120s)")
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Minimax response parse error: {e}") from e

    async def chat_stream(self, messages: list[dict], model: str):
        """
        Streaming chat — yields content chunks as they arrive.
        Uses SSE (server-sent events) format, OpenAI-compatible.
        """
        import httpx

        try:
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
                                delta = chunk.get("choices", [{}])[0].get(
                                    "delta", {})
                                content = delta.get("content")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            body = ""
            try:
                body = e.response.text[:500]
            except Exception:
                pass
            logger.error("[minimax-stream] HTTP %d — %s", code, body)
            raise RuntimeError(f"Minimax API error ({code}): {body[:200]}") from e
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise RuntimeError(f"Minimax stream connection error: {e}") from e

    async def chat_with_usage(self, messages: list[dict],
                              model: str) -> tuple[str, dict]:
        """
        Chat that also returns token usage info.
        Returns (content, usage_dict) where usage_dict has:
          prompt_tokens, completion_tokens, total_tokens
        Minimax also returns cache_creation_input_tokens and
        cache_read_input_tokens when prompt caching is active.
        """
        import httpx

        try:
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
                choices = data.get("choices")
                if not choices:
                    raise ValueError("Empty choices in Minimax response")
                content = choices[0]["message"]["content"]
                usage = data.get("usage", {})
                return content, {
                    "prompt_tokens":     usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens":      usage.get("total_tokens", 0),
                }
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            body = ""
            try:
                body = e.response.text[:500]
            except Exception:
                pass
            logger.error("[minimax] HTTP %d — %s", code, body)
            raise RuntimeError(f"Minimax API error ({code}): {body[:200]}") from e
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise RuntimeError(f"Minimax API connection error: {e}") from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Minimax response parse error: {e}") from e

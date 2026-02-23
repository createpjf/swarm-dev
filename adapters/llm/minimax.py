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

Native function calling: when `tools` kwarg is provided, tool schemas
are sent to the API and any tool_calls in the response are converted
back to <tool_code> text format for transparent parsing by the existing
parse_tool_calls() pipeline.
"""

from __future__ import annotations
import json
import logging
import os

logger = logging.getLogger(__name__)

# Default base URL for Minimax OpenAI-compatible API
MINIMAX_BASE_URL = "https://api.minimax.io/v1"


def _tool_calls_to_text(tool_calls: list[dict]) -> str:
    """Convert OpenAI-format tool_calls to <tool_code> text blocks.

    This allows the existing parse_tool_calls() regex pipeline to
    extract tool invocations without any changes to the agent loop.
    """
    blocks = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "unknown")
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[minimax] Failed to parse tool_call arguments for %s: %r",
                           name, raw_args[:200] if isinstance(raw_args, str) else raw_args)
            args = {}
        block = json.dumps({"tool": name, "params": args}, ensure_ascii=False)
        blocks.append(f"<tool_code>\n{block}\n</tool_code>")
    return "\n".join(blocks)


def _build_payload(model: str, messages: list[dict], **kwargs) -> dict:
    """Build API payload, injecting tools if provided."""
    payload: dict = {"model": model, "messages": messages}
    tools = kwargs.get("tools")
    if tools:
        payload["tools"] = [
            {"type": "function", "function": t} for t in tools
        ]
    return payload


class MinimaxAdapter:

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key  = api_key  or os.getenv("MINIMAX_API_KEY", "")
        self.base_url = base_url or os.getenv("MINIMAX_BASE_URL", MINIMAX_BASE_URL)
        if not self.api_key:
            logger.warning("MINIMAX_API_KEY not set — LLM calls will fail")

    async def chat(self, messages: list[dict], model: str, **kwargs) -> str:
        """Blocking chat — returns full response text."""
        import httpx

        try:
            payload = _build_payload(model, messages, **kwargs)
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices")
                if not choices:
                    raise ValueError(
                        f"Empty choices in Minimax response: {list(data.keys())}")
                message = choices[0]["message"]
                # Native function calling: convert tool_calls to text
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    content = message.get("content") or ""
                    tc_text = _tool_calls_to_text(tool_calls)
                    return f"{content}\n{tc_text}" if content else tc_text
                return message["content"]
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

    async def chat_stream(self, messages: list[dict], model: str, **kwargs):
        """
        Streaming chat — yields content chunks as they arrive.
        Uses SSE (server-sent events) format, OpenAI-compatible.

        When tools are provided and the model returns tool_calls,
        the accumulated tool_calls are converted to text and yielded
        as a final chunk.
        """
        import httpx

        try:
            payload = _build_payload(model, messages, **kwargs)
            payload["stream"] = True
            accumulated_tool_calls: list[dict] = []

            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        # SSE format: "data: {...}"
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = chunk.get("choices", [{}])[0].get(
                                    "delta", {})
                                # Regular content
                                content = delta.get("content")
                                if content:
                                    yield content
                                # Tool call deltas (accumulate)
                                tc_deltas = delta.get("tool_calls")
                                if tc_deltas:
                                    for tcd in tc_deltas:
                                        idx = tcd.get("index", 0)
                                        while len(accumulated_tool_calls) <= idx:
                                            accumulated_tool_calls.append(
                                                {"function": {"name": "", "arguments": ""}})
                                        entry = accumulated_tool_calls[idx]
                                        fn = tcd.get("function", {})
                                        if fn.get("name"):
                                            entry["function"]["name"] = fn["name"]
                                        if fn.get("arguments"):
                                            entry["function"]["arguments"] += fn["arguments"]
                            except json.JSONDecodeError:
                                continue

            # Yield accumulated tool_calls as text block
            if accumulated_tool_calls:
                yield "\n" + _tool_calls_to_text(accumulated_tool_calls)

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
                              model: str, **kwargs) -> tuple[str, dict]:
        """
        Chat that also returns token usage info.
        Returns (content, usage_dict) where usage_dict has:
          prompt_tokens, completion_tokens, total_tokens
        Minimax also returns cache_creation_input_tokens and
        cache_read_input_tokens when prompt caching is active.
        """
        import httpx

        try:
            payload = _build_payload(model, messages, **kwargs)
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices")
                if not choices:
                    raise ValueError("Empty choices in Minimax response")
                message = choices[0]["message"]
                # Native function calling: convert tool_calls to text
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    content = message.get("content") or ""
                    tc_text = _tool_calls_to_text(tool_calls)
                    content = f"{content}\n{tc_text}" if content else tc_text
                else:
                    content = message["content"]
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

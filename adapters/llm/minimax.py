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
import re

logger = logging.getLogger(__name__)

# Default base URL for Minimax OpenAI-compatible API
MINIMAX_BASE_URL = "https://api.minimax.io/v1"


def _repair_truncated_json(raw: str) -> str | None:
    """Attempt to repair a truncated JSON tool-call arguments string.

    MiniMax sometimes truncates long tool_call argument strings mid-content,
    producing malformed JSON like: ``{"content": "# Title\\n\\nsome text...``
    (missing closing ``"}``)

    Strategy:
      1. Find the last complete sentence boundary (newline, period, etc.)
      2. Truncate the value there
      3. Close all open JSON string / object delimiters

    Returns the repaired JSON string, or None if repair is not feasible.
    """
    s = raw.rstrip()
    if not s or s.endswith("}"):
        return None  # not truncated

    # Count open braces to decide how many closing braces we need
    open_braces = 0
    in_string = False
    escape_next = False
    last_string_open = -1

    for i, ch in enumerate(s):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            if in_string:
                last_string_open = i
            continue
        if not in_string:
            if ch == '{':
                open_braces += 1
            elif ch == '}':
                open_braces -= 1

    if open_braces <= 0 and not in_string:
        return None  # doesn't look truncated

    # We're inside an unclosed string or object.
    # Find a good truncation point: last \\n, 。, ., or |
    # (common in markdown tables and CJK text)
    truncate_at = -1
    search_region = s[last_string_open:] if last_string_open >= 0 else s
    # Look for the last clean line break (literal \\n in JSON string)
    for marker in ['\\n', '。', '\\n|', '. ']:
        idx = search_region.rfind(marker)
        if idx > 0:
            truncate_at = (last_string_open if last_string_open >= 0 else 0) + idx
            break

    if truncate_at <= 0:
        # No good truncation point — just truncate at current position
        truncate_at = len(s)

    repaired = s[:truncate_at]

    # Close the string if we're inside one
    if in_string:
        # Remove any trailing incomplete escape sequence
        if repaired.endswith('\\'):
            repaired = repaired[:-1]
        repaired += '"'

    # Close open braces
    for _ in range(open_braces):
        repaired += '}'

    # Verify the repair produces valid JSON
    try:
        json.loads(repaired)
        logger.info("[minimax] Repaired truncated JSON args (%d→%d chars)",
                    len(raw), len(repaired))
        return repaired
    except (json.JSONDecodeError, TypeError):
        # Repair failed — let caller handle via _raw_args fallback
        return None


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
        # MiniMax sometimes returns Python-style \UXXXXXXXX Unicode escapes
        # which are invalid JSON (JSON only supports \uXXXX 4-digit escapes).
        # Convert them to actual Unicode characters before parsing.
        if isinstance(raw_args, str) and "\\U" in raw_args:
            raw_args = re.sub(
                r'\\U([0-9a-fA-F]{8})',
                lambda m: chr(int(m.group(1), 16)),
                raw_args,
            )
        # MiniMax sometimes truncates long arguments, producing incomplete JSON.
        # Try to repair before parsing.
        if isinstance(raw_args, str) and raw_args.strip() and not raw_args.rstrip().endswith('}'):
            repaired = _repair_truncated_json(raw_args)
            if repaired:
                raw_args = repaired
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("[minimax] Failed to parse tool_call arguments for %s: %r",
                           name, raw_args[:300] if isinstance(raw_args, str) else raw_args)
            # Propagate raw string so downstream handlers can attempt recovery
            args = {"_parse_error": str(exc), "_raw_args": raw_args}
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

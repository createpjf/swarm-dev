"""
core/compaction.py
Context Compaction — auto-summarize conversation history when approaching
token limits, OpenClaw-style.

Strategy:
  1. Estimate token count of message history
  2. If above threshold, summarize older messages into a compact digest
  3. Keep recent N turns verbatim for context continuity
  4. Insert summary as a system message prefix

Token estimation: ~4 chars per token (conservative for CJK-heavy text: ~2 chars/token)
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

# Average chars per token — conservative estimate
# English ~4 chars/token, Chinese ~1.5 chars/token, mix ~3
CHARS_PER_TOKEN = 3


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token count estimation for a message list."""
    total_chars = sum(len(m.get("content", "")) for m in messages)
    return total_chars // CHARS_PER_TOKEN


def needs_compaction(messages: list[dict], max_tokens: int = 8000) -> bool:
    """Check if the conversation history needs compaction."""
    return estimate_tokens(messages) > max_tokens


async def compact_history(
    messages: list[dict],
    llm,
    model: str,
    max_context_tokens: int = 8000,
    summary_target_tokens: int = 1500,
    keep_recent_turns: int = 4,
) -> list[dict]:
    """
    Compact conversation history to fit within token budget.

    Strategy:
      1. Split messages into: [system] + [old_turns] + [recent_turns]
      2. Summarize old_turns into a concise digest
      3. Return: [system] + [summary_msg] + [recent_turns]

    Args:
        messages: Full message list [system, user, assistant, ...]
        llm: LLM adapter (must have .chat() method)
        model: Model to use for summarization
        max_context_tokens: Trigger compaction above this
        summary_target_tokens: Target size for the summary
        keep_recent_turns: Keep last N user/assistant pairs verbatim

    Returns:
        Compacted message list, or original if no compaction needed
    """
    if not needs_compaction(messages, max_context_tokens):
        return messages

    # Split messages
    system_msgs = []
    conversation = []

    for m in messages:
        if m["role"] == "system":
            system_msgs.append(m)
        else:
            conversation.append(m)

    # Keep recent turns (each "turn" = 1 user + 1 assistant message)
    keep_count = keep_recent_turns * 2
    if len(conversation) <= keep_count:
        # Not enough to compact
        return messages

    old_msgs    = conversation[:-keep_count]
    recent_msgs = conversation[-keep_count:]

    # Build summarization prompt
    old_text = _format_messages_for_summary(old_msgs)
    target_chars = summary_target_tokens * CHARS_PER_TOKEN

    summary_prompt = [
        {"role": "system", "content": (
            "You are a conversation summarizer. Summarize the following conversation "
            "into a concise digest. Preserve:\n"
            "- Key decisions and conclusions\n"
            "- Important code/content produced\n"
            "- Task outcomes and status\n"
            "- Critical context needed for future turns\n\n"
            f"Target length: ~{target_chars} characters. Be concise but complete."
        )},
        {"role": "user", "content": (
            f"Summarize this conversation:\n\n{old_text}"
        )},
    ]

    try:
        summary = await llm.chat(summary_prompt, model)
        logger.info(
            "[compaction] compressed %d messages → summary (%d chars), "
            "keeping %d recent messages",
            len(old_msgs), len(summary), len(recent_msgs),
        )
    except Exception as e:
        logger.warning("[compaction] failed, keeping original: %s", e)
        return messages

    # Build compacted history — append summary to the last system message
    # (do NOT create a second system message — some APIs reject that)
    summary_block = (
        "\n\n## Previous Conversation Summary\n"
        "(Earlier messages were compacted to save context space)\n\n"
        f"{summary}\n\n"
        "---\n"
        "The conversation continues below with the most recent messages."
    )

    if system_msgs:
        # Merge into existing system prompt (keep single system message)
        system_msgs[-1] = dict(system_msgs[-1])  # don't mutate original
        system_msgs[-1]["content"] += summary_block
        return system_msgs + recent_msgs
    else:
        # No system message exists — create one
        summary_msg = {"role": "system", "content": summary_block.strip()}
        return [summary_msg] + recent_msgs


def _format_messages_for_summary(messages: list[dict]) -> str:
    """Format message list into readable text for summarization."""
    parts = []
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        # Truncate very long messages
        if len(content) > 2000:
            content = content[:1800] + "\n...(truncated)"
        parts.append(f"[{role}]: {content}")
    return "\n\n".join(parts)

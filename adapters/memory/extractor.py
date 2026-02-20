"""
adapters/memory/extractor.py
Post-task memory extraction — pulls reusable knowledge from completed tasks.

Inspired by OpenViking's 6-category extraction:
  User-owned:  profile, preferences, entities, events
  Agent-owned: cases (problem→solution), patterns (recurring observations)

In Cleo, we focus on agent-owned extraction:
  - Cases:    "When I encountered X, the solution was Y"
  - Patterns: "Tasks involving X tend to need Y"
  - Insights: Cross-agent learnings shared via KnowledgeBase

Extraction is lightweight (regex-based) to avoid an extra LLM call.
For deeper extraction, an LLM call can be optionally enabled.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def extract_cases(task_description: str, result: str,
                  agent_id: str) -> list[dict]:
    """
    Extract problem→solution cases from a task result.
    Returns list of {problem, solution, tags} dicts.

    Heuristics:
      - Task description = problem
      - Result = solution
      - If result contains error handling, that's a separate case
    """
    cases = []

    # Main case: task itself is a problem→solution pair
    # Only save if result is meaningful (>50 chars)
    if len(result.strip()) > 50:
        # Extract a concise problem statement
        problem = task_description.strip()[:500]
        # Extract key solution elements
        solution = _extract_key_points(result)

        if problem and solution:
            tags = _extract_tags(task_description + " " + result)
            cases.append({
                "problem": problem,
                "solution": solution,
                "tags": tags,
            })

    return cases


def extract_patterns(task_description: str, result: str,
                     agent_id: str) -> list[dict]:
    """
    Extract recurring patterns from task results.
    Returns list of {pattern, evidence, tags} dicts.
    """
    patterns = []
    text = task_description + "\n" + result
    text_lower = text.lower()

    # Pattern: error handling
    if any(kw in text_lower for kw in ["error", "exception", "failed", "bug", "fix"]):
        patterns.append({
            "pattern": f"{agent_id} handles error/failure scenarios",
            "evidence": [task_description[:200]],
            "tags": ["error-handling"],
        })

    # Pattern: code generation
    if any(kw in text_lower for kw in ["```", "def ", "class ", "function ", "import "]):
        patterns.append({
            "pattern": f"{agent_id} generates code implementations",
            "evidence": [task_description[:200]],
            "tags": ["code-generation"],
        })

    # Pattern: planning/decomposition
    if any(kw in text_lower for kw in ["task:", "subtask", "step 1", "phase "]):
        patterns.append({
            "pattern": f"{agent_id} decomposes tasks into steps",
            "evidence": [task_description[:200]],
            "tags": ["planning"],
        })

    # Pattern: review/quality
    if any(kw in text_lower for kw in ["score", "review", "quality", "correctness"]):
        patterns.append({
            "pattern": f"{agent_id} evaluates quality metrics",
            "evidence": [task_description[:200]],
            "tags": ["review"],
        })

    return patterns


def extract_insight(task_description: str, result: str,
                    agent_id: str) -> Optional[str]:
    """
    Extract a cross-agent insight from a task result.
    Returns a short insight string, or None if nothing notable.

    An insight is a generalizable observation that other agents
    might benefit from.
    """
    result_lower = result.lower()

    # Look for explicit conclusions or key findings
    insight_markers = [
        (r"(?:key finding|conclusion|takeaway|lesson learned)[:\s]+(.+?)(?:\n|$)", 1),
        (r"(?:important(?:ly)?|note that|in summary)[:\s]+(.+?)(?:\n|$)", 1),
    ]

    for pattern, group in insight_markers:
        match = re.search(pattern, result, re.IGNORECASE)
        if match:
            insight = match.group(group).strip()[:200]
            if len(insight) > 20:
                return f"{insight} (from: {task_description[:80]})"

    # If result is a review with score, share the quality signal
    if '"score"' in result_lower and agent_id == "alic":
        try:
            import json
            data = json.loads(result)
            score = data.get("score")
            comment = data.get("comment", "")
            if score is not None:
                return (f"Review score {score}: {comment[:100]} "
                        f"(task: {task_description[:80]})")
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _extract_key_points(text: str, max_length: int = 500) -> str:
    """Extract key points from a result text."""
    # If text is short enough, use as-is
    if len(text) <= max_length:
        return text.strip()

    # Try to find structured content (lists, headers)
    lines = text.strip().split("\n")
    key_lines = []
    for line in lines:
        stripped = line.strip()
        # Keep headers, list items, and code markers
        if (stripped.startswith("#") or
            stripped.startswith("- ") or
            stripped.startswith("* ") or
            stripped.startswith("TASK:") or
            stripped.startswith("```")):
            key_lines.append(stripped)
            if sum(len(l) for l in key_lines) > max_length:
                break

    if key_lines:
        return "\n".join(key_lines)[:max_length]

    # Fallback: first N chars
    return text[:max_length].strip()


def _extract_tags(text: str) -> list[str]:
    """Extract topic tags from text using keyword heuristics."""
    tags = set()
    text_lower = text.lower()

    tag_keywords = {
        "code": ["code", "implement", "function", "class", "module", "api"],
        "planning": ["plan", "decompose", "strategy", "architecture", "design"],
        "review": ["review", "evaluate", "score", "quality", "audit"],
        "debug": ["bug", "fix", "error", "debug", "patch"],
        "test": ["test", "verify", "validate", "assert"],
        "docs": ["document", "readme", "comment", "explain"],
        "deploy": ["deploy", "release", "ship", "ci/cd"],
        "config": ["config", "setup", "install", "environment"],
        "security": ["security", "auth", "permission", "token"],
        "data": ["data", "database", "query", "schema", "csv"],
    }

    for tag, keywords in tag_keywords.items():
        if any(kw in text_lower for kw in keywords):
            tags.add(tag)

    return sorted(tags)[:5]  # Max 5 tags

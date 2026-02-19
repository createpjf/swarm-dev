"""
core/usage_tracker.py
Centralized usage tracking — token counts, costs, per-agent and per-model stats.
File-backed JSON store, process-safe with file locks.
"""

from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    from filelock import FileLock
except ImportError:
    class FileLock:  # type: ignore
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

logger = logging.getLogger(__name__)

USAGE_FILE = "memory/usage_stats.json"
USAGE_LOCK = "memory/usage_stats.lock"

# ── Cost estimation (per 1M tokens) ─────────────────────────────────────────
# Approximate costs for FLock-hosted models (adjust as needed)
MODEL_COSTS = {
    "minimax-m2.1":         {"input": 1.0, "output": 4.0},
    "deepseek-v3.2":        {"input": 0.5, "output": 2.0},
    "qwen3-235b-thinking":  {"input": 1.5, "output": 6.0},
    "kimi-k2.5":            {"input": 1.0, "output": 4.0},
    # Defaults for unknown models
    "_default":             {"input": 1.0, "output": 4.0},
}


def estimate_cost(model: str, prompt_tokens: int,
                  completion_tokens: int) -> float:
    """Estimate cost in USD for a single call."""
    costs = MODEL_COSTS.get(model, MODEL_COSTS["_default"])
    cost = (
        (prompt_tokens / 1_000_000) * costs["input"] +
        (completion_tokens / 1_000_000) * costs["output"]
    )
    return cost


class UsageTracker:
    """
    Process-safe usage statistics store.
    Tracks per-agent, per-model token usage and estimated costs.
    """

    def __init__(self, path: str = USAGE_FILE):
        self.path = path
        self.lock = FileLock(USAGE_LOCK)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def record(
        self,
        agent_id: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
        retries: int = 0,
        failover: bool = False,
    ):
        """Record a single LLM call's usage."""
        total_tokens = prompt_tokens + completion_tokens
        cost = estimate_cost(model, prompt_tokens, completion_tokens)

        entry = {
            "agent_id":          agent_id,
            "model":             model,
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":      total_tokens,
            "cost_usd":          cost,
            "latency_ms":        latency_ms,
            "success":           success,
            "retries":           retries,
            "failover":          failover,
            "ts":                time.time(),
        }

        with self.lock:
            data = self._read()
            data.setdefault("calls", []).append(entry)
            # Update aggregates
            agg = data.setdefault("aggregate", {})
            agg["total_calls"]        = agg.get("total_calls", 0) + 1
            agg["total_prompt_tokens"] = agg.get("total_prompt_tokens", 0) + prompt_tokens
            agg["total_completion_tokens"] = agg.get("total_completion_tokens", 0) + completion_tokens
            agg["total_tokens"]       = agg.get("total_tokens", 0) + total_tokens
            agg["total_cost_usd"]     = agg.get("total_cost_usd", 0) + cost
            agg["total_retries"]      = agg.get("total_retries", 0) + retries
            agg["total_failovers"]    = agg.get("total_failovers", 0) + (1 if failover else 0)
            if success:
                agg["success_count"]  = agg.get("success_count", 0) + 1
            else:
                agg["failure_count"]  = agg.get("failure_count", 0) + 1

            self._write(data)

    def get_summary(self) -> dict:
        """Get aggregated usage summary."""
        data = self._read()
        agg = data.get("aggregate", {})

        # Per-agent breakdown
        by_agent: dict[str, dict] = {}
        by_model: dict[str, dict] = {}

        for call in data.get("calls", []):
            aid = call.get("agent_id", "unknown")
            mid = call.get("model", "unknown")

            if aid not in by_agent:
                by_agent[aid] = {"calls": 0, "tokens": 0, "cost": 0.0}
            by_agent[aid]["calls"] += 1
            by_agent[aid]["tokens"] += call.get("total_tokens", 0)
            by_agent[aid]["cost"]  += call.get("cost_usd", 0)

            if mid not in by_model:
                by_model[mid] = {"calls": 0, "tokens": 0, "cost": 0.0}
            by_model[mid]["calls"] += 1
            by_model[mid]["tokens"] += call.get("total_tokens", 0)
            by_model[mid]["cost"]  += call.get("cost_usd", 0)

        return {
            "aggregate": agg,
            "by_agent":  by_agent,
            "by_model":  by_model,
        }

    def get_session_summary(self, since_ts: float = 0) -> dict:
        """Get usage summary for calls since a timestamp."""
        data = self._read()
        calls = [c for c in data.get("calls", [])
                 if c.get("ts", 0) >= since_ts]

        total_tokens = sum(c.get("total_tokens", 0) for c in calls)
        total_cost = sum(c.get("cost_usd", 0) for c in calls)
        successes = sum(1 for c in calls if c.get("success"))
        latencies = [c.get("latency_ms", 0) for c in calls
                     if c.get("success") and c.get("latency_ms", 0) > 0]

        return {
            "calls":       len(calls),
            "tokens":      total_tokens,
            "cost_usd":    total_cost,
            "successes":   successes,
            "failures":    len(calls) - successes,
            "avg_latency": sum(latencies) / len(latencies) if latencies else 0,
        }

    def clear(self):
        """Reset all usage data."""
        with self.lock:
            self._write({"calls": [], "aggregate": {}})

    def _read(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"calls": [], "aggregate": {}}

    def _write(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

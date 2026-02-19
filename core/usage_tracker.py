"""
core/usage_tracker.py
Centralized usage tracking — token counts, costs, per-agent and per-model stats.
File-backed JSON store, process-safe with file locks.
Budget limits: configurable spending cap with auto-pause.
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
BUDGET_FILE = "config/budget.json"

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


class BudgetExceeded(Exception):
    """Raised when spending exceeds the configured budget limit."""
    pass


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
    Budget enforcement: checks spending against limits and raises BudgetExceeded.
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
        """Record a single LLM call's usage. Checks budget limits."""
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

            # Check budget limits inside lock to prevent concurrent overspend
            self._check_budget(agg)

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

    # ── Budget Management ─────────────────────────────────────────────────

    def _check_budget(self, agg: dict):
        """Check if spending exceeds budget limits. Raises BudgetExceeded."""
        budget = self._read_budget()
        if not budget.get("enabled", False):
            return

        total_cost = agg.get("total_cost_usd", 0)
        max_cost = budget.get("max_cost_usd", 0)
        warn_at = budget.get("warn_at_percent", 80) / 100.0

        if max_cost > 0:
            # Warning threshold
            if total_cost >= max_cost * warn_at:
                pct = (total_cost / max_cost) * 100
                logger.warning(
                    "Budget alert: $%.4f / $%.2f (%.0f%%) spent",
                    total_cost, max_cost, pct)
                # Write alert to a file for the dashboard to pick up
                self._write_alert({
                    "type": "budget_warning",
                    "message": f"Budget {pct:.0f}% used (${total_cost:.4f} / ${max_cost:.2f})",
                    "cost": total_cost,
                    "limit": max_cost,
                    "percent": pct,
                    "ts": time.time(),
                })

            # Hard limit
            if total_cost >= max_cost:
                self._write_alert({
                    "type": "budget_exceeded",
                    "message": f"Budget exceeded: ${total_cost:.4f} >= ${max_cost:.2f}",
                    "cost": total_cost,
                    "limit": max_cost,
                    "ts": time.time(),
                })
                raise BudgetExceeded(
                    f"Budget exceeded: ${total_cost:.4f} >= ${max_cost:.2f}. "
                    f"Increase limit via config/budget.json or API.")

        # Token limit
        max_tokens = budget.get("max_tokens", 0)
        if max_tokens > 0:
            total_tokens = agg.get("total_tokens", 0)
            if total_tokens >= max_tokens:
                raise BudgetExceeded(
                    f"Token limit exceeded: {total_tokens:,} >= {max_tokens:,}")

    @staticmethod
    def _read_budget() -> dict:
        """Read budget config. Returns empty dict if not configured."""
        try:
            with open(BUDGET_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def set_budget(max_cost_usd: float = 0, max_tokens: int = 0,
                   warn_at_percent: int = 80, enabled: bool = True):
        """Set budget limits. Called from CLI or API."""
        budget = {
            "enabled": enabled,
            "max_cost_usd": max_cost_usd,
            "max_tokens": max_tokens,
            "warn_at_percent": warn_at_percent,
            "updated_at": time.time(),
        }
        os.makedirs(os.path.dirname(BUDGET_FILE) or ".", exist_ok=True)
        with open(BUDGET_FILE, "w") as f:
            json.dump(budget, f, indent=2)
        return budget

    @staticmethod
    def get_budget() -> dict:
        """Get current budget config + spending status."""
        try:
            with open(BUDGET_FILE, "r") as f:
                budget = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            budget = {"enabled": False, "max_cost_usd": 0, "max_tokens": 0}

        # Add current spending
        try:
            with open(USAGE_FILE, "r") as f:
                data = json.load(f)
            agg = data.get("aggregate", {})
            budget["current_cost_usd"] = agg.get("total_cost_usd", 0)
            budget["current_tokens"] = agg.get("total_tokens", 0)
            max_cost = budget.get("max_cost_usd", 0)
            if max_cost > 0:
                budget["percent_used"] = round(
                    (budget["current_cost_usd"] / max_cost) * 100, 1)
            else:
                budget["percent_used"] = 0
        except (FileNotFoundError, json.JSONDecodeError):
            budget["current_cost_usd"] = 0
            budget["current_tokens"] = 0
            budget["percent_used"] = 0

        return budget

    def _write_alert(self, alert: dict):
        """Append alert to alerts file for dashboard consumption."""
        alerts_path = "memory/alerts.jsonl"
        try:
            os.makedirs(os.path.dirname(alerts_path) or ".", exist_ok=True)
            with open(alerts_path, "a") as f:
                f.write(json.dumps(alert, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def get_alerts(limit: int = 20) -> list[dict]:
        """Read recent alerts."""
        alerts_path = "memory/alerts.jsonl"
        alerts = []
        try:
            with open(alerts_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            alerts.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        return alerts[-limit:]

    def _read(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"calls": [], "aggregate": {}}

    def _write(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

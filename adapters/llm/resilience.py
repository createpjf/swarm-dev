"""
adapters/llm/resilience.py
Resilient LLM wrapper — OpenClaw-inspired retry, circuit breaker, model failover.

Two-stage failover:
  Stage 1: Retry same model with exponential backoff (for transient errors)
  Stage 2: Failover to fallback models (for persistent auth/model errors)

Error classification:
  - NO_RETRY: 401, 403, 404 — bad key/model, switch immediately
  - RETRY:    429, 500, 502, 503, 504, timeout, connection — transient
  - FATAL:    other 4xx — client error, don't retry

Circuit breaker:
  - After N consecutive failures on a model, mark it as "open" (skip it)
  - Auto-recover after cooldown period
"""

from __future__ import annotations
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Error classification ─────────────────────────────────────────────────────

class ErrorClass:
    NO_RETRY  = "no_retry"    # 401/403/404 — switch model immediately
    RETRY     = "retry"       # 429/5xx/timeout — retry with backoff
    FATAL     = "fatal"       # other errors — don't retry

def classify_error(exc: Exception) -> str:
    """Classify an exception for retry/failover decision."""
    exc_str = str(exc).lower()

    # httpx.HTTPStatusError
    if hasattr(exc, 'response'):
        status = exc.response.status_code
        if status in (401, 403):
            return ErrorClass.NO_RETRY
        if status == 404:
            return ErrorClass.NO_RETRY  # model not found
        if status == 429:
            return ErrorClass.RETRY
        if status == 400:
            # 400 can be transient (payload edge case, token count spike)
            # Retry once before failing over — log body for diagnosis
            logger.warning("[resilience] HTTP 400 — will retry: %s", exc_str[:200])
            return ErrorClass.RETRY
        if 500 <= status < 600:
            return ErrorClass.RETRY
        return ErrorClass.FATAL  # other 4xx (402, 405-428, etc.)

    # RuntimeError wrapping HTTP status (from our adapters)
    if "api error (400)" in exc_str:
        logger.warning("[resilience] Wrapped 400 error — will retry: %s", exc_str[:200])
        return ErrorClass.RETRY

    # Timeout / connection errors
    if any(kw in exc_str for kw in [
        "timeout", "timed out", "connecterror", "connectionerror",
        "connection refused", "network", "unreachable",
    ]):
        return ErrorClass.RETRY

    return ErrorClass.FATAL


# ── Circuit Breaker ──────────────────────────────────────────────────────────

@dataclass
class CircuitState:
    """Per-model circuit breaker state."""
    failures:    int   = 0
    last_fail:   float = 0.0
    is_open:     bool  = False
    open_since:  float = 0.0

    # Config
    threshold:   int   = 3       # consecutive failures to trip
    cooldown:    float = 120.0   # seconds before auto-recover

    def record_failure(self):
        self.failures += 1
        self.last_fail = time.time()
        if self.failures >= self.threshold:
            self.is_open = True
            self.open_since = time.time()
            logger.warning("Circuit OPEN after %d consecutive failures", self.failures)

    def record_success(self):
        self.failures = 0
        if self.is_open:
            self.is_open = False
            logger.info("Circuit CLOSED — model recovered")

    def is_available(self) -> bool:
        if not self.is_open:
            return True
        # Auto-recover after cooldown
        if time.time() - self.open_since > self.cooldown:
            self.is_open = False
            self.failures = 0
            logger.info("Circuit HALF-OPEN — attempting recovery")
            return True
        return False


# ── Usage tracking data ──────────────────────────────────────────────────────

@dataclass
class UsageRecord:
    """Track token usage and cost per call."""
    model:           str   = ""
    prompt_tokens:   int   = 0
    completion_tokens: int = 0
    total_tokens:    int   = 0
    latency_ms:      float = 0.0
    timestamp:       float = field(default_factory=time.time)
    success:         bool  = True
    retries:         int   = 0
    failover_used:   bool  = False


# ── Resilient LLM Wrapper ───────────────────────────────────────────────────

class ResilientLLM:
    """
    Wraps an LLM adapter with retry, circuit breaker, and model failover.

    Usage:
        base_llm = FLockAdapter(api_key=..., base_url=...)
        llm = ResilientLLM(
            adapter=base_llm,
            fallback_models=["deepseek-v3.2", "qwen3-235b-thinking"],
            max_retries=3,
        )
        result = await llm.chat(messages, model="minimax-m2.1")
    """

    def __init__(
        self,
        adapter,
        fallback_models: list[str] | None = None,
        max_retries:     int   = 3,
        base_delay:      float = 1.0,     # initial backoff seconds
        max_delay:       float = 30.0,    # max backoff seconds
        jitter:          float = 0.5,     # jitter factor (0-1)
        cb_threshold:    int   = 3,       # circuit breaker threshold
        cb_cooldown:     float = 120.0,   # circuit breaker cooldown (s)
    ):
        self.adapter          = adapter
        self.fallback_models  = fallback_models or []
        self.max_retries      = max_retries
        self.base_delay       = base_delay
        self.max_delay        = max_delay
        self.jitter           = jitter

        # Per-model circuit breakers
        self._circuits: dict[str, CircuitState] = {}
        self._cb_threshold = cb_threshold
        self._cb_cooldown  = cb_cooldown

        # Usage tracking
        self.usage_log: list[UsageRecord] = []

    def _get_circuit(self, model: str) -> CircuitState:
        if model not in self._circuits:
            self._circuits[model] = CircuitState(
                threshold=self._cb_threshold,
                cooldown=self._cb_cooldown,
            )
        return self._circuits[model]

    async def chat(self, messages: list[dict], model: str) -> str:
        """
        Two-stage resilient chat:
          Stage 1: Try primary model with retries (for transient errors)
          Stage 2: On persistent failure, try fallback models in order
        """
        # Build model sequence: primary + fallbacks
        models_to_try = [model] + [m for m in self.fallback_models if m != model]

        last_exc = None
        total_retries = 0

        for model_idx, current_model in enumerate(models_to_try):
            circuit = self._get_circuit(current_model)

            # Skip models with open circuit
            if not circuit.is_available():
                logger.info("Skipping %s — circuit open", current_model)
                continue

            is_failover = (model_idx > 0)
            if is_failover:
                logger.info("Failover: trying model %s", current_model)

            # Stage 1: Retry loop for current model
            retries = 0
            while retries <= self.max_retries:
                try:
                    start_ts = time.time()

                    # Prefer chat_with_usage() to capture token counts
                    usage_info = {}
                    if hasattr(self.adapter, 'chat_with_usage'):
                        result, usage_info = await self.adapter.chat_with_usage(
                            messages, current_model)
                    else:
                        result = await self.adapter.chat(messages, current_model)

                    latency = (time.time() - start_ts) * 1000

                    # Success!
                    circuit.record_success()

                    # Track usage (with real token counts from API)
                    record = UsageRecord(
                        model=current_model,
                        prompt_tokens=usage_info.get("prompt_tokens", 0),
                        completion_tokens=usage_info.get("completion_tokens", 0),
                        total_tokens=usage_info.get("total_tokens", 0),
                        latency_ms=latency,
                        success=True,
                        retries=total_retries,
                        failover_used=is_failover,
                    )
                    self.usage_log.append(record)

                    if is_failover:
                        logger.info("Failover to %s succeeded", current_model)
                    return result

                except Exception as exc:
                    last_exc = exc
                    error_class = classify_error(exc)

                    logger.warning(
                        "[resilience] %s attempt %d/%d failed (%s): %s",
                        current_model, retries + 1, self.max_retries + 1,
                        error_class, str(exc)[:120],
                    )

                    if error_class == ErrorClass.NO_RETRY:
                        # Auth/permission error — skip to next model
                        circuit.record_failure()
                        break

                    if error_class == ErrorClass.FATAL:
                        # Unknown client error — don't retry
                        circuit.record_failure()
                        break

                    if error_class == ErrorClass.RETRY:
                        circuit.record_failure()
                        retries += 1
                        total_retries += 1

                        if retries > self.max_retries:
                            break

                        # Exponential backoff with jitter
                        delay = min(
                            self.base_delay * (2 ** (retries - 1)),
                            self.max_delay,
                        )
                        actual_delay = delay * (
                            1 + self.jitter * (random.random() * 2 - 1)
                        )
                        logger.info(
                            "Retrying %s in %.1fs (attempt %d/%d)",
                            current_model, actual_delay,
                            retries + 1, self.max_retries + 1,
                        )
                        await asyncio.sleep(actual_delay)

        # All models exhausted — track failure and raise
        self.usage_log.append(UsageRecord(
            model=model,
            success=False,
            retries=total_retries,
            failover_used=(len(models_to_try) > 1),
        ))

        raise last_exc or RuntimeError("All models exhausted")

    async def chat_stream(self, messages: list[dict], model: str):
        """
        Streaming chat with same resilience logic.
        Yields content chunks as they arrive.
        Falls back to non-streaming if adapter doesn't support it.
        Tracks estimated token usage after streaming completes.
        """
        models_to_try = [model] + [m for m in self.fallback_models if m != model]

        last_exc = None
        total_retries = 0
        for model_idx, current_model in enumerate(models_to_try):
            circuit = self._get_circuit(current_model)
            if not circuit.is_available():
                continue

            is_failover = (model_idx > 0)
            retries = 0
            while retries <= self.max_retries:
                try:
                    start_ts = time.time()
                    output_chars = 0

                    if hasattr(self.adapter, 'chat_stream'):
                        async for chunk in self.adapter.chat_stream(
                            messages, current_model
                        ):
                            output_chars += len(chunk)
                            yield chunk
                    else:
                        # Fallback: non-streaming, yield whole result
                        result = await self.adapter.chat(messages, current_model)
                        output_chars = len(result)
                        yield result

                    latency = (time.time() - start_ts) * 1000
                    circuit.record_success()

                    # Estimate tokens from char counts (~4 chars/token)
                    prompt_chars = sum(
                        len(m.get("content", "")) for m in messages)
                    est_prompt = max(1, prompt_chars // 4)
                    est_completion = max(1, output_chars // 4)

                    record = UsageRecord(
                        model=current_model,
                        prompt_tokens=est_prompt,
                        completion_tokens=est_completion,
                        total_tokens=est_prompt + est_completion,
                        latency_ms=latency,
                        success=True,
                        retries=total_retries,
                        failover_used=is_failover,
                    )
                    self.usage_log.append(record)
                    return

                except Exception as exc:
                    last_exc = exc
                    error_class = classify_error(exc)

                    if error_class == ErrorClass.NO_RETRY:
                        circuit.record_failure()
                        break

                    if error_class == ErrorClass.RETRY:
                        circuit.record_failure()
                        retries += 1
                        total_retries += 1
                        if retries > self.max_retries:
                            break
                        delay = min(
                            self.base_delay * (2 ** (retries - 1)),
                            self.max_delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        circuit.record_failure()
                        break

        raise last_exc or RuntimeError("All models exhausted (stream)")

    # ── Usage stats ──────────────────────────────────────────────────────

    def get_usage_summary(self) -> dict:
        """Return aggregated usage statistics."""
        if not self.usage_log:
            return {"total_calls": 0}

        total = len(self.usage_log)
        successes = sum(1 for r in self.usage_log if r.success)
        failures  = total - successes
        retries   = sum(r.retries for r in self.usage_log)
        failovers = sum(1 for r in self.usage_log if r.failover_used)

        latencies = [r.latency_ms for r in self.usage_log
                     if r.success and r.latency_ms > 0]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0

        # Per-model breakdown
        by_model: dict[str, dict] = {}
        for r in self.usage_log:
            m = r.model
            if m not in by_model:
                by_model[m] = {"calls": 0, "successes": 0, "prompt_tokens": 0,
                               "completion_tokens": 0, "total_tokens": 0}
            by_model[m]["calls"] += 1
            if r.success:
                by_model[m]["successes"] += 1
            by_model[m]["prompt_tokens"] += r.prompt_tokens
            by_model[m]["completion_tokens"] += r.completion_tokens
            by_model[m]["total_tokens"] += r.total_tokens

        return {
            "total_calls":   total,
            "successes":     successes,
            "failures":      failures,
            "retry_count":   retries,
            "failover_count": failovers,
            "avg_latency_ms": round(avg_latency, 1),
            "by_model":      by_model,
        }

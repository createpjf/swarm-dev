"""
core/provider_router.py — Smart cross-provider LLM routing.

Sits above ResilientLLM to enable cross-provider failover:
  - MiniMax down → auto-switch to OpenAI → Ollama (local)
  - Latency-weighted selection (prefer faster providers)
  - Cost-aware routing (prefer cheaper for simple tasks)
  - Active health probes (periodic background pings)
  - Provider health dashboard (exposed via gateway)

Architecture:
  ProviderRouter wraps multiple (provider, adapter) pairs.
  Each request picks the best available provider based on:
    1. Health status (circuit breaker per provider)
    2. Latency (EMA-weighted moving average)
    3. Cost tier (configurable per model)
    4. Preference (user can set preferred provider)

  ProviderRouter
    ├── ProviderEntry(minimax, MinimaxAdapter, health, stats)
    ├── ProviderEntry(openai, OpenAIAdapter, health, stats)
    ├── ProviderEntry(ollama, OllamaAdapter, health, stats)
    └── Each entry → ResilientLLM (model-level failover within provider)

Config (agents.yaml):
  provider_router:
    enabled: true
    strategy: "latency"     # latency | cost | preference | round_robin
    preferred: "minimax"    # preferred provider (soft preference)
    probe_interval: 60      # health probe interval (seconds)
    providers:
      minimax:
        models: ["MiniMax-M2.5-highspeed", "MiniMax-M2.5"]
        cost_per_1k_tokens: 0.001
        priority: 1
      openai:
        models: ["gpt-4o-mini", "gpt-4o"]
        cost_per_1k_tokens: 0.01
        priority: 2
      ollama:
        models: ["llama3.2", "qwen2.5"]
        cost_per_1k_tokens: 0
        priority: 3
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import random
from dataclasses import dataclass, field
from typing import Optional, AsyncIterator

logger = logging.getLogger(__name__)


# ── Provider Health ──────────────────────────────────────────────────────────

@dataclass
class ProviderHealth:
    """Health state for a single provider."""

    name: str
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_tokens: int = 0

    # Latency tracking (EMA)
    avg_latency_ms: float = 0.0
    _ema_alpha: float = 0.3  # EMA smoothing factor

    # Circuit breaker
    is_open: bool = False
    open_since: float = 0.0
    cb_threshold: int = 5      # failures before trip
    cb_cooldown: float = 180.0  # seconds before auto-recover

    # Last probe result
    last_probe_ts: float = 0.0
    last_probe_ok: bool = True
    last_probe_latency_ms: float = 0.0

    # Config
    priority: int = 1
    cost_per_1k: float = 0.0

    def record_success(self, latency_ms: float, tokens: int = 0):
        """Record a successful call."""
        self.total_calls += 1
        self.total_tokens += tokens
        self.consecutive_failures = 0
        self.consecutive_successes += 1

        # EMA latency
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = (
                self._ema_alpha * latency_ms +
                (1 - self._ema_alpha) * self.avg_latency_ms
            )

        # Close circuit
        if self.is_open:
            self.is_open = False
            logger.info("[router] Provider %s circuit CLOSED — recovered",
                        self.name)

    def record_failure(self):
        """Record a failed call."""
        self.total_calls += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0

        if self.consecutive_failures >= self.cb_threshold:
            self.is_open = True
            self.open_since = time.time()
            logger.warning("[router] Provider %s circuit OPEN after %d failures",
                           self.name, self.consecutive_failures)

    def is_available(self) -> bool:
        """Check if provider is available (circuit closed or cooled down)."""
        if not self.is_open:
            return True
        # Auto-recover after cooldown
        if time.time() - self.open_since > self.cb_cooldown:
            self.is_open = False
            self.consecutive_failures = 0
            logger.info("[router] Provider %s circuit HALF-OPEN — probing",
                        self.name)
            return True
        return False

    @property
    def success_rate(self) -> float:
        """Return success rate (0.0-1.0)."""
        if self.total_calls == 0:
            return 1.0
        return 1.0 - (self.total_failures / self.total_calls)

    def to_dict(self) -> dict:
        """Serialize for dashboard display."""
        return {
            "name": self.name,
            "available": self.is_available(),
            "circuit_open": self.is_open,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "success_rate": round(self.success_rate, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "consecutive_failures": self.consecutive_failures,
            "total_tokens": self.total_tokens,
            "priority": self.priority,
            "cost_per_1k": self.cost_per_1k,
            "last_probe_ok": self.last_probe_ok,
            "last_probe_latency_ms": round(self.last_probe_latency_ms, 1),
        }


# ── Provider Entry ───────────────────────────────────────────────────────────

@dataclass
class ProviderEntry:
    """A registered provider with its adapter and health state."""

    name: str
    adapter: object         # LLM adapter (OpenAI, MiniMax, etc.)
    models: list[str]       # Available models
    health: ProviderHealth = field(default_factory=lambda: ProviderHealth(""))

    def __post_init__(self):
        if not self.health.name:
            self.health.name = self.name


# ── Routing Strategies ───────────────────────────────────────────────────────

class RoutingStrategy:
    LATENCY    = "latency"       # Pick lowest latency provider
    COST       = "cost"          # Pick cheapest provider
    PREFERENCE = "preference"    # Pick preferred, fall back to others
    ROUND_ROBIN = "round_robin"  # Rotate providers evenly


def _score_provider(entry: ProviderEntry, strategy: str,
                    preferred: str = "") -> float:
    """Score a provider (lower is better)."""
    h = entry.health

    if not h.is_available():
        return float("inf")

    if strategy == RoutingStrategy.LATENCY:
        # Lower latency = lower score (better)
        # Add penalty for recent failures
        base = h.avg_latency_ms if h.avg_latency_ms > 0 else 500.0
        penalty = h.consecutive_failures * 200
        return base + penalty

    elif strategy == RoutingStrategy.COST:
        # Lower cost = lower score (better)
        # Tie-break by latency
        base = h.cost_per_1k * 10000
        return base + (h.avg_latency_ms / 1000)

    elif strategy == RoutingStrategy.PREFERENCE:
        # Preferred gets score 0, others get priority-based score
        if entry.name == preferred:
            return 0 + (h.consecutive_failures * 100)
        return h.priority * 1000 + h.avg_latency_ms

    elif strategy == RoutingStrategy.ROUND_ROBIN:
        # Based on call count (least-used gets priority)
        return h.total_calls + (h.consecutive_failures * 100)

    # Default: priority-based
    return h.priority * 1000


# ── Provider Router ──────────────────────────────────────────────────────────

class ProviderRouter:
    """
    Smart cross-provider LLM router.

    Routes requests to the best available provider based on health,
    latency, cost, and user preference. Falls back to alternative
    providers when the primary is unavailable.
    """

    def __init__(
        self,
        strategy: str = RoutingStrategy.PREFERENCE,
        preferred: str = "",
        probe_interval: float = 60.0,
    ):
        self.strategy = strategy
        self.preferred = preferred
        self.probe_interval = probe_interval
        self._providers: dict[str, ProviderEntry] = {}
        self._probe_task: Optional[asyncio.Task] = None
        self._running = False

    def register(self, name: str, adapter: object, models: list[str],
                 priority: int = 1, cost_per_1k: float = 0.0,
                 cb_threshold: int = 5, cb_cooldown: float = 180.0):
        """Register a provider with its adapter and config."""
        health = ProviderHealth(
            name=name,
            priority=priority,
            cost_per_1k=cost_per_1k,
            cb_threshold=cb_threshold,
            cb_cooldown=cb_cooldown,
        )
        self._providers[name] = ProviderEntry(
            name=name,
            adapter=adapter,
            models=models,
            health=health,
        )
        logger.info("[router] Registered provider %s with %d models "
                    "(priority=%d, cost=%.4f/1k)",
                    name, len(models), priority, cost_per_1k)

    def _sorted_providers(self) -> list[ProviderEntry]:
        """Get providers sorted by routing strategy score."""
        entries = list(self._providers.values())
        entries.sort(key=lambda e: _score_provider(
            e, self.strategy, self.preferred))
        return entries

    def select_provider(self, model_hint: str = "") -> ProviderEntry | None:
        """Select the best available provider.

        Args:
            model_hint: If given, prefer provider that has this model
        """
        # If model_hint matches a specific provider, try that first
        if model_hint:
            for entry in self._providers.values():
                if model_hint in entry.models and entry.health.is_available():
                    return entry

        # Fall back to strategy-based selection
        for entry in self._sorted_providers():
            if entry.health.is_available():
                return entry

        return None

    async def chat(self, messages: list[dict], model: str) -> str:
        """Route a chat request to the best available provider.

        Tries providers in priority order, wrapping each with the
        existing ResilientLLM for model-level failover within a provider.
        """
        providers = self._sorted_providers()
        last_exc = None

        for entry in providers:
            if not entry.health.is_available():
                continue

            # Determine model to use for this provider
            current_model = model if model in entry.models else (
                entry.models[0] if entry.models else model)

            start_ts = time.time()
            try:
                if hasattr(entry.adapter, 'chat_with_usage'):
                    result, usage = await entry.adapter.chat_with_usage(
                        messages, current_model)
                    tokens = usage.get("total_tokens", 0)
                else:
                    result = await entry.adapter.chat(messages, current_model)
                    tokens = 0

                latency = (time.time() - start_ts) * 1000
                entry.health.record_success(latency, tokens)

                logger.info("[router] %s completed in %.0fms (%s)",
                            entry.name, latency, current_model)
                return result

            except Exception as exc:
                latency = (time.time() - start_ts) * 1000
                entry.health.record_failure()
                last_exc = exc
                logger.warning("[router] %s failed (%.0fms): %s",
                               entry.name, latency, str(exc)[:120])
                continue

        raise last_exc or RuntimeError(
            "All providers exhausted. Status: " +
            ", ".join(f"{p.name}={'open' if p.health.is_open else 'ok'}"
                      for p in providers))

    async def chat_stream(self, messages: list[dict],
                          model: str) -> AsyncIterator[str]:
        """Route a streaming chat request with cross-provider failover."""
        providers = self._sorted_providers()
        last_exc = None

        for entry in providers:
            if not entry.health.is_available():
                continue

            current_model = model if model in entry.models else (
                entry.models[0] if entry.models else model)

            start_ts = time.time()
            try:
                output_chars = 0
                if hasattr(entry.adapter, 'chat_stream'):
                    async for chunk in entry.adapter.chat_stream(
                        messages, current_model
                    ):
                        output_chars += len(chunk)
                        yield chunk
                else:
                    result = await entry.adapter.chat(messages, current_model)
                    output_chars = len(result)
                    yield result

                latency = (time.time() - start_ts) * 1000
                est_tokens = max(1, output_chars // 4)
                entry.health.record_success(latency, est_tokens)
                return

            except Exception as exc:
                entry.health.record_failure()
                last_exc = exc
                logger.warning("[router] %s stream failed: %s",
                               entry.name, str(exc)[:120])
                continue

        raise last_exc or RuntimeError("All providers exhausted (stream)")

    # ── Health Probes ────────────────────────────────────────────────────

    async def start_probes(self):
        """Start background health probe loop."""
        if self._running:
            return
        self._running = True
        self._probe_task = asyncio.create_task(self._probe_loop())
        logger.info("[router] Health probes started (interval=%ds)",
                    int(self.probe_interval))

    async def stop_probes(self):
        """Stop health probe loop."""
        self._running = False
        if self._probe_task:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass

    async def _probe_loop(self):
        """Periodically probe provider health."""
        while self._running:
            try:
                await asyncio.sleep(self.probe_interval)
                await self._probe_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[router] Probe error: %s", e)
                await asyncio.sleep(10)

    async def _probe_all(self):
        """Probe all providers with a minimal request."""
        for entry in self._providers.values():
            await self._probe_one(entry)

    async def _probe_one(self, entry: ProviderEntry):
        """Send a minimal health check to a provider."""
        model = entry.models[0] if entry.models else "gpt-4o-mini"
        messages = [{"role": "user", "content": "ping"}]

        start_ts = time.time()
        try:
            # Use a very short max_tokens to minimize cost
            if hasattr(entry.adapter, 'chat'):
                # We can't easily limit tokens in a generic way,
                # so we'll just time the connection + first response
                result = await asyncio.wait_for(
                    entry.adapter.chat(messages, model),
                    timeout=15.0,
                )

            latency = (time.time() - start_ts) * 1000
            entry.health.last_probe_ts = time.time()
            entry.health.last_probe_ok = True
            entry.health.last_probe_latency_ms = latency

            # If circuit was open and probe succeeded, close it
            if entry.health.is_open:
                entry.health.is_open = False
                entry.health.consecutive_failures = 0
                logger.info("[router] Probe: %s recovered (%.0fms)",
                            entry.name, latency)
            else:
                logger.debug("[router] Probe: %s OK (%.0fms)",
                             entry.name, latency)

        except Exception as e:
            latency = (time.time() - start_ts) * 1000
            entry.health.last_probe_ts = time.time()
            entry.health.last_probe_ok = False
            entry.health.last_probe_latency_ms = latency
            logger.debug("[router] Probe: %s FAIL (%.0fms): %s",
                         entry.name, latency, str(e)[:80])

    # ── Dashboard / Status ───────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get routing status for dashboard display."""
        providers = []
        for entry in self._sorted_providers():
            info = entry.health.to_dict()
            info["models"] = entry.models
            info["score"] = round(_score_provider(
                entry, self.strategy, self.preferred), 1)
            providers.append(info)

        total_calls = sum(p.health.total_calls
                          for p in self._providers.values())
        total_tokens = sum(p.health.total_tokens
                           for p in self._providers.values())

        return {
            "strategy": self.strategy,
            "preferred": self.preferred,
            "provider_count": len(self._providers),
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "providers": providers,
        }

    def get_provider_health(self, name: str) -> dict | None:
        """Get health for a specific provider."""
        entry = self._providers.get(name)
        if entry:
            return entry.health.to_dict()
        return None

    @property
    def provider_names(self) -> list[str]:
        return list(self._providers.keys())


# ── Factory / Integration ────────────────────────────────────────────────────

_router: ProviderRouter | None = None


def build_provider_router(config: dict) -> ProviderRouter | None:
    """Build a ProviderRouter from agents.yaml config.

    Config format:
      provider_router:
        enabled: true
        strategy: "preference"
        preferred: "minimax"
        probe_interval: 60
        providers:
          minimax:
            models: ["MiniMax-M2.5-highspeed", "MiniMax-M2.5"]
            cost_per_1k_tokens: 0.001
            priority: 1
            api_key_env: MINIMAX_API_KEY
            base_url_env: MINIMAX_BASE_URL
          openai:
            models: ["gpt-4o-mini", "gpt-4o"]
            cost_per_1k_tokens: 0.01
            priority: 2
          ollama:
            models: ["llama3.2"]
            cost_per_1k_tokens: 0
            priority: 3

    Returns None if router is not enabled or config is missing.
    """
    global _router

    router_cfg = config.get("provider_router", {})
    if not router_cfg.get("enabled", False):
        return None

    strategy = router_cfg.get("strategy", RoutingStrategy.PREFERENCE)
    preferred = router_cfg.get("preferred", "")
    probe_interval = router_cfg.get("probe_interval", 60)

    router = ProviderRouter(
        strategy=strategy,
        preferred=preferred,
        probe_interval=probe_interval,
    )

    providers_cfg = router_cfg.get("providers", {})

    for pname, pcfg in providers_cfg.items():
        models = pcfg.get("models", [])
        priority = pcfg.get("priority", 5)
        cost = pcfg.get("cost_per_1k_tokens", 0.0)
        api_key_env = pcfg.get("api_key_env", "")
        base_url_env = pcfg.get("base_url_env", "")

        api_key = os.getenv(api_key_env) if api_key_env else None
        base_url = os.getenv(base_url_env) if base_url_env else None

        # Build adapter
        adapter = _build_adapter(pname, api_key, base_url)
        if adapter is None:
            logger.warning("[router] Cannot create adapter for %s — skipping",
                           pname)
            continue

        router.register(
            name=pname,
            adapter=adapter,
            models=models,
            priority=priority,
            cost_per_1k=cost,
        )

    if not router.provider_names:
        logger.warning("[router] No providers registered — router disabled")
        return None

    _router = router
    logger.info("[router] Provider router ready: %s (strategy=%s)",
                ", ".join(router.provider_names), strategy)

    # Start background health probes if running inside an event loop.
    # When called from _agent_process (child process), each process has
    # its own asyncio loop so start_probes() will be picked up on the
    # next await.  When called at module level before any loop exists,
    # we skip — probes will be started when the loop is available.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(router.start_probes())
        logger.info("[router] Health probes started (interval=%ds)",
                    probe_interval)
    except RuntimeError:
        # No running event loop — caller must start probes manually
        logger.debug("[router] No event loop yet; call start_probes() later")

    return router


def _build_adapter(provider: str, api_key: str | None,
                   base_url: str | None) -> object | None:
    """Build an LLM adapter for a provider name."""
    try:
        if provider == "minimax":
            from adapters.llm.minimax import MinimaxAdapter
            return MinimaxAdapter(api_key=api_key, base_url=base_url)

        elif provider == "openai":
            from adapters.llm.openai import OpenAIAdapter
            return OpenAIAdapter(api_key=api_key, base_url=base_url)

        elif provider == "flock":
            from adapters.llm.flock import FLockAdapter
            return FLockAdapter(api_key=api_key, base_url=base_url)

        elif provider == "ollama":
            from adapters.llm.ollama import OllamaAdapter
            return OllamaAdapter(api_key=api_key, base_url=base_url)

        else:
            # Default: OpenAI-compatible
            from adapters.llm.openai import OpenAIAdapter
            return OpenAIAdapter(api_key=api_key, base_url=base_url)

    except Exception as e:
        logger.warning("[router] Failed to create %s adapter: %s",
                       provider, e)
        return None


def get_router() -> ProviderRouter | None:
    """Get the global provider router (if enabled)."""
    return _router

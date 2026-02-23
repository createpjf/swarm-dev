"""
tests/test_provider_router.py
Sprint 5.3 — Tests for core/provider_router.py

Covers:
  - ProviderHealth: EMA latency, circuit breaker, availability
  - ProviderRouter: registration, selection strategies, failover
  - Routing strategies: latency, cost, preference, round_robin
  - build_provider_router: config parsing, probe startup
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.provider_router import (
    ProviderHealth,
    ProviderEntry,
    ProviderRouter,
    RoutingStrategy,
    _score_provider,
    build_provider_router,
)


# ══════════════════════════════════════════════════════════════════════════════
#  PROVIDER HEALTH
# ══════════════════════════════════════════════════════════════════════════════

class TestProviderHealth:

    def test_initial_state(self):
        h = ProviderHealth("minimax")
        assert h.name == "minimax"
        assert h.is_available()
        assert h.success_rate == 1.0
        assert h.total_calls == 0

    def test_record_success_updates_latency(self):
        h = ProviderHealth("test")
        h.record_success(200.0, tokens=100)
        assert h.avg_latency_ms == 200.0
        assert h.total_calls == 1
        assert h.total_tokens == 100
        assert h.consecutive_successes == 1

    def test_ema_latency_smoothing(self):
        h = ProviderHealth("test")
        h.record_success(100.0)
        h.record_success(200.0)
        # EMA: 0.3 * 200 + 0.7 * 100 = 130
        assert abs(h.avg_latency_ms - 130.0) < 0.1

    def test_circuit_breaker_trips_after_threshold(self):
        h = ProviderHealth("test", cb_threshold=3)
        for _ in range(3):
            h.record_failure()
        assert h.is_open
        assert not h.is_available()

    def test_circuit_breaker_auto_recovers(self):
        h = ProviderHealth("test", cb_threshold=2, cb_cooldown=0.1)
        h.record_failure()
        h.record_failure()
        assert h.is_open
        # Wait for cooldown
        time.sleep(0.15)
        assert h.is_available()  # should auto-recover
        assert not h.is_open

    def test_success_closes_circuit(self):
        h = ProviderHealth("test", cb_threshold=2)
        h.record_failure()
        h.record_failure()
        assert h.is_open
        h.record_success(100.0)
        assert not h.is_open

    def test_success_rate(self):
        h = ProviderHealth("test")
        h.record_success(100.0)
        h.record_success(100.0)
        h.record_failure()
        # 2 success, 1 failure out of 3
        assert abs(h.success_rate - 2/3) < 0.01

    def test_to_dict(self):
        h = ProviderHealth("minimax", priority=1, cost_per_1k=0.001)
        d = h.to_dict()
        assert d["name"] == "minimax"
        assert d["available"] is True
        assert d["priority"] == 1


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTING STRATEGIES
# ══════════════════════════════════════════════════════════════════════════════

class TestRoutingStrategies:

    def _make_entry(self, name, latency=100, cost=0.01, priority=1,
                    failures=0):
        h = ProviderHealth(name, priority=priority, cost_per_1k=cost)
        h.avg_latency_ms = latency
        h.consecutive_failures = failures
        return ProviderEntry(name=name, adapter=MagicMock(),
                             models=["m1"], health=h)

    def test_latency_prefers_lower(self):
        fast = self._make_entry("fast", latency=50)
        slow = self._make_entry("slow", latency=500)
        assert _score_provider(fast, "latency") < \
               _score_provider(slow, "latency")

    def test_latency_penalizes_failures(self):
        healthy = self._make_entry("healthy", latency=100, failures=0)
        failing = self._make_entry("failing", latency=100, failures=3)
        assert _score_provider(healthy, "latency") < \
               _score_provider(failing, "latency")

    def test_cost_prefers_cheaper(self):
        cheap = self._make_entry("cheap", cost=0.001)
        expensive = self._make_entry("expensive", cost=0.1)
        assert _score_provider(cheap, "cost") < \
               _score_provider(expensive, "cost")

    def test_preference_favors_preferred(self):
        preferred = self._make_entry("minimax", priority=2)
        other = self._make_entry("openai", priority=1)
        assert _score_provider(preferred, "preference",
                               preferred="minimax") < \
               _score_provider(other, "preference", preferred="minimax")

    def test_round_robin_favors_least_used(self):
        fresh = self._make_entry("fresh")
        fresh.health.total_calls = 0
        used = self._make_entry("used")
        used.health.total_calls = 100
        assert _score_provider(fresh, "round_robin") < \
               _score_provider(used, "round_robin")

    def test_unavailable_provider_scores_infinity(self):
        entry = self._make_entry("down", failures=10)
        entry.health.is_open = True
        entry.health.open_since = time.time()
        score = _score_provider(entry, "latency")
        assert score == float("inf")


# ══════════════════════════════════════════════════════════════════════════════
#  PROVIDER ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestProviderRouter:

    def _make_router(self, strategy="preference", preferred="minimax"):
        router = ProviderRouter(
            strategy=strategy, preferred=preferred, probe_interval=60)
        return router

    def test_register_and_select(self):
        router = self._make_router()
        mock_adapter = MagicMock()
        router.register("minimax", mock_adapter, ["model-a", "model-b"],
                        priority=1)
        assert "minimax" in router.provider_names
        entry = router.select_provider()
        assert entry is not None
        assert entry.name == "minimax"

    def test_select_with_model_hint(self):
        router = self._make_router()
        router.register("minimax", MagicMock(), ["mini-model"], priority=1)
        router.register("openai", MagicMock(), ["gpt-4o"], priority=2)

        entry = router.select_provider(model_hint="gpt-4o")
        assert entry.name == "openai"

    def test_failover_on_circuit_open(self):
        router = self._make_router(strategy="preference",
                                   preferred="minimax")
        router.register("minimax", MagicMock(), ["m1"], priority=1)
        router.register("openai", MagicMock(), ["m2"], priority=2)

        # Trip minimax circuit breaker
        for _ in range(5):
            router._providers["minimax"].health.record_failure()

        entry = router.select_provider()
        assert entry.name == "openai"

    def test_no_providers_returns_none(self):
        router = self._make_router()
        assert router.select_provider() is None

    @staticmethod
    def _make_adapter(chat_return=None, chat_side_effect=None):
        """Create a mock adapter that only has .chat (not chat_with_usage).

        ProviderRouter.chat() checks hasattr(adapter, 'chat_with_usage');
        AsyncMock has all attributes, so we use spec to constrain it.
        """
        class _Adapter:
            async def chat(self, messages, model): ...
        adapter = MagicMock(spec=_Adapter)
        adapter.chat = AsyncMock(return_value=chat_return,
                                 side_effect=chat_side_effect)
        return adapter

    @pytest.mark.asyncio
    async def test_chat_routes_to_best_provider(self):
        router = self._make_router()
        adapter = self._make_adapter(chat_return="hello from minimax")
        router.register("minimax", adapter, ["model-a"], priority=1)

        result = await router.chat(
            [{"role": "user", "content": "hi"}], "model-a")
        assert result == "hello from minimax"
        adapter.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_failover_on_exception(self):
        router = self._make_router(strategy="preference",
                                   preferred="minimax")
        # Primary fails
        bad = self._make_adapter(
            chat_side_effect=RuntimeError("provider down"))
        router.register("minimax", bad, ["m1"], priority=1)

        # Fallback succeeds
        good = self._make_adapter(chat_return="fallback response")
        router.register("openai", good, ["m2"], priority=2)

        result = await router.chat(
            [{"role": "user", "content": "hi"}], "m1")
        assert result == "fallback response"
        # Primary was tried and failed
        assert router._providers["minimax"].health.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_chat_all_providers_fail(self):
        router = self._make_router()
        bad = self._make_adapter(
            chat_side_effect=RuntimeError("down"))
        router.register("only", bad, ["m"], priority=1)

        with pytest.raises(RuntimeError, match="down"):
            await router.chat(
                [{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_provider_names_property(self):
        router = self._make_router()
        router.register("a", MagicMock(), ["m1"])
        router.register("b", MagicMock(), ["m2"])
        assert set(router.provider_names) == {"a", "b"}


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD PROVIDER ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildProviderRouter:

    def test_disabled_returns_none(self):
        result = build_provider_router({
            "provider_router": {"enabled": False}
        })
        assert result is None

    def test_missing_config_returns_none(self):
        result = build_provider_router({})
        assert result is None

    def test_no_providers_returns_none(self):
        result = build_provider_router({
            "provider_router": {
                "enabled": True,
                "providers": {},
            }
        })
        assert result is None

    @patch("core.provider_router._build_adapter")
    def test_builds_with_valid_config(self, mock_build):
        """With valid config and working adapters, router should be created."""
        mock_adapter = MagicMock()
        mock_build.return_value = mock_adapter

        result = build_provider_router({
            "provider_router": {
                "enabled": True,
                "strategy": "latency",
                "preferred": "minimax",
                "probe_interval": 30,
                "providers": {
                    "minimax": {
                        "models": ["MiniMax-M2.5"],
                        "priority": 1,
                        "cost_per_1k_tokens": 0.001,
                    },
                    "openai": {
                        "models": ["gpt-4o"],
                        "priority": 2,
                        "cost_per_1k_tokens": 0.01,
                    },
                },
            }
        })

        assert result is not None
        assert "minimax" in result.provider_names
        assert "openai" in result.provider_names
        assert result.strategy == "latency"

    @patch("core.provider_router._build_adapter")
    def test_skips_failed_adapters(self, mock_build):
        """If an adapter fails to build, skip it but continue."""
        def selective_build(provider, api_key, base_url):
            if provider == "minimax":
                return MagicMock()
            return None  # openai fails

        mock_build.side_effect = selective_build

        result = build_provider_router({
            "provider_router": {
                "enabled": True,
                "providers": {
                    "minimax": {"models": ["m1"], "priority": 1},
                    "openai": {"models": ["m2"], "priority": 2},
                },
            }
        })

        assert result is not None
        assert "minimax" in result.provider_names
        assert "openai" not in result.provider_names

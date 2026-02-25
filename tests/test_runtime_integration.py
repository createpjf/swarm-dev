"""
tests/test_runtime_integration.py — Phase 6: Runtime abstraction integration tests.

Tests:
  - Runtime factory (create_runtime) for all 3 modes
  - ProcessRuntime basic interface
  - InProcessRuntime basic interface
  - LazyRuntime basic interface + on-demand start
  - AgentRuntime ABC contract
  - Runtime mode configuration
"""

import pytest


# ══════════════════════════════════════════════════════════════════════════════
#  Runtime Factory Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRuntimeFactory:
    """Test create_runtime() produces the correct runtime type."""

    def test_default_process(self):
        from core.runtime import create_runtime
        from core.runtime.process import ProcessRuntime
        runtime = create_runtime({})
        assert isinstance(runtime, ProcessRuntime)

    def test_explicit_process(self):
        from core.runtime import create_runtime
        from core.runtime.process import ProcessRuntime
        runtime = create_runtime({"runtime": {"mode": "process"}})
        assert isinstance(runtime, ProcessRuntime)

    def test_in_process(self):
        from core.runtime import create_runtime
        from core.runtime.in_process import InProcessRuntime
        runtime = create_runtime({"runtime": {"mode": "in_process"}})
        assert isinstance(runtime, InProcessRuntime)

    def test_lazy(self):
        from core.runtime import create_runtime
        from core.runtime.lazy import LazyRuntime
        config = {
            "runtime": {
                "mode": "lazy",
                "always_on": ["leo"],
                "idle_shutdown": 300,
            },
            "agents": [
                {"id": "leo", "role": "planner", "model": "mock"},
                {"id": "jerry", "role": "executor", "model": "mock"},
            ],
        }
        runtime = create_runtime(config)
        assert isinstance(runtime, LazyRuntime)

    def test_invalid_mode_raises(self):
        from core.runtime import create_runtime
        with pytest.raises(ValueError, match="Unknown runtime mode"):
            create_runtime({"runtime": {"mode": "nonexistent"}})


# ══════════════════════════════════════════════════════════════════════════════
#  ABC Contract Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAgentRuntimeABC:
    """Test that the ABC defines the correct interface."""

    def test_abc_cannot_instantiate(self):
        from core.runtime.base import AgentRuntime
        with pytest.raises(TypeError):
            AgentRuntime()

    def test_abc_has_required_methods(self):
        from core.runtime.base import AgentRuntime
        required = ["start", "is_alive", "agent_ids", "stop", "stop_all"]
        for method in required:
            assert hasattr(AgentRuntime, method)

    def test_abc_has_helper_methods(self):
        from core.runtime.base import AgentRuntime
        helpers = ["start_all", "all_alive", "ensure_running", "procs"]
        for method in helpers:
            assert hasattr(AgentRuntime, method)


# ══════════════════════════════════════════════════════════════════════════════
#  ProcessRuntime Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessRuntime:
    """Test ProcessRuntime (current default)."""

    def test_initial_state(self):
        from core.runtime.process import ProcessRuntime
        runtime = ProcessRuntime()
        assert runtime.agent_ids() == []
        assert runtime.all_alive() == {}

    def test_procs_empty(self):
        from core.runtime.process import ProcessRuntime
        runtime = ProcessRuntime()
        assert isinstance(runtime.procs, list)

    def test_is_alive_unknown_agent(self):
        from core.runtime.process import ProcessRuntime
        runtime = ProcessRuntime()
        assert runtime.is_alive("nonexistent") is False

    def test_stop_all_empty(self):
        from core.runtime.process import ProcessRuntime
        runtime = ProcessRuntime()
        # Should not raise when no agents
        runtime.stop_all()


# ══════════════════════════════════════════════════════════════════════════════
#  InProcessRuntime Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestInProcessRuntime:
    """Test InProcessRuntime basic interface."""

    def test_initial_state(self):
        from core.runtime.in_process import InProcessRuntime
        runtime = InProcessRuntime()
        assert runtime.agent_ids() == []
        assert runtime.all_alive() == {}

    def test_is_alive_unknown(self):
        from core.runtime.in_process import InProcessRuntime
        runtime = InProcessRuntime()
        assert runtime.is_alive("nonexistent") is False

    def test_stop_all_empty(self):
        from core.runtime.in_process import InProcessRuntime
        runtime = InProcessRuntime()
        runtime.stop_all()  # should not raise


# ══════════════════════════════════════════════════════════════════════════════
#  LazyRuntime Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestLazyRuntime:
    """Test LazyRuntime interface and configuration."""

    def _make_config(self, always_on=None, idle_shutdown=300):
        return {
            "runtime": {
                "mode": "lazy",
                "always_on": always_on or ["leo"],
                "idle_shutdown": idle_shutdown,
            },
            "agents": [
                {"id": "leo", "role": "planner", "model": "mock"},
                {"id": "jerry", "role": "executor", "model": "mock"},
                {"id": "alic", "role": "reviewer", "model": "mock"},
            ],
        }

    def test_initial_state(self):
        from core.runtime.lazy import LazyRuntime
        config = self._make_config()
        runtime = LazyRuntime(config)
        assert len(runtime.agent_ids()) == 0  # not started yet

    def test_always_on_config(self):
        from core.runtime.lazy import LazyRuntime
        config = self._make_config(always_on=["leo", "jerry"])
        runtime = LazyRuntime(config)
        assert runtime._always_on == {"leo", "jerry"}

    def test_idle_shutdown_config(self):
        from core.runtime.lazy import LazyRuntime
        config = self._make_config(idle_shutdown=600)
        runtime = LazyRuntime(config)
        assert runtime._idle_shutdown == 600

    def test_stop_all_empty(self):
        from core.runtime.lazy import LazyRuntime
        config = self._make_config()
        runtime = LazyRuntime(config)
        runtime.stop_all()


# ══════════════════════════════════════════════════════════════════════════════
#  Config Integration Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRuntimeConfig:
    """Test runtime configuration in agents.yaml format."""

    def test_config_default_mode(self):
        """Default config (no runtime key) should use process mode."""
        from core.runtime import create_runtime
        from core.runtime.process import ProcessRuntime
        runtime = create_runtime({"agents": []})
        assert isinstance(runtime, ProcessRuntime)

    def test_config_yaml_structure(self):
        """Verify expected config structure matches agents.yaml."""
        import yaml
        config_str = """
runtime:
  mode: lazy
  always_on: [leo]
  idle_shutdown: 300
agents:
  - id: leo
    role: planner
    model: mock
"""
        config = yaml.safe_load(config_str)
        assert config["runtime"]["mode"] == "lazy"
        assert config["runtime"]["always_on"] == ["leo"]
        assert config["runtime"]["idle_shutdown"] == 300

    def test_all_modes_instantiate(self):
        """All 3 runtime modes can be instantiated from config."""
        from core.runtime import create_runtime

        modes = ["process", "in_process", "lazy"]
        for mode in modes:
            config = {
                "runtime": {"mode": mode, "always_on": ["leo"], "idle_shutdown": 300},
                "agents": [{"id": "leo", "role": "planner", "model": "mock"}],
            }
            runtime = create_runtime(config)
            assert runtime is not None, f"Failed to create runtime mode={mode}"

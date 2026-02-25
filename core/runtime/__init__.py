"""
core/runtime/ — Pluggable AgentRuntime abstraction.

Decouples agent lifecycle from Orchestrator, enabling:
  - ProcessRuntime   : current behavior (mp.Process per agent)
  - InProcessRuntime : asyncio.Task per agent (Phase 2)
  - LazyRuntime      : on-demand agent startup (Phase 3)

Usage::

    from core.runtime import create_runtime

    runtime = create_runtime(config)       # reads config["runtime"]["mode"]
    runtime.start(agent_def, config, wakeup)
    ...
    runtime.shutdown()
"""

from core.runtime.base import AgentRuntime
from core.runtime.process import ProcessRuntime

__all__ = ["AgentRuntime", "ProcessRuntime", "create_runtime"]


def create_runtime(config: dict) -> AgentRuntime:
    """Factory: build the right runtime from config["runtime"]["mode"].

    Defaults to ``process`` (zero behaviour change from pre-Runtime code).
    """
    runtime_cfg = config.get("runtime", {})
    mode = runtime_cfg.get("mode", "process")

    if mode == "process":
        return ProcessRuntime()
    elif mode == "in_process":
        from core.runtime.in_process import InProcessRuntime
        return InProcessRuntime()
    elif mode == "lazy":
        from core.runtime.lazy import LazyRuntime
        return LazyRuntime(config)
    else:
        raise ValueError(
            f"Unknown runtime mode '{mode}'. "
            f"Valid: process, in_process, lazy"
        )

"""
core/context_bus.py
Shared file-backed KV store.
Every agent reads it at the start of each task.
The snapshot is injected into the agent's system prompt.
Key format: "{agent_id}:{key}"
"""

from __future__ import annotations
import json
import logging
import os

try:
    from filelock import FileLock
except ImportError:
    import warnings
    warnings.warn(
        "filelock package not installed. ContextBus is NOT process-safe. "
        "Install with: pip install filelock",
        RuntimeWarning, stacklevel=2,
    )

    class FileLock:  # type: ignore
        def __init__(self, path):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

logger = logging.getLogger(__name__)

BUS_FILE = ".context_bus.json"
BUS_LOCK = ".context_bus.lock"


class ContextBus:
    """
    File-locked KV store shared by all agent processes.
    Keys are namespaced: "{agent_id}:{key}".
    """

    def __init__(self, path: str = BUS_FILE):
        self.path = path
        self.lock = FileLock(BUS_LOCK)
        if not os.path.exists(self.path):
            self._write({})

    def publish(self, agent_id: str, key: str, value: str):
        """Write a value under '{agent_id}:{key}'."""
        ns_key = f"{agent_id}:{key}"
        with self.lock:
            data = self._read()
            data[ns_key] = value
            self._write(data)

    def get(self, agent_id: str, key: str) -> str:
        """Read a value by agent_id and key. Returns '' if not found."""
        return self._read().get(f"{agent_id}:{key}", "")

    def snapshot(self) -> dict:
        """Return the full KV store as a dict (read-only copy)."""
        return self._read()

    def _read(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

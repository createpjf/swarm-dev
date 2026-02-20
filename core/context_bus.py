"""
core/context_bus.py
Shared file-backed KV store with layered context and TTL support.
Every agent reads it at the start of each task.
The snapshot is injected into the agent's system prompt.
Key format: "{agent_id}:{key}"

Context Layers:
  L0 TASK    — cleared when the current task completes
  L1 SESSION — TTL 3600s (1 hour)
  L2 SHORT   — TTL 86400s (1 day), default
  L3 LONG    — permanent, no TTL
"""

from __future__ import annotations
import json
import logging
import os
import time

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

# ── Context Layers ────────────────────────────────────────────────────────────
LAYER_TASK    = 0   # cleared when task completes
LAYER_SESSION = 1   # TTL = 3600s
LAYER_SHORT   = 2   # TTL = 86400s (default)
LAYER_LONG    = 3   # permanent

_DEFAULT_TTL = {
    LAYER_TASK:    None,    # no auto-expiry, cleared explicitly
    LAYER_SESSION: 3600,    # 1 hour
    LAYER_SHORT:   86400,   # 1 day
    LAYER_LONG:    None,    # permanent
}


class ContextBus:
    """
    File-locked KV store shared by all agent processes.
    Keys are namespaced: "{agent_id}:{key}".

    Each entry can have a layer (0-3) and optional TTL.
    Backward compatible: plain string values are treated as LAYER_SHORT.
    """

    def __init__(self, path: str = BUS_FILE):
        self.path = path
        self.lock = FileLock(BUS_LOCK)
        if not os.path.exists(self.path):
            self._write({})

    def publish(self, agent_id: str, key: str, value: str,
                layer: int = LAYER_SHORT, ttl: int | None = None):
        """Write a value under '{agent_id}:{key}' with layer and TTL.

        Args:
            agent_id: Publishing agent's ID.
            key: Context key name.
            value: Context value (string).
            layer: Context layer (LAYER_TASK..LAYER_LONG). Default: LAYER_SHORT.
            ttl: Optional TTL override in seconds. If None, uses layer default.
        """
        ns_key = f"{agent_id}:{key}"
        if ttl is None:
            ttl = _DEFAULT_TTL.get(layer)
        entry = {
            "v": value,
            "layer": layer,
            "ttl": ttl,
            "ts": time.time(),
        }
        with self.lock:
            data = self._read()
            data[ns_key] = entry
            self._write(data)

    def get(self, agent_id: str, key: str) -> str:
        """Read a value by agent_id and key. Returns '' if not found or expired."""
        raw = self._read().get(f"{agent_id}:{key}", "")
        if isinstance(raw, dict):
            if self._is_expired(raw):
                return ""
            return raw.get("v", "")
        return raw  # backward compat: plain string

    def snapshot(self) -> dict:
        """Return the full KV store as a dict (backward compatible).
        Unwraps layered entries to plain values. Filters expired entries.
        """
        raw = self._read()
        result = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                if self._is_expired(v):
                    continue
                result[k] = v.get("v", "")
            else:
                result[k] = v  # backward compat
        return result

    def snapshot_for_agent(self, agent_id: str,
                           max_layer: int = LAYER_SHORT) -> dict:
        """Return context visible to a specific agent, filtered by layer and TTL.

        Includes:
          - Entries published by this agent (any layer up to max_layer)
          - Entries published by other agents (any layer up to max_layer)
        Excludes:
          - Expired entries
          - Entries above max_layer

        Returns:
            {key: value} dict with unwrapped values.
        """
        raw = self._read()
        result = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                if self._is_expired(v):
                    continue
                entry_layer = v.get("layer", LAYER_SHORT)
                if entry_layer > max_layer:
                    continue
                result[k] = v.get("v", "")
            else:
                result[k] = v  # backward compat: plain strings always included
        return result

    def clear_task_layer(self):
        """Clear all LAYER_TASK (L0) entries. Called when a task completes."""
        with self.lock:
            data = self._read()
            keys_to_remove = []
            for k, v in data.items():
                if isinstance(v, dict) and v.get("layer", LAYER_SHORT) == LAYER_TASK:
                    keys_to_remove.append(k)
            if keys_to_remove:
                for k in keys_to_remove:
                    del data[k]
                self._write(data)
                logger.debug("Cleared %d task-layer entries", len(keys_to_remove))

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count of removed entries."""
        removed = 0
        with self.lock:
            data = self._read()
            keys_to_remove = []
            for k, v in data.items():
                if isinstance(v, dict) and self._is_expired(v):
                    keys_to_remove.append(k)
            if keys_to_remove:
                for k in keys_to_remove:
                    del data[k]
                self._write(data)
                removed = len(keys_to_remove)
        return removed

    @staticmethod
    def _is_expired(entry: dict) -> bool:
        """Check if a layered entry has expired based on its TTL."""
        ttl = entry.get("ttl")
        if ttl is None:
            return False
        ts = entry.get("ts", 0)
        return (time.time() - ts) > ttl

    def _read(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

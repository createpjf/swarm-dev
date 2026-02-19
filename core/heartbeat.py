"""
core/heartbeat.py
Lightweight file-based heartbeat for agent processes.

Each agent writes a JSON heartbeat file every N seconds:
  .heartbeats/{agent_id}.json = {
    "agent_id": "planner",
    "pid": 12345,
    "status": "idle" | "working" | "review",
    "task_id": "abc123" | null,
    "last_beat": 1718000000.0,
    "started_at": 1718000000.0,
    "beats": 42
  }

The gateway reads all heartbeat files to determine agent online/offline status.
An agent is "online" if last_beat < threshold seconds ago.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

HEARTBEAT_DIR = ".heartbeats"
BEAT_INTERVAL = 2.0      # seconds between heartbeats
OFFLINE_THRESHOLD = 8.0   # consider offline if no beat for this long


class Heartbeat:
    """Per-agent heartbeat writer. Call beat() periodically."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.started_at = time.time()
        self.beats = 0
        self.status = "idle"
        self.task_id: str | None = None
        self._path = os.path.join(HEARTBEAT_DIR, f"{agent_id}.json")
        os.makedirs(HEARTBEAT_DIR, exist_ok=True)

    def beat(self, status: str = "idle", task_id: str | None = None,
             progress: str | None = None):
        """Write heartbeat file. Called from agent loop.

        Args:
            status: idle/working/review
            task_id: current task being worked on
            progress: human-readable progress message (e.g. "building prompt...")
        """
        self.beats += 1
        self.status = status
        self.task_id = task_id
        data = {
            "agent_id": self.agent_id,
            "pid": os.getpid(),
            "status": status,
            "task_id": task_id,
            "progress": progress,  # visible on dashboard
            "last_beat": time.time(),
            "started_at": self.started_at,
            "beats": self.beats,
        }
        try:
            # Atomic write: write to temp then rename
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.debug("[%s] heartbeat write failed: %s", self.agent_id, e)

    def stop(self):
        """Remove heartbeat file on clean shutdown."""
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
        except OSError:
            pass


def read_all_heartbeats(threshold: float = OFFLINE_THRESHOLD) -> list[dict]:
    """
    Read all heartbeat files and return agent statuses.
    Returns list of dicts with added 'online' and 'age' fields.
    Always includes all agents from config (even if no heartbeat file exists).
    """
    # Collect heartbeat file data
    hb_data: dict[str, dict] = {}
    if os.path.isdir(HEARTBEAT_DIR):
        now = time.time()
        for fname in os.listdir(HEARTBEAT_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(HEARTBEAT_DIR, fname)
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                age = now - data.get("last_beat", 0)
                data["online"] = age < threshold
                data["age"] = round(age, 1)
                data["uptime"] = round(now - data.get("started_at", now), 1)
                aid = data.get("agent_id", fname.replace(".json", ""))
                hb_data[aid] = data
            except (json.JSONDecodeError, OSError, KeyError):
                continue

    # Ensure all agents from config are represented (even if offline)
    try:
        import yaml
        if os.path.exists("config/agents.yaml"):
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            for a in cfg.get("agents", []):
                aid = a["id"]
                if aid not in hb_data:
                    hb_data[aid] = {
                        "agent_id": aid,
                        "pid": None,
                        "status": "offline",
                        "task_id": None,
                        "last_beat": 0,
                        "online": False,
                        "age": -1,
                        "uptime": 0,
                    }
    except Exception:
        pass

    results = list(hb_data.values())
    results.sort(key=lambda d: d.get("agent_id", ""))
    return results

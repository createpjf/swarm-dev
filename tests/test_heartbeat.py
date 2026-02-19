"""
tests/test_heartbeat.py
Heartbeat and agent status tests.
"""

import json
import os
import pytest
from core.heartbeat import Heartbeat, read_all_heartbeats


class TestHeartbeat:
    def test_beat_creates_file(self, tmp_workdir):
        hb = Heartbeat("test-agent")
        hb.beat("working", "task-123", progress="loading context...")

        path = os.path.join(".heartbeats", "test-agent.json")
        assert os.path.exists(path)

        with open(path) as f:
            data = json.load(f)
        assert data["agent_id"] == "test-agent"
        assert data["status"] == "working"
        assert data["task_id"] == "task-123"
        assert data["progress"] == "loading context..."
        assert data["beats"] == 1

    def test_stop_removes_file(self, tmp_workdir):
        hb = Heartbeat("test-agent")
        hb.beat("idle")
        hb.stop()
        assert not os.path.exists(os.path.join(".heartbeats", "test-agent.json"))

    def test_read_all_includes_config_agents(self, tmp_workdir):
        agents = read_all_heartbeats()
        # From conftest agents.yaml: planner, executor, reviewer
        agent_ids = {a["agent_id"] for a in agents}
        assert "planner" in agent_ids
        assert "executor" in agent_ids
        assert "reviewer" in agent_ids
        # All should be offline (no heartbeat files)
        assert all(not a["online"] for a in agents)

    def test_online_detection(self, tmp_workdir):
        hb = Heartbeat("executor")
        hb.beat("working")
        agents = read_all_heartbeats()
        executor = next(a for a in agents if a["agent_id"] == "executor")
        assert executor["online"] is True
        hb.stop()

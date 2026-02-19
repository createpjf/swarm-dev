"""
tests/conftest.py
Shared fixtures for swarm-dev tests.
Provides isolated temporary directories and mock adapters.
"""

import json
import os
import tempfile
import pytest


@pytest.fixture
def tmp_workdir(tmp_path, monkeypatch):
    """Provide an isolated working directory for file-backed stores."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("config", exist_ok=True)
    os.makedirs("memory", exist_ok=True)
    os.makedirs("skills", exist_ok=True)
    os.makedirs(".mailboxes", exist_ok=True)
    os.makedirs(".heartbeats", exist_ok=True)
    os.makedirs(".logs", exist_ok=True)

    # Minimal agents.yaml
    config = {
        "llm": {"provider": "mock"},
        "memory": {"backend": "mock"},
        "chain": {"enabled": False},
        "max_idle_cycles": 5,
        "resilience": {
            "max_retries": 1,
            "base_delay": 0.01,
            "max_delay": 0.1,
        },
        "reputation": {"peer_review_agents": ["reviewer"]},
        "agents": [
            {
                "id": "planner",
                "role": "Strategic planner.",
                "model": "mock-model",
                "skills": ["_base"],
            },
            {
                "id": "executor",
                "role": "Implementation agent.",
                "model": "mock-model",
                "skills": ["_base"],
            },
            {
                "id": "reviewer",
                "role": "Peer reviewer.",
                "model": "mock-model",
                "skills": ["_base"],
            },
        ],
    }
    with open("config/agents.yaml", "w") as f:
        import yaml
        yaml.dump(config, f)

    # Minimal skill
    with open("skills/_base.md", "w") as f:
        f.write("# Base Skill\nYou are a helpful agent.\n")

    return tmp_path


class MockLLM:
    """Mock LLM adapter for testing without API calls."""

    def __init__(self, responses=None):
        self.responses = responses or ["Mock LLM response"]
        self._call_count = 0
        self.usage_log = []

    async def chat(self, messages, model=None):
        idx = min(self._call_count, len(self.responses) - 1)
        self._call_count += 1
        return self.responses[idx]

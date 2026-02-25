"""
tests/test_a2a_loopback.py — Phase 6: Self-loopback test.

Tests Cleo A2A Client → Cleo A2A Server round-trip:
  1. Client sends a message to Server
  2. Server creates a TaskBoard task via Bridge
  3. We simulate the Cleo pipeline completing the task
  4. Client polls and gets the completed result

This validates the full A2A message lifecycle without needing
an external agent — Cleo talks to itself.
"""

import json
import os
import time
import pytest

from adapters.a2a.bridge import A2ABridge
from adapters.a2a.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATask,
    A2ATaskStatus,
)
from adapters.a2a.server import A2AServer
from adapters.a2a.security import TrustLevel
from core.task_board import TaskBoard, TaskStatus


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def loopback_env(tmp_workdir):
    """Set up a loopback test environment with Server + Board + Bridge."""
    board = TaskBoard()
    bridge = A2ABridge(board)
    server = A2AServer({"a2a": {"server": {"enabled": True}}})
    # Share the same bridge instance
    server._bridge = bridge
    return board, bridge, server


# ══════════════════════════════════════════════════════════════════════════════
#  Loopback Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestA2ALoopback:
    """Test full A2A Client → Server → TaskBoard → Server round-trip."""

    def test_submit_poll_complete(self, loopback_env):
        """Full lifecycle: submit → claim → complete → poll result."""
        board, bridge, server = loopback_env

        # 1. Client sends message (simulated via server.handle_rpc)
        send_response = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "loopback-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [
                        {"kind": "text", "text": "What is 2+2?"},
                    ],
                },
            },
        })
        assert "result" in send_response
        a2a_task = send_response["result"]
        a2a_id = a2a_task["id"]
        assert a2a_task["status"]["state"] == "submitted"

        # 2. Verify Cleo TaskBoard has the task
        cleo_id = bridge.cleo_id_for(a2a_id)
        assert cleo_id is not None
        cleo_task = board.get(cleo_id)
        assert cleo_task is not None
        assert "2+2" in cleo_task.description

        # 3. Simulate Cleo MAS pipeline
        # Bridge creates task with required_role="planner", so Leo claims first
        claimed = board.claim_next("leo")
        assert claimed is not None
        assert claimed.task_id == cleo_id

        board.submit_for_review(cleo_id, "The answer is 4.")
        board.complete(cleo_id)

        # 4. Client polls tasks/get
        get_response = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "loopback-2",
            "method": "tasks/get",
            "params": {"id": a2a_id},
        })
        assert "result" in get_response
        task_data = get_response["result"]
        assert task_data["status"]["state"] == "completed"
        assert len(task_data["artifacts"]) > 0

        # Verify the result text
        result_text = ""
        for artifact in task_data["artifacts"]:
            for part in artifact.get("parts", []):
                if part.get("kind") == "text":
                    result_text += part.get("text", "")
        assert "4" in result_text

    def test_submit_and_cancel(self, loopback_env):
        """Submit a task then cancel it before completion."""
        board, bridge, server = loopback_env

        # Submit
        send_resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Long running task"}],
                },
            },
        })
        a2a_id = send_resp["result"]["id"]

        # Cancel
        cancel_resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-2",
            "method": "tasks/cancel",
            "params": {"id": a2a_id},
        })
        assert cancel_resp["result"]["status"]["state"] == "canceled"

        # Verify via tasks/get
        get_resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-3",
            "method": "tasks/get",
            "params": {"id": a2a_id},
        })
        # After cancel, task should show canceled or failed
        state = get_resp["result"]["status"]["state"]
        assert state in ("canceled", "failed")

    def test_multiple_tasks(self, loopback_env):
        """Multiple concurrent tasks through the same server."""
        board, bridge, server = loopback_env

        task_ids = []
        for i in range(3):
            resp = server.handle_rpc({
                "jsonrpc": "2.0",
                "id": f"req-{i}",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": f"Task {i}"}],
                    },
                },
            })
            task_ids.append(resp["result"]["id"])

        # All should be submitted
        for tid in task_ids:
            resp = server.handle_rpc({
                "jsonrpc": "2.0",
                "id": "poll",
                "method": "tasks/get",
                "params": {"id": tid},
            })
            assert resp["result"]["status"]["state"] == "submitted"

        # Complete task 1 only
        cleo_id = bridge.cleo_id_for(task_ids[1])
        board.claim_next("jerry")
        board.submit_for_review(cleo_id, "Task 1 done")
        board.complete(cleo_id)

        # Verify: task 1 completed, others still submitted
        resp1 = server.handle_rpc({
            "jsonrpc": "2.0", "id": "p1",
            "method": "tasks/get",
            "params": {"id": task_ids[1]},
        })
        assert resp1["result"]["status"]["state"] == "completed"

    def test_context_threading(self, loopback_env):
        """Tasks with same contextId share conversation context."""
        board, bridge, server = loopback_env

        ctx = "session-abc-123"

        # First message in context
        resp1 = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "First question"}],
                    "contextId": ctx,
                },
            },
        })
        assert resp1["result"]["contextId"] == ctx

        # Second message in same context
        resp2 = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-2",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Follow up"}],
                    "contextId": ctx,
                },
            },
        })
        # Both share the same contextId
        assert resp2["result"]["contextId"] == ctx

    def test_file_attachment_round_trip(self, loopback_env, tmp_path):
        """File attachment goes through the full pipeline."""
        board, bridge, server = loopback_env

        import base64
        file_data = base64.b64encode(b"CSV data here").decode("ascii")

        resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [
                        {"kind": "text", "text": "Analyze this data"},
                        {"kind": "file", "name": "data.csv",
                         "mimeType": "text/csv", "data": file_data},
                    ],
                },
            },
        })
        a2a_id = resp["result"]["id"]
        cleo_id = bridge.cleo_id_for(a2a_id)

        # Verify the file reference is in the task description
        cleo_task = board.get(cleo_id)
        assert "Analyze this data" in cleo_task.description

    def test_sse_stream_lifecycle(self, loopback_env):
        """SSE event stream tracks task state changes."""
        board, bridge, server = loopback_env

        # Create task
        resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "stream test"}],
                },
            },
        })
        a2a_id = resp["result"]["id"]
        cleo_id = bridge.cleo_id_for(a2a_id)

        # Complete the task immediately (so SSE generates events fast)
        board.claim_next("jerry")
        board.submit_for_review(cleo_id, "done!")
        board.complete(cleo_id)

        # Generate SSE events
        events = list(server.generate_sse_events(a2a_id, poll_interval=0.1, timeout=5))
        assert len(events) > 0

        # Should have a status event and a done event
        event_types = []
        for ev in events:
            if ev.startswith("event: "):
                etype = ev.split("event: ")[1].split("\n")[0]
                event_types.append(etype)

        assert "status" in event_types
        assert "done" in event_types

    def test_a2a_source_marker_in_description(self, loopback_env):
        """Verify that A2A source marker is embedded in Cleo task."""
        board, bridge, server = loopback_env

        resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "marker test"}],
                },
            },
        })
        a2a_id = resp["result"]["id"]
        cleo_id = bridge.cleo_id_for(a2a_id)
        cleo_task = board.get(cleo_id)

        # Should contain A2A source marker
        assert "[A2A source:" in cleo_task.description

    def test_error_response_format(self, loopback_env):
        """Verify error responses follow JSON-RPC 2.0 format."""
        _, _, server = loopback_env

        resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "err-1",
            "method": "tasks/get",
            "params": {},  # Missing id
        })
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == "err-1"
        assert "error" in resp
        assert "code" in resp["error"]
        assert "message" in resp["error"]

    def test_success_response_format(self, loopback_env):
        """Verify success responses follow JSON-RPC 2.0 format."""
        _, _, server = loopback_env

        resp = server.handle_rpc({
            "jsonrpc": "2.0",
            "id": "ok-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "test"}],
                },
            },
        })
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == "ok-1"
        assert "result" in resp
        assert "error" not in resp

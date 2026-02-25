"""
adapters/a2a/server.py — A2A Server: Cleo as an A2A-compliant agent.

Handles inbound A2A JSON-RPC 2.0 requests from external agents:
  - Agent Card serving (/.well-known/agent.json)
  - message/send — synchronous task submission + wait for result
  - tasks/get — query task status
  - tasks/cancel — cancel a running task
  - message/stream — SSE streaming (poll-based heartbeat relay)

All methods go through the A2ABridge which translates between A2A
and Cleo's internal TaskBoard. From the agents' perspective (Leo/Jerry/Alic),
A2A tasks look like any other channel source.

Usage::

    server = A2AServer(config)
    # In gateway.py:
    # GET  /.well-known/agent.json → server.handle_agent_card(handler)
    # POST /a2a                    → server.handle_rpc(handler)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from adapters.a2a.bridge import A2ABridge
from adapters.a2a.models import (
    AgentCard,
    A2AMessage,
    A2APart,
    A2ATask,
    A2ATaskStatus,
)

logger = logging.getLogger(__name__)


class A2AServer:
    """A2A protocol server handler.

    Plugs into Cleo's Gateway (BaseHTTPRequestHandler) and handles
    all A2A JSON-RPC 2.0 methods.
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: Full Cleo config dict (reads config["a2a"]["server"])
        """
        config = config or {}
        a2a_cfg = config.get("a2a", {}).get("server", {})

        self.enabled = a2a_cfg.get("enabled", False)
        self._bridge = A2ABridge()
        self._agent_card = self._build_agent_card(config)

        logger.info("[a2a:server] initialized (enabled=%s)", self.enabled)

    # ── Agent Card ─────────────────────────────────────────────────────────

    def _build_agent_card(self, config: dict) -> AgentCard:
        """Build the Agent Card from config."""
        a2a_cfg = config.get("a2a", {}).get("server", {})

        # Determine server URL
        port = int(os.environ.get("CLEO_GATEWAY_PORT",
                    os.environ.get("SWARM_GATEWAY_PORT", "19789")))
        hostname = os.environ.get("CLEO_HOSTNAME", f"localhost:{port}")
        scheme = "https" if "localhost" not in hostname else "http"
        base_url = f"{scheme}://{hostname}"

        card = AgentCard(
            url=f"{base_url}/a2a",
        )
        return card

    def get_agent_card_dict(self) -> dict:
        """Return Agent Card as a dict (for JSON response)."""
        return self._agent_card.to_dict()

    # ── JSON-RPC 2.0 dispatcher ───────────────────────────────────────────

    def handle_rpc(self, body: dict) -> dict:
        """Dispatch a JSON-RPC 2.0 request to the appropriate handler.

        Args:
            body: Parsed JSON-RPC request body

        Returns:
            JSON-RPC response dict
        """
        if not self.enabled:
            return self._error_response(
                body.get("id"), -32000,
                "A2A Server is disabled")

        method = body.get("method", "")
        params = body.get("params", {})
        rpc_id = body.get("id")

        # Validate JSON-RPC 2.0 format
        if body.get("jsonrpc") != "2.0":
            return self._error_response(
                rpc_id, -32600,
                "Invalid Request: jsonrpc must be '2.0'")

        if not method:
            return self._error_response(
                rpc_id, -32600,
                "Invalid Request: method is required")

        logger.info("[a2a:server] RPC method=%s, id=%s", method, rpc_id)

        # Route to handler
        if method == "message/send":
            return self._handle_message_send(rpc_id, params)
        elif method == "tasks/get":
            return self._handle_tasks_get(rpc_id, params)
        elif method == "tasks/cancel":
            return self._handle_tasks_cancel(rpc_id, params)
        else:
            return self._error_response(
                rpc_id, -32601,
                f"Method not found: {method}")

    # ── message/send ───────────────────────────────────────────────────────

    def _handle_message_send(self, rpc_id: Any, params: dict) -> dict:
        """Handle message/send — submit a task and return immediately.

        For true sync behavior (wait for completion), external clients
        should use message/stream or poll via tasks/get.

        In the MVP, message/send creates the task and returns submitted.
        The client can poll tasks/get for completion.
        """
        try:
            # Extract optional contextId for conversation threading
            context_id = params.get("message", {}).get("contextId", "")

            # Bridge: A2A message → Cleo TaskBoard
            a2a_task = self._bridge.inbound_message(
                params, context_id=context_id)

            logger.info("[a2a:server] message/send: created task %s",
                         a2a_task.id)

            return self._success_response(rpc_id, a2a_task.to_dict())

        except Exception as e:
            logger.error("[a2a:server] message/send failed: %s", e,
                          exc_info=True)
            return self._error_response(
                rpc_id, -32000,
                f"Internal error: {e}")

    # ── message/send with wait (async variant) ─────────────────────────────

    async def handle_message_send_sync(self, rpc_id: Any,
                                        params: dict,
                                        timeout: float = 300) -> dict:
        """Handle message/send with synchronous wait for completion.

        This is the full A2A message/send semantics — blocks until the
        task reaches a terminal state or times out.

        Used when the gateway runs the request in an async context.
        """
        try:
            context_id = params.get("message", {}).get("contextId", "")
            a2a_task = self._bridge.inbound_message(
                params, context_id=context_id)

            logger.info("[a2a:server] message/send (sync): waiting for %s",
                         a2a_task.id)

            # Wait for Cleo pipeline to complete
            result = await self._bridge.wait_for_completion(
                a2a_task.id, timeout=timeout)

            return self._success_response(rpc_id, result.to_dict())

        except Exception as e:
            logger.error("[a2a:server] message/send (sync) failed: %s", e,
                          exc_info=True)
            return self._error_response(
                rpc_id, -32000,
                f"Internal error: {e}")

    # ── tasks/get ──────────────────────────────────────────────────────────

    def _handle_tasks_get(self, rpc_id: Any, params: dict) -> dict:
        """Handle tasks/get — query current task status."""
        a2a_id = params.get("id", "")
        if not a2a_id:
            return self._error_response(
                rpc_id, -32602,
                "Missing required param: id")

        task = self._bridge.get_task_status(a2a_id)
        return self._success_response(rpc_id, task.to_dict())

    # ── tasks/cancel ───────────────────────────────────────────────────────

    def _handle_tasks_cancel(self, rpc_id: Any, params: dict) -> dict:
        """Handle tasks/cancel — cancel a running task."""
        a2a_id = params.get("id", "")
        if not a2a_id:
            return self._error_response(
                rpc_id, -32602,
                "Missing required param: id")

        task = self._bridge.cancel_task(a2a_id)
        return self._success_response(rpc_id, task.to_dict())

    # ── SSE stream handler ─────────────────────────────────────────────────

    def generate_sse_events(self, a2a_id: str,
                             poll_interval: float = 1.0,
                             timeout: float = 300) -> Any:
        """Generator that yields SSE events for a task.

        Used by the gateway's stream endpoint. Yields formatted SSE strings.

        Args:
            a2a_id: A2A task ID
            poll_interval: Seconds between polls
            timeout: Max total duration

        Yields:
            SSE event strings (data: {...}\n\n)
        """
        deadline = time.time() + timeout
        last_state = ""
        terminal_states = {"completed", "failed", "canceled"}

        while time.time() < deadline:
            task = self._bridge.get_task_status(a2a_id)
            current_state = task.status.state

            # Emit event on state change or periodically
            if current_state != last_state:
                yield self._sse_event("status", task.status.to_dict())
                last_state = current_state

                # Emit artifacts on completion
                if current_state == "completed" and task.artifacts:
                    for art in task.artifacts:
                        yield self._sse_event("artifact", art.to_dict())

                # Stop on terminal state
                if current_state in terminal_states:
                    yield self._sse_event("done", {"state": current_state})
                    return

            time.sleep(poll_interval)

        # Timeout
        yield self._sse_event("error", {"message": "Stream timeout"})

    # ── JSON-RPC response helpers ──────────────────────────────────────────

    @staticmethod
    def _success_response(rpc_id: Any, result: Any) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": result,
        }

    @staticmethod
    def _error_response(rpc_id: Any, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": code,
                "message": message,
            },
        }

    @staticmethod
    def _sse_event(event_type: str, data: Any) -> str:
        """Format an SSE event string."""
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event_type}\ndata: {payload}\n\n"

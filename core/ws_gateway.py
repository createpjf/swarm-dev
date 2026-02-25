"""
core/ws_gateway.py — WebSocket gateway for real-time agent state push.

Supplements the existing SSE mechanism with a proper WebSocket server.
Benefits over SSE:
  - Bidirectional communication (client can send commands)
  - Lower overhead (no HTTP request/response per event)
  - Binary data support (screenshots, files)
  - Connection multiplexing (multiple channels per connection)

Architecture:
  - Runs alongside the HTTP gateway on a separate port (default: gateway_port + 1)
  - Broadcasts state changes to all connected clients
  - Supports client-initiated task submission via WebSocket
  - Token authentication on connect (same token as HTTP gateway)

Protocol:
  Connect: ws://localhost:{port}?token={gateway_token}
  Server → Client events: {"event": "state"|"task_update"|"alert", "data": {...}}
  Client → Server commands: {"action": "submit_task"|"ping", "data": {...}}

Dependencies:
  pip install websockets
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional, Set

logger = logging.getLogger(__name__)

try:
    import websockets
    from websockets.server import serve, WebSocketServerProtocol
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


# ── Event Types ───────────────────────────────────────────────────────────────

class WSEvent:
    """WebSocket event types."""
    STATE = "state"              # Full state snapshot
    TASK_UPDATE = "task_update"  # Single task changed
    TASK_COMPLETE = "task_complete"  # Task finished
    AGENT_STATUS = "agent_status"   # Agent status change
    ALERT = "alert"              # System alert/notification
    PONG = "pong"                # Response to ping


# ── WebSocket Server ──────────────────────────────────────────────────────────

class WebSocketGateway:
    """
    WebSocket server for real-time Cleo state broadcasting.

    Features:
      - Token authentication
      - Broadcast to all connected clients
      - Client command handling (task submission)
      - Automatic reconnection support (client-side)
      - Rate-limited state snapshots (max 2/sec)
    """

    def __init__(self, port: int = 19790, token: str = ""):
        """
        Args:
            port: WebSocket server port
            token: Authentication token (same as gateway token)
        """
        if not _HAS_WEBSOCKETS:
            raise RuntimeError(
                "websockets not installed. Install with: pip install websockets"
            )
        self.port = port
        self.token = token
        self._clients: Set[WebSocketServerProtocol] = set()
        self._server = None
        self._broadcast_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_state_hash = ""

    async def start(self):
        """Start the WebSocket server."""
        self._running = True
        self._server = await serve(
            self._handler,
            "0.0.0.0",
            self.port,
            ping_interval=30,
            ping_timeout=10,
        )
        # Start periodic state broadcast
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        logger.info("[ws] WebSocket gateway started on port %d", self.port)

    async def stop(self):
        """Stop the WebSocket server."""
        self._running = False
        if self._broadcast_task:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Close all client connections
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        logger.info("[ws] WebSocket gateway stopped")

    # ── Connection Handler ───────────────────────────────────────────────

    async def _handler(self, websocket: WebSocketServerProtocol):
        """Handle a new WebSocket connection."""
        # Authenticate
        if self.token:
            # Extract token from query params or first message
            # websockets 15.x: use websocket.path (not .request.path)
            _ws_path = getattr(websocket, 'path', '') or ''
            query_params = dict(
                p.split("=", 1) for p in (_ws_path.split("?", 1)[1]
                if "?" in _ws_path else "").split("&")
                if "=" in p
            )
            client_token = query_params.get("token", "")
            if client_token != self.token:
                await websocket.close(4001, "Invalid token")
                logger.warning("[ws] Connection rejected: invalid token")
                return

        self._clients.add(websocket)
        client_id = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        logger.info("[ws] Client connected: %s (%d total)",
                    client_id, len(self._clients))

        try:
            # Send initial state snapshot
            snapshot = self._build_snapshot()
            await websocket.send(json.dumps({
                "event": WSEvent.STATE,
                "data": snapshot,
            }, ensure_ascii=False, default=str))

            # Listen for client commands
            async for message in websocket:
                await self._handle_client_message(websocket, message)

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.warning("[ws] Client error: %s", e)
        finally:
            self._clients.discard(websocket)
            logger.info("[ws] Client disconnected: %s (%d remaining)",
                        client_id, len(self._clients))

    async def _handle_client_message(self, ws: WebSocketServerProtocol,
                                      message: str):
        """Handle a message from a connected client."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            await ws.send(json.dumps({"event": "error", "data": "Invalid JSON"}))
            return

        action = data.get("action", "")

        if action == "ping":
            await ws.send(json.dumps({
                "event": WSEvent.PONG,
                "data": {"ts": time.time()},
            }))

        elif action == "submit_task":
            # Task submission via WebSocket
            description = data.get("data", {}).get("description", "")
            if not description:
                await ws.send(json.dumps({
                    "event": "error",
                    "data": "Missing task description",
                }))
                return
            result = await self._submit_task(description)
            await ws.send(json.dumps({
                "event": "task_submitted",
                "data": result,
            }, default=str))

        elif action == "subscribe":
            # Client can subscribe to specific event types
            # (future: per-client filtering)
            await ws.send(json.dumps({
                "event": "subscribed",
                "data": {"channels": data.get("data", {}).get("channels", ["*"])},
            }))

        else:
            await ws.send(json.dumps({
                "event": "error",
                "data": f"Unknown action: {action}",
            }))

    # ── Broadcasting ─────────────────────────────────────────────────────

    async def _broadcast_loop(self):
        """Periodically broadcast state updates to all clients."""
        while self._running:
            try:
                if self._clients:
                    snapshot = self._build_snapshot()
                    state_str = json.dumps(snapshot, ensure_ascii=False, default=str)
                    state_hash = str(hash(state_str))

                    if state_hash != self._last_state_hash:
                        self._last_state_hash = state_hash
                        message = json.dumps({
                            "event": WSEvent.STATE,
                            "data": snapshot,
                        }, ensure_ascii=False, default=str)

                        # Broadcast to all connected clients
                        disconnected = set()
                        for ws in self._clients:
                            try:
                                await ws.send(message)
                            except Exception:
                                disconnected.add(ws)

                        # Clean up disconnected clients
                        self._clients -= disconnected

                await asyncio.sleep(1.0)  # 1 Hz broadcast rate

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[ws] Broadcast error: %s", e)
                await asyncio.sleep(2.0)

    async def broadcast_event(self, event: str, data: dict):
        """Send a specific event to all connected clients.

        Use this for targeted events (task completion, alerts) rather than
        waiting for the periodic broadcast cycle.
        """
        if not self._clients:
            return

        message = json.dumps({
            "event": event,
            "data": data,
        }, ensure_ascii=False, default=str)

        disconnected = set()
        for ws in self._clients:
            try:
                await ws.send(message)
            except Exception:
                disconnected.add(ws)
        self._clients -= disconnected

    # ── State Building ───────────────────────────────────────────────────

    def _build_snapshot(self) -> dict:
        """Build the current state snapshot (same as SSE snapshot)."""
        snapshot: dict = {"ts": time.time(), "clients": len(self._clients)}

        # Task board
        try:
            if os.path.exists(".task_board.json"):
                with open(".task_board.json") as f:
                    tasks = json.load(f)
                compact_tasks = {}
                for tid, t in tasks.items():
                    compact_tasks[tid] = {
                        "s": t.get("status", "?"),
                        "a": t.get("agent_id", ""),
                        "d": (t.get("description", ""))[:60],
                        "ca": t.get("claimed_at"),
                        "co": t.get("completed_at"),
                        "rc": t.get("retry_count", 0),
                    }
                    scores = t.get("review_scores", [])
                    if scores:
                        avg = sum(r["score"] for r in scores) / len(scores)
                        compact_tasks[tid]["rs"] = int(avg)
                    pr = t.get("partial_result", "")
                    if pr:
                        compact_tasks[tid]["pr"] = pr[-200:]
                    cost = t.get("cost_usd")
                    if cost is not None:
                        compact_tasks[tid]["cost"] = round(cost, 4)
                    pid = t.get("parent_id")
                    if pid:
                        compact_tasks[tid]["pid"] = pid
                    cs = t.get("critique_spec")
                    if cs:
                        compact_tasks[tid]["cs"] = cs
                snapshot["tasks"] = compact_tasks
        except Exception:
            snapshot["tasks"] = {}

        # Agent status from context bus
        try:
            if os.path.exists(".context_bus.json"):
                with open(".context_bus.json") as f:
                    ctx = json.load(f)
                agents = {}
                for aid, info in ctx.get("agents", {}).items():
                    agents[aid] = {
                        "status": info.get("status", "idle"),
                        "current_task": info.get("current_task_id", ""),
                        "last_active": info.get("last_active", 0),
                    }
                snapshot["agents"] = agents
        except Exception:
            snapshot["agents"] = {}

        return snapshot

    # ── Task Submission ──────────────────────────────────────────────────

    async def _submit_task(self, description: str) -> dict:
        """Submit a task via WebSocket (runs in executor to avoid blocking)."""
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, self._submit_task_sync, description)
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _submit_task_sync(self, description: str) -> dict:
        """Synchronous task submission."""
        try:
            from core.task_board import TaskBoard
            board = TaskBoard()
            task_id = board.create_task(description)
            return {"ok": True, "task_id": task_id}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def client_count(self) -> int:
        """Number of connected WebSocket clients."""
        return len(self._clients)

    @property
    def is_running(self) -> bool:
        return self._running


# ── Factory ───────────────────────────────────────────────────────────────────

_instance: WebSocketGateway | None = None


async def start_ws_gateway(port: int = 0, token: str = "") -> WebSocketGateway:
    """Start the WebSocket gateway (singleton).

    Args:
        port: WebSocket port (default: HTTP gateway port + 1)
        token: Auth token (default: same as CLEO_GATEWAY_TOKEN)
    """
    global _instance
    if _instance and _instance.is_running:
        return _instance

    if not port:
        # Default: HTTP gateway port + 1
        http_port = int(os.environ.get("CLEO_GATEWAY_PORT", "19789"))
        port = http_port + 1

    if not token:
        token = os.environ.get("CLEO_GATEWAY_TOKEN", "")

    if not _HAS_WEBSOCKETS:
        logger.warning(
            "[ws] websockets not installed — WebSocket gateway disabled. "
            "Install with: pip install websockets"
        )
        return None

    _instance = WebSocketGateway(port=port, token=token)
    await _instance.start()
    return _instance


async def stop_ws_gateway():
    """Stop the WebSocket gateway."""
    global _instance
    if _instance:
        await _instance.stop()
        _instance = None

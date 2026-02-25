"""
adapters/a2a/bridge.py — A2A ↔ Cleo TaskBoard bidirectional mapping.

Responsibilities:
  - Inbound:  A2A message/send → Cleo TaskBoard task
  - Outbound: Cleo task result → A2A Artifacts
  - State:    Cleo 7-state → A2A 5-state mapping
  - ID:       a2a_task_id ↔ cleo_task_id persistent mapping

The Bridge treats A2A as "just another channel" — like Telegram or Discord.
Agent internals (Leo/Jerry/Alic pipeline) are completely unaware of A2A.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from adapters.a2a.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATask,
    A2ATaskStatus,
)

logger = logging.getLogger(__name__)

# Cleo TaskBoard state → A2A Task state
STATE_MAP: dict[str, str] = {
    "pending":   "submitted",
    "claimed":   "working",
    "review":    "working",
    "paused":    "working",
    "completed": "completed",
    "failed":    "failed",
    "cancelled": "canceled",   # Cleo double-l → A2A single-l
}

# Persistent mapping file
TASK_MAP_FILE = ".a2a_task_map.json"


class A2ABridge:
    """Bidirectional mapper between A2A protocol and Cleo TaskBoard.

    Usage::

        bridge = A2ABridge(board)
        a2a_task = bridge.inbound_message(a2a_msg_dict)
        # ... Cleo pipeline runs ...
        artifacts = bridge.outbound_result(cleo_task_id)
    """

    def __init__(self, board=None):
        """
        Args:
            board: TaskBoard instance (lazy-imported if None).
        """
        self._board = board
        self._task_map: dict[str, str] = {}  # a2a_id → cleo_id
        self._reverse_map: dict[str, str] = {}  # cleo_id → a2a_id
        self._context_sessions: dict[str, str] = {}  # contextId → session_key
        self._load_map()

    @property
    def board(self):
        if self._board is None:
            from core.task_board import TaskBoard
            self._board = TaskBoard()
        return self._board

    # ── Persistent task ID mapping ─────────────────────────────────────────

    def _load_map(self):
        """Load persisted a2a_id ↔ cleo_id mapping."""
        try:
            if os.path.exists(TASK_MAP_FILE):
                with open(TASK_MAP_FILE, "r", encoding="utf-8") as f:
                    self._task_map = json.load(f)
                self._reverse_map = {v: k for k, v in self._task_map.items()}
                logger.debug("[a2a:bridge] loaded %d task mappings",
                             len(self._task_map))
        except Exception as e:
            logger.warning("[a2a:bridge] failed to load task map: %s", e)
            self._task_map = {}
            self._reverse_map = {}

    def _save_map(self):
        """Persist a2a_id ↔ cleo_id mapping to disk."""
        try:
            with open(TASK_MAP_FILE, "w", encoding="utf-8") as f:
                json.dump(self._task_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[a2a:bridge] failed to save task map: %s", e)

    def _register_mapping(self, a2a_id: str, cleo_id: str):
        """Register bidirectional ID mapping."""
        self._task_map[a2a_id] = cleo_id
        self._reverse_map[cleo_id] = a2a_id
        self._save_map()

    # ── Inbound: A2A → Cleo ────────────────────────────────────────────────

    def inbound_message(self, message_dict: dict,
                        context_id: str = "") -> A2ATask:
        """Convert an A2A message/send into a Cleo TaskBoard task.

        Args:
            message_dict: The A2A message params dict (contains "message" key)
            context_id: Optional A2A contextId for conversation continuity

        Returns:
            A2ATask with submitted status
        """
        msg = A2AMessage.from_dict(message_dict.get("message", message_dict))

        # 1. Extract text content
        text = msg.get_text()

        # 2. Handle file attachments → save to workspace
        files = msg.get_files()
        for fp in files:
            saved_path = self._save_file_part(fp)
            if saved_path:
                text += f"\n[附件: {saved_path}]"

        # 3. Context/session mapping
        ctx_id = context_id or f"ctx-{uuid.uuid4().hex[:12]}"
        session_key = f"a2a:{ctx_id}"
        self._context_sessions[ctx_id] = session_key

        # 4. Create Cleo TaskBoard task
        # Embed A2A source marker in description (TaskBoard has no metadata field)
        tagged_text = f"[A2A source: {ctx_id}] {text}"
        cleo_task = self.board.create(
            description=tagged_text,
            required_role="planner",
        )
        # TaskBoard.create() returns a Task dataclass
        cleo_id = cleo_task.task_id

        # 5. Register ID mapping
        a2a_id = f"a2a-{uuid.uuid4().hex[:12]}"
        self._register_mapping(a2a_id, cleo_id)

        logger.info("[a2a:bridge] inbound: a2a_id=%s → cleo_id=%s, text_len=%d",
                     a2a_id, cleo_id, len(text))

        return A2ATask(
            id=a2a_id,
            contextId=ctx_id,
            status=A2ATaskStatus(state="submitted"),
            history=[msg],
            metadata={"cleo_task_id": cleo_id},
        )

    def _save_file_part(self, part: A2APart) -> str:
        """Save a FilePart to workspace/a2a/ and return the path."""
        try:
            workspace = os.environ.get("CLEO_WORKSPACE", "workspace")
            a2a_dir = os.path.join(workspace, "a2a")
            os.makedirs(a2a_dir, exist_ok=True)

            filename = part.name or f"attachment_{uuid.uuid4().hex[:8]}"
            filepath = os.path.join(a2a_dir, filename)

            if part.data:
                # Base64-encoded inline data
                raw = base64.b64decode(part.data)
                with open(filepath, "wb") as f:
                    f.write(raw)
                logger.debug("[a2a:bridge] saved file: %s (%d bytes)",
                             filepath, len(raw))
                return filepath
            elif part.uri:
                # URI reference — just record the path
                return part.uri

        except Exception as e:
            logger.warning("[a2a:bridge] failed to save file part: %s", e)
        return ""

    # ── Outbound: Cleo → A2A ──────────────────────────────────────────────

    def outbound_result(self, cleo_task_id: str) -> list[A2AArtifact]:
        """Convert a Cleo task's result into A2A Artifacts.

        Args:
            cleo_task_id: The Cleo TaskBoard task ID

        Returns:
            List of A2AArtifact objects
        """
        task = self._get_cleo_task(cleo_task_id)
        if not task:
            return []

        artifacts: list[A2AArtifact] = []

        # Main text result — Task is a dataclass with .result attribute
        result_text = task.result or ""
        if result_text:
            artifacts.append(A2AArtifact(
                name="result",
                description="Task execution result",
                parts=[A2APart.text_part(result_text)],
            ))

        # Output files (extracted from result text if present)
        # TaskBoard doesn't have output_files — files are referenced in result text

        return artifacts

    # ── State query ────────────────────────────────────────────────────────

    def get_task_status(self, a2a_id: str) -> A2ATask:
        """Get current A2A task status from underlying Cleo task.

        Args:
            a2a_id: The A2A task ID

        Returns:
            A2ATask with current status and artifacts (if completed)
        """
        cleo_id = self._task_map.get(a2a_id)
        if not cleo_id:
            return A2ATask(
                id=a2a_id,
                status=A2ATaskStatus(state="failed"),
                metadata={"error": "Task not found"},
            )

        task = self._get_cleo_task(cleo_id)
        if not task:
            return A2ATask(
                id=a2a_id,
                status=A2ATaskStatus(state="failed"),
                metadata={"error": "Cleo task not found"},
            )

        # Task is a dataclass — use attribute access
        cleo_status = task.status.value if hasattr(task.status, 'value') else str(task.status)
        a2a_state = STATE_MAP.get(cleo_status, "working")

        # Build status with optional progress message
        status_msg = None
        if a2a_state == "working":
            progress = self._get_heartbeat_progress_from_task(task)
            if progress:
                status_msg = A2AMessage(
                    role="agent",
                    parts=[A2APart.text_part(progress)],
                )

        result = A2ATask(
            id=a2a_id,
            status=A2ATaskStatus(state=a2a_state, message=status_msg),
        )

        # Attach artifacts if completed
        if a2a_state == "completed":
            result.artifacts = self.outbound_result(cleo_id)

        return result

    def cancel_task(self, a2a_id: str) -> A2ATask:
        """Cancel an A2A task (maps to Cleo TaskBoard cancel).

        Args:
            a2a_id: The A2A task ID

        Returns:
            A2ATask with canceled status
        """
        cleo_id = self._task_map.get(a2a_id)
        if not cleo_id:
            return A2ATask(
                id=a2a_id,
                status=A2ATaskStatus(state="failed"),
                metadata={"error": "Task not found"},
            )

        try:
            self.board.cancel(cleo_id)
            logger.info("[a2a:bridge] cancelled: a2a_id=%s → cleo_id=%s",
                         a2a_id, cleo_id)
        except Exception as e:
            logger.warning("[a2a:bridge] cancel failed: %s", e)

        return A2ATask(
            id=a2a_id,
            status=A2ATaskStatus(state="canceled"),
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get_cleo_task(self, cleo_id: str):
        """Read a task from the Cleo TaskBoard (returns Task dataclass or None)."""
        try:
            return self.board.get(cleo_id)
        except Exception:
            return None

    def _get_heartbeat_progress_from_task(self, task) -> str:
        """Read heartbeat file for in-progress status message."""
        try:
            claimed_by = getattr(task, 'agent_id', "") or ""
            if not claimed_by:
                return ""
            hb_path = f".heartbeats/{claimed_by}.json"
            if os.path.exists(hb_path):
                with open(hb_path, "r", encoding="utf-8") as f:
                    hb = json.load(f)
                status = hb.get("status", "")
                progress = hb.get("progress", "")
                if status or progress:
                    return f"{status}: {progress}" if progress else status
        except Exception:
            pass
        return ""

    # ── Waiting for task completion ────────────────────────────────────────

    async def wait_for_completion(self, a2a_id: str,
                                   timeout: float = 300,
                                   poll_interval: float = 2.0) -> A2ATask:
        """Async-wait for a Cleo task to reach terminal state.

        Used by message/send (synchronous A2A method) which must return
        the final result.

        Args:
            a2a_id: A2A task ID
            timeout: Max wait seconds
            poll_interval: Polling interval seconds

        Returns:
            A2ATask with final status (completed/failed/canceled)
        """
        import asyncio

        cleo_id = self._task_map.get(a2a_id)
        if not cleo_id:
            return A2ATask(
                id=a2a_id,
                status=A2ATaskStatus(state="failed"),
                metadata={"error": "Task not found"},
            )

        deadline = time.time() + timeout
        terminal_states = {"completed", "failed", "cancelled"}

        while time.time() < deadline:
            task = self._get_cleo_task(cleo_id)
            if task:
                status_val = task.status.value if hasattr(task.status, 'value') else str(task.status)
                if status_val in terminal_states:
                    return self.get_task_status(a2a_id)
            await asyncio.sleep(poll_interval)

        # Timeout
        logger.warning("[a2a:bridge] task %s timed out after %.0fs",
                        a2a_id, timeout)
        return A2ATask(
            id=a2a_id,
            status=A2ATaskStatus(state="failed"),
            metadata={"error": f"Timeout after {timeout}s"},
        )

    # ── Lookup helpers ─────────────────────────────────────────────────────

    def cleo_id_for(self, a2a_id: str) -> Optional[str]:
        """Get Cleo task ID for an A2A task ID."""
        return self._task_map.get(a2a_id)

    def a2a_id_for(self, cleo_id: str) -> Optional[str]:
        """Get A2A task ID for a Cleo task ID."""
        return self._reverse_map.get(cleo_id)

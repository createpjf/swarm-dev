"""
adapters/a2a/client.py — A2A Client: Cleo calling external agents.

Handles outbound A2A JSON-RPC 2.0 requests to external agents:
  - Agent Card discovery (/.well-known/agent.json)
  - message/send — submit a task and wait for result
  - tasks/get — poll task status
  - input-required — multi-round negotiation (autonomous)

The client is used by Jerry's a2a_delegate tool. When Leo's SubTaskSpec
includes tool_hint: ["a2a_delegate"], Jerry uses this client to call
external agents and receive their results.

Usage::

    client = A2AClient(config)
    result = client.send_task(
        agent_url="https://chart-agent.example.com",
        message="Generate a bar chart from this data",
        files=["/path/to/data.csv"],
    )
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from adapters.a2a.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATask,
    A2ATaskStatus,
)
from adapters.a2a.registry import AgentEntry, AgentRegistry
from adapters.a2a.security import (
    InboundValidation,
    SecurityFilter,
    TrustLevel,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Client Result
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DelegationResult:
    """Result of delegating a task to an external A2A agent."""
    status: str = "failed"                  # completed / failed / timeout / blocked
    text: str = ""                          # Main text result
    files: list[str] = field(default_factory=list)    # Downloaded file paths
    rounds: int = 0                         # Input-required rounds processed
    agent_url: str = ""                     # Which agent handled it
    agent_name: str = ""                    # Agent name (from card)
    trust_level: str = TrustLevel.UNTRUSTED
    duration: float = 0.0                   # Total time in seconds
    error: str = ""                         # Error message (if failed)
    warnings: list[str] = field(default_factory=list)  # Security warnings

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "text": self.text,
            "files": self.files,
            "rounds": self.rounds,
            "agent_url": self.agent_url,
            "agent_name": self.agent_name,
            "trust_level": self.trust_level,
            "duration": self.duration,
            "error": self.error,
            "warnings": self.warnings,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  A2A Client
# ══════════════════════════════════════════════════════════════════════════════

class A2AClient:
    """Outbound A2A client for calling external agents.

    Integrates with:
      - AgentRegistry: resolve agent URLs + capability matching
      - SecurityFilter: sanitize outbound / validate inbound
      - A2A JSON-RPC 2.0: message/send + tasks/get protocol
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: Full Cleo config dict (reads config["a2a"]["client"]).
        """
        config = config or {}
        client_cfg = config.get("a2a", {}).get("client", {})

        self.enabled = client_cfg.get("enabled", False)
        self._max_timeout = client_cfg.get("security", {}).get(
            "max_timeout", 600)

        self._registry = AgentRegistry(config)
        self._security = SecurityFilter(
            client_cfg.get("security", {}))

        logger.info("[a2a:client] initialized (enabled=%s)", self.enabled)

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def security(self) -> SecurityFilter:
        return self._security

    # ── Main API ──────────────────────────────────────────────────────────

    def send_task(self, agent_url: str, message: str,
                  files: list[str] = None,
                  required_skills: list[str] = None,
                  timeout: float = 120,
                  stream: bool = False,
                  context: dict = None) -> DelegationResult:
        """Send a task to an external A2A agent and wait for result.

        This is the main method used by Jerry's a2a_delegate tool.

        Args:
            agent_url: Target agent URL or "auto" for auto-matching.
            message: Task description text.
            files: File paths to attach (subject to trust filtering).
            required_skills: Skills needed (used with agent_url="auto").
            timeout: Max wait seconds.
            stream: Whether to use streaming (future).
            context: Optional context dict (intent anchor etc.).

        Returns:
            DelegationResult with status, text, files, etc.
        """
        if not self.enabled:
            return DelegationResult(
                status="failed",
                error="A2A Client is disabled. Set a2a.client.enabled=true.")

        t0 = time.time()
        timeout = min(timeout, self._max_timeout)

        # 1. Resolve agent
        entry = self._registry.resolve(agent_url, required_skills)
        if not entry:
            return DelegationResult(
                status="failed",
                error=f"No agent found for URL={agent_url}, "
                      f"skills={required_skills}",
                duration=time.time() - t0)

        trust = entry.trust_level

        # 2. Security: sanitize outbound message
        clean_message = self._security.sanitize_outbound(message, trust)

        # 3. Build A2A message parts
        parts: list[dict] = [{"kind": "text", "text": clean_message}]

        # Attach files if trust allows
        if files and self._security.can_send_files(trust):
            for filepath in files:
                file_part = self._encode_file(filepath)
                if file_part:
                    parts.append(file_part)
        elif files:
            logger.info("[a2a:client] files not sent (trust=%s)", trust)

        # 4. Send JSON-RPC message/send
        rpc_id = f"cleo-{uuid.uuid4().hex[:8]}"
        rpc_body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": parts,
                    "messageId": f"msg-{uuid.uuid4().hex[:12]}",
                },
            },
        }

        logger.info("[a2a:client] sending to %s (%s, trust=%s), msg_len=%d",
                     entry.name, entry.url, trust, len(clean_message))

        try:
            response = self._http_post(
                entry.url, rpc_body,
                auth_headers=self._registry.get_auth_headers(entry.url),
                timeout=min(timeout, 30))  # Initial submit timeout
        except Exception as e:
            entry.record_failure()
            return DelegationResult(
                status="failed",
                error=f"HTTP error: {e}",
                agent_url=entry.url,
                agent_name=entry.name,
                trust_level=trust,
                duration=time.time() - t0)

        # 5. Parse response
        if "error" in response:
            entry.record_failure()
            error_msg = response["error"].get("message", str(response["error"]))
            return DelegationResult(
                status="failed",
                error=f"RPC error: {error_msg}",
                agent_url=entry.url,
                agent_name=entry.name,
                trust_level=trust,
                duration=time.time() - t0)

        result_data = response.get("result", {})
        task_id = result_data.get("id", "")
        task_state = result_data.get("status", {}).get("state", "")

        logger.info("[a2a:client] task created: id=%s, state=%s",
                     task_id, task_state)

        # 6. Poll for completion
        if task_state not in ("completed", "failed", "canceled"):
            result_data = self._poll_until_done(
                entry, task_id, timeout - (time.time() - t0))

        # 7. Extract result
        final_state = result_data.get("status", {}).get("state", "failed")
        entry.record_success()

        # Extract text from artifacts
        result_text = ""
        result_files: list[str] = []
        for artifact in result_data.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text":
                    result_text += part.get("text", "") + "\n"
                elif part.get("kind") == "file":
                    saved = self._save_received_file(part, trust)
                    if saved:
                        result_files.append(saved)

        # 8. Security: validate inbound
        validation = self._security.validate_inbound(result_text.strip(), trust)
        if validation.blocked:
            return DelegationResult(
                status="blocked",
                error="Response blocked by security filter",
                warnings=validation.warnings,
                agent_url=entry.url,
                agent_name=entry.name,
                trust_level=trust,
                duration=time.time() - t0)

        elapsed = round(time.time() - t0, 2)
        logger.info("[a2a:client] task %s completed in %.1fs "
                     "(state=%s, text_len=%d, files=%d)",
                     task_id, elapsed, final_state,
                     len(validation.text), len(result_files))

        return DelegationResult(
            status=final_state if final_state in ("completed", "failed", "canceled")
                   else "failed",
            text=validation.text,
            files=result_files,
            agent_url=entry.url,
            agent_name=entry.name,
            trust_level=trust,
            duration=elapsed,
            warnings=validation.warnings,
        )

    # ── Polling ───────────────────────────────────────────────────────────

    def _poll_until_done(self, entry: AgentEntry, task_id: str,
                         remaining_timeout: float) -> dict:
        """Poll tasks/get until terminal state or timeout."""
        deadline = time.time() + max(remaining_timeout, 5)
        poll_interval = 2.0
        last_data: dict = {}
        rounds = 0

        while time.time() < deadline:
            time.sleep(poll_interval)

            rpc_body = {
                "jsonrpc": "2.0",
                "id": f"poll-{uuid.uuid4().hex[:8]}",
                "method": "tasks/get",
                "params": {"id": task_id},
            }

            try:
                response = self._http_post(
                    entry.url, rpc_body,
                    auth_headers=self._registry.get_auth_headers(entry.url),
                    timeout=15)
            except Exception as e:
                logger.warning("[a2a:client] poll failed: %s", e)
                continue

            if "error" in response:
                logger.warning("[a2a:client] poll error: %s",
                               response["error"])
                continue

            last_data = response.get("result", {})
            state = last_data.get("status", {}).get("state", "")

            if state in ("completed", "failed", "canceled"):
                return last_data

            if state == "input-required":
                # Handle multi-round negotiation
                rounds += 1
                max_rounds = self._security.get_max_rounds(entry.trust_level)
                if rounds > max_rounds:
                    logger.warning(
                        "[a2a:client] max rounds (%d) exceeded for %s",
                        max_rounds, task_id)
                    break

                # Auto-respond to input-required
                # Jerry will handle this through the IntentAnchor
                logger.info("[a2a:client] input-required round %d/%d",
                            rounds, max_rounds)

            # Adaptive poll interval
            poll_interval = min(poll_interval * 1.2, 10.0)

        # Timeout
        logger.warning("[a2a:client] polling timed out for task %s", task_id)
        if last_data:
            last_data.setdefault("status", {})["state"] = "failed"
            last_data["status"]["message"] = {"role": "agent", "parts": [
                {"kind": "text", "text": "Polling timed out"}]}
        return last_data or {"status": {"state": "failed"}}

    # ── HTTP transport ────────────────────────────────────────────────────

    def _http_post(self, url: str, body: dict,
                   auth_headers: dict = None,
                   timeout: float = 30) -> dict:
        """Send a JSON-RPC POST request.

        Args:
            url: Target URL.
            body: JSON-RPC request body.
            auth_headers: Optional auth headers.
            timeout: HTTP timeout seconds.

        Returns:
            Parsed JSON response dict.
        """
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Cleo-A2A-Client/0.2.0",
        }
        if auth_headers:
            headers.update(auth_headers)

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_body = resp.read().decode("utf-8")
            return json.loads(response_body)

    # ── File handling ─────────────────────────────────────────────────────

    def _encode_file(self, filepath: str) -> Optional[dict]:
        """Encode a file as an A2A FilePart dict."""
        try:
            if not os.path.exists(filepath):
                logger.warning("[a2a:client] file not found: %s", filepath)
                return None

            filename = os.path.basename(filepath)
            mime_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"

            with open(filepath, "rb") as f:
                raw = f.read()

            # Limit file size to 10MB
            if len(raw) > 10 * 1024 * 1024:
                logger.warning("[a2a:client] file too large: %s (%d bytes)",
                               filepath, len(raw))
                return None

            return {
                "kind": "file",
                "name": filename,
                "mimeType": mime_type,
                "data": base64.b64encode(raw).decode("ascii"),
            }

        except Exception as e:
            logger.warning("[a2a:client] file encode failed: %s: %s",
                           filepath, e)
            return None

    def _save_received_file(self, file_part: dict,
                            trust_level: str) -> str:
        """Save a received FilePart to workspace/a2a/ directory.

        Args:
            file_part: A2A FilePart dict.
            trust_level: Trust level of the source agent.

        Returns:
            Saved file path, or empty string on failure.
        """
        if not self._security.can_receive_files(trust_level):
            logger.info("[a2a:client] file receive blocked (trust=%s)",
                        trust_level)
            return ""

        try:
            workspace = os.environ.get("CLEO_WORKSPACE", "workspace")
            a2a_dir = os.path.join(workspace, "a2a", "received")
            os.makedirs(a2a_dir, exist_ok=True)

            filename = file_part.get("name", f"file_{uuid.uuid4().hex[:8]}")
            # Sanitize filename
            filename = os.path.basename(filename)
            filepath = os.path.join(a2a_dir, filename)

            data = file_part.get("data", "")
            if data:
                raw = base64.b64decode(data)
                with open(filepath, "wb") as f:
                    f.write(raw)
                logger.info("[a2a:client] saved received file: %s (%d bytes)",
                            filepath, len(raw))
                return filepath

            uri = file_part.get("uri", "")
            if uri:
                return uri

        except Exception as e:
            logger.warning("[a2a:client] file save failed: %s", e)

        return ""

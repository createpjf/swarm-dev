"""
adapters/channels/session.py
File-backed session store for channel conversations.

Tracks per-user/group sessions across channel interactions.
Stores conversation history per session in separate JSONL files.
Uses the same FileLock pattern as ContextBus and TaskBoard.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    from filelock import FileLock
except ImportError:
    class FileLock:  # type: ignore
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

logger = logging.getLogger(__name__)

SESSIONS_FILE = "memory/channel_sessions.json"
SESSIONS_LOCK = "memory/channel_sessions.lock"
HISTORY_DIR = "memory/sessions"
MAX_HISTORY_MESSAGES = 50   # FIFO limit per session


@dataclass
class ChannelSession:
    """Represents a channel conversation session."""
    session_id: str             # "{channel}:{chat_id}"
    channel: str                # "telegram" | "discord" | "feishu"
    chat_id: str                # platform chat/group ID
    user_ids: list[str] = field(default_factory=list)
    user_names: list[str] = field(default_factory=list)
    message_count: int = 0
    last_task_id: str = ""
    last_active: float = 0.0
    created_at: float = field(default_factory=time.time)


class SessionStore:
    """
    File-locked JSON store for channel sessions.
    Thread-safe and process-safe via FileLock.

    Session metadata in: memory/channel_sessions.json
    Conversation history in: memory/sessions/{session_id}.jsonl
    """

    def __init__(self, path: str = SESSIONS_FILE):
        self.path = path
        self.lock = FileLock(SESSIONS_LOCK)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        os.makedirs(HISTORY_DIR, exist_ok=True)
        if not os.path.exists(path):
            self._write({})

    def get_or_create(self, channel: str, chat_id: str,
                      user_id: str = "", user_name: str = "") -> ChannelSession:
        """Get existing session or create a new one."""
        session_id = f"{channel}:{chat_id}"
        with self.lock:
            data = self._read()
            if session_id in data:
                session = self._from_dict(data[session_id])
                # Track new users
                if user_id and user_id not in session.user_ids:
                    session.user_ids.append(user_id)
                if user_name and user_name not in session.user_names:
                    session.user_names.append(user_name)
                session.message_count += 1
                session.last_active = time.time()
                data[session_id] = asdict(session)
                self._write(data)
                return session
            else:
                session = ChannelSession(
                    session_id=session_id,
                    channel=channel,
                    chat_id=chat_id,
                    user_ids=[user_id] if user_id else [],
                    user_names=[user_name] if user_name else [],
                    message_count=1,
                    last_active=time.time(),
                )
                data[session_id] = asdict(session)
                self._write(data)
                return session

    def update_task(self, session_id: str, task_id: str):
        """Update the last task ID for a session."""
        with self.lock:
            data = self._read()
            if session_id in data:
                data[session_id]["last_task_id"] = task_id
                data[session_id]["last_active"] = time.time()
                self._write(data)

    # ── Conversation History ──────────────────────────────────

    def add_message(self, session_id: str, role: str, content: str,
                    user_name: str = ""):
        """
        Append a message to the session's conversation history.

        Args:
            session_id: "{channel}:{chat_id}"
            role: "user" or "assistant"
            content: message text
            user_name: optional display name for user messages
        """
        msg = {
            "role": role,
            "content": content,
            "ts": time.time(),
        }
        if user_name:
            msg["user"] = user_name

        history_path = self._history_path(session_id)
        try:
            with open(history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("[session] Failed to write history: %s", e)
            return

        # FIFO trim: if over limit, rewrite with last N messages
        self._trim_history(history_path)

    def get_history(self, session_id: str,
                    max_turns: int = 10) -> list[dict]:
        """
        Load the most recent conversation turns for a session.

        Args:
            session_id: "{channel}:{chat_id}"
            max_turns: max number of user+assistant pairs to return
                       (actual messages returned = up to max_turns * 2)

        Returns:
            List of {role, content, ts, user?} dicts, oldest first.
        """
        history_path = self._history_path(session_id)
        if not os.path.exists(history_path):
            return []

        messages = []
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return []

        # Return last max_turns*2 messages (user + assistant pairs)
        limit = max_turns * 2
        return messages[-limit:]

    def format_history_for_prompt(self, session_id: str,
                                   max_turns: int = 10) -> str:
        """
        Format conversation history as a prompt-injectable string.

        Returns a markdown-formatted conversation history suitable
        for injection into an agent's task description.
        """
        messages = self.get_history(session_id, max_turns)
        if not messages:
            return ""

        lines = ["## 对话历史 (Conversation History)\n"]
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            user = msg.get("user", "")
            # Truncate very long messages to save tokens
            if len(content) > 500:
                content = content[:500] + "…"
            if role == "user":
                prefix = f"**[{user}]**" if user else "**[用户]**"
            else:
                prefix = "**[助手]**"
            lines.append(f"{prefix} {content}\n")

        return "\n".join(lines)

    # ── Query Methods ─────────────────────────────────────────

    def get_active_sessions(self, max_age_hours: int = 24) -> list[ChannelSession]:
        """Return sessions active within the last N hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        data = self._read()
        sessions = []
        for s in data.values():
            if s.get("last_active", 0) > cutoff:
                sessions.append(self._from_dict(s))
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        return sessions

    def get_all_sessions(self) -> list[ChannelSession]:
        """Return all sessions."""
        data = self._read()
        return [self._from_dict(s) for s in data.values()]

    # ── Internal ──────────────────────────────────────────────

    def _history_path(self, session_id: str) -> str:
        """Get the JSONL file path for a session's history."""
        # Sanitize session_id for filesystem (replace : with _)
        safe_id = session_id.replace(":", "_").replace("/", "_")
        return os.path.join(HISTORY_DIR, f"{safe_id}.jsonl")

    def _trim_history(self, path: str):
        """Trim history file to MAX_HISTORY_MESSAGES (FIFO)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > MAX_HISTORY_MESSAGES:
                # Keep only the last MAX_HISTORY_MESSAGES
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-MAX_HISTORY_MESSAGES:])
        except OSError:
            pass

    def _read(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _from_dict(d: dict) -> ChannelSession:
        return ChannelSession(
            session_id=d.get("session_id", ""),
            channel=d.get("channel", ""),
            chat_id=d.get("chat_id", ""),
            user_ids=d.get("user_ids", []),
            user_names=d.get("user_names", []),
            message_count=d.get("message_count", 0),
            last_task_id=d.get("last_task_id", ""),
            last_active=d.get("last_active", 0),
            created_at=d.get("created_at", 0),
        )

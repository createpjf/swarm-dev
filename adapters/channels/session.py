"""
adapters/channels/session.py
File-backed session store for channel conversations.

This is the **sole conversation persistence layer** for all channel interactions.
(The former core/conversation_history.py was removed as dead code in Sprint 5.1.)

Tracks per-user/group sessions across channel interactions.
Stores conversation history per session in separate JSONL files.
Uses the same FileLock pattern as ContextBus and TaskBoard.

V0.03+: Also serves as the persistence layer for Dashboard sessions
(multi-session support — ChatGPT-style conversation list).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from core.protocols import FileLock  # shared fallback

logger = logging.getLogger(__name__)

# ── Absolute paths (Gateway CWD may differ from project root) ──
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))  # adapters/channels/ → adapters/ → project root
SESSIONS_FILE = os.path.join(_PROJECT_ROOT, "memory", "channel_sessions.json")
SESSIONS_LOCK = os.path.join(_PROJECT_ROOT, "memory", "channel_sessions.lock")
HISTORY_DIR   = os.path.join(_PROJECT_ROOT, "memory", "sessions")

MAX_HISTORY_MESSAGES = 200  # FIFO limit per session (increased for dashboard)
CHARS_PER_TOKEN = 3         # conservative (English ~4, CJK ~1.5)
SESSION_EXPIRE_HOURS = 24   # start fresh if idle longer than this
GROUP_USER_ISOLATION = True  # isolate per-user contexts in group chats


@dataclass
class ChannelSession:
    """Represents a channel conversation session."""
    session_id: str             # "{channel}:{chat_id}"
    channel: str                # "telegram" | "discord" | "feishu" | "dashboard"
    chat_id: str                # platform chat/group ID (UUID for dashboard)
    user_ids: list[str] = field(default_factory=list)
    user_names: list[str] = field(default_factory=list)
    message_count: int = 0
    last_task_id: str = ""
    last_active: float = 0.0
    created_at: float = field(default_factory=time.time)
    # V0.03+: Dashboard session fields
    title: str = ""             # user-visible session name
    pinned: bool = False        # pinned to top of list
    no_expire: bool = False     # skip auto-expiry (dashboard sessions)


class SessionStore:
    """
    File-locked JSON store for channel sessions.
    Thread-safe and process-safe via FileLock.
    """

    def __init__(self, path: str = SESSIONS_FILE):
        self.path = path
        self.lock = FileLock(SESSIONS_LOCK)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        os.makedirs(HISTORY_DIR, exist_ok=True)
        if not os.path.exists(path):
            self._write({})

    def get_or_create(self, channel: str, chat_id: str,
                      user_id: str = "", user_name: str = "",
                      is_group: bool = False) -> ChannelSession:
        """Get existing session or create a new one.

        When GROUP_USER_ISOLATION is True and is_group is True,
        sessions are keyed per-user within the group to prevent
        cross-user context bleeding in group chats.
        """
        if is_group and GROUP_USER_ISOLATION and user_id:
            session_id = f"{channel}:{chat_id}:{user_id}".lower().strip()
        else:
            session_id = f"{channel}:{chat_id}".lower().strip()
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
        """Append a message to the session's conversation history.

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
        """Load the most recent conversation turns for a session.

        Returns list of {role, content, ts, user?} dicts, oldest first.
        Returns empty list if session is expired (idle > SESSION_EXPIRE_HOURS).
        Dashboard sessions (no_expire=True) skip the expiry check.
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

        if not messages:
            return []

        # Check session expiry — if idle too long, start fresh
        # (skip for dashboard sessions with no_expire=True)
        skip_expiry = False
        data = self._read()
        if session_id in data:
            skip_expiry = data[session_id].get("no_expire", False)

        if not skip_expiry:
            last_ts = messages[-1].get("ts", 0)
            idle_hours = (time.time() - last_ts) / 3600
            if idle_hours > SESSION_EXPIRE_HOURS:
                logger.info("Session %s expired (idle %.1fh), starting fresh",
                            session_id, idle_hours)
                return []

        # Return last max_turns*2 messages (user + assistant pairs)
        limit = max_turns * 2
        return messages[-limit:]

    def format_history_for_prompt(self, session_id: str,
                                   max_turns: int = 10) -> str:
        """Format conversation history as a prompt-injectable string.

        Returns a markdown-formatted conversation history suitable
        for injection into an agent's task description. Token-aware:
        truncates individual messages to keep total reasonable.
        """
        messages = self.get_history(session_id, max_turns)
        if not messages:
            return ""

        lines = [
            "## 对话历史 (Conversation History)\n"
            "Below is the recent conversation with this user. "
            "Use this context to understand references like '继续', "
            "'the previous one', 'do the same for...', etc.\n"
        ]
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            user = msg.get("user", "")
            # Truncate very long messages to save tokens
            if len(content) > 800:
                content = content[:750] + "…(truncated)"
            if role == "user":
                prefix = f"**[{user}]**" if user else "**[User]**"
            else:
                prefix = "**[Assistant]**"
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

    def cleanup_expired(self, max_idle_hours: float = SESSION_EXPIRE_HOURS) -> int:
        """Remove sessions idle longer than max_idle_hours.

        Also deletes the associated conversation history JSONL files.
        Skips sessions with no_expire=True (dashboard sessions).
        Returns the number of sessions removed.
        """
        cutoff = time.time() - (max_idle_hours * 3600)
        removed = 0
        with self.lock:
            data = self._read()
            expired_keys = [
                k for k, v in data.items()
                if v.get("last_active", 0) < cutoff
                and not v.get("no_expire", False)
            ]
            for key in expired_keys:
                # Delete history file
                history_path = self._history_path(key)
                try:
                    if os.path.exists(history_path):
                        os.remove(history_path)
                except OSError:
                    pass
                del data[key]
                removed += 1
            if removed:
                self._write(data)
                logger.info("[session] cleaned up %d expired sessions "
                            "(idle > %.1fh)", removed, max_idle_hours)
        return removed

    # ── Dashboard Session Methods ─────────────────────────────

    def create_dashboard_session(self, title: str = "") -> ChannelSession:
        """Create a new dashboard session with a UUID chat_id.

        Dashboard sessions have no_expire=True and channel='dashboard'.
        """
        chat_id = uuid.uuid4().hex[:12]
        session_id = f"dashboard:{chat_id}"
        now = time.time()
        session = ChannelSession(
            session_id=session_id,
            channel="dashboard",
            chat_id=chat_id,
            message_count=0,
            last_active=now,
            created_at=now,
            title=title,
            no_expire=True,
        )
        with self.lock:
            data = self._read()
            data[session_id] = asdict(session)
            self._write(data)
        return session

    def list_dashboard_sessions(self) -> list[ChannelSession]:
        """Return all dashboard sessions, sorted by last_active descending."""
        data = self._read()
        sessions = []
        for s in data.values():
            if s.get("channel") == "dashboard":
                sessions.append(self._from_dict(s))
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        return sessions

    def rename_session(self, session_id: str, title: str):
        """Update a session's title."""
        with self.lock:
            data = self._read()
            if session_id in data:
                data[session_id]["title"] = title
                self._write(data)

    def pin_session(self, session_id: str, pinned: bool = True):
        """Pin or unpin a session."""
        with self.lock:
            data = self._read()
            if session_id in data:
                data[session_id]["pinned"] = pinned
                self._write(data)

    def delete_session(self, session_id: str):
        """Delete a session and its history file."""
        with self.lock:
            data = self._read()
            if session_id in data:
                # Delete history file
                history_path = self._history_path(session_id)
                try:
                    if os.path.exists(history_path):
                        os.remove(history_path)
                except OSError:
                    pass
                del data[session_id]
                self._write(data)

    # ── Internal ──

    def _history_path(self, session_id: str) -> str:
        """Get the JSONL file path for a session's history."""
        safe_id = session_id.replace(":", "_").replace("/", "_")
        return os.path.join(HISTORY_DIR, f"{safe_id}.jsonl")

    def _trim_history(self, path: str):
        """Trim history file to MAX_HISTORY_MESSAGES (FIFO)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > MAX_HISTORY_MESSAGES:
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
            title=d.get("title", ""),
            pinned=d.get("pinned", False),
            no_expire=d.get("no_expire", False),
        )

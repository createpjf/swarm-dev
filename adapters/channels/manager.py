"""
adapters/channels/manager.py
Central coordinator for all channel adapters.

Responsibilities:
  - Load and start enabled channel adapters from config
  - Receive normalized messages from all channels
  - Serialize task submissions through a sequential queue
  - Monitor task completion via TaskBoard polling
  - Deliver results back to the originating channel/chat
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from threading import Thread
from typing import Optional

from .base import ChannelAdapter, ChannelMessage
from .session import SessionStore

logger = logging.getLogger(__name__)

# Message length limits per platform
PLATFORM_LIMITS = {
    "telegram": 4096,
    "discord": 2000,
    "feishu": 10000,
    "slack": 4000,
}

TASK_TIMEOUT = 600  # 10 minutes
POLL_INTERVAL = 2   # seconds between TaskBoard polls
STATUS_INTERVAL = 30  # seconds before sending "still processing" message
HEALTH_CHECK_INTERVAL = 60  # seconds between health checks


@dataclass
class PendingChannelTask:
    """Tracks a task submitted from a channel, for result delivery."""
    task_id: str
    channel: str
    chat_id: str
    session_id: str
    adapter: ChannelAdapter
    submitted_at: float
    status_sent: bool = False


class ChannelManager:
    """
    Manages all channel adapters and routes messages to/from the Cleo system.

    Usage:
        manager = ChannelManager(config)
        await manager.start()   # starts all enabled adapters
        ...
        await manager.stop()    # graceful shutdown
    """

    def __init__(self, config: dict):
        self.config = config
        self.channels_config = config.get("channels", {})
        self.adapters: list[ChannelAdapter] = []
        # NOTE: Queue is created lazily in start() to ensure it's bound to
        # the correct event loop (critical for Python 3.9 compatibility).
        self._queue: Optional[asyncio.Queue] = None
        self._sessions = SessionStore()
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Persistent orchestrator pool — created lazily on first message
        self._persistent_orch = None
        self._orch_lock = threading.Lock()

    async def start(self):
        """Load and start all enabled channel adapters."""
        self._running = True
        self._loop = asyncio.get_event_loop()
        # Create queue inside the running event loop (Python 3.9 compat)
        self._queue = asyncio.Queue()

        # Load adapters
        self._load_adapters()

        if not self.adapters:
            logger.info("No channel adapters enabled")
            return

        # Start all adapters
        for adapter in self.adapters:
            try:
                adapter.set_callback(self._on_message)
                await adapter.start()
                logger.info("Channel adapter started: %s", adapter.channel_name)
            except Exception as e:
                logger.error("Failed to start %s adapter: %s",
                             adapter.channel_name, e)

        # Start the task processor
        self._processor_task = asyncio.create_task(self._task_processor())
        # Start the health monitor
        self._health_task = asyncio.create_task(self._health_monitor())
        # Start session cleanup timer
        self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())
        logger.info("ChannelManager started with %d adapter(s)", len(self.adapters))

    async def stop(self):
        """Stop all adapters and the persistent agent pool gracefully."""
        self._running = False
        # Shut down persistent agent pool
        with self._orch_lock:
            if self._persistent_orch:
                try:
                    self._persistent_orch.shutdown()
                except Exception as e:
                    logger.error("Error shutting down agent pool: %s", e)
                self._persistent_orch = None
        for task in (self._processor_task, self._health_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for adapter in self.adapters:
            try:
                await adapter.stop()
                logger.info("Channel adapter stopped: %s", adapter.channel_name)
            except Exception as e:
                logger.error("Error stopping %s: %s", adapter.channel_name, e)

    async def reload(self):
        """Hot-reload: stop all adapters, re-read config, restart enabled ones."""
        logger.info("Reloading channel manager...")

        # Stop existing adapters
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception as e:
                logger.error("Error stopping %s during reload: %s",
                             adapter.channel_name, e)
        self.adapters.clear()

        # Re-read config from disk (use absolute path for reliability)
        import yaml
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(_project_root, "config", "agents.yaml")
        try:
            with open(config_path, "r") as f:
                fresh_config = yaml.safe_load(f) or {}
            self.channels_config = fresh_config.get("channels", {})
            self.config["channels"] = self.channels_config
        except Exception as e:
            logger.error("Failed to reload channels config from %s: %s",
                         config_path, e)
            return

        # Re-load .env into os.environ (absolute path)
        env_path = os.path.join(_project_root, ".env")
        try:
            from core.env_loader import load_dotenv
            load_dotenv(env_path)
        except Exception:
            pass

        # Load and start adapters
        self._load_adapters()
        for adapter in self.adapters:
            try:
                adapter.set_callback(self._on_message)
                await adapter.start()
                logger.info("Channel adapter restarted: %s", adapter.channel_name)
            except Exception as e:
                logger.error("Failed to restart %s adapter: %s",
                             adapter.channel_name, e)

        # Ensure processor task is running
        if not self._processor_task or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._task_processor())
        # Ensure health monitor is running
        if not self._health_task or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_monitor())

        logger.info("Channel manager reloaded: %d adapter(s) running",
                     len(self.adapters))

    def get_status(self) -> list[dict]:
        """Return status of all adapters (for /v1/channels endpoint)."""
        # Canonical list of known channels
        known_channels = ["telegram", "discord", "feishu", "slack"]
        statuses = []

        for name in known_channels:
            cfg = self.channels_config.get(name, {})
            adapter = self._get_adapter(name)

            status: dict = {
                "channel": name,
                "enabled": cfg.get("enabled", False),
                "running": adapter._running if adapter else False,
            }

            # Add config details for dashboard
            token_env_keys = self._get_token_env_keys(name, cfg)
            status["token_configured"] = all(
                bool(os.environ.get(cfg.get(k, ""), ""))
                for k in token_env_keys
            ) if token_env_keys else False
            status["mention_required"] = cfg.get("mention_required", True)
            status["config"] = {
                k: v for k, v in cfg.items()
                if k not in ("enabled",) and not k.endswith("_token")
            }

            if not adapter and cfg.get("enabled", False):
                status["reason"] = "SDK not installed"
            elif not cfg.get("enabled", False):
                status["reason"] = "disabled"

            statuses.append(status)

        # Include any extra channels from config not in the known list
        for name in self.channels_config:
            if name not in known_channels:
                cfg = self.channels_config[name]
                adapter = self._get_adapter(name)
                statuses.append({
                    "channel": name,
                    "enabled": cfg.get("enabled", False),
                    "running": adapter._running if adapter else False,
                    "token_configured": False,
                    "mention_required": cfg.get("mention_required", True),
                    "config": {},
                    "reason": "disabled or SDK not installed",
                })

        return statuses

    @staticmethod
    def _get_token_env_keys(channel_name: str, cfg: dict) -> list[str]:
        """Return the config keys that reference env vars for tokens."""
        if channel_name in ("telegram", "discord"):
            return ["bot_token_env"]
        elif channel_name == "feishu":
            return ["app_id_env", "app_secret_env"]
        elif channel_name == "slack":
            return ["bot_token_env", "app_token_env"]
        return []

    # ── Session context for tool access ──

    _active_session_path = ".channel_session.json"

    def _save_active_session(self, msg: ChannelMessage):
        """Persist the active channel session so tools (e.g. send_file) can
        route messages back to the correct channel/chat."""
        try:
            data = {
                "session_id": msg.session_id,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "user_id": msg.user_id,
                "user_name": msg.user_name,
                "ts": time.time(),
            }
            with open(self._active_session_path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning("Failed to save active session: %s", e)

    @staticmethod
    def get_active_session() -> Optional[dict]:
        """Read the active channel session info (used by tool handlers)."""
        path = ChannelManager._active_session_path
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return None

    async def send_file(self, session_id: str, file_path: str,
                        caption: str = "", reply_to: str = "") -> str:
        """Send a file to a channel chat. Returns sent message ID.

        Args:
            session_id: Channel session ID in format "channel:chat_id"
            file_path: Path to the file to send
            caption: Optional caption/message with the file
            reply_to: Optional message ID to reply to
        """
        if ":" not in session_id:
            logger.error("Invalid session_id for send_file: %s", session_id)
            return ""
        channel, chat_id = session_id.split(":", 1)
        adapter = self._get_adapter(channel)
        if not adapter:
            logger.error("No adapter for channel '%s' in send_file", channel)
            return ""
        return await adapter.send_file(chat_id, file_path, caption, reply_to)

    # ── Internal ──

    def _load_adapters(self):
        """Load enabled channel adapters. Skip gracefully if SDK not installed."""
        for channel_name, channel_cfg in self.channels_config.items():
            if not channel_cfg.get("enabled", False):
                continue

            adapter = self._create_adapter(channel_name, channel_cfg)
            if adapter:
                self.adapters.append(adapter)

    def _create_adapter(self, name: str, cfg: dict) -> Optional[ChannelAdapter]:
        """Create a channel adapter by name. Returns None if SDK unavailable."""
        if name == "telegram":
            try:
                from .telegram import TelegramAdapter
                return TelegramAdapter(cfg)
            except ImportError:
                logger.warning(
                    "Telegram adapter skipped: python-telegram-bot not installed. "
                    "Install with: pip install python-telegram-bot")
                return None

        elif name == "discord":
            try:
                from .discord_adapter import DiscordAdapter
                return DiscordAdapter(cfg)
            except ImportError:
                logger.warning(
                    "Discord adapter skipped: discord.py not installed. "
                    "Install with: pip install discord.py")
                return None

        elif name == "feishu":
            try:
                from .feishu import FeishuAdapter
                return FeishuAdapter(cfg)
            except ImportError:
                logger.warning(
                    "Feishu adapter skipped: lark-oapi not installed. "
                    "Install with: pip install lark-oapi")
                return None

        elif name == "slack":
            try:
                from .slack import SlackAdapter
                return SlackAdapter(cfg)
            except ImportError:
                logger.warning(
                    "Slack adapter skipped: slack-sdk not installed. "
                    "Install with: pip install 'slack-sdk[socket-mode]'")
                return None

        else:
            logger.warning("Unknown channel adapter: %s", name)
            return None

    async def _on_message(self, msg: ChannelMessage):
        """Callback from channel adapters — enqueue message for processing.

        Security checks (in order):
          1. Rate limiting — reject if user sending too fast
          2. User authentication — reject if user not authorized
          3. Pairing code check — handle pairing flow for new users
        """
        logger.info("[%s] message from %s (%s): %s",
                    msg.channel, msg.user_name, msg.chat_id, msg.text[:80])

        # ── Rate limiting ──
        try:
            from core.rate_limiter import channel_limiter
            rate_key = f"{msg.channel}:{msg.user_id}"
            if not channel_limiter.allow(rate_key):
                logger.warning("[rate] User %s rate-limited on %s",
                               msg.user_id, msg.channel)
                if channel_limiter.should_warn(rate_key, cooldown=30):
                    adapter = self._get_adapter(msg.channel)
                    if adapter:
                        await adapter.send_message(
                            msg.chat_id,
                            "⚠️ 消息发送过快，请稍后再试。\n"
                            "Rate limited — please wait a moment.")
                return
        except ImportError:
            pass  # rate_limiter not available, continue without

        # ── User authentication ──
        try:
            from core.user_auth import get_user_auth
            channel_cfg = self.channels_config.get(msg.channel, {})
            auth_mode = channel_cfg.get("auth_mode", "pairing")
            allowed_users = channel_cfg.get("allowed_users", [])

            auth = get_user_auth(auth_mode)
            if not auth.is_authorized(msg.channel, msg.user_id, allowed_users):
                # Check if this is a pairing code submission
                if auth_mode == "pairing" and msg.text.strip().isdigit():
                    result = auth.verify_pairing_code(
                        msg.channel, msg.user_id,
                        msg.text.strip(), msg.user_name)
                    adapter = self._get_adapter(msg.channel)
                    if adapter:
                        emoji = "✅" if result["ok"] else "❌"
                        await adapter.send_message(
                            msg.chat_id, f"{emoji} {result['message']}")
                    if result["ok"]:
                        logger.info("[auth] User %s paired on %s",
                                    msg.user_id, msg.channel)
                    return

                # Not authorized — auto-generate code and send to user
                adapter = self._get_adapter(msg.channel)
                if adapter:
                    code = auth.generate_pairing_code(
                        label=f"auto:{msg.channel}:{msg.user_id}")
                    await adapter.send_message(
                        msg.chat_id,
                        f"🔒 First-time verification required.\n"
                        f"Your code: {code}\n"
                        f"Please send this code back to verify.")
                    logger.info("[pairing] Auto-generated code for %s:%s (%s)",
                                msg.channel, msg.user_id, msg.user_name)
                return
        except ImportError:
            pass  # user_auth not available, continue without

        await self._queue.put(msg)

    async def _task_processor(self):
        """
        Sequential task processor — consumes from queue, one at a time.
        For each message:
          1. Send typing indicator
          2. Submit task via Orchestrator
          3. Poll TaskBoard until complete or timeout
          4. Send result back to channel
        """
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Find the adapter for this channel
            adapter = self._get_adapter(msg.channel)
            if not adapter:
                logger.error("No adapter found for channel: %s", msg.channel)
                continue

            # Track session (per-user in groups for isolation)
            session = self._sessions.get_or_create(
                msg.channel, msg.chat_id, msg.user_id, msg.user_name,
                is_group=msg.is_group)

            # Show queue position if there are waiting messages
            queue_size = self._queue.qsize()
            if queue_size > 0:
                await adapter.send_message(
                    msg.chat_id,
                    f"⏳ 任务已排队 (前方还有 {queue_size} 个任务)...")

            try:
                await self._process_message(msg, adapter, session)
            except Exception as e:
                logger.exception("Error processing channel message: %s", e)
                try:
                    await adapter.send_message(
                        msg.chat_id, f"❌ 处理失败: {e}")
                except Exception:
                    pass

    async def _process_message(self, msg: ChannelMessage,
                                adapter: ChannelAdapter,
                                session):
        """End-to-end message processing pipeline for every channel adapter.

        Orchestrates the full lifecycle of a single user message from
        ingestion to response delivery.  Runs inside the asyncio event
        loop of the channel adapter (e.g. Telegram long-poll loop).

        **Pipeline stages:**

        1. **Typing indicator** — immediate visual feedback to the user.
        2. **Session history** — persist the user message to the
           ``SessionStore`` and load the last 10 turns for context
           injection into the task description.
        3. **Task submission** — delegate to ``_submit_task()`` which
           runs synchronously in a thread-pool executor (because the
           ``TaskBoard`` uses file locks).  The persistent orchestrator
           pool picks up the new task from the board.
        4. **Polling** — ``_wait_for_result()`` polls the ``TaskBoard``
           until the task reaches ``completed`` / ``failed`` status or
           a configurable timeout (default 120 s) expires.
        5. **Response delivery** — the result is truncated to 2000 chars
           for session storage, then chunked per platform character
           limits (Telegram 4096, Discord 2000) and sent back.

        **Error handling:**
        - Task submission failure → send "❌ 任务提交失败".
        - Timeout → send "⏰ 任务超时" message.
        - Unhandled exception → caught by the caller ``_on_message()``
          which sends a generic error reply.

        Args:
            msg:     Normalised ``ChannelMessage`` with text, user info,
                     session_id, and channel type.
            adapter: Platform-specific ``ChannelAdapter`` for sending
                     replies and typing indicators.
            session: ``ChannelSession`` instance — its session_id may differ
                     from msg.session_id when per-user group isolation is on.
        """
        # Use the session object's session_id (may be per-user in groups)
        sid = session.session_id

        # Send typing indicator
        await adapter.send_typing(msg.chat_id)

        # Save user message to session conversation history
        self._sessions.add_message(sid, "user", msg.text, msg.user_name)

        # Load conversation history for context injection
        session_history = self._sessions.format_history_for_prompt(
            sid, max_turns=10)
        if session_history:
            logger.info("[session:%s] loaded conversation history for context",
                        sid)

        # Save channel session info for tool access (e.g., send_file)
        self._save_active_session(msg)

        # Track user preferences (non-blocking, best-effort)
        try:
            from adapters.memory.user_profile import UserProfileStore
            profile_store = UserProfileStore()
            profile_store.record_interaction(
                user_id=msg.user_id, text=msg.text,
                channel=msg.channel, display_name=msg.user_name)
        except Exception:
            pass  # User profiling is optional

        # Inject image attachment context if present
        task_text = msg.text
        for att in (msg.attachments or []):
            if att.get("type") == "image" and att.get("file_path"):
                task_text += (
                    f"\n\n[Image attached: {att['file_path']}]"
                    f"\nUse the analyze_image tool with "
                    f"image_path=\"{att['file_path']}\" to understand this image."
                )

        # Submit task via Orchestrator (with session history + channel tag)
        task_id = await asyncio.get_event_loop().run_in_executor(
            None, self._submit_task, task_text, session_history, msg.channel)

        if not task_id:
            await adapter.send_message(msg.chat_id, "❌ 任务提交失败")
            return

        self._sessions.update_task(sid, task_id)
        await adapter.send_message(
            msg.chat_id, f"🚀 任务已提交，正在处理...")

        # Poll for completion
        result = await self._wait_for_result(
            task_id, msg, adapter)

        if result:
            # Save assistant response to session conversation history
            self._sessions.add_message(sid, "assistant", result[:2000])

            # Chunk and send result
            chunks = self._chunk_message(
                result, PLATFORM_LIMITS.get(msg.channel, 4096))
            for chunk in chunks:
                await adapter.send_message(msg.chat_id, chunk)
                if len(chunks) > 1:
                    await asyncio.sleep(0.5)  # rate limit
        else:
            await adapter.send_message(
                msg.chat_id, "⏰ 任务超时，请稍后重试或简化请求")

    # ── Persistent Orchestrator Pool ─────────────────────────────
    #
    # Instead of spawning 3 OS processes per message (2-5s overhead),
    # we keep a single pool of agent processes alive between messages.
    # Agents self-claim tasks from the file-based TaskBoard.
    # Pool is created lazily on first message, restarted if agents
    # exit due to idle timeout (default: 5 minutes of inactivity).

    AGENT_IDLE_CYCLES = 300  # ~5 minutes (1s per cycle in _agent_loop)

    def _submit_task(self, description: str,
                     session_history: str = "",
                     channel_name: str = "") -> Optional[str]:
        """Submit a task to the persistent agent pool (runs in thread pool).

        Agents are kept alive between messages to avoid process startup
        overhead. The pool is created on first call and restarted if all
        agents have exited due to idle timeout.

        Args:
            description: The user's message / task description.
            session_history: Formatted conversation history to inject
                             into the task description for context continuity.
        """
        try:
            from core.task_board import TaskBoard

            board = TaskBoard()

            # Inject channel source tag + conversation history
            source_tag = f"[source:{channel_name}]\n\n" if channel_name else ""
            if session_history:
                full_description = (
                    f"{source_tag}"
                    f"{session_history}\n"
                    f"---\n\n"
                    f"## 当前消息 (Current Message)\n"
                    f"{description}"
                )
            else:
                full_description = f"{source_tag}{description}"

            # Ensure agents are running (start or restart pool)
            with self._orch_lock:
                self._ensure_agents_running(board)

            # Archive completed/failed tasks from previous messages
            # NOTE: We no longer destroy .context_bus.json or .mailboxes
            # to preserve cross-round context for session continuity.
            self._archive_completed_tasks(board)

            # Submit task — persistent agents will claim it from the board
            task = board.create(full_description, required_role="planner")
            logger.info("Submitted channel task %s to persistent pool",
                        task.task_id)
            return task.task_id

        except Exception as e:
            logger.error("Failed to submit channel task: %s", e)
            return None

    def _ensure_agents_running(self, board):
        """Start or restart the persistent agent process pool.

        Called with self._orch_lock held.  On first call, archives stale
        tasks from previous server sessions and launches all agents.
        On subsequent calls, checks process health and restarts if needed.
        """
        if self._persistent_orch is None:
            from core.orchestrator import Orchestrator

            # Archive and clear stale tasks from previous server sessions
            try:
                from core.task_history import save_round
                old_data = board._read()
                if old_data:
                    save_round(old_data)
            except Exception:
                pass
            board.clear(force=True)

            self._persistent_orch = Orchestrator()
            # Extended idle timeout: agents stay alive between messages
            self._persistent_orch.config["max_idle_cycles"] = \
                self.AGENT_IDLE_CYCLES
            self._persistent_orch._launch_all()
            logger.info("Persistent agent pool started (%d processes)",
                        len(self._persistent_orch.procs))
            return

        # Check for hot-reload signal (new agent created via API)
        reload_signal = ".agent_reload_signal"
        if os.path.exists(reload_signal):
            try:
                os.remove(reload_signal)
                logger.info("Agent config changed — hot-reloading pool")
                # Graceful restart: let existing tasks finish, then relaunch
                for p in self._persistent_orch.procs:
                    if p.is_alive():
                        p.terminate()
                self._persistent_orch.procs.clear()
                # Re-read config and launch
                from core.orchestrator import Orchestrator
                self._persistent_orch = Orchestrator()
                self._persistent_orch.config["max_idle_cycles"] = \
                    self.AGENT_IDLE_CYCLES
                self._persistent_orch._launch_all()
                logger.info("Agent pool hot-reloaded (%d processes)",
                            len(self._persistent_orch.procs))
                return
            except Exception as e:
                logger.warning("Hot-reload failed: %s", e)

        # Check process health — restart pool if all agents exited
        alive = [p for p in self._persistent_orch.procs if p.is_alive()]
        if not alive:
            logger.info("All agent processes exited (idle timeout), "
                        "restarting pool")
            self._persistent_orch.procs.clear()
            self._persistent_orch._launch_all()
            logger.info("Agent pool restarted (%d processes)",
                        len(self._persistent_orch.procs))
        elif len(alive) < len(self._persistent_orch.procs):
            dead = [p.name for p in self._persistent_orch.procs
                    if not p.is_alive()]
            logger.warning("Agent processes died: %s (%d/%d alive)",
                           dead, len(alive),
                           len(self._persistent_orch.procs))
            self._persistent_orch.procs = alive

    @staticmethod
    def _archive_completed_tasks(board):
        """Archive completed/failed tasks without clearing the entire board.

        Unlike the previous board.clear(force=True), this preserves any
        pending or in-progress tasks so persistent agents can keep working.
        """
        try:
            from core.task_history import save_round
            data = board._read()
            done = {k: v for k, v in data.items()
                    if v.get("status") in
                    ("completed", "failed", "cancelled")}
            if done:
                save_round(done)
                with board.lock:
                    fresh = board._read()
                    for tid in done:
                        fresh.pop(tid, None)
                    board._write(fresh)
        except Exception as e:
            logger.debug("Task archival failed (non-critical): %s", e)

    async def _wait_for_result(self, task_id: str,
                                msg: ChannelMessage,
                                adapter: ChannelAdapter) -> Optional[str]:
        """Poll TaskBoard until the task completes or times out."""
        from core.task_board import TaskBoard

        start = time.time()
        status_sent = False

        while time.time() - start < TASK_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)

            board = TaskBoard()
            data = board._read()

            # Check if all tasks are done (including subtasks)
            if not data:
                continue

            active_states = {"pending", "claimed", "review", "critique",
                             "blocked", "paused", "synthesizing"}
            has_active = any(
                t.get("status") in active_states for t in data.values())

            if not has_active:
                # All done — prefer root task result (Leo's synthesis)
                root = data.get(task_id)
                if root and root.get("result"):
                    return self._clean_result(root["result"])
                # Maybe closeout hasn't written yet, wait briefly
                await asyncio.sleep(2)
                data = board._read()
                root = data.get(task_id)
                if root and root.get("result"):
                    return self._clean_result(root["result"])
                # Fallback to collected executor results
                result = board.collect_results(task_id)
                return self._clean_result(result) if result else "(无结果)"

            # Send typing indicator after 30s (less noisy)
            elapsed = time.time() - start
            if not status_sent and elapsed > STATUS_INTERVAL:
                await adapter.send_typing(msg.chat_id)
                status_sent = True

        return None  # timeout

    @staticmethod
    def _clean_result(text: str) -> str:
        """Strip internal metadata from result before sending to user.

        Removes: agent/task HTML comments, thinking tags, raw JSON task
        delegations, separator lines, and excessive blank lines.
        """
        import re

        # Remove <!-- agent:xxx task:xxx --> markers
        text = re.sub(r'<!--\s*agent:.*?-->', '', text)
        # Remove <think>...</think> reasoning traces
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        # Remove raw JSON task arrays (planner delegation output)
        # Matches: [ {"task": "...", "role": "..."} ]  or similar
        text = re.sub(
            r'```json\s*\n?\s*\[[\s\S]*?"(?:task|role)"[\s\S]*?\]\s*\n?```',
            '', text)
        text = re.sub(
            r'^\s*\[\s*\{[^}]*"(?:task|role)"[^}]*\}\s*\]\s*$',
            '', text, flags=re.MULTILINE)
        # Remove separator lines between merged results
        text = re.sub(r'\n---\n', '\n\n', text)
        # Collapse excessive blank lines
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    async def _health_monitor(self):
        """Periodically check adapter health and auto-reconnect dead ones."""
        while self._running:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

            for adapter in self.adapters:
                if not self._running:
                    break
                try:
                    alive = await adapter.health_check()
                    if not alive:
                        logger.warning(
                            "Health check failed for %s — triggering reconnect",
                            adapter.channel_name)
                        asyncio.create_task(adapter.reconnect())
                except Exception as e:
                    logger.error("Health check error for %s: %s",
                                 adapter.channel_name, e)

    async def _session_cleanup_loop(self):
        """Periodically clean up expired sessions (every 30 minutes)."""
        cleanup_interval = 1800  # 30 minutes
        while self._running:
            try:
                await asyncio.sleep(cleanup_interval)
            except asyncio.CancelledError:
                break
            try:
                removed = self._sessions.cleanup_expired()
                if removed:
                    logger.info("[session] periodic cleanup removed %d "
                                "expired sessions", removed)
            except Exception as e:
                logger.warning("[session] cleanup error: %s", e)

    def _get_adapter(self, channel_name: str) -> Optional[ChannelAdapter]:
        """Find adapter by channel name."""
        for adapter in self.adapters:
            if adapter.channel_name == channel_name:
                return adapter
        return None

    @staticmethod
    def _chunk_message(text: str, max_len: int) -> list[str]:
        """Split a message into chunks respecting platform limits.
        Tries to split at paragraph boundaries, then line boundaries."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            # Try to split at a paragraph boundary
            split_at = remaining.rfind("\n\n", 0, max_len)
            if split_at <= 0:
                # Try line boundary
                split_at = remaining.rfind("\n", 0, max_len)
            if split_at <= 0:
                # Try space
                split_at = remaining.rfind(" ", 0, max_len)
            if split_at <= 0:
                # Hard split
                split_at = max_len

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()

        return chunks


def start_channel_manager(config: dict) -> ChannelManager:
    """
    Start channel manager in a dedicated asyncio thread.
    Called from gateway.start_gateway().

    Always creates a ChannelManager instance (even if no channels are
    currently enabled) so that channels enabled later via the Dashboard
    can be hot-reloaded without restarting the gateway.
    """
    manager = ChannelManager(config)

    def _run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        manager._loop = loop
        try:
            loop.run_until_complete(manager.start())
            loop.run_forever()
        except Exception as e:
            logger.error("Channel manager event loop error: %s", e)
        finally:
            loop.close()

    thread = Thread(target=_run_loop, daemon=True, name="channel-manager")
    thread.start()
    logger.info("Channel manager thread started")
    return manager

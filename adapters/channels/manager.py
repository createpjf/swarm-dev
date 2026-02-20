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
    Manages all channel adapters and routes messages to/from the Swarm system.

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
        self._queue: asyncio.Queue[ChannelMessage] = asyncio.Queue()
        self._sessions = SessionStore()
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self):
        """Load and start all enabled channel adapters."""
        self._running = True
        self._loop = asyncio.get_event_loop()

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
        logger.info("ChannelManager started with %d adapter(s)", len(self.adapters))

    async def stop(self):
        """Stop all adapters gracefully."""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        for adapter in self.adapters:
            try:
                await adapter.stop()
                logger.info("Channel adapter stopped: %s", adapter.channel_name)
            except Exception as e:
                logger.error("Error stopping %s: %s", adapter.channel_name, e)

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
        """Callback from channel adapters — enqueue message for processing."""
        logger.info("[%s] message from %s (%s): %s",
                    msg.channel, msg.user_name, msg.chat_id, msg.text[:80])
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

            # Track session
            session = self._sessions.get_or_create(
                msg.channel, msg.chat_id, msg.user_id, msg.user_name)

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
        """Process a single channel message end-to-end."""
        # Send typing indicator
        await adapter.send_typing(msg.chat_id)

        # Submit task via Orchestrator
        task_id = await asyncio.get_event_loop().run_in_executor(
            None, self._submit_task, msg.text)

        if not task_id:
            await adapter.send_message(msg.chat_id, "❌ 任务提交失败")
            return

        self._sessions.update_task(msg.session_id, task_id)
        await adapter.send_message(
            msg.chat_id, f"🚀 任务已提交，正在处理...")

        # Poll for completion
        result = await self._wait_for_result(
            task_id, msg, adapter)

        if result:
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

    def _submit_task(self, description: str) -> Optional[str]:
        """Submit a task via Orchestrator (runs in thread pool)."""
        try:
            from core.orchestrator import Orchestrator
            from core.task_board import TaskBoard

            board = TaskBoard()

            # Archive old tasks for context persistence
            try:
                from core.task_history import save_round
                old_data = board._read()
                if old_data:
                    save_round(old_data)
            except Exception:
                pass

            # Clear state for new task
            board.clear(force=True)
            for fp in [".context_bus.json"]:
                if os.path.exists(fp):
                    os.remove(fp)
            import glob
            for fp in glob.glob(".mailboxes/*.jsonl"):
                os.remove(fp)

            # Submit and launch
            orch = Orchestrator()
            task_id = orch.submit(description)

            # Run agents in background thread
            def _run():
                try:
                    orch._launch_all()
                    orch._wait()
                except Exception as e:
                    logger.error("Channel task execution error: %s", e)

            t = Thread(target=_run, daemon=True)
            t.start()

            return task_id
        except Exception as e:
            logger.error("Failed to submit channel task: %s", e)
            return None

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
                             "blocked", "paused"}
            has_active = any(
                t.get("status") in active_states for t in data.values())

            if not has_active:
                # All done — collect results
                result = board.collect_results(task_id)
                if result:
                    return result
                # Maybe no results yet, keep waiting briefly
                await asyncio.sleep(2)
                result = board.collect_results(task_id)
                return result or "(无结果)"

            # Send status update after 30s
            elapsed = time.time() - start
            if not status_sent and elapsed > STATUS_INTERVAL:
                await adapter.send_typing(msg.chat_id)
                await adapter.send_message(
                    msg.chat_id, "⏳ 仍在处理中，请稍候...")
                status_sent = True

        return None  # timeout

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


def start_channel_manager(config: dict) -> Optional[ChannelManager]:
    """
    Start channel adapters in a dedicated asyncio thread.
    Called from gateway.start_gateway().
    Returns the ChannelManager instance (or None if no channels enabled).
    """
    channels_config = config.get("channels", {})
    if not channels_config:
        return None

    has_enabled = any(
        ch.get("enabled", False) for ch in channels_config.values()
    )
    if not has_enabled:
        return None

    manager = ChannelManager(config)

    def _run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
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

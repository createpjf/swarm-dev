"""
adapters/channels/slack.py
Slack channel adapter using slack-sdk Socket Mode (async, no public URL).

Install: pip install 'slack-sdk[socket-mode]>=3.27'

Features:
  - Socket Mode (no webhook / public URL needed)
  - Group mention filtering (configurable)
  - Thread support via thread_ts
  - DM + channel messages
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from .base import ChannelAdapter, ChannelMessage

logger = logging.getLogger(__name__)


class SlackAdapter(ChannelAdapter):
    """Slack bot adapter using slack-sdk Socket Mode."""

    def __init__(self, config: dict):
        super().__init__(config)
        self._web_client = None   # slack_sdk.web.async_client.AsyncWebClient
        self._handler = None      # AsyncSocketModeHandler
        self._bot_user_id: str = ""

    @property
    def channel_name(self) -> str:
        return "slack"

    async def start(self):
        """Initialize and start the Slack bot with Socket Mode."""
        from slack_sdk.web.async_client import AsyncWebClient
        from slack_sdk.socket_mode.aio import AsyncSocketModeClient
        from slack_sdk.socket_mode.async_listeners import (
            AsyncSocketModeRequestListener,
        )
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse

        bot_token_env = self.config.get("bot_token_env", "SLACK_BOT_TOKEN")
        bot_token = os.environ.get(bot_token_env, "")
        if not bot_token:
            raise ValueError(
                f"Slack bot token not found in env var: {bot_token_env}")

        app_token_env = self.config.get("app_token_env", "SLACK_APP_TOKEN")
        app_token = os.environ.get(app_token_env, "")
        if not app_token:
            raise ValueError(
                f"Slack app token not found in env var: {app_token_env}")

        self._web_client = AsyncWebClient(token=bot_token)

        # Get bot user ID for mention detection
        auth_resp = await self._web_client.auth_test()
        self._bot_user_id = auth_resp.get("user_id", "")
        bot_name = auth_resp.get("user", "slack-bot")
        logger.info("Slack bot connected: %s (user_id=%s)",
                     bot_name, self._bot_user_id)

        # Create Socket Mode client
        socket_client = AsyncSocketModeClient(
            app_token=app_token,
            web_client=self._web_client,
        )

        # Register event listener
        async def _event_listener(
            client: AsyncSocketModeClient,
            req: SocketModeRequest,
        ):
            # Acknowledge immediately
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id))

            if req.type == "events_api":
                event = req.payload.get("event", {})
                event_type = event.get("type", "")
                if event_type in ("message", "app_mention"):
                    await self._handle_event(event)

        socket_client.socket_mode_request_listeners.append(_event_listener)

        # Connect
        await socket_client.connect()
        self._handler = socket_client
        self._running = True

    async def stop(self):
        """Stop the Slack bot gracefully."""
        self._running = False
        if self._handler:
            try:
                await self._handler.close()
            except Exception as e:
                logger.warning("Slack shutdown error: %s", e)

    async def send_message(self, chat_id: str, text: str,
                           reply_to: str = "", **kwargs) -> str:
        """Send a message to a Slack channel or DM."""
        if not self._web_client:
            return ""

        try:
            resp = await self._web_client.chat_postMessage(
                channel=chat_id,
                text=text,
                thread_ts=reply_to if reply_to else None,
            )
            if resp.get("ok"):
                return resp.get("ts", "")
            logger.error("Slack send failed: %s", resp.get("error"))
            return ""
        except Exception as e:
            logger.error("Slack send error to %s: %s", chat_id, e)
            return ""

    async def send_typing(self, chat_id: str):
        """Slack has no persistent typing indicator API."""
        pass

    # ── Event Handler ──

    async def _handle_event(self, event: dict):
        """Process incoming Slack message event."""
        # Skip bot messages and subtypes (edits, joins, etc.)
        if event.get("bot_id") or event.get("subtype"):
            return

        text = event.get("text", "").strip()
        if not text:
            return

        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts", "")

        # Determine if group (channels start with C, groups with G)
        is_group = channel_id.startswith(("C", "G"))

        # Group mention filtering
        if is_group and self.config.get("mention_required", True):
            if not self._is_mentioned(text):
                return
            text = self._strip_mention(text)

        if not text.strip():
            return

        # Allowed channels check
        allowed = self.config.get("allowed_channels", [])
        if allowed and channel_id not in allowed:
            logger.debug("Slack channel %s not in allowed list", channel_id)
            return

        # Get user display name
        user_name = await self._get_user_name(user_id)

        # Build normalized message
        channel_msg = ChannelMessage(
            channel="slack",
            chat_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            text=text.strip(),
            message_id=ts,
            reply_to_message_id=thread_ts,
            is_group=is_group,
            raw=event,
        )

        if self._callback:
            await self._callback(channel_msg)

    # ── Helpers ──

    def _is_mentioned(self, text: str) -> bool:
        """Check if the bot is @mentioned in the message text."""
        if not self._bot_user_id:
            return False
        # Slack mentions look like <@U12345678>
        return f"<@{self._bot_user_id}>" in text

    def _strip_mention(self, text: str) -> str:
        """Remove @bot mention from the message text."""
        if self._bot_user_id:
            pattern = re.compile(
                rf'<@{re.escape(self._bot_user_id)}>\s*')
            text = pattern.sub('', text)
        return text.strip()

    async def _get_user_name(self, user_id: str) -> str:
        """Get a user's display name from Slack API."""
        if not self._web_client or not user_id:
            return user_id or "Unknown"
        try:
            resp = await self._web_client.users_info(user=user_id)
            if resp.get("ok"):
                user = resp.get("user", {})
                return (user.get("real_name")
                        or user.get("profile", {}).get("display_name")
                        or user.get("name")
                        or user_id)
        except Exception:
            pass
        return user_id

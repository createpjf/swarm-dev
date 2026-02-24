"""
adapters/channels/telegram.py
Telegram channel adapter using python-telegram-bot (async, long-polling).

Install: pip install python-telegram-bot>=21.0

Features:
  - Long-polling (no public URL needed)
  - Group mention filtering (configurable)
  - MarkdownV2 with fallback to plain text
  - /start, /status, /cancel commands
  - Voice/audio message transcription via Whisper
  - Auto-reconnect with exponential backoff
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from typing import Optional

from .base import ChannelAdapter, ChannelMessage

logger = logging.getLogger(__name__)

# Reconnect settings
_RECONNECT_BASE_DELAY = 5     # seconds
_RECONNECT_MAX_DELAY = 300    # 5 minutes cap
_RECONNECT_MAX_RETRIES = 0    # 0 = infinite retries


def _md_to_telegram_html(text: str) -> str:
    """Convert standard Markdown to Telegram-safe HTML.

    Telegram HTML supports: <b>, <i>, <u>, <s>, <code>, <pre>,
    <a href="">, <blockquote>, <tg-spoiler>.
    Unsupported tags are silently ignored by Telegram, so this is safe.
    """
    # ── Step 0: protect code blocks from further processing ──────────
    code_blocks: list[str] = []

    def _stash_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        # Escape HTML entities inside code
        code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if lang:
            placeholder = f"\x00CB{len(code_blocks)}\x00"
            code_blocks.append(
                f'<pre><code class="language-{lang}">{code}</code></pre>')
        else:
            placeholder = f"\x00CB{len(code_blocks)}\x00"
            code_blocks.append(f"<pre>{code}</pre>")
        return placeholder

    text = re.sub(r'```(\w*)\n([\s\S]*?)```', _stash_code_block, text)

    # Inline code: stash too
    inline_codes: list[str] = []

    def _stash_inline(m):
        code = m.group(1)
        code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        placeholder = f"\x00IC{len(inline_codes)}\x00"
        inline_codes.append(f"<code>{code}</code>")
        return placeholder

    text = re.sub(r'`([^`\n]+)`', _stash_inline, text)

    # ── Step 1: escape remaining HTML entities ───────────────────────
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # ── Step 2: Markdown → HTML conversions ──────────────────────────
    # Headers ## → bold (Telegram has no <h1>)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # Bold **text** → <b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)

    # Italic *text* (not inside bold) → <i>
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', text)

    # Italic _text_ → <i>
    text = re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'<i>\1</i>', text)

    # Strikethrough ~~text~~ → <s>
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # Links [text](url) → <a href="">
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # Unordered lists  - item / * item → • item
    text = re.sub(r'^[\s]*[-*]\s+', '• ', text, flags=re.MULTILINE)

    # Horizontal rules --- / *** / ___ → ——————
    text = re.sub(r'^[-*_]{3,}\s*$', '——————', text, flags=re.MULTILINE)

    # Block quotes > text → │ text
    text = re.sub(r'^&gt;\s?(.*)$', r'│ \1', text, flags=re.MULTILINE)

    # ── Step 3: restore stashed code ─────────────────────────────────
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    # Clean up excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


class TelegramAdapter(ChannelAdapter):
    """Telegram bot adapter using python-telegram-bot with auto-reconnect."""

    def __init__(self, config: dict):
        super().__init__(config)
        self._app = None  # telegram.ext.Application
        self._bot_username: str = ""
        self._reconnect_task: Optional[asyncio.Task] = None
        self._consecutive_failures: int = 0
        self._intentional_stop: bool = False

    @property
    def channel_name(self) -> str:
        return "telegram"

    async def start(self):
        """Initialize and start the Telegram bot with long-polling."""
        self._intentional_stop = False
        await self._connect()

    async def _connect(self):
        """Internal: build app, register handlers, start polling."""
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            MessageHandler,
            filters,
        )

        token_env = self.config.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
        token = os.environ.get(token_env, "")
        if not token:
            raise ValueError(
                f"Telegram bot token not found in env var: {token_env}")

        self._app = (
            ApplicationBuilder()
            .token(token)
            .build()
        )

        # Register handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text_message,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.VOICE | filters.AUDIO,
                self._on_voice_message,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.IMAGE,
                self._on_photo_message,
            )
        )

        # Global error handler — catches network errors during polling
        self._app.add_error_handler(self._on_error)

        # Get bot info
        await self._app.initialize()
        bot_info = await self._app.bot.get_me()
        self._bot_username = bot_info.username or ""
        logger.info("Telegram bot connected: @%s", self._bot_username)

        # Start polling in background
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        self._running = True
        self._consecutive_failures = 0

    async def _on_error(self, update, context):
        """Handle errors from python-telegram-bot polling."""
        error = context.error
        from telegram.error import NetworkError, TimedOut, RetryAfter

        if isinstance(error, RetryAfter):
            logger.warning("Telegram rate-limited, retry after %ss",
                           error.retry_after)
            return  # library handles this automatically
        if isinstance(error, (NetworkError, TimedOut)):
            logger.warning("Telegram network error: %s", error)
            return  # library retries automatically for transient errors

        # Unknown error — log it
        logger.error("Telegram error: %s", error, exc_info=error)

    async def stop(self):
        """Stop the Telegram bot gracefully."""
        self._intentional_stop = True
        self._running = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        await self._shutdown_app()

    async def _shutdown_app(self):
        """Shutdown the current telegram application instance."""
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("Telegram shutdown error: %s", e)
            finally:
                self._app = None

    async def reconnect(self):
        """Force a reconnect cycle: shutdown then re-connect with backoff."""
        if self._intentional_stop:
            return
        logger.info("Telegram reconnecting...")
        self._running = False
        await self._shutdown_app()

        delay = _RECONNECT_BASE_DELAY
        attempt = 0

        while not self._intentional_stop:
            attempt += 1
            self._consecutive_failures += 1
            try:
                logger.info("Telegram reconnect attempt #%d (delay=%ds)",
                            attempt, delay)
                await self._connect()
                logger.info("Telegram reconnected successfully after %d attempt(s)",
                            attempt)
                return
            except Exception as e:
                logger.error("Telegram reconnect failed: %s", e)
                if self._intentional_stop:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def health_check(self) -> bool:
        """Return True if the bot is connected and polling is alive."""
        if not self._running or not self._app:
            return False
        try:
            await self._app.bot.get_me()
            return True
        except Exception:
            return False

    async def send_message(self, chat_id: str, text: str,
                           reply_to: str = "", **kwargs) -> str:
        """Send a message to a Telegram chat."""
        if not self._app:
            return ""

        try:
            # Convert Markdown → Telegram HTML and send
            msg = await self._app.bot.send_message(
                chat_id=int(chat_id),
                text=_md_to_telegram_html(text),
                parse_mode="HTML",
                reply_to_message_id=int(reply_to) if reply_to else None,
            )
            return str(msg.message_id)
        except Exception:
            # Fallback: strip markdown symbols and send plain text
            try:
                plain = re.sub(r'[*_`#~\[\]()>]', '', text)
                msg = await self._app.bot.send_message(
                    chat_id=int(chat_id),
                    text=plain,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                )
                return str(msg.message_id)
            except Exception as e:
                logger.error("Telegram send failed to %s: %s", chat_id, e)
                return ""

    # File type → Telegram API method mapping
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    _AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".flac", ".aac"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    _MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Telegram limit

    async def send_file(self, chat_id: str, file_path: str,
                        caption: str = "", reply_to: str = "",
                        **kwargs) -> str:
        """Send a file via Telegram with automatic type detection and retry.

        Detects file type by extension and uses the appropriate Telegram
        API method (send_photo, send_audio, send_video, send_document).
        Retries up to 3 times with exponential backoff on transient errors.
        """
        if not self._app:
            return ""
        if not os.path.isfile(file_path):
            logger.error("Telegram send_file: file not found: %s", file_path)
            return ""

        # Check file size
        file_size = os.path.getsize(file_path)
        if file_size > self._MAX_FILE_SIZE:
            logger.warning("Telegram send_file: file too large (%d MB > 50 MB): %s",
                           file_size // (1024*1024), file_path)
            # Still try — Telegram Bot API may accept via URL for premium bots
            # but warn the user via caption
            if not caption:
                caption = ""
            caption = f"⚠️ File is {file_size//(1024*1024)}MB (Telegram limit: 50MB)\n{caption}"

        ext = os.path.splitext(file_path)[1].lower()
        cap = caption[:1024] if caption else None
        reply_id = int(reply_to) if reply_to else None

        # Retry with exponential backoff
        last_error = None
        for attempt in range(3):
            if attempt > 0:
                import asyncio
                await asyncio.sleep(2 ** attempt)  # 2s, 4s backoff
                logger.info("Telegram send_file retry %d/3 for %s",
                            attempt + 1, os.path.basename(file_path))
            try:
                msg = await self._send_file_by_type(
                    chat_id, file_path, ext, cap, reply_id)
                logger.info("Telegram sent %s (%s) to %s (msg %s)",
                            os.path.basename(file_path), ext or "doc",
                            chat_id, msg.message_id)
                return str(msg.message_id)
            except Exception as e:
                last_error = e
                logger.warning("Telegram send_file attempt %d failed: %s",
                               attempt + 1, e)

        logger.error("Telegram send_file failed after 3 attempts: %s", last_error)
        # Fallback to base class (sends filename as text)
        return await super().send_file(chat_id, file_path, caption, reply_to)

    async def _send_file_by_type(self, chat_id, file_path, ext, caption, reply_id):
        """Dispatch to the correct Telegram API based on file extension."""
        bot = self._app.bot
        cid = int(chat_id)

        with open(file_path, "rb") as f:
            if ext in self._IMAGE_EXTS:
                return await bot.send_photo(
                    chat_id=cid, photo=f,
                    caption=caption,
                    reply_to_message_id=reply_id)
            elif ext in self._AUDIO_EXTS:
                return await bot.send_audio(
                    chat_id=cid, audio=f,
                    filename=os.path.basename(file_path),
                    caption=caption,
                    reply_to_message_id=reply_id)
            elif ext in self._VIDEO_EXTS:
                return await bot.send_video(
                    chat_id=cid, video=f,
                    filename=os.path.basename(file_path),
                    caption=caption,
                    reply_to_message_id=reply_id)
            else:
                return await bot.send_document(
                    chat_id=cid, document=f,
                    filename=os.path.basename(file_path),
                    caption=caption,
                    reply_to_message_id=reply_id)

    async def send_audio(self, chat_id: str, file_path: str,
                         caption: str = "", reply_to: str = "",
                         as_voice: bool = True, **kwargs) -> str:
        """Send audio/voice message via Telegram.

        Args:
            chat_id: Telegram chat ID
            file_path: Path to audio file (mp3, ogg, wav)
            caption: Optional caption
            reply_to: Message ID to reply to
            as_voice: Send as voice message (OGG) or audio file
        """
        if not self._app:
            return ""
        if not os.path.isfile(file_path):
            logger.error("Telegram send_audio: file not found: %s", file_path)
            return ""

        try:
            ext = os.path.splitext(file_path)[1].lower()

            if as_voice:
                # Voice messages must be OGG/Opus — convert if needed
                ogg_path = file_path
                if ext not in (".ogg", ".oga", ".opus"):
                    ogg_path = await self._convert_to_ogg(file_path)
                    if not ogg_path:
                        # Fallback: send as audio file
                        return await self._send_audio_file(
                            chat_id, file_path, caption, reply_to)

                with open(ogg_path, "rb") as f:
                    msg = await self._app.bot.send_voice(
                        chat_id=int(chat_id),
                        voice=f,
                        caption=caption[:1024] if caption else None,
                        reply_to_message_id=int(reply_to) if reply_to else None,
                    )
                # Clean up temp OGG if we converted
                if ogg_path != file_path:
                    try:
                        os.remove(ogg_path)
                    except OSError:
                        pass
            else:
                msg = await self._send_audio_file(
                    chat_id, file_path, caption, reply_to)
                return msg

            logger.info("Telegram sent voice to %s (msg %s)",
                        chat_id, msg.message_id)
            return str(msg.message_id)

        except Exception as e:
            logger.error("Telegram send_audio failed: %s", e)
            # Fallback to document
            return await self.send_file(chat_id, file_path, caption, reply_to)

    async def _send_audio_file(self, chat_id: str, file_path: str,
                               caption: str = "", reply_to: str = "") -> str:
        """Send as audio file (not voice message)."""
        try:
            with open(file_path, "rb") as f:
                msg = await self._app.bot.send_audio(
                    chat_id=int(chat_id),
                    audio=f,
                    filename=os.path.basename(file_path),
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                )
            return str(msg.message_id)
        except Exception as e:
            logger.error("Telegram send_audio_file failed: %s", e)
            return ""

    async def _convert_to_ogg(self, input_path: str) -> str | None:
        """Convert audio to OGG/Opus for voice messages (requires ffmpeg)."""
        import shutil
        if not shutil.which("ffmpeg"):
            logger.warning("ffmpeg not found — cannot convert to OGG voice")
            return None

        import tempfile
        import asyncio
        ogg_path = tempfile.mktemp(suffix=".ogg")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", input_path, "-c:a", "libopus",
                "-b:a", "64k", "-vbr", "on", "-y", ogg_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode == 0 and os.path.isfile(ogg_path):
                return ogg_path
        except Exception as e:
            logger.warning("OGG conversion failed: %s", e)
        return None

    async def send_typing(self, chat_id: str):
        """Send typing indicator."""
        if self._app:
            try:
                await self._app.bot.send_chat_action(
                    chat_id=int(chat_id), action="typing")
            except Exception:
                pass

    # ── Handlers ──

    async def _on_text_message(self, update, context):
        """Handle incoming text messages."""
        if not update.effective_message or not update.effective_chat:
            return

        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user

        text = msg.text or ""
        is_group = chat.type in ("group", "supergroup")

        # Group mention filtering
        if is_group and self.config.get("mention_required", True):
            if not self._is_mentioned(update):
                return  # Ignore messages without @mention in groups
            # Remove the @mention from the text
            text = self._strip_mention(text)

        if not text.strip():
            return

        # User allowlist check
        allowed = self.config.get("allowed_users", [])
        if allowed and str(user.id) not in [str(u) for u in allowed]:
            logger.debug("Telegram user %s not in allowed list", user.id)
            return

        # Build normalized message
        channel_msg = ChannelMessage(
            channel="telegram",
            chat_id=str(chat.id),
            user_id=str(user.id) if user else "unknown",
            user_name=self._get_display_name(user),
            text=text.strip(),
            message_id=str(msg.message_id),
            reply_to_message_id=(
                str(msg.reply_to_message.message_id)
                if msg.reply_to_message else ""
            ),
            is_group=is_group,
            raw=update,
        )

        if self._callback:
            await self._callback(channel_msg)
        else:
            logger.warning("Telegram text message dropped: callback not set (update %d)",
                           update.update_id)

    async def _on_voice_message(self, update, context):
        """Handle incoming voice/audio messages — download + transcribe via Whisper."""
        if not update.effective_message or not update.effective_chat:
            return

        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user

        is_group = chat.type in ("group", "supergroup")

        # Group mention: voice in groups is always processed (can't @mention in voice)
        # But still check user allowlist
        allowed = self.config.get("allowed_users", [])
        if allowed and str(user.id) not in [str(u) for u in allowed]:
            logger.debug("Telegram voice from user %s not in allowed list", user.id)
            return

        # Get the file object (voice or audio)
        voice = msg.voice or msg.audio
        if not voice:
            return

        # Send "processing" feedback
        try:
            await msg.reply_text("🎙️ Transcribing voice message...")
        except Exception:
            pass

        # Download the audio file
        try:
            tg_file = await voice.get_file()
            ext = ".ogg"  # Telegram voice messages are OGG/Opus
            if msg.audio:
                # Audio files might have other formats
                mime = msg.audio.mime_type or ""
                if "mp3" in mime:
                    ext = ".mp3"
                elif "mp4" in mime or "m4a" in mime:
                    ext = ".m4a"
                elif "wav" in mime:
                    ext = ".wav"
                elif "flac" in mime:
                    ext = ".flac"

            tmp = tempfile.NamedTemporaryFile(
                suffix=ext, prefix="cleo_voice_", delete=False)
            tmp_path = tmp.name
            tmp.close()

            await tg_file.download_to_drive(tmp_path)
            logger.info("Voice file downloaded: %s (%.1f KB)",
                        tmp_path, os.path.getsize(tmp_path) / 1024)
        except Exception as e:
            logger.error("Failed to download voice file: %s", e)
            try:
                await msg.reply_text(f"Failed to download voice file: {e}")
            except Exception:
                pass
            return

        # Transcribe via Whisper
        try:
            from core.tools import _handle_transcribe
            result = _handle_transcribe(file_path=tmp_path)

            if result.get("ok"):
                text = result["text"]
                if not text.strip():
                    await msg.reply_text("(Voice message was empty or inaudible)")
                    return

                logger.info("Voice transcribed: %d chars from %s",
                            len(text), os.path.basename(tmp_path))

                # Forward transcribed text as a normal message
                caption = msg.caption or ""
                combined = f"{text}\n{caption}".strip() if caption else text

                channel_msg = ChannelMessage(
                    channel="telegram",
                    chat_id=str(chat.id),
                    user_id=str(user.id) if user else "unknown",
                    user_name=self._get_display_name(user),
                    text=combined,
                    message_id=str(msg.message_id),
                    reply_to_message_id=(
                        str(msg.reply_to_message.message_id)
                        if msg.reply_to_message else ""
                    ),
                    is_group=is_group,
                    attachments=[{"type": "voice_transcription",
                                  "original_file": tmp_path,
                                  "duration": getattr(voice, "duration", 0)}],
                    raw=update,
                )

                if self._callback:
                    await self._callback(channel_msg)
                else:
                    logger.warning("Telegram voice message dropped: callback not set")
            else:
                error = result.get("error", "unknown error")
                logger.error("Whisper transcription failed: %s", error)
                await msg.reply_text(f"Transcription failed: {error}")
        except ImportError:
            logger.error("core.tools not available for transcription")
            await msg.reply_text("Voice transcription not available (tools module missing)")
        except Exception as e:
            logger.error("Voice transcription error: %s", e)
            try:
                await msg.reply_text(f"Voice transcription error: {e}")
            except Exception:
                pass
        finally:
            # Clean up temp file
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass

    async def _on_photo_message(self, update, context):
        """Handle incoming photo messages — download and pass as attachment."""
        if not update.effective_message or not update.effective_chat:
            return

        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user

        is_group = chat.type in ("group", "supergroup")

        # Group mention filter for photo messages with caption
        if is_group and self.config.get("mention_required", True):
            if not self._is_mentioned(update):
                return

        # User allowlist check
        allowed = self.config.get("allowed_users", [])
        if allowed and str(user.id) not in [str(u) for u in allowed]:
            logger.debug("Telegram photo from user %s not in allowed list",
                         user.id)
            return

        # Get the best-quality photo (last in list = largest)
        photo = None
        if msg.photo:
            photo = msg.photo[-1]  # Highest resolution
        elif msg.document and msg.document.mime_type and \
                msg.document.mime_type.startswith("image/"):
            photo = msg.document

        if not photo:
            return

        # Download photo to temp file
        try:
            tg_file = await photo.get_file()
            ext = ".jpg"
            if hasattr(photo, "mime_type") and photo.mime_type:
                mime = photo.mime_type
                if "png" in mime:
                    ext = ".png"
                elif "gif" in mime:
                    ext = ".gif"
                elif "webp" in mime:
                    ext = ".webp"

            tmp = tempfile.NamedTemporaryFile(
                suffix=ext, prefix="cleo_photo_", delete=False)
            tmp_path = tmp.name
            tmp.close()

            await tg_file.download_to_drive(tmp_path)
            file_size = os.path.getsize(tmp_path)
            logger.info("Photo downloaded: %s (%.1f KB)",
                        tmp_path, file_size / 1024)
        except Exception as e:
            logger.error("Failed to download photo: %s", e)
            try:
                await msg.reply_text(f"Failed to download photo: {e}")
            except Exception:
                pass
            return

        # Build caption text
        caption = msg.caption or ""
        if is_group:
            caption = self._strip_mention(caption)
        text = caption.strip() if caption.strip() else "[Photo received]"

        # Build normalized message with image attachment
        channel_msg = ChannelMessage(
            channel="telegram",
            chat_id=str(chat.id),
            user_id=str(user.id) if user else "unknown",
            user_name=self._get_display_name(user),
            text=text,
            message_id=str(msg.message_id),
            reply_to_message_id=(
                str(msg.reply_to_message.message_id)
                if msg.reply_to_message else ""
            ),
            is_group=is_group,
            attachments=[{
                "type": "image",
                "file_path": tmp_path,
                "file_size": file_size,
                "mime_type": f"image/{ext.lstrip('.')}",
            }],
            raw=update,
        )

        if self._callback:
            await self._callback(channel_msg)
        else:
            logger.warning("Telegram photo message dropped: callback not set")

    async def _cmd_start(self, update, context):
        """Handle /start command."""
        await update.effective_message.reply_text(
            "🤖 Cleo Agent ready! Send me a task and I'll process it.\n\n"
            "Commands:\n"
            "/status — Check task status\n"
            "/cancel — Cancel current task"
        )

    async def _cmd_status(self, update, context):
        """Handle /status command."""
        try:
            from core.task_board import TaskBoard
            board = TaskBoard()
            data = board._read()
            if not data:
                await update.effective_message.reply_text("No active tasks.")
                return

            lines = []
            for tid, t in data.items():
                status = t.get("status", "unknown")
                desc = t.get("description", "")[:50]
                emoji = {"pending": "⏳", "claimed": "🔄",
                         "completed": "✅", "failed": "❌"}.get(status, "❓")
                lines.append(f"{emoji} {desc}... [{status}]")

            await update.effective_message.reply_text(
                "📋 Task Board:\n" + "\n".join(lines[-10:]))
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}")

    async def _cmd_cancel(self, update, context):
        """Handle /cancel command — cancels all active tasks."""
        try:
            from core.task_board import TaskBoard
            board = TaskBoard()
            count = board.cancel_all()
            if count:
                await update.effective_message.reply_text(
                    f"🛑 已取消 {count} 个任务。")
            else:
                await update.effective_message.reply_text(
                    "ℹ️ 当前没有正在执行的任务。")
        except Exception as e:
            logger.error("Cancel command error: %s", e)
            await update.effective_message.reply_text(f"取消失败: {e}")

    # ── Helpers ──

    def _is_mentioned(self, update) -> bool:
        """Check if the bot is @mentioned in a group message."""
        msg = update.effective_message
        if not msg:
            return False

        # Check message entities for bot mention
        if msg.entities:
            for entity in msg.entities:
                if entity.type == "mention":
                    mention_text = msg.text[entity.offset:
                                            entity.offset + entity.length]
                    if mention_text.lower() == f"@{self._bot_username.lower()}":
                        return True
                elif entity.type == "text_mention":
                    # For users without usernames
                    bot_id = self._app.bot.id if self._app else None
                    if entity.user and entity.user.id == bot_id:
                        return True

        return False

    def _strip_mention(self, text: str) -> str:
        """Remove @bot_username from the message text."""
        if self._bot_username:
            pattern = re.compile(
                rf'@{re.escape(self._bot_username)}\s*', re.IGNORECASE)
            text = pattern.sub('', text)
        return text.strip()

    @staticmethod
    def _get_display_name(user) -> str:
        """Get a user's display name."""
        if not user:
            return "Unknown"
        if user.full_name:
            return user.full_name
        if user.username:
            return user.username
        return str(user.id)

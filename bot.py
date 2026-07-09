"""
bot.py
======
Telegram Mirror Pro — single-file version.

Monitors ONE Telegram source channel in real time (via Telethon, using a
user account session) and republishes new supported messages into your
own private group using your own bot (via the Bot API). Messages are
never forwarded — they are always sent as brand-new messages, with
usernames/links/text replaced along the way.

Everything — including all secrets AND all replacement rules — is
configured through environment variables (Railway Variables). Nothing
sensitive is hardcoded, and no separate replace.json file is needed.

------------------------------------------------------------------------
REQUIRED ENVIRONMENT VARIABLES (set these in Railway → Variables)
------------------------------------------------------------------------
API_ID            Telegram API ID from https://my.telegram.org
API_HASH          Telegram API hash from https://my.telegram.org
SESSION_STRING    Telethon session string (generate once, see bottom of
                  this file for a one-off snippet you can run locally)
BOT_TOKEN         Bot token from @BotFather. The bot must already be a
                  member of TARGET_CHAT with permission to send messages.
SOURCE_CHANNEL    The single channel to monitor, e.g. @examplechannel
TARGET_CHAT       The destination chat id, e.g. -1001234567890

------------------------------------------------------------------------
OPTIONAL ENVIRONMENT VARIABLES
------------------------------------------------------------------------
ADMIN_CHAT_ID         Chat id allowed to send /status and /reload.
                      Defaults to TARGET_CHAT if unset.
LOG_LEVEL             DEBUG / INFO / WARNING / ERROR (default: INFO)

USERNAME_REPLACEMENTS  JSON object mapping source -> target @handles.
                        Example:
                        {"@JoinCrypto":"@MyCryptoChannel","@CoinTelegraph":"@MyChannel"}

LINK_REPLACEMENTS      JSON object mapping bare t.me paths (no @, no
                        protocol) source -> target. Matches any
                        https://t.me/, http://t.me/, t.me/, telegram.me/
                        variant. Example:
                        {"JoinCrypto":"MyChannel","CoinTelegraph":"MyChannel"}

TEXT_REPLACEMENTS      JSON object of plain substring replacements,
                        applied last. Example:
                        {"CoinTelegraph":"My Brand","Crypto News":"Heisen News"}

All three replacement variables are optional; leave them unset (or "{}")
to disable that kind of replacement.
------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

logger = logging.getLogger("telegram-mirror")


# ============================================================================
# Configuration
# ============================================================================

def _get_env(name: str, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, default)
    if required and (value is None or value.strip() == ""):
        print(f"[config] Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value or ""


def _get_json_env(name: str) -> dict:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("must be a JSON object")
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Environment variable %s is not valid JSON (%s); ignoring it", name, exc)
        return {}


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_string: str
    bot_token: str
    source_channel: str
    target_chat: str
    log_level: str
    admin_chat_id: str


def load_settings() -> Settings:
    api_id_raw = _get_env("API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("[config] API_ID must be an integer", file=sys.stderr)
        sys.exit(1)

    return Settings(
        api_id=api_id,
        api_hash=_get_env("API_HASH"),
        session_string=_get_env("SESSION_STRING"),
        bot_token=_get_env("BOT_TOKEN"),
        source_channel=_get_env("SOURCE_CHANNEL"),
        target_chat=_get_env("TARGET_CHAT"),
        log_level=_get_env("LOG_LEVEL", required=False, default="INFO"),
        admin_chat_id=_get_env("ADMIN_CHAT_ID", required=False, default=""),
    )


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Telethon's own internals are extremely chatty at DEBUG (every network
    # packet, ping/pong, etc). Keep them quiet regardless of our own
    # LOG_LEVEL so that setting LOG_LEVEL=DEBUG shows *our* debug lines
    # (like the raw source message dump) without being buried under
    # thousands of low-level network log lines.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


# ============================================================================
# Replacement engine (rules come from env vars, reloadable via /reload)
# ============================================================================

_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_USERNAME_PATTERN = re.compile(r"@([A-Za-z0-9_]{4,32})")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_MD_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _strip_markup(text: str) -> str:
    """Best-effort plain-text fallback: drop HTML tags and collapse
    markdown-style [text](url) links down to 'text (url)', used only if
    a formatted send ever fails to parse."""
    text = _MD_LINK_PATTERN.sub(r"\1 (\2)", text)
    text = _HTML_TAG_PATTERN.sub("", text)
    text = text.replace("**", "").replace("__", "")
    return text


class ReplacementEngine:
    """Holds username/link/text replacement rules, loaded from env vars."""

    def __init__(self):
        self._usernames: dict[str, str] = {}
        self._links: dict[str, str] = {}
        self._text: dict[str, str] = {}
        self._footer_marker: str = ""
        self._footer_html: str = ""
        self.reload()

    def reload(self) -> None:
        """Re-read replacement rules from environment variables.

        Note: on most platforms (including Railway) changing an env var
        requires a redeploy/restart to take effect in the running
        process. /reload re-parses whatever values are currently loaded
        in this process's environment — use it after editing rules and
        restarting the service, or wire REPLACE rules to a redeploy hook
        if you need true hot-reload.
        """
        usernames = _get_json_env("USERNAME_REPLACEMENTS")
        links = _get_json_env("LINK_REPLACEMENTS")
        text = _get_json_env("TEXT_REPLACEMENTS")

        self._usernames = {self._norm(k): self._norm(v) for k, v in usernames.items()}
        self._links = {k.strip().lstrip("@"): v.strip().lstrip("@") for k, v in links.items()}
        self._text = dict(text)

        # Optional footer override: everything from FOOTER_MARKER (e.g.
        # "Powered by") to the end of the message is replaced wholesale
        # with a fixed footer built from your own bot/channel links —
        # this sidesteps needing to know the source's exact hidden URLs.
        self._footer_marker = os.getenv("FOOTER_MARKER", "Powered by").strip()
        bot_url = os.getenv("FOOTER_BOT_URL", "").strip()
        channel_url = os.getenv("FOOTER_CHANNEL_URL", "").strip()
        brand_text = os.getenv("FOOTER_BRAND_TEXT", "").strip()
        bot_label = os.getenv("FOOTER_BOT_LABEL", "BOT").strip()
        channel_label = os.getenv("FOOTER_CHANNEL_LABEL", "CHANNEL").strip()

        if bot_url and channel_url and brand_text:
            self._footer_html = (
                f"Powered by {brand_text}\n"
                f'<a href="{bot_url}">{bot_label}</a> | <a href="{channel_url}">{channel_label}</a>'
            )
        else:
            self._footer_html = ""

        logger.info(
            "Replacement rules loaded: %d usernames, %d links, %d text rules, footer override %s",
            len(self._usernames), len(self._links), len(self._text),
            "enabled" if self._footer_html else "disabled",
        )

    @staticmethod
    def _norm(value: str) -> str:
        value = value.strip()
        return value if value.startswith("@") else f"@{value}"

    def apply(self, text: str) -> str:
        if not text:
            return text
        text = self._replace_links(text)
        text = self._replace_usernames(text)
        text = self._replace_text(text)
        text = self._replace_footer(text)
        return text

    def _replace_links(self, text: str) -> str:
        def _sub(match: re.Match) -> str:
            target = self._links.get(match.group(1))
            return f"https://t.me/{target}" if target else match.group(0)

        return _LINK_PATTERN.sub(_sub, text)

    def _replace_usernames(self, text: str) -> str:
        def _sub(match: re.Match) -> str:
            handle = f"@{match.group(1)}"
            return self._usernames.get(handle, match.group(0))

        return _USERNAME_PATTERN.sub(_sub, text)

    def _replace_text(self, text: str) -> str:
        for source, target in self._text.items():
            if source:
                text = text.replace(source, target)
        return text

    def _replace_footer(self, text: str) -> str:
        if not self._footer_html or not self._footer_marker:
            return text
        idx = text.find(self._footer_marker)
        if idx == -1:
            return text
        return text[:idx] + self._footer_html


# ============================================================================
# Dedup + stats
# ============================================================================

class DedupeCache:
    def __init__(self, max_size: int = 5000):
        self._max_size = max_size
        self._seen: set[tuple[int, int]] = set()
        self._order: deque[tuple[int, int]] = deque()

    def already_mirrored(self, chat_id: int, message_id: int) -> bool:
        return (chat_id, message_id) in self._seen

    def mark_mirrored(self, chat_id: int, message_id: int) -> None:
        key = (chat_id, message_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self._max_size:
            self._seen.discard(self._order.popleft())


@dataclass
class Stats:
    started_at: float = field(default_factory=time.time)
    connected: bool = False
    messages_mirrored: int = 0
    stickers_mirrored: int = 0
    errors: int = 0

    def uptime_human(self) -> str:
        seconds = int(time.time() - self.started_at)
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)


# ============================================================================
# Bot API client (sending + admin command polling)
# ============================================================================

_API_ROOT = "https://api.telegram.org"
_STICKER_EXT_BY_MIME = {
    "image/webp": "sticker.webp",
    "application/x-tgsticker": "sticker.tgs",
    "video/webm": "sticker.webm",
}


class BotSender:
    def __init__(self, bot_token: str, target_chat: str):
        self._token = bot_token
        self.target_chat = target_chat
        self._session: Optional[aiohttp.ClientSession] = None
        self._update_offset = 0

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def _url(self, method: str) -> str:
        return f"{_API_ROOT}/bot{self._token}/{method}"

    async def _post(self, method: str, **kwargs) -> dict:
        assert self._session is not None
        for attempt in range(1, 6):
            try:
                async with self._session.post(self._url(method), **kwargs) as resp:
                    data = await resp.json()
                    if resp.status == 429:
                        retry_after = data.get("parameters", {}).get("retry_after", 5)
                        logger.warning("Bot API FloodWait: sleeping %ss (attempt %d)", retry_after, attempt)
                        await asyncio.sleep(retry_after + 0.5)
                        continue
                    if not data.get("ok", False):
                        logger.error("Bot API error on %s: %s", method, data)
                    return data
            except aiohttp.ClientError as exc:
                wait = min(2 ** attempt, 30)
                logger.warning("Network error calling %s (%s); retrying in %ss", method, exc, wait)
                await asyncio.sleep(wait)
        logger.error("Giving up on %s after repeated failures", method)
        return {"ok": False}

    async def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
        disable_preview: bool = False,
    ) -> bool:
        payload = {"chat_id": chat_id or self.target_chat, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if disable_preview:
            # Both are set for compatibility across Bot API versions:
            # link_preview_options is the current field, disable_web_page_preview
            # is the older one some clients/relays still expect.
            payload["link_preview_options"] = {"is_disabled": True}
            payload["disable_web_page_preview"] = True
        data = await self._post("sendMessage", json=payload)
        if not data.get("ok") and parse_mode:
            # Fall back to plain text if the formatted markup was invalid,
            # so a formatting glitch never silently drops the whole message.
            # Strip any tags/markdown syntax first so the fallback doesn't
            # show raw <b>, **, or [text](url) to the reader.
            logger.warning("sendMessage with parse_mode=%s failed, retrying as plain text", parse_mode)
            payload.pop("parse_mode", None)
            payload["text"] = _strip_markup(text)
            data = await self._post("sendMessage", json=payload)
        return bool(data.get("ok"))

    async def send_sticker(self, sticker_bytes: bytes, filename: str, chat_id: Optional[str] = None) -> bool:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id or self.target_chat))
        form.add_field("sticker", io.BytesIO(sticker_bytes), filename=filename)
        data = await self._post("sendSticker", data=form)
        return bool(data.get("ok"))

    async def delete_webhook(self) -> None:
        """Clear any previously-set webhook so getUpdates polling doesn't
        conflict with it (fixes 409 'terminated by other getUpdates request'
        errors caused by a leftover webhook from earlier testing)."""
        data = await self._post("deleteWebhook", json={"drop_pending_updates": False})
        if data.get("ok"):
            logger.info("Webhook cleared (if any was set)")
        else:
            logger.warning("Could not clear webhook: %s", data)

    async def get_updates(self, timeout: int = 25) -> Optional[list[dict]]:
        """Returns a list of updates on success (possibly empty), or None
        if the API call itself failed (e.g. bad token, network issue)."""
        payload = {"offset": self._update_offset, "timeout": timeout, "allowed_updates": ["message"]}
        data = await self._post("getUpdates", json=payload, timeout=aiohttp.ClientTimeout(total=timeout + 10))
        if not data.get("ok"):
            return None
        updates = data.get("result", [])
        if updates:
            self._update_offset = updates[-1]["update_id"] + 1
        return updates


# ============================================================================
# Telethon listener (source channel -> replacements -> bot sender)
# ============================================================================

class MirrorListener:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_string: str,
        source_channel: str,
        sender: BotSender,
        replacer: ReplacementEngine,
        dedupe: DedupeCache,
        stats: Stats,
    ):
        self._client = TelegramClient(StringSession(session_string), api_id, api_hash)
        # Use HTML as the formatting flavor: message.text will then return
        # e.g. <b>bold</b> and <a href="...">text</a>, which is exactly
        # what Telegram Bot API's parse_mode=HTML expects — avoiding the
        # markdown-flavor mismatches that show up as literal ** or _ signs.
        self._client.parse_mode = "html"
        self.source_channel = source_channel
        self._sender = sender
        self._replacer = replacer
        self._dedupe = dedupe
        self._stats = stats

    async def start(self) -> None:
        self._client.add_event_handler(self._on_new_message, events.NewMessage(chats=self.source_channel))

        backoff = 5
        first_connect = True
        while True:
            try:
                await self._client.start()
                self._stats.connected = True
                logger.info("Connected to Telegram. Monitoring source channel: %s", self.source_channel)
                backoff = 5
                if first_connect:
                    first_connect = False
                    await self._send_startup_confirmation()
                await self._client.run_until_disconnected()
            except (ConnectionError, OSError) as exc:
                logger.warning("Telegram connection lost (%s). Reconnecting in %ss", exc, backoff)
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected error in listener loop; will retry")
            finally:
                self._stats.connected = False

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _send_startup_confirmation(self) -> None:
        """On first connect, fetch the most recent message from the source
        channel and mirror it right away, so you get immediate visible
        proof the bot is deployed and working — without waiting for a new
        post to arrive."""
        try:
            messages = await self._client.get_messages(self.source_channel, limit=1)
            if not messages:
                logger.info("Source channel has no messages yet; skipping startup confirmation")
                return

            message = messages[0]
            chat_id = message.chat_id or 0

            if message.sticker is not None:
                logger.info("Mirroring most recent message (sticker) as startup confirmation")
                await self._handle_sticker(message)
            elif self._is_ignored_media(message):
                logger.info("Most recent source message is unsupported media; sending a text notice instead")
                await self._sender.send_message(
                    "✅ Bot deployed and connected. The most recent source message "
                    "is an unsupported media type, so it wasn't mirrored, but the "
                    "listener is live and watching for new messages."
                )
            elif message.text:
                logger.info("Mirroring most recent message as startup confirmation")
                await self._handle_text(message)
            else:
                await self._sender.send_message("✅ Bot deployed and connected. Listening for new messages now.")

            self._dedupe.mark_mirrored(chat_id, message.id)
        except Exception:  # noqa: BLE001 - startup confirmation must never crash the app
            logger.exception("Failed to send startup confirmation message")

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        message = event.message
        chat_id = event.chat_id or 0

        if self._dedupe.already_mirrored(chat_id, message.id):
            return

        try:
            if message.sticker is not None:
                logger.info("Sticker detected (msg id=%s)", message.id)
                await self._handle_sticker(message)
            elif self._is_ignored_media(message):
                logger.debug("Ignoring unsupported media message id=%s", message.id)
                return
            elif message.text:
                logger.info("Text message detected (msg id=%s)", message.id)
                await self._handle_text(message)
            else:
                return

            self._dedupe.mark_mirrored(chat_id, message.id)
        except FloodWaitError as exc:
            logger.warning("FloodWait from Telegram: sleeping %ss", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
        except Exception:  # noqa: BLE001
            self._stats.errors += 1
            logger.exception("Error while mirroring message id=%s", message.id)

    @staticmethod
    def _is_ignored_media(message) -> bool:
        return bool(
            message.photo
            or message.video
            or message.gif
            or message.voice
            or message.audio
            or (message.document and not message.sticker)
            or message.poll
            or message.contact
            or message.geo
            or getattr(message, "story", None)
        )

    async def _handle_text(self, message) -> None:
        # message.text (with client.parse_mode = "html") returns the
        # message as HTML: <b>, <i>, and crucially <a href="..."> for any
        # hyperlinked/text-link entities. Our replacements run over that
        # HTML — including URLs inside href="..." — and we send with
        # parse_mode=HTML, which Telegram's Bot API parses natively with
        # no ambiguity (unlike legacy Markdown flavors).
        raw = message.text or message.raw_text or ""
        logger.debug("Raw source message (formatted): %s", raw)
        cleaned = self._replacer.apply(raw)
        if not cleaned.strip():
            return
        ok = await self._sender.send_message(cleaned, parse_mode="HTML", disable_preview=True)
        if ok:
            self._stats.messages_mirrored += 1
            logger.info("Message mirrored (msg id=%s)", message.id)
        else:
            logger.error("Failed to send mirrored message (msg id=%s)", message.id)

    async def _handle_sticker(self, message) -> None:
        doc = message.sticker
        mime = getattr(doc, "mime_type", "") or ""
        filename = _STICKER_EXT_BY_MIME.get(mime, "sticker.webp")

        sticker_bytes = await self._client.download_media(message, file=bytes)
        if not sticker_bytes:
            logger.error("Could not download sticker for message id=%s", message.id)
            return

        ok = await self._sender.send_sticker(sticker_bytes, filename)
        if ok:
            self._stats.stickers_mirrored += 1
            logger.info("Sticker mirrored (msg id=%s)", message.id)
        else:
            logger.error("Failed to send mirrored sticker (msg id=%s)", message.id)

    async def stop(self) -> None:
        await self._client.disconnect()


# ============================================================================
# Application wiring + admin commands (/status, /reload)
# ============================================================================

class MirrorApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stats = Stats()
        self.dedupe = DedupeCache()
        self.replacer = ReplacementEngine()
        self.sender = BotSender(settings.bot_token, settings.target_chat)
        self.listener = MirrorListener(
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            session_string=settings.session_string,
            source_channel=settings.source_channel,
            sender=self.sender,
            replacer=self.replacer,
            dedupe=self.dedupe,
            stats=self.stats,
        )
        self._admin_chat = settings.admin_chat_id or settings.target_chat
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        logger.info("Application started")
        await self.sender.start()
        await self.sender.delete_webhook()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                pass

        tasks = [
            asyncio.create_task(self.listener.start(), name="listener"),
            asyncio.create_task(self._admin_command_loop(), name="admin_commands"),
            asyncio.create_task(self._shutdown_event.wait(), name="shutdown_waiter"),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        await self._shutdown()

    async def _shutdown(self) -> None:
        logger.info("Shutdown initiated")
        await self.listener.stop()
        await self.sender.close()
        logger.info("Shutdown complete")

    async def _admin_command_loop(self) -> None:
        logger.info("Admin command listener started")
        while True:
            try:
                updates = await self.sender.get_updates(timeout=25)
            except Exception:  # noqa: BLE001
                logger.exception("Error polling admin commands; retrying shortly")
                await asyncio.sleep(5)
                continue

            if updates is None:
                # The API call failed (bad token, auth error, etc.) — the
                # error was already logged inside _post. Back off instead
                # of retrying dozens of times per second.
                await asyncio.sleep(10)
                continue

            for update in updates:
                message = update.get("message") or {}
                text = (message.get("text") or "").strip()
                chat_id = str(message.get("chat", {}).get("id", ""))

                if not text.startswith("/"):
                    continue
                if self._admin_chat and chat_id != str(self._admin_chat):
                    continue

                await self._handle_admin_command(text, chat_id)

    async def _handle_admin_command(self, text: str, chat_id: str) -> None:
        command = text.split()[0].lower()

        if command == "/status":
            reply = (
                "Running: yes\n"
                f"Connected: {'yes' if self.stats.connected else 'no'}\n"
                f"Source: {self.listener.source_channel}\n"
                f"Destination: {self.sender.target_chat}\n"
                f"Messages mirrored: {self.stats.messages_mirrored}\n"
                f"Stickers mirrored: {self.stats.stickers_mirrored}\n"
                f"Uptime: {self.stats.uptime_human()}"
            )
            await self.sender.send_message(reply, chat_id=chat_id)
            logger.info("Handled /status command")

        elif command == "/reload":
            self.replacer.reload()
            await self.sender.send_message("Replacement rules reloaded.", chat_id=chat_id)
            logger.info("Handled /reload command")


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    app = MirrorApp(settings)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()


# ============================================================================
# ONE-TIME LOCAL HELPER — run this separately, NOT part of the bot itself,
# to generate SESSION_STRING. Copy the block below into its own file
# (e.g. generate_session.py) and run it locally once:
#
#   from telethon.sync import TelegramClient
#   from telethon.sessions import StringSession
#   api_id = int(input("API_ID: "))
#   api_hash = input("API_HASH: ")
#   with TelegramClient(StringSession(), api_id, api_hash) as client:
#       print(client.session.save())
# ============================================================================

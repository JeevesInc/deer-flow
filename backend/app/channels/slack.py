"""Slack channel — connects via Socket Mode (no public IP needed)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from markdown_to_mrkdwn import SlackMarkdownConverter

from app.channels.base import Channel
from app.channels.message_bus import InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)

_slack_md_converter = SlackMarkdownConverter()


class SlackChannel(Channel):
    """Slack IM channel using Socket Mode (WebSocket, no public IP).

    Configuration keys (in ``config.yaml`` under ``channels.slack``):
        - ``bot_token``: Slack Bot User OAuth Token (xoxb-...).
        - ``app_token``: Slack App-Level Token (xapp-...) for Socket Mode.
        - ``allowed_users``: (optional) List of allowed Slack user IDs. Empty = allow all.
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="slack", bus=bus, config=config)
        self._socket_client = None
        self._web_client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._allowed_users: set[str] = set(config.get("allowed_users", []))
        self._own_bot_id: str | None = None
        # Dedup: track recently processed message timestamps to avoid
        # double-processing when Slack sends both 'message' and 'app_mention'
        self._seen_ts: dict[str, float] = {}  # ts -> wall-clock time

    async def start(self) -> None:
        if self._running:
            return

        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
        except ImportError:
            logger.error("slack-sdk is not installed. Install it with: uv add slack-sdk")
            return

        self._SocketModeResponse = SocketModeResponse

        bot_token = self.config.get("bot_token", "")
        app_token = self.config.get("app_token", "")

        if not bot_token or not app_token:
            logger.error("Slack channel requires bot_token and app_token")
            return

        self._web_client = WebClient(token=bot_token)
        self._socket_client = SocketModeClient(
            app_token=app_token,
            web_client=self._web_client,
        )
        self._loop = asyncio.get_event_loop()

        # Fetch our own bot_id so we can ignore only self-messages (not other bots like Gmail)
        try:
            auth_info = self._web_client.auth_test()
            self._own_bot_id = auth_info.get("bot_id")
            logger.info("Slack bot identity: user_id=%s, bot_id=%s", auth_info.get("user_id"), self._own_bot_id)
        except Exception:
            logger.warning("Could not fetch bot identity; falling back to rejecting all bot messages")

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_event)

        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Start socket mode in background thread
        asyncio.get_event_loop().run_in_executor(None, self._socket_client.connect)
        logger.info("Slack channel started")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        if self._socket_client:
            self._socket_client.close()
            self._socket_client = None
        logger.info("Slack channel stopped")

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        if not self._web_client:
            return

        kwargs: dict[str, Any] = {
            "channel": msg.chat_id,
            "text": _slack_md_converter.convert(msg.text),
        }
        if msg.thread_ts:
            kwargs["thread_ts"] = msg.thread_ts

        last_exc: Exception | None = None
        for attempt in range(_max_retries):
            try:
                await asyncio.to_thread(self._web_client.chat_postMessage, **kwargs)
                # Add a completion reaction to the thread root
                if msg.thread_ts:
                    await asyncio.to_thread(
                        self._add_reaction,
                        msg.chat_id,
                        msg.thread_ts,
                        "white_check_mark",
                    )
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _max_retries - 1:
                    delay = 2**attempt  # 1s, 2s
                    logger.warning(
                        "[Slack] send failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        logger.error("[Slack] send failed after %d attempts: %s", _max_retries, last_exc)
        # Add failure reaction on error
        if msg.thread_ts:
            try:
                await asyncio.to_thread(
                    self._add_reaction,
                    msg.chat_id,
                    msg.thread_ts,
                    "x",
                )
            except Exception:
                pass
        raise last_exc  # type: ignore[misc]

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        if not self._web_client:
            return False

        try:
            kwargs: dict[str, Any] = {
                "channel": msg.chat_id,
                "file": str(attachment.actual_path),
                "filename": attachment.filename,
                "title": attachment.filename,
            }
            if msg.thread_ts:
                kwargs["thread_ts"] = msg.thread_ts

            await asyncio.to_thread(self._web_client.files_upload_v2, **kwargs)
            logger.info("[Slack] file uploaded: %s to channel=%s", attachment.filename, msg.chat_id)
            return True
        except Exception:
            logger.exception("[Slack] failed to upload file: %s", attachment.filename)
            return False

    # -- internal ----------------------------------------------------------

    def _add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> None:
        """Add an emoji reaction to a message (best-effort, non-blocking)."""
        if not self._web_client:
            return
        try:
            self._web_client.reactions_add(
                channel=channel_id,
                timestamp=timestamp,
                name=emoji,
            )
        except Exception as exc:
            if "already_reacted" not in str(exc):
                logger.warning("[Slack] failed to add reaction %s: %s", emoji, exc)

    def _send_running_reply(self, channel_id: str, thread_ts: str) -> None:
        """Send a 'Working on it......' reply in the thread (called from SDK thread)."""
        if not self._web_client:
            return
        try:
            self._web_client.chat_postMessage(
                channel=channel_id,
                text=":hourglass_flowing_sand: Working on it...",
                thread_ts=thread_ts,
            )
            logger.info("[Slack] 'Working on it...' reply sent in channel=%s, thread_ts=%s", channel_id, thread_ts)
        except Exception:
            logger.exception("[Slack] failed to send running reply in channel=%s", channel_id)

    def _on_socket_event(self, client, req) -> None:
        """Called by slack-sdk for each Socket Mode event."""
        try:
            # Acknowledge the event
            response = self._SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)

            event_type = req.type
            if event_type != "events_api":
                return

            event = req.payload.get("event", {})
            etype = event.get("type", "")

            # Handle message events (DM or @mention)
            if etype in ("message", "app_mention"):
                self._handle_message_event(event)

        except Exception:
            logger.exception("Error processing Slack event")

    @staticmethod
    def _clean_slack_text(text: str) -> str:
        """Convert Slack mrkdwn text to plain text suitable for the agent.

        Slack wraps URLs as ``<URL>`` or ``<URL|label>``, user mentions as
        ``<@U1234>``, and channel references as ``<#C1234|channel-name>``.

        This method:
        - Unwraps URLs: ``<https://example.com>`` → ``https://example.com``
        - Unwraps labeled URLs: ``<https://example.com|Example>`` → ``https://example.com``
        - Preserves user mentions: ``<@U1234>`` stays as-is
        - Simplifies channel refs: ``<#C1234|general>`` → ``#general``
        """
        def _replace(m: re.Match) -> str:
            inner = m.group(1)
            # User mention: <@U1234> — keep as-is
            if inner.startswith("@"):
                return m.group(0)
            # Channel reference: <#C1234|channel-name>
            if inner.startswith("#"):
                parts = inner.split("|", 1)
                return f"#{parts[1]}" if len(parts) == 2 else inner
            # URL: <https://...|label> or <https://...>
            parts = inner.split("|", 1)
            return parts[0]

        return re.sub(r"<([^>]+)>", _replace, text)

    @staticmethod
    def _extract_attachment_urls(event: dict) -> list[str]:
        """Pull URLs from Slack attachments, files, and blocks that may not appear in text."""
        urls: list[str] = []
        # Attachments (link unfurls, bot-posted rich content)
        for att in event.get("attachments", []):
            for key in ("original_url", "from_url", "title_link", "app_unfurl_url"):
                url = att.get(key)
                if url and url not in urls:
                    urls.append(url)
        # Shared files (Google Drive shares, uploaded docs)
        for f in event.get("files", []):
            for key in ("url_private", "permalink", "url_private_download"):
                url = f.get(key)
                if url and url not in urls:
                    urls.append(url)
        return urls

    # Subtypes that indicate metadata events, not real messages
    _IGNORED_SUBTYPES = {
        "message_changed", "message_deleted", "message_replied",
        "channel_join", "channel_leave", "channel_topic", "channel_purpose",
        "channel_archive", "channel_unarchive", "ekm_access_denied",
        "me_message", "group_join", "group_leave",
    }

    def _handle_message_event(self, event: dict) -> None:
        # Ignore our own messages (loop prevention)
        msg_bot_id = event.get("bot_id")
        if msg_bot_id and self._own_bot_id and msg_bot_id == self._own_bot_id:
            return
        # If we couldn't determine our own bot_id, fall back to blocking all bot messages
        if msg_bot_id and not self._own_bot_id:
            return
        # Ignore non-message subtypes (edits, joins, etc.) but allow bot_message subtype
        subtype = event.get("subtype", "")
        if subtype in self._IGNORED_SUBTYPES:
            return

        # Dedup: Slack sends both 'message' and 'app_mention' for @mentions in channels.
        # Skip if we already processed this exact message timestamp.
        msg_ts = event.get("ts", "")
        now = time.monotonic()
        # Prune entries older than 10 seconds
        self._seen_ts = {ts: t for ts, t in self._seen_ts.items() if now - t < 10}
        if msg_ts in self._seen_ts:
            logger.debug("Dedup: skipping already-seen message ts=%s", msg_ts)
            return
        self._seen_ts[msg_ts] = now

        user_id = event.get("user", "") or event.get("bot_id", "")

        # Check allowed users (bot-forwarded messages bypass this check)
        if self._allowed_users and user_id not in self._allowed_users and not msg_bot_id:
            logger.debug("Ignoring message from non-allowed user: %s", user_id)
            return

        text = self._clean_slack_text(event.get("text", "")).strip()

        # Extract URLs from Slack attachments/files that aren't in the text body
        extra_urls = self._extract_attachment_urls(event)
        if extra_urls:
            text = text + "\n\nAttached links:\n" + "\n".join(extra_urls) if text else "\n".join(extra_urls)

        if not text:
            return

        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        if text.startswith("/"):
            msg_type = InboundMessageType.COMMAND
        else:
            msg_type = InboundMessageType.CHAT

        # topic_id: use thread_ts as the topic identifier.
        # For threaded messages, thread_ts is the root message ts (shared topic).
        # For non-threaded messages, thread_ts is the message's own ts (new topic).
        inbound = self._make_inbound(
            chat_id=channel_id,
            user_id=user_id,
            text=text,
            msg_type=msg_type,
            thread_ts=thread_ts,
        )
        inbound.topic_id = thread_ts

        if self._loop and self._loop.is_running():
            # Acknowledge with an eyes reaction
            self._add_reaction(channel_id, event.get("ts", thread_ts), "eyes")
            # Send "running" reply first (fire-and-forget from SDK thread)
            self._send_running_reply(channel_id, thread_ts)
            asyncio.run_coroutine_threadsafe(self.bus.publish_inbound(inbound), self._loop)

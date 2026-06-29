"""Slack channel — connects via Socket Mode (no public IP needed)."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
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
        self._own_user_id: str | None = None
        # Owner-notification config: DM the owner every time someone OTHER than
        # them messages the analyst. Falls back to env var if not in config.
        self._owner_user_id: str = (
            config.get("owner_user_id")
            or os.environ.get("SLACK_OWNER_USER_ID", "")
        ).strip()
        # Cache for Slack user_id → display_name lookups so we don't hammer
        # users.info on every inbound notification.
        self._user_name_cache: dict[str, tuple[str, str]] = {}
        # Dedup: track recently processed message timestamps to avoid
        # double-processing when Slack sends both 'message' and 'app_mention'
        self._seen_ts: dict[str, float] = {}  # ts -> wall-clock time
        self._seen_ts_lock = threading.Lock()
        # Progress tracking: map thread_ts -> (msg_ts, created_mono) of our
        # "Working on it..." message so we can edit it in-place with updates.
        # Entries are TTL-pruned after 10 minutes to prevent leaks.
        self._progress_message_ts: dict[str, tuple[str, float]] = {}
        self._PROGRESS_TTL = 600  # seconds
        # Assistant threads: track threads opened via the Slack assistant panel
        # so we can use assistant-specific UX (setStatus, suggested prompts, titles).
        # Persisted to disk so they survive gateway restarts.
        self._assistant_threads_path = Path(__file__).resolve().parent.parent.parent / ".deer-flow" / "_assistant_threads.json"
        self._assistant_threads: set[str] = self._load_assistant_threads()
        self._assistant_context: dict[str, str] = {}  # thread_ts -> viewing channel_id
        self._root_context_injected: set[str] = set()  # thread_ts values where root msg already injected

    def _load_assistant_threads(self) -> set[str]:
        """Load persisted assistant thread_ts values from disk."""
        try:
            if self._assistant_threads_path.exists():
                import json
                data = json.loads(self._assistant_threads_path.read_text())
                # Only keep entries from the last 7 days to avoid unbounded growth
                cutoff = time.time() - 7 * 86400
                threads = {ts for ts, t in data.items() if t > cutoff}
                logger.info("[Slack] Loaded %d assistant threads from disk", len(threads))
                return threads
        except Exception as exc:
            logger.warning("[Slack] Failed to load assistant threads: %s", exc)
        return set()

    def _save_assistant_threads(self) -> None:
        """Persist assistant thread_ts values to disk."""
        try:
            import json
            # Store as {thread_ts: timestamp} for TTL pruning
            now = time.time()
            data = {}
            # Load existing to preserve timestamps
            if self._assistant_threads_path.exists():
                try:
                    data = json.loads(self._assistant_threads_path.read_text())
                except Exception:
                    pass
            # Update with current set
            for ts in self._assistant_threads:
                if ts not in data:
                    data[ts] = now
            # Prune old entries
            cutoff = now - 7 * 86400
            data = {ts: t for ts, t in data.items() if t > cutoff and ts in self._assistant_threads}
            self._assistant_threads_path.parent.mkdir(parents=True, exist_ok=True)
            self._assistant_threads_path.write_text(json.dumps(data))
        except Exception as exc:
            logger.warning("[Slack] Failed to save assistant threads: %s", exc)

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

        # Fetch our own bot_id so we can ignore only self-messages (not other bots like Gmail).
        # Retry once on failure since this is critical for correct message filtering.
        for _attempt in range(2):
            try:
                auth_info = self._web_client.auth_test()
                self._own_bot_id = auth_info.get("bot_id")
                self._own_user_id = auth_info.get("user_id")
                logger.info("Slack bot identity: user_id=%s, bot_id=%s", self._own_user_id, self._own_bot_id)
                break
            except Exception:
                if _attempt == 0:
                    logger.warning("Could not fetch bot identity, retrying...")
                    await asyncio.sleep(2)
                else:
                    logger.error(
                        "Could not fetch bot identity after 2 attempts. "
                        "Will use user_id-based filtering as fallback (other bots will still be allowed through)."
                    )

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

        slack_text = _slack_md_converter.convert(msg.text)
        is_assistant = msg.thread_ts and self._is_assistant_thread(msg.thread_ts)

        # --- Non-final (progress) updates ---
        if not msg.is_final and msg.thread_ts:
            if is_assistant:
                # Assistant thread: update the native status indicator
                # Truncate for status (short descriptions work best)
                status_text = msg.text[:100].split("\n")[0] if msg.text else "Working..."
                await asyncio.to_thread(
                    self._set_assistant_status,
                    msg.chat_id,
                    msg.thread_ts,
                    status_text,
                )
                return

            entry = self._progress_message_ts.get(msg.thread_ts)
            if entry:
                progress_ts = entry[0]
                try:
                    await asyncio.to_thread(
                        self._web_client.chat_update,
                        channel=msg.chat_id,
                        ts=progress_ts,
                        text=slack_text,
                    )
                    return
                except Exception as exc:
                    logger.warning("[Slack] progress update failed, will post new message: %s", exc)
            # No cached progress message — post a new one and cache it
            try:
                resp = await asyncio.to_thread(
                    self._web_client.chat_postMessage,
                    channel=msg.chat_id,
                    text=slack_text,
                    thread_ts=msg.thread_ts,
                )
                new_ts = resp.get("ts")
                if new_ts:
                    self._progress_message_ts[msg.thread_ts] = (new_ts, time.monotonic())
                return
            except Exception as exc:
                logger.warning("[Slack] progress post failed: %s", exc)
                return  # Non-final updates are best-effort

        # --- Final message: post as a new reply ---
        kwargs: dict[str, Any] = {
            "channel": msg.chat_id,
            "text": slack_text,
        }
        if msg.thread_ts:
            kwargs["thread_ts"] = msg.thread_ts

        last_exc: Exception | None = None
        for attempt in range(_max_retries):
            try:
                await asyncio.to_thread(self._web_client.chat_postMessage, **kwargs)
                if msg.thread_ts:
                    if is_assistant:
                        # Assistant thread: clear status (auto-clears, but be explicit)
                        # and set a thread title from the response
                        try:
                            await asyncio.to_thread(
                                self._set_assistant_status,
                                msg.chat_id,
                                msg.thread_ts,
                                "",  # empty clears the status
                            )
                        except Exception:
                            pass
                        # Auto-title from the first ~60 chars of the response
                        title = (msg.text or "").strip().split("\n")[0][:60]
                        if title:
                            await asyncio.to_thread(
                                self._set_assistant_title,
                                msg.chat_id,
                                msg.thread_ts,
                                title,
                            )
                    else:
                        # Regular thread: clean up progress message + add checkmark
                        entry = self._progress_message_ts.pop(msg.thread_ts, None)
                        progress_ts = entry[0] if entry else None
                        if progress_ts:
                            try:
                                await asyncio.to_thread(
                                    self._web_client.chat_delete,
                                    channel=msg.chat_id,
                                    ts=progress_ts,
                                )
                            except Exception:
                                pass  # Best-effort cleanup
                        # Add completion reaction to the thread root
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
                    import random
                    delay = 2**attempt + random.uniform(0, 1)  # jitter to avoid thundering herd
                    logger.warning(
                        "[Slack] send failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        _max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        logger.error("[Slack] send failed after %d attempts: %s", _max_retries, last_exc)
        # Clean up progress tracking on failure
        if msg.thread_ts:
            self._progress_message_ts.pop(msg.thread_ts, None)
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

    def _resolve_user_identity(self, user_id: str) -> tuple[str, str]:
        """Best-effort Slack user_id → (display_name, title) with caching.

        Tries the configured web client first. Bot tokens usually lack the
        ``users:read`` scope, so on failure we fall back to a user-token
        client built from ``SLACK_USER_TOKEN`` (which has the scope and also
        exposes the profile title). Falls back to (user_id, "") on total
        failure so the owner notification still has *something* useful.
        """
        if not user_id:
            return ("", "")
        cached = self._user_name_cache.get(user_id)
        if cached is not None:
            return cached
        name, title = user_id, ""
        clients = []
        if self._web_client:
            clients.append(self._web_client)
        user_tok = os.environ.get("SLACK_USER_TOKEN", "").strip()
        if user_tok:
            try:
                from slack_sdk import WebClient
                clients.append(WebClient(token=user_tok))
            except Exception:
                pass
        for client in clients:
            try:
                resp = client.users_info(user=user_id)
                profile = resp.get("user", {}).get("profile", {})
                name = (
                    profile.get("real_name")
                    or profile.get("display_name")
                    or user_id
                )
                title = (profile.get("title") or "").strip()
                break
            except Exception as e:
                logger.debug("users_info failed for %s: %s", user_id, e)
                continue
        result = (name, title)
        self._user_name_cache[user_id] = result
        return result

    def _notify_owner_of_inbound(self, user_id: str, channel_id: str,
                                 thread_ts: str, text: str) -> None:
        """
        DM the owner whenever someone OTHER than them messages the analyst.
        Best-effort and silent on failure — must never block the inbound path.
        """
        if not self._owner_user_id or not self._web_client:
            return
        # Skip the owner's own messages and the bot's own messages.
        if not user_id:
            return
        if user_id == self._owner_user_id or user_id == self._own_user_id:
            return
        try:
            sender_name, sender_title = self._resolve_user_identity(user_id)
            who = f"*{sender_name}*"
            if sender_title:
                who += f" — {sender_title}"
            snippet = text[:280].replace("\n", " ").strip()
            notification = (
                f":eyes: {who} (`{user_id}`) just messaged the analyst.\n"
                f"> {snippet}"
            )
            self._web_client.chat_postMessage(
                channel=self._owner_user_id,
                text=notification,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as e:
            logger.warning("[Slack] owner notification failed for user_id=%s: %s", user_id, e)

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
        """Send a 'Working on it......' reply in the thread (called from SDK thread).

        Caches the posted message's ``ts`` so that subsequent progress updates
        can edit it in-place via ``chat_update``.
        """
        if not self._web_client:
            return
        try:
            resp = self._web_client.chat_postMessage(
                channel=channel_id,
                text=":hourglass_flowing_sand: Working on it...",
                thread_ts=thread_ts,
            )
            # Cache the ts so we can edit this message with progress updates
            msg_ts = resp.get("ts")
            if msg_ts:
                self._progress_message_ts[thread_ts] = (msg_ts, time.monotonic())
            logger.info("[Slack] 'Working on it...' reply sent in channel=%s, thread_ts=%s, msg_ts=%s", channel_id, thread_ts, msg_ts)
        except Exception:
            logger.exception("[Slack] failed to send running reply in channel=%s", channel_id)

    # -- Assistant-specific handlers ------------------------------------------

    _ASSISTANT_PROMPTS = [
        {"title": "Portfolio snapshot", "message": "What's the current portfolio balance and DPD rates?"},
        {"title": "Check my email", "message": "Any important emails I should know about?"},
        {"title": "Meeting prep", "message": "Prep dossiers for my next meeting"},
        {"title": "Borrowing base", "message": "Run the US and MX borrowing base for yesterday"},
    ]

    def _handle_assistant_thread_started(self, event: dict) -> None:
        """Handle the assistant_thread_started event.

        Fires when a user opens a new assistant thread from the Slack sidebar.
        We set suggested prompts and track this as an assistant thread.
        """
        assistant_thread = event.get("assistant_thread", {})
        channel_id = assistant_thread.get("channel_id", "")
        thread_ts = assistant_thread.get("thread_ts", "")
        context = assistant_thread.get("context", {})
        context_channel = context.get("channel_id", "")

        if not channel_id or not thread_ts:
            return

        self._assistant_threads.add(thread_ts)
        self._save_assistant_threads()
        if context_channel:
            self._assistant_context[thread_ts] = context_channel

        logger.info(
            "[Slack] Assistant thread started: channel=%s, thread_ts=%s, context_channel=%s",
            channel_id, thread_ts, context_channel,
        )

        if not self._web_client:
            return

        # Set suggested prompts
        try:
            self._web_client.assistant_threads_setSuggestedPrompts(
                channel_id=channel_id,
                thread_ts=thread_ts,
                prompts=self._ASSISTANT_PROMPTS,
            )
        except Exception as exc:
            logger.warning("[Slack] Failed to set suggested prompts: %s", exc)

    def _handle_assistant_context_changed(self, event: dict) -> None:
        """Handle the assistant_thread_context_changed event.

        Fires when the user navigates to a different channel while the
        assistant panel is open.  We update the stored context so the
        agent knows what the user is looking at.
        """
        assistant_thread = event.get("assistant_thread", {})
        thread_ts = assistant_thread.get("thread_ts", "")
        context = assistant_thread.get("context", {})
        context_channel = context.get("channel_id", "")

        if thread_ts and context_channel:
            self._assistant_context[thread_ts] = context_channel
            logger.info("[Slack] Assistant context changed: thread_ts=%s -> channel=%s", thread_ts, context_channel)

    def _set_assistant_status(self, channel_id: str, thread_ts: str, status: str = "Thinking...") -> None:
        """Set the native assistant loading indicator (instead of 'Working on it...' message)."""
        if not self._web_client:
            return
        try:
            self._web_client.assistant_threads_setStatus(
                channel_id=channel_id,
                thread_ts=thread_ts,
                status=status,
            )
        except Exception as exc:
            logger.warning("[Slack] Failed to set assistant status: %s", exc)

    def _set_assistant_title(self, channel_id: str, thread_ts: str, title: str) -> None:
        """Set the assistant thread title (shows in DM history)."""
        if not self._web_client:
            return
        try:
            self._web_client.assistant_threads_setTitle(
                channel_id=channel_id,
                thread_ts=thread_ts,
                title=title[:255],  # Slack title limit
            )
        except Exception as exc:
            logger.warning("[Slack] Failed to set assistant title: %s", exc)

    def _is_assistant_thread(self, thread_ts: str) -> bool:
        return thread_ts in self._assistant_threads

    # -- Socket Mode event router ------------------------------------------

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
            elif etype == "assistant_thread_started":
                self._handle_assistant_thread_started(event)
            elif etype == "assistant_thread_context_changed":
                self._handle_assistant_context_changed(event)

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
        """Pull URLs from Slack attachments and blocks that may not appear in text.

        Excludes Slack-hosted file URLs (files.slack.com) because those require
        bot-token authentication and are handled separately by _download_event_files
        which inlines text content or notes binary files.
        """
        urls: list[str] = []
        # Attachments (link unfurls, bot-posted rich content)
        for att in event.get("attachments", []):
            for key in ("original_url", "from_url", "title_link", "app_unfurl_url"):
                url = att.get(key)
                if url and url not in urls:
                    urls.append(url)
        # Shared files — only include external/public URLs, not Slack-hosted ones
        # (Slack file URLs need bot token auth and are already handled by download+inline)
        for f in event.get("files", []):
            # permalink_public is the only publicly accessible file URL
            url = f.get("permalink_public")
            if url and url not in urls:
                urls.append(url)
        return urls

    def _fetch_slack_message(self, channel_id: str, message_ts: str) -> str | None:
        """Fetch a Slack message's text content using the Web API.

        Uses conversations.history (or conversations.replies for threaded
        messages) to retrieve the actual message text.  Returns None on error.
        """
        if not self._web_client:
            return None
        try:
            # Try conversations.history with inclusive=true to get the exact message
            resp = self._web_client.conversations_history(
                channel=channel_id,
                latest=message_ts,
                inclusive=True,
                limit=1,
            )
            msgs = resp.get("messages", [])
            if msgs:
                return msgs[0].get("text", "")
        except Exception as exc:
            logger.debug("[Slack] conversations.history failed for %s/%s: %s", channel_id, message_ts, exc)
            # Try conversations.replies as fallback (message might be in a thread)
            try:
                resp = self._web_client.conversations_replies(
                    channel=channel_id,
                    ts=message_ts,
                    limit=1,
                )
                msgs = resp.get("messages", [])
                if msgs:
                    return msgs[0].get("text", "")
            except Exception as exc2:
                logger.debug("[Slack] conversations.replies also failed: %s", exc2)
        return None

    # Regex: https://WORKSPACE.slack.com/archives/CHANNEL_ID/pTIMESTAMP
    _SLACK_ARCHIVE_RE = re.compile(
        r"https?://[a-zA-Z0-9\-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d+)"
    )

    def _resolve_single_slack_url(self, url: str) -> str | None:
        """If *url* is a Slack archive link, fetch the message and return
        a text block like ``[Slack message from #channel]: ...``.
        Returns None if it's not a Slack URL or fetch fails.
        """
        m = self._SLACK_ARCHIVE_RE.search(url)
        if not m:
            return None
        channel_id = m.group(1)
        # Slack archive timestamps: p1234567890123456 -> 1234567890.123456
        raw_ts = m.group(2)
        message_ts = raw_ts[:10] + "." + raw_ts[10:] if len(raw_ts) > 10 else raw_ts
        msg_text = self._fetch_slack_message(channel_id, message_ts)
        if msg_text:
            cleaned = self._clean_slack_text(msg_text)
            logger.info("[Slack] Resolved archive URL -> %d chars from channel %s", len(cleaned), channel_id)
            return f"[Slack message from channel {channel_id}]:\n{cleaned}"
        return None

    def _fetch_root_message_text(self, channel_id: str, thread_ts: str) -> str | None:
        """Fetch the root message of a Slack thread, including any forwarded/attached content.

        This is used to give the agent context when a user replies in a thread
        whose root message was posted by the bot (e.g. email notifications).
        Extracts text from the message body, rich blocks (section, header), and
        attachments/forwarded content.
        """
        if not self._web_client:
            return None
        try:
            resp = self._web_client.conversations_history(
                channel=channel_id,
                latest=thread_ts,
                inclusive=True,
                limit=1,
            )
            msgs = resp.get("messages", [])
            if not msgs:
                return None
            root = msgs[0]
            parts = []

            # Primary text field
            body = self._clean_slack_text(root.get("text", "")).strip()
            if body:
                parts.append(body)

            # Extract text from rich blocks (header, section, context, rich_text)
            for block in root.get("blocks", []):
                btype = block.get("type", "")
                if btype in ("section", "header", "context"):
                    bt = block.get("text", {})
                    if isinstance(bt, dict):
                        block_text = self._clean_slack_text(bt.get("text", "")).strip()
                        if block_text and block_text not in body:
                            parts.append(block_text)
                    # Also check fields (section blocks can have multiple fields)
                    for field in block.get("fields", []):
                        if isinstance(field, dict):
                            ft = self._clean_slack_text(field.get("text", "")).strip()
                            if ft:
                                parts.append(ft)
                elif btype == "rich_text":
                    # rich_text blocks have elements with nested text
                    for elem in block.get("elements", []):
                        for sub in elem.get("elements", []):
                            if sub.get("type") == "text":
                                parts.append(sub.get("text", "").strip())

            # Forwarded content from attachments
            forwarded = self._extract_forwarded_text(root)
            if forwarded:
                parts.append(f"Forwarded message:\n{forwarded}")

            # Quoted blocks (modern Slack forwarding)
            quoted = self._extract_quoted_blocks_text(root)
            if quoted:
                parts.append(f"Forwarded message:\n{quoted}")

            text = "\n\n".join(p for p in parts if p)
            return text if text else None
        except Exception as exc:
            logger.warning("[Slack] Failed to fetch root message for thread_ts=%s: %s", thread_ts, exc)
            return None

    def _resolve_slack_archive_urls(self, text: str) -> str:
        """Find Slack archive URLs in *text* and append the resolved message content."""
        urls = self._SLACK_ARCHIVE_RE.findall(text)
        if not urls:
            return text
        resolved_parts = []
        for channel_id, raw_ts in urls:
            message_ts = raw_ts[:10] + "." + raw_ts[10:] if len(raw_ts) > 10 else raw_ts
            msg_text = self._fetch_slack_message(channel_id, message_ts)
            if msg_text:
                cleaned = self._clean_slack_text(msg_text)
                resolved_parts.append(f"[Slack message from channel {channel_id}]:\n{cleaned}")
        if resolved_parts:
            text = text + "\n\n" + "\n\n".join(resolved_parts)
        return text

    @staticmethod
    def _extract_quoted_blocks_text(event: dict) -> str:
        """Extract text from rich_text_quote elements in Slack blocks.

        When a user forwards/shares a Slack message, the quoted content
        often appears in top-level ``blocks`` as ``rich_text_quote`` elements
        rather than in ``attachments``.  This is common in newer Slack clients.
        We only extract ``rich_text_quote`` elements here — ``rich_text_section``
        elements duplicate what is already in ``event["text"]``.
        """
        parts: list[str] = []
        for block in event.get("blocks", []):
            if block.get("type") != "rich_text":
                continue
            for element in block.get("elements", []):
                if element.get("type") != "rich_text_quote":
                    continue
                text_parts: list[str] = []
                for child in element.get("elements", []):
                    child_type = child.get("type", "")
                    if child_type == "text":
                        text_parts.append(child.get("text", ""))
                    elif child_type == "link":
                        text_parts.append(child.get("text", "") or child.get("url", ""))
                    elif child_type == "user":
                        text_parts.append(f"<@{child.get('user_id', '')}>")
                    elif child_type == "channel":
                        text_parts.append(f"<#{child.get('channel_id', '')}>")
                chunk = "".join(text_parts).strip()
                if chunk:
                    parts.append(chunk)
        return "\n\n".join(parts)

    @staticmethod
    def _extract_forwarded_text(event: dict) -> str:
        """Extract text content from forwarded messages, emails, and rich attachments.

        When a user forwards a Slack message or email, the content appears in
        the ``attachments`` array with fields like ``text``, ``fallback``,
        ``pretext``, ``title``, and ``author_name``.  The parent message's
        ``text`` field is often empty, so without this extraction the forwarded
        content is silently lost.
        """
        parts: list[str] = []
        for att in event.get("attachments", []):
            # Skip link-unfurl attachments that only have a URL preview
            # (those are handled by _extract_attachment_urls)
            if att.get("is_app_unfurl") or att.get("service_name"):
                att_text = att.get("text", "").strip()
                if att_text:
                    parts.append(att_text)
                continue

            lines: list[str] = []
            author = att.get("author_name") or att.get("author_subname", "")
            if author:
                lines.append(f"[From {author}]")
            # Title (common in email forwards: subject line)
            title = att.get("title", "").strip()
            if title:
                lines.append(f"Subject: {title}")
            pretext = att.get("pretext", "").strip()
            if pretext:
                lines.append(pretext)
            att_text = att.get("text", "").strip()
            if not att_text:
                att_text = att.get("fallback", "").strip()
            if att_text:
                lines.append(att_text)
            # Footer (email signatures, timestamps)
            footer = att.get("footer", "").strip()
            if footer:
                lines.append(f"— {footer}")
            if lines:
                parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def _download_slack_file(self, file_info: dict) -> str | None:
        """Download a Slack-hosted file using the bot token.

        Returns the local file path on success, None on failure.
        Files are saved to ``.deer-flow/slack_downloads/``.
        """
        url = file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            logger.warning("[Slack] File has no download URL: keys=%s", list(file_info.keys()))
            return None

        name = file_info.get("name", "file")
        safe_name = re.sub(r'[^\w\-.]', '_', name)
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_name = f"{ts}_{safe_name}"

        backend_dir = Path(__file__).resolve().parent.parent.parent
        dl_dir = backend_dir / ".deer-flow" / "slack_downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        dest = dl_dir / safe_name

        # Try Slack SDK first (handles auth automatically), fall back to urllib
        if self._web_client:
            try:
                resp = self._web_client.api_call(
                    api_method="",
                    http_verb="GET",
                    api_url=url,
                )
                if resp.status_code == 200:
                    dest.write_bytes(resp.data)
                    size_kb = len(resp.data) / 1024
                    logger.info("[Slack] Downloaded file via SDK: %s (%.1f KB) -> %s", name, size_kb, dest)
                    return str(dest)
                else:
                    logger.warning("[Slack] SDK download failed (status %d) for %s, trying urllib", resp.status_code, name)
            except Exception as exc:
                exc_str = str(exc)
                if "missing_scope" in exc_str or "files:read" in exc_str:
                    logger.error(
                        "[Slack] Cannot download file '%s': bot token is missing the 'files:read' scope. "
                        "Add it at api.slack.com/apps → OAuth & Permissions → Bot Token Scopes, then reinstall the app.",
                        name,
                    )
                    return None
                logger.warning("[Slack] SDK download failed for %s: %s, trying urllib", name, exc)

        # Fallback: urllib with explicit Bearer token
        bot_token = self.config.get("bot_token", "")
        if not bot_token:
            logger.warning("[Slack] No bot_token for file download")
            return None

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    # Slack redirects to a login/error page when auth fails (missing scope or invalid token)
                    logger.error(
                        "[Slack] Cannot download file '%s': Slack returned an HTML page instead of the file. "
                        "The bot token likely lacks the 'files:read' scope. "
                        "Add it at api.slack.com/apps → OAuth & Permissions → Bot Token Scopes, then reinstall the app.",
                        name,
                    )
                    return None
                data = resp.read()
            dest.write_bytes(data)
            size_kb = len(data) / 1024
            logger.info("[Slack] Downloaded file via urllib: %s (%.1f KB) -> %s", name, size_kb, dest)
            return str(dest)
        except Exception as exc:
            logger.error("[Slack] Failed to download file %s from %s: %s", name, url[:100], exc)
            return None

    def _download_event_files(self, event: dict) -> list[dict]:
        """Download all files from a Slack event. Returns list of {name, path, size, mimetype}."""
        files = event.get("files", [])
        if not files:
            return []

        downloaded = []
        for f in files:
            mode = f.get("mode", "")
            # Skip external files (Google Drive links etc.) — those are handled as URLs
            if mode == "external":
                continue

            local_path = self._download_slack_file(f)
            if local_path:
                downloaded.append({
                    "name": f.get("name", "file"),
                    "path": local_path,
                    "size": f.get("size", 0),
                    "mimetype": f.get("mimetype", ""),
                    "filetype": f.get("filetype", ""),
                })
        return downloaded

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
        # If we couldn't determine our own bot_id, use user_id as fallback.
        # Only block messages from our own user_id — let other bots through.
        if msg_bot_id and not self._own_bot_id:
            msg_user = event.get("user", "")
            if self._own_user_id and msg_user == self._own_user_id:
                return
            # Unknown bot but not us — allow through (could be Gmail, Calendar, etc.)
        # Ignore non-message subtypes (edits, joins, etc.) but allow bot_message subtype
        subtype = event.get("subtype", "")
        if subtype in self._IGNORED_SUBTYPES:
            return

        # Dedup: Slack sends both 'message' and 'app_mention' for @mentions in channels.
        # Skip if we already processed this exact message timestamp.
        msg_ts = event.get("ts", "")
        now = time.monotonic()
        with self._seen_ts_lock:
            # Prune entries older than 60 seconds
            self._seen_ts = {ts: t for ts, t in self._seen_ts.items() if now - t < 60}
            if msg_ts in self._seen_ts:
                logger.debug("Dedup: skipping already-seen message ts=%s", msg_ts)
                return
            self._seen_ts[msg_ts] = now
            # Prune orphaned progress messages older than TTL
            self._progress_message_ts = {
                k: v for k, v in self._progress_message_ts.items()
                if now - v[1] < self._PROGRESS_TTL
            }

        user_id = event.get("user", "") or event.get("bot_id", "")

        # Check allowed users (bot-forwarded messages bypass this check)
        if self._allowed_users and user_id not in self._allowed_users and not msg_bot_id:
            logger.debug("Ignoring message from non-allowed user: %s", user_id)
            return

        # Debug: log raw event structure for diagnosing forwarded message/email issues
        att_count = len(event.get("attachments", []))
        file_count = len(event.get("files", []))
        block_count = len(event.get("blocks", []))
        if att_count or file_count or block_count:
            logger.info(
                "[Slack] Event has attachments=%d files=%d blocks=%d subtype=%s text_len=%d",
                att_count, file_count, block_count, event.get("subtype", "(none)"),
                len(event.get("text", "")),
            )
            for i, att in enumerate(event.get("attachments", [])):
                logger.info(
                    "[Slack]   attachment[%d]: keys=%s, text_len=%d, fallback_len=%d, from_url=%s",
                    i, list(att.keys()), len(att.get("text", "")), len(att.get("fallback", "")),
                    att.get("from_url", "(none)")[:120],
                )
            for i, f in enumerate(event.get("files", [])):
                logger.info(
                    "[Slack]   file[%d]: name=%s, mimetype=%s, filetype=%s, mode=%s, size=%s, url_private=%s",
                    i, f.get("name"), f.get("mimetype"), f.get("filetype"),
                    f.get("mode"), f.get("size"),
                    ("present" if f.get("url_private") or f.get("url_private_download") else "MISSING"),
                )

        # Download any Slack-hosted files before processing text
        downloaded_files = self._download_event_files(event)

        text = self._clean_slack_text(event.get("text", "")).strip()

        # Extract forwarded message content from attachments
        forwarded_text = self._extract_forwarded_text(event)
        if forwarded_text:
            logger.info("[Slack] Extracted forwarded content (%d chars) from attachments", len(forwarded_text))
            text = text + "\n\nForwarded message:\n" + forwarded_text if text else "Forwarded message:\n" + forwarded_text

        # Extract quoted content from blocks (modern Slack message forwarding embeds the
        # forwarded message as a rich_text_quote block rather than in attachments)
        quoted_text = self._extract_quoted_blocks_text(event)
        if quoted_text:
            logger.info("[Slack] Extracted quoted content (%d chars) from blocks", len(quoted_text))
            text = text + "\n\nForwarded message:\n" + quoted_text if text else "Forwarded message:\n" + quoted_text

        # Resolve Slack archive URLs to actual message content
        # Pattern: https://WORKSPACE.slack.com/archives/CHANNEL_ID/pTIMESTAMP
        text = self._resolve_slack_archive_urls(text)

        # Extract URLs from Slack attachments/files that aren't in the text body
        extra_urls = self._extract_attachment_urls(event)
        if extra_urls:
            # Also resolve any Slack archive URLs in attachment URLs
            resolved_urls = []
            for url in extra_urls:
                resolved = self._resolve_single_slack_url(url)
                if resolved:
                    resolved_urls.append(resolved)
                else:
                    resolved_urls.append(url)
            text = text + "\n\nAttached links:\n" + "\n".join(resolved_urls) if text else "\n".join(resolved_urls)

        # Inline text-based file contents so the agent can read them directly.
        # The sandbox can only access /mnt/user-data/ paths, and the DeerFlow
        # thread doesn't exist yet at this point, so we can't copy files into
        # the thread's uploads dir.  For text files (HTML emails, .txt, .eml,
        # .csv, .json, etc.) we read the content inline.  Binary files are
        # noted but can't be accessed until a future upload integration.
        _TEXT_MIMETYPES = {"text/", "application/json", "application/xml", "application/csv"}
        _TEXT_EXTENSIONS = {".html", ".htm", ".txt", ".eml", ".csv", ".json", ".xml", ".md", ".log"}
        # Anthropic vision: jpeg/png/gif/webp, max 5 MB per image (base64 inflates ~33%,
        # so cap raw bytes at 3.75 MB).
        _IMAGE_MIMETYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        _IMAGE_MAX_BYTES = 3_750_000
        image_blocks: list[dict] = []
        if downloaded_files:
            for df in downloaded_files:
                mime = df.get("mimetype", "") or ""
                name = df.get("name", "")
                ext = os.path.splitext(name)[1].lower() if name else ""
                is_text = any(mime.startswith(t) for t in _TEXT_MIMETYPES) or ext in _TEXT_EXTENSIONS
                is_image = mime in _IMAGE_MIMETYPES

                if is_text and df.get("path") and os.path.isfile(df["path"]):
                    try:
                        with open(df["path"], encoding="utf-8", errors="replace") as fh:
                            content = fh.read(500_000)  # Cap at 500 KB of text
                        # Strip HTML tags for cleaner agent consumption
                        if ext in (".html", ".htm") or mime.startswith("text/html"):
                            content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
                            content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
                            content = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
                            content = re.sub(r'</?p[^>]*>', '\n', content, flags=re.IGNORECASE)
                            content = re.sub(r'<[^>]+>', '', content)
                            content = re.sub(r'\n{3,}', '\n\n', content).strip()
                        label = f"Attached file: {name}"
                        text = f"{text}\n\n{label}\n---\n{content}" if text else f"{label}\n---\n{content}"
                        logger.info("[Slack] Inlined text file: %s (%d chars)", name, len(content))
                    except Exception as exc:
                        logger.error("[Slack] Failed to read text file %s: %s", df["path"], exc)
                        text = f"{text}\n\n(Attached file {name} could not be read: {exc})" if text else f"(Attached file {name} could not be read: {exc})"
                elif is_image and df.get("path") and os.path.isfile(df["path"]):
                    raw_size = os.path.getsize(df["path"])
                    if raw_size > _IMAGE_MAX_BYTES:
                        size_mb = raw_size / 1_000_000
                        note = f"(Image {name} too large to inline: {size_mb:.1f} MB > 3.75 MB cap)"
                        text = f"{text}\n\n{note}" if text else note
                        logger.warning("[Slack] Image %s too large to inline (%d bytes)", name, raw_size)
                        continue
                    try:
                        from deerflow.utils.images import downscale_for_anthropic
                        with open(df["path"], "rb") as fh:
                            raw_bytes = fh.read()
                        # Anthropic rejects images >2000px on any side in many-image
                        # requests. Downscale before base64 so we don't poison the thread.
                        safe_bytes, mime = downscale_for_anthropic(raw_bytes, mime)
                        b64 = base64.b64encode(safe_bytes).decode("ascii")
                        image_blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        })
                        label = f"Attached image: {name}"
                        text = f"{text}\n\n{label}" if text else label
                        logger.info(
                            "[Slack] Inlined image: %s (%d -> %d bytes, %s)",
                            name, raw_size, len(safe_bytes), mime,
                        )
                    except Exception as exc:
                        logger.error("[Slack] Failed to read image %s: %s", df["path"], exc)
                        text = f"{text}\n\n(Image {name} could not be read: {exc})" if text else f"(Image {name} could not be read: {exc})"
                else:
                    size_str = f"{df['size'] / 1024:.1f} KB" if df.get('size') else "unknown size"
                    note = f"(Attached binary file: {name}, {mime or df.get('filetype', 'unknown')}, {size_str} — cannot be read directly, ask user to share via Google Drive)"
                    text = f"{text}\n\n{note}" if text else note

        if not text:
            logger.warning(
                "[Slack] No text extracted from event (attachments=%d, files=%d, blocks=%d, subtype=%s) — ignoring",
                len(event.get("attachments", [])),
                len(event.get("files", [])),
                len(event.get("blocks", [])),
                event.get("subtype", "(none)"),
            )
            return

        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        # If this is a threaded reply (user replying to a bot message or notification),
        # and we haven't injected the root message context yet, fetch the root message
        # and prepend it so the agent knows what the user is referring to.
        event_ts = event.get("ts", "")
        is_threaded_reply = thread_ts and event_ts and thread_ts != event_ts
        if is_threaded_reply and thread_ts not in self._root_context_injected:
            self._root_context_injected.add(thread_ts)
            root_text = self._fetch_root_message_text(channel_id, thread_ts)
            if root_text:
                logger.info("[Slack] Injecting root message context (%d chars) for thread_ts=%s", len(root_text), thread_ts)
                text = f"[Thread context — the message being replied to:]\n{root_text}\n\n[User's reply:]\n{text}"

        # Detect commands: /cmd, !cmd, or unambiguous bare keywords like "btw"
        # Note: /cmd is intercepted by Slack for registered slash commands, so
        # custom commands use ! prefix (e.g. !learn, !promote, !reject).
        _BARE_COMMANDS = {"btw", "status", "new", "help", "models", "memory"}
        text_lower = text.strip().lower()
        if text_lower.startswith("/") or text_lower.startswith("!"):
            msg_type = InboundMessageType.COMMAND
        elif text_lower in _BARE_COMMANDS:
            msg_type = InboundMessageType.COMMAND
            logger.info("[Slack] bare command detected: %r", text_lower)
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
        if image_blocks:
            inbound.metadata["image_blocks"] = image_blocks

        # Structured log line for Loki/Grafana — emit once per dispatched inbound.
        # Tile "Conversations with non-owner users" filters this on user_id.
        logger.info(
            "[Slack inbound] user_id=%s channel=%s thread_ts=%s text=%r",
            user_id, channel_id, thread_ts, text[:120],
        )

        # Notify owner if a non-owner user is talking to the analyst.
        self._notify_owner_of_inbound(user_id, channel_id, thread_ts, text)

        if self._loop and self._loop.is_running():
            if self._is_assistant_thread(thread_ts):
                # Assistant thread: use native status indicator + inject context
                self._set_assistant_status(channel_id, thread_ts, "Thinking...")
                context_channel = self._assistant_context.get(thread_ts)
                if context_channel:
                    inbound.metadata["assistant_context_channel"] = context_channel
                    # Prepend context so the agent knows what the user is viewing
                    inbound.text = f"[User is currently viewing Slack channel {context_channel}]\n\n{inbound.text}"
            else:
                # Regular DM/channel: use reaction + progress message
                self._add_reaction(channel_id, event.get("ts", thread_ts), "eyes")
                self._send_running_reply(channel_id, thread_ts)
            asyncio.run_coroutine_threadsafe(self.bus.publish_inbound(inbound), self._loop)

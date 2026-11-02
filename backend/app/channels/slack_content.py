"""Slack message-content extraction (P3-2 split from slack.py).

The forwarded-message-extraction area is one of the most incident-prone parts
of the Slack channel — content arrives in attachments, rich_text_quote
blocks, archive links, and files depending on client version, and a miss
means the agent silently answers without the forwarded context. These are
pure(ish) functions so that behavior is unit-testable without a live
SlackChannel: functions that need the Web API take `client` explicitly and
are None-safe.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Regex: https://WORKSPACE.slack.com/archives/CHANNEL_ID/pTIMESTAMP
SLACK_ARCHIVE_RE = re.compile(
    r"https?://[a-zA-Z0-9\-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d+)"
)


def archive_ts_to_message_ts(raw_ts: str) -> str:
    """Slack archive timestamps: p1234567890123456 -> 1234567890.123456"""
    return raw_ts[:10] + "." + raw_ts[10:] if len(raw_ts) > 10 else raw_ts


def clean_slack_text(text: str) -> str:
    """Convert Slack mrkdwn text to plain text suitable for the agent.

    Slack wraps URLs as ``<URL>`` or ``<URL|label>``, user mentions as
    ``<@U1234>``, and channel references as ``<#C1234|channel-name>``.

    - Unwraps URLs: ``<https://example.com>`` → ``https://example.com``
    - Unwraps labeled URLs: ``<https://example.com|Example>`` → ``https://example.com``
    - Preserves user mentions: ``<@U1234>`` stays as-is
    - Simplifies channel refs: ``<#C1234|general>`` → ``#general``
    """
    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith("@"):
            return m.group(0)
        if inner.startswith("#"):
            parts = inner.split("|", 1)
            return f"#{parts[1]}" if len(parts) == 2 else inner
        parts = inner.split("|", 1)
        return parts[0]

    return re.sub(r"<([^>]+)>", _replace, text)


def extract_attachment_urls(event: dict) -> list[str]:
    """Pull URLs from Slack attachments and blocks that may not appear in text.

    Excludes Slack-hosted file URLs (files.slack.com) because those require
    bot-token authentication and are handled separately by the file-download
    path which inlines text content or notes binary files.
    """
    urls: list[str] = []
    for att in event.get("attachments", []):
        for key in ("original_url", "from_url", "title_link", "app_unfurl_url"):
            url = att.get(key)
            if url and url not in urls:
                urls.append(url)
    for f in event.get("files", []):
        # permalink_public is the only publicly accessible file URL
        url = f.get("permalink_public")
        if url and url not in urls:
            urls.append(url)
    return urls


def extract_quoted_blocks_text(event: dict) -> str:
    """Extract text from rich_text_quote elements in Slack blocks.

    When a user forwards/shares a Slack message, the quoted content often
    appears in top-level ``blocks`` as ``rich_text_quote`` elements rather
    than in ``attachments`` (common in newer Slack clients). Only
    ``rich_text_quote`` elements are extracted — ``rich_text_section``
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


def extract_forwarded_text(event: dict) -> str:
    """Extract text content from forwarded messages, emails, and rich attachments.

    When a user forwards a Slack message or email, the content appears in the
    ``attachments`` array with fields like ``text``, ``fallback``, ``pretext``,
    ``title``, and ``author_name``. The parent message's ``text`` field is
    often empty, so without this extraction the forwarded content is silently
    lost.
    """
    parts: list[str] = []
    for att in event.get("attachments", []):
        # Link-unfurl attachments only carry a URL preview (URLs are handled
        # by extract_attachment_urls) — keep just their text if any.
        if att.get("is_app_unfurl") or att.get("service_name"):
            att_text = att.get("text", "").strip()
            if att_text:
                parts.append(att_text)
            continue

        lines: list[str] = []
        author = att.get("author_name") or att.get("author_subname", "")
        if author:
            lines.append(f"[From {author}]")
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
        footer = att.get("footer", "").strip()
        if footer:
            lines.append(f"— {footer}")
        if lines:
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Web-API-backed resolution (client injected; all None-safe / fail-open)
# ---------------------------------------------------------------------------

def fetch_slack_message(client, channel_id: str, message_ts: str) -> str | None:
    """Fetch a Slack message's text via conversations.history, falling back
    to conversations.replies for threaded messages. None on any failure."""
    if not client:
        return None
    try:
        resp = client.conversations_history(
            channel=channel_id, latest=message_ts, inclusive=True, limit=1,
        )
        msgs = resp.get("messages", [])
        if msgs:
            return msgs[0].get("text", "")
    except Exception as exc:
        logger.debug("[Slack] conversations.history failed for %s/%s: %s",
                     channel_id, message_ts, exc)
        try:
            resp = client.conversations_replies(
                channel=channel_id, ts=message_ts, limit=1,
            )
            msgs = resp.get("messages", [])
            if msgs:
                return msgs[0].get("text", "")
        except Exception as exc2:
            logger.debug("[Slack] conversations.replies also failed: %s", exc2)
    return None


def resolve_single_slack_url(client, url: str) -> str | None:
    """If *url* is a Slack archive link, fetch the message and return a text
    block like ``[Slack message from #channel]: ...``. None otherwise."""
    m = SLACK_ARCHIVE_RE.search(url)
    if not m:
        return None
    channel_id = m.group(1)
    message_ts = archive_ts_to_message_ts(m.group(2))
    msg_text = fetch_slack_message(client, channel_id, message_ts)
    if msg_text:
        cleaned = clean_slack_text(msg_text)
        logger.info("[Slack] Resolved archive URL -> %d chars from channel %s",
                    len(cleaned), channel_id)
        return f"[Slack message from channel {channel_id}]:\n{cleaned}"
    return None


def resolve_slack_archive_urls(client, text: str) -> str:
    """Find Slack archive URLs in *text* and append the resolved message content."""
    urls = SLACK_ARCHIVE_RE.findall(text)
    if not urls:
        return text
    resolved_parts = []
    for channel_id, raw_ts in urls:
        message_ts = archive_ts_to_message_ts(raw_ts)
        msg_text = fetch_slack_message(client, channel_id, message_ts)
        if msg_text:
            cleaned = clean_slack_text(msg_text)
            resolved_parts.append(f"[Slack message from channel {channel_id}]:\n{cleaned}")
    if resolved_parts:
        text = text + "\n\n" + "\n\n".join(resolved_parts)
    return text


def fetch_root_message_text(client, channel_id: str, thread_ts: str) -> str | None:
    """Fetch the root message of a Slack thread, including forwarded/attached
    content — used to give the agent context when a user replies in a thread
    whose root message was posted by the bot (e.g. email notifications)."""
    if not client:
        return None
    try:
        resp = client.conversations_history(
            channel=channel_id, latest=thread_ts, inclusive=True, limit=1,
        )
        msgs = resp.get("messages", [])
        if not msgs:
            return None
        root = msgs[0]
        parts = []

        body = clean_slack_text(root.get("text", "")).strip()
        if body:
            parts.append(body)

        # Rich blocks (header, section, context, rich_text)
        for block in root.get("blocks", []):
            btype = block.get("type", "")
            if btype in ("section", "header", "context"):
                bt = block.get("text", {})
                if isinstance(bt, dict):
                    block_text = clean_slack_text(bt.get("text", "")).strip()
                    if block_text and block_text not in body:
                        parts.append(block_text)
                for field in block.get("fields", []):
                    if isinstance(field, dict):
                        ft = clean_slack_text(field.get("text", "")).strip()
                        if ft:
                            parts.append(ft)
            elif btype == "rich_text":
                for elem in block.get("elements", []):
                    for sub in elem.get("elements", []):
                        if sub.get("type") == "text":
                            parts.append(sub.get("text", "").strip())

        forwarded = extract_forwarded_text(root)
        if forwarded:
            parts.append(f"Forwarded message:\n{forwarded}")

        quoted = extract_quoted_blocks_text(root)
        if quoted:
            parts.append(f"Forwarded message:\n{quoted}")

        text = "\n\n".join(p for p in parts if p)
        return text if text else None
    except Exception as exc:
        logger.warning("[Slack] Failed to fetch root message for thread_ts=%s: %s",
                       thread_ts, exc)
        return None

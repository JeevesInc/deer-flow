"""Shared Slack helpers for crons and scripts (P2-4 / GW-F13).

The DM-the-owner block was copy-pasted ~10x across crons with drifting
error handling. This is the one canonical implementation.

NOTE for the AGENT: outbound sends to other people must still go through
`slack-search/slack_tool.py send` (it feeds the owner audit log). This
module is for infra code (crons/alerts) messaging BRIAN only.
"""

import logging
import os

log = logging.getLogger("shared.slack")


def get_client(token: str | None = None):
    """A slack_sdk WebClient from SLACK_BOT_TOKEN (or an explicit token)."""
    from slack_sdk import WebClient

    tok = token or os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    return WebClient(token=tok)


def owner_user_id() -> str:
    # Canonical resolution (incl. the bot-id-as-owner misconfig guard) lives
    # in identity.py — the single source of truth for these ids (P3-5).
    from identity import owner_slack_id
    return owner_slack_id()


def dm_owner(text: str, *, blocks=None) -> bool:
    """Best-effort DM to the owner. Never raises; returns True on success."""
    try:
        client = get_client()
        ch = client.conversations_open(users=[owner_user_id()])["channel"]["id"]
        client.chat_postMessage(channel=ch, text=text, blocks=blocks)
        return True
    except Exception as e:  # noqa: BLE001 - alerting must never take the caller down
        log.warning("dm_owner failed: %s", e)
        return False

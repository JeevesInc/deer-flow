"""Canonical Slack/owner identity constants (P3-5).

The Brian-vs-bot id mixup is a documented recurring bug class: multiple
scripts carried their own ``SLACK_OWNER_USER_ID`` fallback and at least one
(webhook_receiver) shipped with the BOT's id as the owner default, so DMs
meant for Brian went to the bot. This module is the single source of truth —
never hardcode either id anywhere else.

Usage:
    sys.path.insert(0, <this _shared dir>)
    from identity import owner_slack_id, BOT_SLACK_ID, OWNER_EMAIL
"""

import logging
import os

log = logging.getLogger("identity")

# Brian's real Slack user id.
OWNER_SLACK_ID_FALLBACK = "U05B5HGNCN9"
# The analyst bot's OWN Slack user id — never a valid "owner" value.
BOT_SLACK_ID = "U09PQTZ5DHC"

OWNER_EMAIL = os.environ.get("GOOGLE_CALENDAR_EMAIL", "brian.mauck@tryjeeves.com")


def owner_slack_id() -> str:
    """Resolve the owner's Slack id from env with a misconfiguration guard.

    If SLACK_OWNER_USER_ID is unset, empty, or — the recurring failure —
    set to the bot's own id, return the known-good owner id instead.
    """
    val = (os.environ.get("SLACK_OWNER_USER_ID") or "").strip()
    if not val:
        return OWNER_SLACK_ID_FALLBACK
    if val == BOT_SLACK_ID:
        log.error(
            "SLACK_OWNER_USER_ID is set to the BOT's own id (%s) — using the "
            "owner fallback %s. Fix the env.", BOT_SLACK_ID, OWNER_SLACK_ID_FALLBACK,
        )
        return OWNER_SLACK_ID_FALLBACK
    return val

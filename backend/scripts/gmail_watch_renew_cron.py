#!/usr/bin/env python3
"""gmail-watch-renew cron — keeps the Gmail Pub/Sub watch alive.

Gmail `users.watch` expires after **7 days max**. If nothing renews it, Pub/Sub
silently stops publishing and every inbound-email proposal dies with no error.

This is exactly what happened 2026-05-27 → 2026-06-30 (33 days dark): the watch
was set up once on 2026-05-20 and never renewed, so it expired ~7 days later,
and the push subscription then auto-deleted after 31 days of inactivity. See the
webhook pipeline notes in MEMORY.md.

This cron renews the watch on gateway startup and every RENEW_INTERVAL after,
giving a 2-day safety margin under the 7-day expiry. Renewal failures are logged
and Slack-alerted (never silent), then retried hourly.

Exposes `run_loop()` for cron_supervisor (must never return — it loops forever).
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# scripts/ -> backend/ ; setup_gmail_watch.py lives in backend/
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

TOPIC = os.environ.get(
    "GMAIL_PUBSUB_TOPIC", "projects/brian-sandbox-458317/topics/gmail-push"
)
RENEW_INTERVAL = 5 * 24 * 3600   # 5 days (watch lives 7 → 2-day margin)
RETRY_DELAY = 3600               # retry hourly on failure
_STATE_PATH = _BACKEND / ".webhook_gmail_state.json"


def _slack_alert(text: str) -> None:
    """Best-effort DM to the owner. Never raises."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    owner = os.environ.get("SLACK_OWNER_USER_ID")
    if not token or not owner:
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        ch = client.conversations_open(users=[owner])["channel"]["id"]
        client.chat_postMessage(channel=ch, text=text)
    except Exception as e:
        logger.warning("[gmail-watch-renew] slack alert failed: %s", e)


def _advance_state_pointer(history_id: int) -> None:
    """Point last_history_id at the fresh watch historyId.

    Gmail only retains history for ~a week, so a stale pointer (after downtime)
    would 404 on the first push and silently drop that email. Advancing to the
    current historyId avoids that; seen_ids still dedups any minor overlap.
    """
    try:
        state = {}
        if _STATE_PATH.exists():
            state = json.loads(_STATE_PATH.read_text())
        state["last_history_id"] = int(history_id)
        state.setdefault("seen_ids", [])
        _STATE_PATH.write_text(json.dumps(state, indent=2))
        logger.info("[gmail-watch-renew] state pointer set to %s", history_id)
    except Exception as e:
        logger.warning("[gmail-watch-renew] could not advance state pointer: %s", e)


def _renew_once() -> dict:
    from setup_gmail_watch import watch_gmail

    res = watch_gmail(TOPIC)
    hist = int(res.get("historyId", 0))
    exp_ms = int(res.get("expiration", 0))
    if hist:
        _advance_state_pointer(hist)
    logger.info(
        "[gmail-watch-renew] watch renewed (historyId=%s, expiration_ms=%s)",
        hist, exp_ms,
    )
    return res


def run_loop() -> None:
    while True:
        try:
            _renew_once()
            sleep_for = RENEW_INTERVAL
        except Exception as e:
            logger.exception("[gmail-watch-renew] renewal FAILED: %s", e)
            _slack_alert(
                ":rotating_light: *Gmail watch renewal FAILED* — "
                + str(e)[:300] + "\n"
                "Inbound-email proposals will stop within ~7 days if this keeps "
                "failing. Check Google creds / Pub/Sub topic."
            )
            sleep_for = RETRY_DELAY
        time.sleep(sleep_for)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _renew_once()

#!/usr/bin/env python3
"""
bot_dm_history_cron.py — Track every Slack DM the analyst bot has with
non-owner users, and emit structured `[Bot DM]` log lines to
`bot_dm_history.log` so Grafana / Loki can show them.

Why this exists: when the agent writes ad-hoc Python in its sandbox and
calls `slack_sdk.WebClient.chat_postMessage` directly, those outbound
messages bypass the channels message bus, so they never reach gateway.log
or supervisor.log. The only ground truth is Slack itself. This cron pulls
Slack via the bot token, diffs against per-channel `last_ts` state, and
appends one log line per new bot-authored message.

Runs under app/gateway/cron_supervisor.py via run_loop() — pattern matches
analytics_cron, dossier_cron, etc.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bot-dm-history] %(message)s")
log = logging.getLogger("bot-dm-history")

# Load .env so this script works both standalone (one-shot) and inside the
# gateway cron context. Idempotent — won't overwrite vars already set.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            if _k and _v and _k not in os.environ:
                os.environ[_k] = _v

POLL_INTERVAL = int(os.environ.get("BOT_DM_POLL_INTERVAL", "300"))  # 5 min
STATE_PATH = Path(__file__).resolve().parent.parent / ".bot_dm_state.json"
# Emit log lines into the repo root so promtail (which tails /host-logs/*.log)
# picks them up alongside gateway.log etc.
LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "bot_dm_history.log"

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _HAS_SLACK_SDK = True
except ImportError:
    _HAS_SLACK_SDK = False
    WebClient = None  # type: ignore
    SlackApiError = Exception  # type: ignore

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
OWNER_USER_ID = os.environ.get("SLACK_OWNER_USER_ID", "")


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def _append_log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _resolve_user(client: WebClient, user_id: str, cache: dict) -> dict:
    if user_id in cache:
        return cache[user_id]
    info = {"id": user_id, "name": user_id, "is_bot": False}
    try:
        resp = client.users_info(user=user_id)
        u = resp.get("user", {})
        prof = u.get("profile", {})
        info["name"] = prof.get("real_name") or prof.get("display_name") or u.get("name") or user_id
        info["is_bot"] = bool(u.get("is_bot"))
    except SlackApiError:
        pass
    cache[user_id] = info
    return info


def _emit_message(msg: dict, channel_id: str, other_user: dict, bot_user_id: str) -> None:
    """Emit one structured log line for a bot-authored message."""
    text = (msg.get("text") or "").replace("\n", "\\n").replace("\r", " ")
    ts = msg.get("ts", "")
    try:
        when = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        when = ts
    # Single-line structured format; promtail extracts the to_user_id label.
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"[Bot DM] sent_at={when} ts={ts} "
        f"to_user_id={other_user['id']} to_user_name={other_user['name']!r} "
        f"channel={channel_id} bot_user_id={bot_user_id} "
        f"text={text!r}"
    )
    _append_log(line)


def poll_once(client: WebClient, bot_user_id: str, state: dict, user_cache: dict) -> int:
    """One pass: enumerate IM channels, emit new bot messages, return count."""
    emitted = 0
    cursor = None
    while True:
        params = {"types": "im", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.conversations_list(**params)
        except SlackApiError as e:
            log.error("conversations_list failed: %s", e)
            return emitted
        for im in resp.get("channels", []):
            other_user_id = im.get("user", "")
            if not other_user_id or other_user_id == OWNER_USER_ID or other_user_id == bot_user_id:
                continue
            info = _resolve_user(client, other_user_id, user_cache)
            if info.get("is_bot"):
                continue

            channel_id = im["id"]
            last_ts = state.get(channel_id, {}).get("last_ts", "0")

            # Pull only messages newer than last_ts
            try:
                hist = client.conversations_history(channel=channel_id, oldest=last_ts, limit=200)
            except SlackApiError as e:
                log.warning("conversations_history failed for %s: %s", channel_id, e)
                continue

            new_msgs = hist.get("messages", [])
            # Slack returns newest-first; sort oldest-first so log order is chronological
            new_msgs.sort(key=lambda m: float(m.get("ts", "0")))
            max_ts = last_ts
            for m in new_msgs:
                m_ts = m.get("ts", "0")
                if m_ts == last_ts:
                    continue  # exact-equal boundary
                # Only emit messages authored by the bot
                if m.get("user") == bot_user_id:
                    _emit_message(m, channel_id, info, bot_user_id)
                    emitted += 1
                if float(m_ts) > float(max_ts or "0"):
                    max_ts = m_ts

            if max_ts != last_ts:
                state[channel_id] = {"last_ts": max_ts, "user_id": other_user_id, "user_name": info["name"]}
            time.sleep(0.3)  # tier-3 rate-limit safety

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return emitted


def run_loop():
    if not _HAS_SLACK_SDK:
        log.error("slack_sdk not installed — bot-dm-history cron disabled")
        return
    if not BOT_TOKEN or not OWNER_USER_ID:
        log.error("SLACK_BOT_TOKEN or SLACK_OWNER_USER_ID missing — bot-dm-history cron disabled")
        return
    log.info("bot_dm_history cron starting (poll=%ds, log=%s, state=%s)",
             POLL_INTERVAL, LOG_PATH, STATE_PATH)
    client = WebClient(token=BOT_TOKEN)
    try:
        auth = client.auth_test()
    except SlackApiError as e:
        log.error("auth_test failed: %s", e)
        return
    bot_user_id = auth["user_id"]
    log.info("Bot identity: %s (%s)", auth.get("user", bot_user_id), bot_user_id)

    user_cache: dict = {}
    while True:
        try:
            state = _load_state()
            emitted = poll_once(client, bot_user_id, state, user_cache)
            _save_state(state)
            if emitted:
                log.info("Emitted %d new bot DM message(s)", emitted)
        except Exception as e:
            log.error("poll cycle failed: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    # Bootstrap helper: if `--once` passed, run a single poll and exit.
    if "--once" in sys.argv:
        client = WebClient(token=BOT_TOKEN)
        auth = client.auth_test()
        bot_user_id = auth["user_id"]
        state = _load_state()
        emitted = poll_once(client, bot_user_id, state, {})
        _save_state(state)
        log.info("Once-mode emitted %d message(s)", emitted)
    else:
        run_loop()

#!/usr/bin/env python3
"""owner-slack-draft cron — proactively drafts replies to Slack messages BRIAN receives.

Unlike slack_dm_monitor_cron (which watches DMs to the *bot*), this watches DMs to
*Brian* using his user token (xoxp), and for messages he hasn't answered, drafts a
reply and posts it as a card to the approval/pool channel. Brian reviews inline:
reply "send it" → the agent sends it AS Brian (slack_tool send-as-owner); paste edits
to revise; ignore to skip. Nothing is ever sent without Brian's explicit approval.

Noise control (the whole point — only surface dropped balls):
  - SETTLE window: a message must sit unanswered for SETTLE_SECONDS (30 min) before
    it's eligible. If Brian replies himself in that window, it never surfaces.
  - skip-if-replied: only the LATEST message per DM is considered, and only if it's
    from the other person (if Brian's reply is the latest, he's handled it → skip).
    This also debounces bursts (we draft once against the settled latest ask).
  - reply-worthiness classifier: only messages that genuinely need a written reply
    from Brian (skips FYI / automated / banter).
  - one open card per conversation: supersede (chat_update) instead of stacking.
  - LOOKBACK cap: ignores messages older than MAX_LOOKBACK so first run doesn't
    card an ancient backlog.

Exposes run_loop() for cron_supervisor (must never return).

v1 scope: DMs only. @mentions in channels are a planned add-on (noisier; start narrow).
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[3] / "backend"
_SHARED = _HERE.parent.parent / "_shared"
for p in (_SHARED, _BACKEND):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="[OwnerSlackDraft %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("owner_slack_draft")

BRIAN = os.environ.get("SLACK_OWNER_USER_ID", "U05B5HGNCN9")
QUEUE_CHANNEL = os.environ.get("ANALYST_QUEUE_CHANNEL_ID", "")
POLL_INTERVAL = int(os.environ.get("OWNER_SLACK_DRAFT_INTERVAL", "300"))      # 5 min
SETTLE_SECONDS = int(os.environ.get("OWNER_SLACK_SETTLE_SECONDS", "1800"))    # 30 min
MAX_LOOKBACK = int(os.environ.get("OWNER_SLACK_LOOKBACK_SECONDS", "28800"))   # 8 h
IGNORED_USERS = {"USLACKBOT", ""}

_STATE_PATH = _BACKEND / ".deer-flow" / "_owner_slack_draft_state.json"
_PROPOSAL_LOG = _BACKEND / ".deer-flow" / "proposal_log.jsonl"


# --------------------------------------------------------------------------- state
def _load_state() -> dict:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed": [], "open_cards": {}}  # processed: msg ts; open_cards: dm_id -> {card_ts, msg_ts}


def _save_state(state: dict) -> None:
    if len(state.get("processed", [])) > 1000:
        state["processed"] = state["processed"][-1000:]
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- slack
def _user_client():
    from slack_sdk import WebClient
    tok = os.environ.get("SLACK_USER_TOKEN")
    if not tok:
        raise RuntimeError("SLACK_USER_TOKEN not set")
    return WebClient(token=tok)


def _bot_client():
    from slack_sdk import WebClient
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    return WebClient(token=tok)


def _display_name(user_client, user_id: str) -> str:
    try:
        prof = user_client.users_info(user=user_id)["user"]["profile"]
        return prof.get("display_name") or prof.get("real_name") or user_id
    except Exception:
        return user_id


def _list_dm_channels(user_client, cutoff_epoch: float) -> set:
    """Enumerate Brian's DM channel ids.

    Prefers conversations.list(types=im) — clean and complete — which needs the
    `im:read` scope. If that scope isn't granted, falls back to search-based
    discovery (works with current scopes but gets crowded out by high-volume
    channel bots, so it's best-effort). Add `im:read` to the user token for
    reliable coverage.
    """
    try:
        ims = user_client.conversations_list(types="im", limit=200).get("channels", [])
        return {im["id"] for im in ims if im.get("id")}
    except Exception as e:
        if "missing_scope" in str(e):
            log.info("im:read not granted — falling back to search discovery (add im:read for reliable coverage)")
        else:
            log.warning("conversations.list(im) failed (%s) — falling back to search", e)
        return _discover_dm_channels(user_client, cutoff_epoch)


def _discover_dm_channels(user_client, cutoff_epoch: float) -> set:
    """Find DM channel ids with recent activity, via search.

    The user token has `im:history` (read a DM we know) but NOT `im:read` (list
    DMs), so conversations.list(types=im) is unavailable. Search exposes
    channel.is_im, so we use it purely to DISCOVER active DM channel ids, then
    read each with conversations.history (im:history).
    """
    from datetime import datetime, timezone as _tz
    date_str = datetime.fromtimestamp(cutoff_epoch, _tz.utc).strftime("%Y-%m-%d")
    found = set()
    try:
        for page in range(1, 11):  # cap ~1000 most-recent messages
            r = user_client.search_messages(
                query=f"after:{date_str}", count=100, sort="timestamp", sort_dir="desc", page=page
            )
            ms = r.get("messages", {})
            matches = ms.get("matches", [])
            if not matches:
                break
            oldest = None
            for m in matches:
                ch = m.get("channel", {})
                if ch.get("is_im") and ch.get("id"):
                    found.add(ch["id"])
                try:
                    t = float(m.get("ts", "0"))
                    oldest = t if oldest is None else min(oldest, t)
                except Exception:
                    pass
            if page >= ms.get("paging", {}).get("pages", 1):
                break
            if oldest is not None and oldest < cutoff_epoch:
                break  # paged past the lookback window
    except Exception as e:
        log.warning("DM discovery via search failed: %s", e)
    return found


# --------------------------------------------------------------- classify + draft
def _classify_and_draft(name: str, text: str):
    """Returns (classification dict, draft str) reusing the webhook's models.

    classification is None / draft is '' when no reply is warranted.
    """
    import webhook_receiver as wr
    cls = wr.classify_with_llm(
        source="slack",
        sender=name,
        subject=f"Slack DM from {name}",
        body_preview=text,
        extra_context="This is a direct message Brian received and has NOT replied to.",
    )
    if not cls.get("actionable"):
        return cls, ""
    draft = wr.draft_reply(
        name, f"Slack DM from {name}", text,
        cls.get("summary", ""), cls.get("proposed_action", ""), source="slack",
    )
    return cls, draft


# --------------------------------------------------------------------------- card
def _card_text(name: str, msg_text: str, draft: str, dm_id: str, card_ts: str | None) -> str:
    lines = [
        f":speech_balloon: *Reply draft — DM from {name}*",
        "",
        "> " + (msg_text[:600].replace("\n", "\n> ")),
        "",
        "*Draft reply (sends as you):*",
        "```",
        draft,
        "```",
        "",
        "_Reply *send it* to send it as you · paste edits to revise · ignore to skip._",
    ]
    if card_ts:
        lines.append(f"_[approval] to send as you: `python slack_tool.py send-as-owner --from-proposal {card_ts}`_")
    lines.append(f"_src: slack · target: {dm_id}_")
    return "\n".join(lines)


def _log_proposal(card_ts: str, dm_id: str, name: str, sender_id: str,
                  msg_text: str, draft: str, cls: dict) -> None:
    entry = {
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "source": "slack",
        "gmail_msg_id": "",
        "slack_channel": QUEUE_CHANNEL,        # where the CARD lives (for lookup)
        "slack_ts": card_ts,                   # the card ts
        "slack_target_channel": dm_id,         # where the reply should go (as Brian)
        "slack_target_thread": "",
        "sender_email": "",
        "sender_domain": "slack",
        "sender_display": name,
        "sender_slack_id": sender_id,
        "subject": f"Slack DM from {name}",
        "category": cls.get("category", "slack reply"),
        "priority": cls.get("priority", "medium"),
        "actionable": True,
        "summary": cls.get("summary", ""),
        "proposed_action": cls.get("proposed_action", ""),
        "draft_reply": draft,
        "reasoning": cls.get("reasoning", ""),
    }
    try:
        _PROPOSAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _PROPOSAL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.error("failed to log proposal: %s", e)


# --------------------------------------------------------------------------- poll
def _poll_once(dry_run: bool = False) -> None:
    if not QUEUE_CHANNEL:
        log.info("ANALYST_QUEUE_CHANNEL_ID not set — nothing to do")
        return
    user = _user_client()
    bot = _bot_client()
    state = _load_state()
    processed = set(state.get("processed", []))
    open_cards = state.get("open_cards", {})
    now = time.time()

    dm_channels = _list_dm_channels(user, now - MAX_LOOKBACK)
    log.info("found %d DM channel(s) to check", len(dm_channels))
    for dm_id in dm_channels:
        try:
            hist = user.conversations_history(channel=dm_id, limit=10).get("messages", [])
        except Exception as e:
            log.warning("history failed for %s: %s", dm_id, e)
            continue
        if not hist:
            continue
        last = hist[0]  # newest first
        other = last.get("user")

        # If Brian (or a bot) sent the latest message, the conversation is handled
        # → close any open card and move on.
        if other == BRIAN or last.get("bot_id") or not other or other in IGNORED_USERS:
            open_cards.pop(dm_id, None)
            continue

        ts = last.get("ts")
        text = (last.get("text") or "").strip()
        if not ts or not text:
            continue
        age = now - float(ts)
        if age < SETTLE_SECONDS or age > MAX_LOOKBACK:
            continue
        if ts in processed:
            continue

        name = _display_name(user, other)
        cls, draft = _classify_and_draft(name, text)
        if not draft:
            log.info("DM from %s: no reply warranted (actionable=%s)", name, cls.get("actionable"))
            if not dry_run:
                processed.add(ts)
            continue

        if dry_run:
            log.info("[dry-run] WOULD card DM from %s | %s", name, cls.get("category", ""))
            log.info("[dry-run]   msg: %s", text[:120])
            log.info("[dry-run]   draft: %s", draft[:200])
            continue

        processed.add(ts)  # don't reconsider this exact message regardless of outcome
        existing = open_cards.get(dm_id)
        try:
            if existing and existing.get("card_ts"):
                # Supersede the open card rather than stacking a second one.
                bot.chat_update(
                    channel=QUEUE_CHANNEL, ts=existing["card_ts"],
                    text=_card_text(name, text, draft, dm_id, existing["card_ts"]),
                )
                card_ts = existing["card_ts"]
                log.info("updated open card for DM from %s", name)
            else:
                resp = bot.chat_postMessage(
                    channel=QUEUE_CHANNEL,
                    text=_card_text(name, text, draft, dm_id, None),
                )
                card_ts = resp.get("ts", "")
                # Re-render with the card ts embedded (so the approval one-liner works).
                if card_ts:
                    bot.chat_update(
                        channel=QUEUE_CHANNEL, ts=card_ts,
                        text=_card_text(name, text, draft, dm_id, card_ts),
                    )
                log.info("posted reply-draft card for DM from %s", name)
            open_cards[dm_id] = {"card_ts": card_ts, "msg_ts": ts}
            _log_proposal(card_ts, dm_id, name, other, text, draft, cls)
        except Exception as e:
            log.error("failed to post/update card for DM from %s: %s", name, e)

    state["processed"] = list(processed)
    state["open_cards"] = open_cards
    _save_state(state)


def run_loop() -> None:
    log.info(
        "owner-slack-draft starting (settle=%ds, poll=%ds, queue=%s)",
        SETTLE_SECONDS, POLL_INTERVAL, QUEUE_CHANNEL or "(unset)",
    )
    while True:
        try:
            _poll_once()
        except Exception:
            log.exception("poll cycle failed")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one poll cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="log what would be carded; post nothing")
    args = ap.parse_args()
    if args.once or args.dry_run:
        _poll_once(dry_run=args.dry_run)
    else:
        run_loop()

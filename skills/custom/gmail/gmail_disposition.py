#!/usr/bin/env python3
"""Gmail-side disposition probe for proposal outcome labeling.

Why this exists
---------------
`proposal_learner.label_outcome()` used to short-circuit to `ignored` whenever
the Slack proposal thread had no replies. That conflated two very different
things:

  1. Brian never looked at it            → the proposal really was noise.
  2. Brian read the email and replied in Gmail, never touching the Slack thread
                                          → the proposal was RIGHT, and useful.

Both landed as `ignored` (487 of 523 labeled outcomes as of 2026-08-26), and the
synthesizer is explicitly told to mine IGNORED cases for "where the classifier
got it wrong". So every correct-but-actioned-in-Gmail proposal was teaching the
classifier to stop surfacing that kind of email. The feedback loop had its sign
flipped for the majority of its input.

This module asks Gmail what actually happened to the underlying thread, so the
labeler can separate the two. It is read-only — metadata format, no bodies.

Dispositions
------------
  owner_replied      Owner sent a message in the thread AFTER the proposal.
                     Strongest evidence the proposal was acted on.
  owner_replied_pre  Owner had already replied BEFORE the proposal was posted;
                     the proposal was moot on arrival (a webhook timing miss).
  thread_progressed  Someone else replied after the proposal — handled, but not
                     by Brian.
  archived           No reply, but the thread left the inbox → actively triaged.
  read_no_action     Read, still in inbox, no reply → seen and parked.
  unread             Still unread and in the inbox → genuinely never looked at.
  unknown            Probe unavailable (no creds, API error, message deleted).

Only `unread`, `read_no_action` and `unknown` still support an `ignored` label.

Usage:
    from gmail_disposition import probe_disposition
    d = probe_disposition("19e4c46733305859", posted_at="2026-05-21T20:42:57+00:00")
    d["disposition"], d["evidence"]

CLI (debugging):
    python gmail_disposition.py <gmail_msg_id> [posted_at_iso]
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_SHARED = _HERE.parent.parent / "_shared"
if _SHARED.exists() and str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

log = logging.getLogger("gmail_disposition")

# The owner's own address, used to decide whether a thread message is "Brian
# replied". Falls back to the known value rather than failing closed, because a
# wrong-but-present default only costs us one misprobe, whereas returning
# `unknown` for everything silently restores the old broken behavior.
OWNER_EMAIL = os.environ.get("GMAIL_OWNER_EMAIL", "brian.mauck@tryjeeves.com").lower()

# Dispositions that mean the proposal was acted on somewhere other than Slack.
# `owner_replied_pre` belongs here too: the email was genuinely actionable and
# got answered, the webhook was just late. Either way it is not evidence that
# surfacing it was a mistake.
HANDLED_DISPOSITIONS = frozenset({"owner_replied", "thread_progressed", "owner_replied_pre"})
# Archiving without replying is a deliberate dismissal — a real negative signal,
# but weaker than Brian typing "skip", so it gets its own label rather than
# being folded into `rejected`.
DISMISSED_DISPOSITIONS = frozenset({"archived"})
# Dispositions that still justify labeling the proposal `ignored`.
IGNORABLE_DISPOSITIONS = frozenset({"unread", "read_no_action", "unknown"})


def outcome_for_disposition(disposition: str) -> str:
    """Map a Gmail disposition onto a proposal outcome label.

    Only called when the Slack thread has no replies — an explicit Slack reply
    always wins over inferred Gmail state.
    """
    if disposition in HANDLED_DISPOSITIONS:
        return "handled_elsewhere"
    if disposition in DISMISSED_DISPOSITIONS:
        return "dismissed"
    return "ignored"

_UNKNOWN = {"disposition": "unknown", "evidence": "", "thread_len": 0, "labels": []}


def _service():
    from googleapiclient.discovery import build

    from google_auth import get_credentials

    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive timestamps in the proposal log are UTC by construction.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _header(headers: list[dict], name: str) -> str:
    lname = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == lname:
            return h.get("value") or ""
    return ""


def _is_owner(from_header: str) -> bool:
    return OWNER_EMAIL in (from_header or "").lower()


def _internal_dt(msg: dict) -> datetime | None:
    raw = msg.get("internalDate")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def probe_disposition(gmail_msg_id: str, posted_at: str = "", service=None) -> dict:
    """Ask Gmail what happened to the thread behind a proposal.

    Returns a dict with keys: disposition, evidence, thread_len, labels.
    Never raises — an unreachable API degrades to `unknown`, which the caller
    treats exactly like the old behavior.
    """
    if not gmail_msg_id:
        return dict(_UNKNOWN, evidence="No gmail_msg_id on the proposal.")

    try:
        svc = service or _service()
    except Exception as e:  # noqa: BLE001 — creds/import problems must not break labeling
        log.warning("Gmail probe unavailable: %s", e)
        return dict(_UNKNOWN, evidence=f"Gmail auth unavailable: {e}")

    try:
        anchor = (
            svc.users()
            .messages()
            .get(userId="me", id=gmail_msg_id, format="metadata", metadataHeaders=["From"])
            .execute()
        )
        thread_id = anchor.get("threadId")
        if not thread_id:
            return dict(_UNKNOWN, evidence="Message has no threadId.")

        thread = (
            svc.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — deleted messages 404 here, which is normal
        log.info("Gmail probe failed for msg=%s: %s", gmail_msg_id, e)
        return dict(_UNKNOWN, evidence=f"Gmail lookup failed: {e}")

    messages = thread.get("messages", []) or []
    if not messages:
        return dict(_UNKNOWN, evidence="Thread returned no messages.")

    cutoff = _parse_iso(posted_at)
    # Labels on the most recent message are what the inbox actually shows.
    labels = list(messages[-1].get("labelIds", []) or [])

    owner_after: datetime | None = None
    owner_before: datetime | None = None
    other_after: datetime | None = None

    for m in messages:
        headers = (m.get("payload") or {}).get("headers", [])
        sent_at = _internal_dt(m)
        from_hdr = _header(headers, "From")
        # With no cutoff we cannot order events, so treat everything as "after"
        # and let the owner_replied branch win — matching the intuition that a
        # reply existing at all is stronger evidence than no reply.
        after = True if cutoff is None or sent_at is None else sent_at > cutoff

        if _is_owner(from_hdr):
            if after:
                owner_after = owner_after or sent_at or cutoff
            else:
                owner_before = sent_at
        elif after and m.get("id") != gmail_msg_id:
            other_after = other_after or sent_at

    thread_len = len(messages)

    if owner_after is not None:
        return {
            "disposition": "owner_replied",
            "evidence": f"Owner sent a message in the Gmail thread after the proposal ({thread_len} msgs).",
            "thread_len": thread_len,
            "labels": labels,
        }
    if other_after is not None:
        return {
            "disposition": "thread_progressed",
            "evidence": f"Thread received {thread_len} messages; someone other than the owner replied after the proposal.",
            "thread_len": thread_len,
            "labels": labels,
        }
    if owner_before is not None:
        return {
            "disposition": "owner_replied_pre",
            "evidence": "Owner had already replied before the proposal was posted; the proposal was moot.",
            "thread_len": thread_len,
            "labels": labels,
        }
    if "UNREAD" in labels:
        return {
            "disposition": "unread",
            "evidence": "Thread is still unread in the inbox.",
            "thread_len": thread_len,
            "labels": labels,
        }
    if "INBOX" not in labels:
        return {
            "disposition": "archived",
            "evidence": "Thread was archived or moved out of the inbox without a reply.",
            "thread_len": thread_len,
            "labels": labels,
        }
    return {
        "disposition": "read_no_action",
        "evidence": "Thread was read but is still sitting in the inbox with no reply.",
        "thread_len": thread_len,
        "labels": labels,
    }


def is_handled(disposition: str) -> bool:
    """True when the Gmail side shows the proposal was acted on."""
    return disposition in HANDLED_DISPOSITIONS


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    _posted = sys.argv[2] if len(sys.argv) > 2 else ""
    import json as _json

    print(_json.dumps(probe_disposition(sys.argv[1], _posted), indent=2))

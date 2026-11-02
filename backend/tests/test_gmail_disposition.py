"""Tests for the Gmail-side proposal disposition probe.

Regression guard for the label collapse fixed 2026-08-26: `label_outcome()`
short-circuited to `ignored` whenever the Slack proposal thread had no replies,
which conflated "Brian never looked at it" with "Brian answered in Gmail". 445
of 470 historical rows were mislabeled that way, and the pattern synthesizer is
explicitly pointed at IGNORED cases to learn what NOT to surface — so the
feedback loop was training the classifier against its own correct calls.

These tests stub the Gmail API entirely; no credentials or network needed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
GMAIL_DIR = BACKEND_DIR.parent / "skills" / "custom" / "gmail"
DISPOSITION = GMAIL_DIR / "gmail_disposition.py"

POSTED_AT = "2026-05-21T20:00:00+00:00"
BEFORE_MS = 1779393600000  # 2026-05-21T19:20:00Z — before the proposal
AFTER_MS = 1779400000000   # 2026-05-21T21:06:40Z — after the proposal


def _load():
    spec = importlib.util.spec_from_file_location("gmail_disposition_under_test", DISPOSITION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gd():
    return _load()


class _FakeExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeMessages:
    def __init__(self, thread_id):
        self._thread_id = thread_id

    def get(self, **_kw):
        return _FakeExec({"threadId": self._thread_id})


class _FakeThreads:
    def __init__(self, messages):
        self._messages = messages

    def get(self, **_kw):
        return _FakeExec({"messages": self._messages})


class _FakeUsers:
    def __init__(self, messages, thread_id="T1"):
        self._messages = messages
        self._thread_id = thread_id

    def messages(self):
        return _FakeMessages(self._thread_id)

    def threads(self):
        return _FakeThreads(self._messages)


class _FakeService:
    def __init__(self, messages):
        self._messages = messages

    def users(self):
        return _FakeUsers(self._messages)


def _msg(sender: str, internal_ms: int, labels=None, msg_id="m1"):
    return {
        "id": msg_id,
        "internalDate": str(internal_ms),
        "labelIds": labels if labels is not None else ["INBOX"],
        "payload": {"headers": [{"name": "From", "value": sender}]},
    }


OWNER = "Brian Mauck <brian.mauck@tryjeeves.com>"
OTHER = "Counterparty <someone@cim-llc.com>"


def _probe(gd, messages):
    return gd.probe_disposition("gm1", posted_at=POSTED_AT, service=_FakeService(messages))


# -- the core distinction this module exists for -----------------------------


def test_owner_reply_after_proposal_is_not_ignored(gd):
    """The exact case that produced 433 bogus `ignored` labels."""
    result = _probe(gd, [_msg(OTHER, BEFORE_MS, msg_id="gm1"), _msg(OWNER, AFTER_MS, msg_id="m2")])
    assert result["disposition"] == "owner_replied"
    assert gd.outcome_for_disposition(result["disposition"]) == "handled_elsewhere"


def test_unread_inbox_thread_stays_ignored(gd):
    result = _probe(gd, [_msg(OTHER, BEFORE_MS, labels=["INBOX", "UNREAD"], msg_id="gm1")])
    assert result["disposition"] == "unread"
    assert gd.outcome_for_disposition(result["disposition"]) == "ignored"


def test_read_but_parked_stays_ignored(gd):
    result = _probe(gd, [_msg(OTHER, BEFORE_MS, labels=["INBOX"], msg_id="gm1")])
    assert result["disposition"] == "read_no_action"
    assert gd.outcome_for_disposition(result["disposition"]) == "ignored"


def test_archived_without_reply_is_dismissed(gd):
    """Archiving is a deliberate "not actionable" — a real signal, but softer
    than an explicit rejection, so it must not be folded into `rejected`."""
    result = _probe(gd, [_msg(OTHER, BEFORE_MS, labels=[], msg_id="gm1")])
    assert result["disposition"] == "archived"
    assert gd.outcome_for_disposition(result["disposition"]) == "dismissed"


def test_third_party_reply_counts_as_handled(gd):
    result = _probe(gd, [_msg(OTHER, BEFORE_MS, msg_id="gm1"), _msg(OTHER, AFTER_MS, msg_id="m2")])
    assert result["disposition"] == "thread_progressed"
    assert gd.outcome_for_disposition(result["disposition"]) == "handled_elsewhere"


def test_owner_reply_before_proposal_is_moot_not_ignored(gd):
    """A late webhook is a pipeline miss, not evidence the email was noise."""
    result = _probe(gd, [_msg(OTHER, BEFORE_MS - 1000, msg_id="gm1"), _msg(OWNER, BEFORE_MS)])
    assert result["disposition"] == "owner_replied_pre"
    assert gd.outcome_for_disposition(result["disposition"]) == "handled_elsewhere"


# -- failure modes must degrade to the old behavior, never crash -------------


def test_api_failure_degrades_to_unknown_then_ignored(gd):
    class _Boom:
        def users(self):
            raise RuntimeError("gmail is down")

    result = gd.probe_disposition("gm1", posted_at=POSTED_AT, service=_Boom())
    assert result["disposition"] == "unknown"
    assert gd.outcome_for_disposition(result["disposition"]) == "ignored"


def test_missing_message_id_is_unknown(gd):
    result = gd.probe_disposition("", posted_at=POSTED_AT, service=_FakeService([]))
    assert result["disposition"] == "unknown"


def test_empty_thread_is_unknown(gd):
    result = _probe(gd, [])
    assert result["disposition"] == "unknown"


def test_missing_posted_at_prefers_reply_evidence(gd):
    """Without a cutoff we cannot order events; an owner reply existing at all
    is still stronger evidence than no reply, so it must not fall back to
    `ignored`."""
    result = gd.probe_disposition(
        "gm1", posted_at="", service=_FakeService([_msg(OTHER, BEFORE_MS, msg_id="gm1"), _msg(OWNER, AFTER_MS)])
    )
    assert result["disposition"] == "owner_replied"


def test_owner_match_is_case_insensitive(gd):
    result = _probe(gd, [_msg(OTHER, BEFORE_MS, msg_id="gm1"), _msg("BRIAN.MAUCK@TRYJEEVES.COM", AFTER_MS)])
    assert result["disposition"] == "owner_replied"

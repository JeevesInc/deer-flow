"""Tests for at-least-once Gmail inbound processing (P3-6, 2026-07-02).

Invariants:
  - seen_ids is marked ONLY after successful handling (a crash between fetch
    and proposal-post used to lose the message forever).
  - history listing follows nextPageToken (single-page used to truncate bursts).
  - last_history_id advances only when the listing succeeded; 404-too-old
    re-anchors loudly; transient failures leave the pointer for catch-up.
  - failed messages land in a bounded pending_retry queue that the sweep
    drains, giving up (loudly) after GMAIL_MAX_RETRY_ATTEMPTS.
"""
from __future__ import annotations

import json

import pytest

import webhook_receiver as wr


@pytest.fixture()
def state_path(tmp_path, monkeypatch):
    p = tmp_path / "gmail_state.json"
    monkeypatch.setattr(wr, "_gmail_state_path", lambda: p)
    return p


def _state(p):
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def test_mark_seen_clears_pending(state_path):
    wr._add_gmail_pending_retry("m1")
    wr._mark_gmail_seen("m1")
    s = _state(state_path)
    assert "m1" in s["seen_ids"]
    assert s["pending_retry"] == []


def test_pending_retry_increments_attempts(state_path):
    wr._add_gmail_pending_retry("m1")
    wr._add_gmail_pending_retry("m1")
    s = _state(state_path)
    assert len(s["pending_retry"]) == 1
    assert s["pending_retry"][0]["attempts"] == 2


def test_pending_retry_skips_already_seen(state_path):
    wr._mark_gmail_seen("m1")
    wr._add_gmail_pending_retry("m1")
    assert _state(state_path)["pending_retry"] == []


# ---------------------------------------------------------------------------
# Processing: seen only after success
# ---------------------------------------------------------------------------

def _msg(msg_id):
    return {"meta": {"id": msg_id}, "full": {"id": msg_id}}


def test_success_marks_seen(state_path, monkeypatch):
    monkeypatch.setattr(wr, "_fetch_new_gmail_messages", lambda hid: [_msg("ok1")])
    monkeypatch.setattr(wr, "_handle_gmail_message", lambda m: None)
    wr._process_gmail_notification("123")
    s = _state(state_path)
    assert "ok1" in s["seen_ids"]
    assert s["pending_retry"] == []


def test_failure_queues_retry_not_seen(state_path, monkeypatch):
    monkeypatch.setattr(wr, "_fetch_new_gmail_messages", lambda hid: [_msg("bad1")])

    def boom(m):
        raise RuntimeError("classifier down")
    monkeypatch.setattr(wr, "_handle_gmail_message", boom)
    wr._process_gmail_notification("123")
    s = _state(state_path)
    assert "bad1" not in s["seen_ids"]
    assert [p["id"] for p in s["pending_retry"]] == ["bad1"]


# ---------------------------------------------------------------------------
# History listing: pagination + pointer semantics
# ---------------------------------------------------------------------------

class FakeGmail:
    """Minimal fake of the googleapiclient chain used by the receiver."""

    def __init__(self, pages=None, list_error=None, message_errors=()):
        self.pages = pages or []
        self.list_error = list_error
        self.message_errors = set(message_errors)
        self.list_calls = 0

    def users(self):
        return self

    def history(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self._kind = ("list", kwargs)
        return self

    def get(self, **kwargs):
        self._kind = ("get", kwargs)
        return self

    def execute(self):
        kind, kwargs = self._kind
        if kind == "list":
            if self.list_error:
                raise self.list_error
            page = self.pages[self.list_calls]
            self.list_calls += 1
            return page
        msg_id = kwargs["id"]
        if msg_id in self.message_errors:
            raise RuntimeError(f"fetch failed for {msg_id}")
        return {"id": msg_id}


def _page(ids, next_token=None, hid="900"):
    p = {"history": [{"messagesAdded": [{"message": {"id": i}} for i in ids]}],
         "historyId": hid}
    if next_token:
        p["nextPageToken"] = next_token
    return p


def test_pagination_follows_next_page_token():
    fake = FakeGmail(pages=[_page(["a", "b"], next_token="t2", hid="800"),
                            _page(["c"], hid="900")])
    ids, hid = wr._list_new_message_ids(fake, "700")
    assert ids == ["a", "b", "c"]
    assert hid == "900"
    assert fake.list_calls == 2


def test_fetch_advances_pointer_on_success(state_path, monkeypatch):
    state_path.write_text(json.dumps({"last_history_id": "700", "seen_ids": ["a"],
                                      "pending_retry": []}))
    fake = FakeGmail(pages=[_page(["a", "b"], hid="900")])
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: fake)
    msgs = wr._fetch_new_gmail_messages("850")
    # "a" already seen -> only "b" fetched; pointer -> response historyId.
    assert [m["meta"]["id"] for m in msgs] == ["b"]
    assert _state(state_path)["last_history_id"] == 900


def test_transient_listing_failure_keeps_pointer(state_path, monkeypatch):
    state_path.write_text(json.dumps({"last_history_id": "700", "seen_ids": [],
                                      "pending_retry": []}))
    fake = FakeGmail(list_error=RuntimeError("503 backend error"))
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: fake)
    assert wr._fetch_new_gmail_messages("850") == []
    assert _state(state_path)["last_history_id"] == "700"  # NOT advanced


def test_404_too_old_reanchors(state_path, monkeypatch):
    state_path.write_text(json.dumps({"last_history_id": "700", "seen_ids": [],
                                      "pending_retry": []}))
    fake = FakeGmail(list_error=RuntimeError("HttpError 404: history not found"))
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: fake)
    assert wr._fetch_new_gmail_messages("850") == []
    assert _state(state_path)["last_history_id"] == 850


def test_per_message_fetch_failure_goes_to_pending(state_path, monkeypatch):
    state_path.write_text(json.dumps({"last_history_id": "700", "seen_ids": [],
                                      "pending_retry": []}))
    fake = FakeGmail(pages=[_page(["good", "flaky"], hid="900")],
                     message_errors={"flaky"})
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: fake)
    msgs = wr._fetch_new_gmail_messages("850")
    assert [m["meta"]["id"] for m in msgs] == ["good"]
    s = _state(state_path)
    assert [p["id"] for p in s["pending_retry"]] == ["flaky"]
    assert s["last_history_id"] == 900  # listing succeeded -> advance


def test_first_push_anchors_without_fetch(state_path, monkeypatch):
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: FakeGmail())
    assert wr._fetch_new_gmail_messages("500") == []
    assert _state(state_path)["last_history_id"] == 500


# ---------------------------------------------------------------------------
# Retry sweep
# ---------------------------------------------------------------------------

def test_sweep_retries_and_marks_seen(state_path, monkeypatch):
    wr._add_gmail_pending_retry("r1")
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: object())
    monkeypatch.setattr(wr, "_fetch_gmail_message", lambda svc, mid: _msg(mid))
    monkeypatch.setattr(wr, "_handle_gmail_message", lambda m: None)
    wr._sweep_gmail_pending()
    s = _state(state_path)
    assert "r1" in s["seen_ids"]
    assert s["pending_retry"] == []


def test_history_id_stored_as_int_even_when_api_returns_string(state_path, monkeypatch):
    """Gmail's history.list returns historyId as a STRING; storing it verbatim
    broke the watch-renewal cron with str>int (2026-07-02 incident)."""
    state_path.write_text(json.dumps({"last_history_id": 700, "seen_ids": [],
                                      "pending_retry": []}))
    fake = FakeGmail(pages=[_page(["a"], hid="900")])  # string hid, like the real API
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: fake)
    wr._fetch_new_gmail_messages("850")
    assert _state(state_path)["last_history_id"] == 900  # int, not "900"


def test_watch_seed_survives_string_state(tmp_path):
    """Renewal must not die when the state file holds a string history id."""
    import setup_gmail_watch as sgw
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"last_history_id": "5851800", "seen_ids": []}))
    # New watch id is AHEAD of stored -> keep the stored (older) pointer so the
    # gap still gets fetched; crucially: no str>int TypeError.
    assert sgw.seed_state_pointer(p, 5851900) is False
    assert json.loads(p.read_text())["last_history_id"] == "5851800"


def test_watch_seed_reseeds_on_rewind_and_missing(tmp_path):
    import setup_gmail_watch as sgw
    p = tmp_path / "state.json"
    # Missing state -> seed
    assert sgw.seed_state_pointer(p, 100) is True
    assert json.loads(p.read_text())["last_history_id"] == 100
    # Stored ahead of the fresh watch (Gmail reset / new mailbox) -> reseed
    p.write_text(json.dumps({"last_history_id": "999999", "seen_ids": []}))
    assert sgw.seed_state_pointer(p, 500) is True
    assert json.loads(p.read_text())["last_history_id"] == 500


def test_sweep_gives_up_after_max_attempts(state_path, monkeypatch):
    for _ in range(wr.GMAIL_MAX_RETRY_ATTEMPTS):
        wr._add_gmail_pending_retry("hopeless")
    monkeypatch.setattr(wr, "_get_gmail_service", lambda: object())

    def never_called(svc, mid):
        raise AssertionError("should not refetch a given-up message")
    monkeypatch.setattr(wr, "_fetch_gmail_message", never_called)
    wr._sweep_gmail_pending()
    s = _state(state_path)
    assert "hopeless" in s["seen_ids"]  # retired, no longer retried
    assert s["pending_retry"] == []

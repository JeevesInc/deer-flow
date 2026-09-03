"""Tests for the 2026-07-08 proposal-quality fixes.

Invariants:
  - Learned-pattern lookup goes through the Gateway HTTP endpoint (embedded
    Qdrant is single-process; in-process mem0 reads from the receiver silently
    failed 501 times in a row before this).
  - Chronic lookup failure ESCALATES: after PATTERN_LOOKUP_ALERT_THRESHOLD
    consecutive failures the owner gets a Slack alert (at most once per
    PATTERN_LOOKUP_ALERT_INTERVAL). Success resets the counter.
  - One card per Gmail thread: follow-ups within the freshness window nest
    under the original card instead of spawning new top-level cards.
  - Gateway /api/memory/mem0 accepts a user_id namespace param.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import webhook_receiver as wr


# ---------------------------------------------------------------------------
# Pattern lookup via Gateway
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, status=200, exc=None):
        self._payload = payload
        self._status = status
        self._exc = exc
        self.calls = []

    def __call__(self, *a, **kw):  # httpx.Client(...) → self
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        self.calls.append((url, params))
        if self._exc:
            raise self._exc
        return _FakeResponse(self._payload, self._status)


@pytest.fixture(autouse=True)
def _reset_lookup_state(tmp_path, monkeypatch):
    # Redirect the learned-pattern disk cache into tmp_path. Without this the
    # write-through cache added 2026-09-02 would (a) pollute the live
    # .deer-flow cache with test strings and (b) leak patterns between tests.
    monkeypatch.setattr(wr, "_pattern_cache_path",
                        lambda: tmp_path / "proposal_pattern_cache.json")
    wr._pattern_lookup_failures = 0
    wr._pattern_lookup_last_alert = 0.0
    yield
    wr._pattern_lookup_failures = 0
    wr._pattern_lookup_last_alert = 0.0


def _patch_http(monkeypatch, fake):
    monkeypatch.setattr(wr.httpx, "Client", fake)


def test_patterns_fetched_via_gateway(monkeypatch):
    fake = _FakeClient(payload={"results": [{"memory": "p1"}, {"memory": " p2 "}], "count": 2})
    _patch_http(monkeypatch, fake)
    out = wr._get_proposal_patterns("a@b.com", "b.com", "subj")
    assert out == ["p1", "p2"]
    url, params = fake.calls[0]
    assert url.endswith("/api/memory/mem0")
    assert params["user_id"] == wr.PROPOSAL_PATTERNS_USER_ID


def test_success_resets_failure_counter(monkeypatch):
    wr._pattern_lookup_failures = 7
    _patch_http(monkeypatch, _FakeClient(payload={"results": []}))
    wr._get_proposal_patterns("a@b.com", "b.com", "s")
    assert wr._pattern_lookup_failures == 0


def test_error_key_counts_as_failure(monkeypatch):
    _patch_http(monkeypatch, _FakeClient(payload={"results": [], "error": "boom"}))
    assert wr._get_proposal_patterns("a@b.com", "b.com", "s") == []
    assert wr._pattern_lookup_failures == 1


def test_chronic_failure_alerts_owner_once(monkeypatch):
    """Cold cache + chronic outage = the classifier really is blind → alert once."""
    _patch_http(monkeypatch, _FakeClient(exc=RuntimeError("conn refused")))
    alerts = []
    monkeypatch.setattr(wr, "_post_slack", lambda text, **kw: alerts.append(text) or {})
    for _ in range(wr.PATTERN_LOOKUP_ALERT_THRESHOLD + 5):
        wr._get_proposal_patterns("a@b.com", "b.com", "s")
    assert len(alerts) == 1  # alerted at threshold, then suppressed for 24h
    # The alert must carry the remediation, not just the symptom.
    assert "fix" in alerts[0].lower()
    assert "learned patterns" in alerts[0].lower()


def test_cached_patterns_served_during_outage(monkeypatch):
    """2026-09-02: a Qdrant outage must DEGRADE the feedback loop, not sever it."""
    _patch_http(monkeypatch, _FakeClient(payload={"results": [{"memory": "p1"}]}))
    assert wr._get_proposal_patterns("a@b.com", "b.com", "s") == ["p1"]

    _patch_http(monkeypatch, _FakeClient(exc=RuntimeError("WinError 10061")))
    assert wr._get_proposal_patterns("a@b.com", "b.com", "other subject") == ["p1"]


def test_cache_hit_suppresses_owner_alert(monkeypatch):
    """Degraded-but-working is a log line, not a page."""
    _patch_http(monkeypatch, _FakeClient(payload={"results": [{"memory": "p1"}]}))
    wr._get_proposal_patterns("a@b.com", "b.com", "s")

    alerts = []
    monkeypatch.setattr(wr, "_post_slack", lambda text, **kw: alerts.append(text) or {})
    _patch_http(monkeypatch, _FakeClient(exc=RuntimeError("WinError 10061")))
    for _ in range(wr.PATTERN_LOOKUP_ALERT_THRESHOLD + 5):
        wr._get_proposal_patterns("a@b.com", "b.com", "s")
    assert alerts == []


def test_cache_falls_back_to_domain_for_new_sender(monkeypatch):
    _patch_http(monkeypatch, _FakeClient(payload={"results": [{"memory": "dom"}]}))
    wr._get_proposal_patterns("known@b.com", "b.com", "s")

    _patch_http(monkeypatch, _FakeClient(exc=RuntimeError("WinError 10061")))
    assert wr._get_proposal_patterns("brand.new@b.com", "b.com", "s") == ["dom"]


def test_stale_cache_is_not_served(monkeypatch):
    _patch_http(monkeypatch, _FakeClient(payload={"results": [{"memory": "p1"}]}))
    wr._get_proposal_patterns("a@b.com", "b.com", "s")

    monkeypatch.setattr(wr, "PROPOSAL_PATTERN_CACHE_MAX_AGE", -1)
    _patch_http(monkeypatch, _FakeClient(exc=RuntimeError("WinError 10061")))
    assert wr._get_proposal_patterns("a@b.com", "b.com", "s") == []


def test_below_threshold_no_alert(monkeypatch):
    _patch_http(monkeypatch, _FakeClient(exc=RuntimeError("conn refused")))
    alerts = []
    monkeypatch.setattr(wr, "_post_slack", lambda text, **kw: alerts.append(text) or {})
    for _ in range(wr.PATTERN_LOOKUP_ALERT_THRESHOLD - 1):
        wr._get_proposal_patterns("a@b.com", "b.com", "s")
    assert alerts == []


# ---------------------------------------------------------------------------
# Thread dedup
# ---------------------------------------------------------------------------

@pytest.fixture()
def threads_path(tmp_path, monkeypatch):
    p = tmp_path / "proposal_threads.json"
    monkeypatch.setattr(wr, "_proposal_threads_path", lambda: p)
    return p


def test_record_and_get_roundtrip(threads_path):
    wr._record_proposal_thread("t1", {"channel": "C1", "ts": "111.222"})
    entry = wr._get_proposal_thread("t1")
    assert entry["ts"] == "111.222"
    assert entry["channel"] == "C1"


def test_stale_entry_ignored(threads_path):
    old = (datetime.now(timezone.utc) - timedelta(seconds=wr.PROPOSAL_THREAD_FRESH_SECONDS + 60)).isoformat()
    threads_path.write_text(json.dumps({"t1": {"channel": "C1", "ts": "1.2", "posted_at": old}}))
    assert wr._get_proposal_thread("t1") == {}


def test_record_prunes_stale_entries(threads_path):
    old = (datetime.now(timezone.utc) - timedelta(seconds=wr.PROPOSAL_THREAD_FRESH_SECONDS + 60)).isoformat()
    threads_path.write_text(json.dumps({"old": {"channel": "C1", "ts": "1.2", "posted_at": old}}))
    wr._record_proposal_thread("new", {"channel": "C1", "ts": "3.4"})
    stored = json.loads(threads_path.read_text())
    assert "old" not in stored and "new" in stored


def test_missing_thread_id_noop(threads_path):
    wr._record_proposal_thread("", {"channel": "C1", "ts": "1.2"})
    assert wr._get_proposal_thread("") == {}
    assert not threads_path.exists()


def test_followup_posts_into_existing_slack_thread(monkeypatch):
    posts = []
    monkeypatch.setattr(wr, "_post_slack",
                        lambda text, blocks=None, channel=None, thread_ts=None:
                        posts.append({"channel": channel, "thread_ts": thread_ts}) or {"channel": channel, "ts": "9.9"})
    cls = {"priority": "medium", "category": "x", "summary": "s", "proposed_action": "", "reasoning": "r"}
    wr._post_action_proposal("a@b.com", "subj", cls, "mid", thread_ts="111.222", thread_channel="C7")
    assert posts[0]["thread_ts"] == "111.222"
    assert posts[0]["channel"] == "C7"


def test_first_card_posts_top_level(monkeypatch):
    posts = []
    monkeypatch.setattr(wr, "_post_slack",
                        lambda text, blocks=None, channel=None, thread_ts=None:
                        posts.append({"thread_ts": thread_ts}) or {"channel": "C", "ts": "1.1"})
    cls = {"priority": "high", "category": "x", "summary": "s", "proposed_action": "do", "reasoning": "r"}
    wr._post_action_proposal("a@b.com", "subj", cls, "mid")
    assert posts[0]["thread_ts"] is None


# ---------------------------------------------------------------------------
# Gateway memory router: user_id namespace passthrough
# ---------------------------------------------------------------------------

def test_search_mem0_passes_user_id(monkeypatch):
    import sys
    import types

    from app.gateway.routers import memory as mem_router

    seen = {}
    fake = types.ModuleType("deerflow.agents.memory.mem0_store")
    fake.search_memories = lambda q, top_k=10, **kw: seen.update({"q": q, "top_k": top_k, **kw}) or [{"memory": "m"}]
    fake.get_all_memories = lambda **kw: []
    monkeypatch.setitem(sys.modules, "deerflow.agents.memory.mem0_store", fake)

    out = mem_router.search_mem0(q="hello", limit=3, user_id="proposal-patterns")
    assert out["count"] == 1
    assert seen == {"q": "hello", "top_k": 3, "user_id": "proposal-patterns"}


def test_search_mem0_default_namespace_omits_user_id(monkeypatch):
    import sys
    import types

    from app.gateway.routers import memory as mem_router

    seen = {}
    fake = types.ModuleType("deerflow.agents.memory.mem0_store")
    fake.search_memories = lambda q, top_k=10, **kw: seen.update(kw) or []
    fake.get_all_memories = lambda **kw: []
    monkeypatch.setitem(sys.modules, "deerflow.agents.memory.mem0_store", fake)

    mem_router.search_mem0(q="hello", limit=3)
    assert "user_id" not in seen

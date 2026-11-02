"""Durable Tickets (Super-Agent proposal Phase 1)."""

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from deerflow.tickets import store as S
from deerflow.tools.builtins import ticket_tools as TT

BACKEND = Path(__file__).resolve().parents[1]


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("ticket_watchdog_cron", BACKEND / "scripts" / "ticket_watchdog_cron.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store(tmp_path):
    return S.TicketStore(tmp_path / "tickets.db")


def _age(store, tid, when):
    with store._conn() as c:
        c.execute("UPDATE tickets SET last_progress_at=? WHERE id=?", (when.isoformat(), tid))


def test_open_dedupes_same_title_on_thread(store):
    t1, c1 = store.open("NB BBC", "Brian: send the BBC", langgraph_thread_id="th1")
    t2, c2 = store.open("nb bbc", "again", langgraph_thread_id="th1")
    assert c1 and not c2 and t1["id"] == t2["id"]
    _, c3 = store.open("NB BBC", "other thread", langgraph_thread_id="th2")
    assert c3


def test_open_requires_mandate(store):
    with pytest.raises(S.TicketError):
        store.open("x", "  ")


def test_close_refuses_without_deliverable(store):
    t, _ = store.open("Deck", "Brian: build the deck")
    with pytest.raises(S.TicketError, match="deliverable"):
        store.close(t["id"], "done")
    out = store.close(t["id"], "https://drive.google.com/file/d/abc")
    assert out["status"] == S.DELIVERED and out["deliverables"][0]["deliverable"].startswith("https://")
    with pytest.raises(S.TicketError):
        store.progress(t["id"], "more")


def test_agent_cannot_set_terminal_status_via_progress(store):
    t, _ = store.open("x", "y")
    for bad in (S.DELIVERED, S.STOPPED):
        with pytest.raises(S.TicketError):
            store.progress(t["id"], "note", status=bad)
    assert store.progress(t["id"], "draft r123 ready", status=S.WAITING_ON_BRIAN)["status"] == S.WAITING_ON_BRIAN


def test_stop_requires_brian_quote(store):
    t, _ = store.open("x", "y")
    with pytest.raises(S.TicketError):
        store.stop(t["id"], "")
    assert store.stop(t["id"], "drop it")["status"] == S.STOPPED


def test_business_seconds_skips_nights_and_weekends():
    pt = S._PT
    fri_5pm = datetime(2026, 10, 30, 17, 0, tzinfo=pt)
    mon_10am = datetime(2026, 11, 2, 10, 0, tzinfo=pt)
    assert S.business_seconds_between(fri_5pm, mon_10am) == 3 * 3600  # 1h Fri + 2h Mon
    assert S.business_seconds_between(mon_10am, fri_5pm) == 0


def test_origin_reverse_lookup(tmp_path):
    p = tmp_path / "store.json"
    p.write_text(json.dumps({"slack:D1:1793.1": {"thread_id": "abc"}, "slack:D1": {"thread_id": "zzz"}}))
    assert TT.origin_for_thread("abc", p) == "slack:D1:1793.1"
    assert TT.origin_for_thread("nope", p) is None
    assert TT.origin_for_thread(None, p) is None


def test_tools_roundtrip(store, monkeypatch):
    monkeypatch.setattr(TT, "_store", store)
    monkeypatch.setattr(TT, "origin_for_thread", lambda tid, *a: "slack:D1:1.2")

    class RT:
        context = {"thread_id": "th9"}
        state = {}

    out = TT.ticket_open_tool.func(runtime=RT(), title="CIM Exh E", mandate="Brian: file Exhibit E")
    assert out.startswith("Opened T-")
    tid = out.split()[1]
    assert store.get(tid)["origin"] == "slack:D1:1.2"
    assert "Error" in TT.ticket_close_tool.func(ticket_id=tid, deliverable="ok")
    assert "Error" in TT.ticket_update_tool.func(ticket_id=tid, note="x", status="delivered")
    assert tid in TT.ticket_list_tool.func(runtime=RT(), this_thread_only=True)
    assert TT.ticket_close_tool.func(ticket_id=tid, deliverable="draft r-555 in Gmail").startswith("Delivered")


def test_watchdog_redispatches_then_alerts(store):
    W = _load_watchdog()
    now = datetime(2026, 11, 3, 16, 0, tzinfo=S._PT)  # Tue 4pm
    t, _ = store.open("Stale", "Brian: do it", langgraph_thread_id="th1", origin="slack:D1:1.2")
    _age(store, t["id"], now - timedelta(days=1))
    fresh, _ = store.open("Fresh", "Brian: other")
    _age(store, fresh["id"], now - timedelta(hours=1))
    waiting, _ = store.open("Waiting", "Brian: decide")
    store.progress(waiting["id"], "draft ready", status=S.WAITING_ON_BRIAN)
    _age(store, waiting["id"], now - timedelta(days=3))

    dispatched, notes = [], []

    def fake_dispatch(st, tk):
        dispatched.append(tk["id"])
        st.mark_redispatched(tk["id"])
        _age(st, tk["id"], now - timedelta(days=1))  # still no progress

    def notify(who, text, thread_ts=None):
        notes.append(text)

    for _ in range(W.MAX_REDISPATCH):
        assert W.check_once(store, now, fake_dispatch, notify) == [(t["id"], "redispatched")]
    assert dispatched == [t["id"]] * W.MAX_REDISPATCH
    assert W.check_once(store, now, fake_dispatch, notify) == [(t["id"], "alerted")]
    assert W.check_once(store, now, fake_dispatch, notify) == []  # cooldown
    assert len(notes) == 1 and t["id"] in notes[0]
    assert store.get(t["id"])["status"] == S.OPEN  # never silently closed


def test_watchdog_ping_mode_does_not_dispatch(store, monkeypatch):
    W = _load_watchdog()
    monkeypatch.setattr(W, "MODE", "ping")
    now = datetime(2026, 11, 3, 16, 0, tzinfo=S._PT)
    t, _ = store.open("Stale", "Brian: do it")
    _age(store, t["id"], now - timedelta(days=1))
    called = []
    acts = W.check_once(store, now, lambda *a: called.append(1), lambda *a, **k: None)
    assert acts == [(t["id"], "alerted")] and not called


def test_resume_prompt_carries_mandate_and_draft_rule(store):
    W = _load_watchdog()
    t, _ = store.open("X", 'Brian: "send NB the BBC"')
    p = W.resume_prompt(store.get(t["id"]))
    assert "send NB the BBC" in p and "Gmail draft" in p and "ticket_close" in p

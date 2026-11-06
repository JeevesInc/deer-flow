"""Super-Agent Phases 2-4: subscriptions, steering, goal watchdog, sources, panel."""

import asyncio
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares import steering_middleware as SM
from deerflow.tickets import steering
from deerflow.tickets import store as S

BACKEND = Path(__file__).resolve().parents[1]


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("ticket_watchdog_cron2", BACKEND / "scripts" / "ticket_watchdog_cron.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.TicketStore(tmp_path / "tickets.db")
    from deerflow.tools.builtins import ticket_tools as TT

    monkeypatch.setattr(TT, "_store", st)
    return st


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("DEERFLOW_STATE_DIR", str(d))
    return d


# -- store: subscriptions / reopen -------------------------------------------


def test_tickets_for_key_prefers_active_and_reopen(store):
    a, _ = store.open("Old", "m", origin="slack:D1:1")
    store.subscribe(a["id"], "gmail:abc")
    store.close(a["id"], "https://drive.google.com/x")
    b, _ = store.open("New", "m")
    store.subscribe(b["id"], "gmail:abc")
    owners = store.tickets_for_key("gmail:abc")
    assert [t["id"] for t in owners] == [b["id"], a["id"]]
    re_a = store.reopen(a["id"], "reply from Max")
    assert re_a["status"] == S.OPEN and re_a["redispatch_count"] == 0
    store.stop(b["id"], "drop it")
    assert [t["id"] for t in store.tickets_for_key("gmail:abc")] == [a["id"]]
    with pytest.raises(S.TicketError):
        store.reopen(b["id"], "x")


# -- steering inbox + heartbeats ---------------------------------------------


def test_steering_push_pop_once(state_dir):
    steering.push("th/1", "use V2 instead")
    steering.push("th/1", "and skip MX")
    got = steering.pop_all("th/1")
    assert [g["text"] for g in got] == ["use V2 instead", "and skip MX"]
    assert steering.pop_all("th/1") == []


def test_heartbeats(state_dir):
    assert steering.read_beats() == {}
    steering.beat("gmail")
    assert "gmail" in steering.read_beats()


# -- SteeringMiddleware -------------------------------------------------------


def _rt(thread_id="th1"):
    return SimpleNamespace(context={"thread_id": thread_id})


def test_middleware_injects_steering(state_dir, store):
    mw = SM.SteeringMiddleware()
    steering.push("th1", "stop, use V2 instead")
    out = mw.before_model({"messages": [HumanMessage("go")]}, _rt())
    assert out and "use V2 instead" in out["messages"][0].content
    assert mw.before_model({"messages": [HumanMessage("go")]}, _rt()) is None


def test_middleware_never_splits_tool_pair(state_dir, store):
    mw = SM.SteeringMiddleware()
    steering.push("th1", "hi")
    ai = AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1"}])
    assert mw.before_model({"messages": [ai]}, _rt()) is None
    assert steering.pending("th1")  # still waiting for the next safe point


def test_middleware_auto_subscribes_gmail_draft(state_dir, store):
    t, _ = store.open("Reply to Max", "Brian: draft the reply", langgraph_thread_id="th1")
    mw = SM.SteeringMiddleware()
    tool_out = "Draft created successfully!\n  Draft ID: r-123\n  Thread:   18f2a3b4c5d6e7f8\n"
    msgs = [HumanMessage("go"), AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1"}]), ToolMessage(content=tool_out, tool_call_id="c1", name="bash")]
    mw.before_model({"messages": msgs}, _rt())
    assert "gmail:18f2a3b4c5d6e7f8" in store.subscriptions(t["id"])


def test_middleware_repeated_error_note_once(state_dir, store):
    mw = SM.SteeringMiddleware()
    msgs = [HumanMessage("go")]
    for i in range(4):
        msgs.append(AIMessage(content="", tool_calls=[{"name": "bash", "args": {"c": i}, "id": f"c{i}"}]))
        msgs.append(ToolMessage(content=f"Error: connection to server at port 54{i}39 failed\nOperationalError: timeout expired {i}000", tool_call_id=f"c{i}", name="bash"))
    out = mw.before_model({"messages": msgs}, _rt())
    assert out and "[WATCHDOG]" in out["messages"][-1].content
    assert mw.before_model({"messages": msgs}, _rt()) is None


# -- watchdog: goal check, sources, panel, metrics ----------------------------


def test_goal_check_escalates_steer_cancel_alert(store, state_dir, monkeypatch):
    W = _load_watchdog()
    t, _ = store.open("NB BBC", "Brian: build the NB BBC", langgraph_thread_id="th1")
    msgs = [{"type": "ai", "content": "", "tool_calls": [{"name": "bash", "args": {"cmd": "ls"}}]}]
    events = []
    kw = dict(
        fetch=lambda tid: ("busy", msgs),
        judge_fn=lambda m, a: {"verdict": "drifting", "reason": "building the CIM report"},
        notify=lambda who, text, thread_ts=None: events.append(("dm", text)),
        dispatch=lambda st, tk, reason=None: events.append(("dispatch", reason)),
        cancel=lambda tid: events.append(("cancel", tid)),
    )
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    state = {}
    assert W.goal_check(store, state, **kw) == [(t["id"], "steered")]
    assert any("Re-read the mandate" in p["text"] for p in steering.pop_all("th1"))
    assert W.goal_check(store, state, **kw) == [(t["id"], "cancelled_redispatched")]
    assert events[0] == ("cancel", "th1") and events[1][0] == "dispatch"
    assert W.goal_check(store, state, **kw) == [(t["id"], "alerted_drift")]
    assert W.goal_check(store, state, **kw) == []  # hands off after the DM


def test_goal_check_on_track_or_idle_does_nothing(store, state_dir):
    W = _load_watchdog()
    store.open("X", "m", langgraph_thread_id="th1")
    assert W.goal_check(store, {}, fetch=lambda tid: ("idle", []), judge_fn=lambda *a: 1 / 0) == []
    msgs = [{"type": "ai", "content": "working", "tool_calls": []}]
    assert W.goal_check(store, {}, fetch=lambda tid: ("busy", msgs), judge_fn=lambda *a: {"verdict": "on_track"}) == []


def test_source_check_alerts_once_per_day():
    W = _load_watchdog()
    now = datetime(2026, 11, 3, 16, 0, tzinfo=S._PT)
    beats = {"gmail": (now - timedelta(days=1)).isoformat()}
    sent = []
    state = {}
    notify = lambda who, text, thread_ts=None: sent.append(text)  # noqa: E731
    assert W.source_check(state, now, beats, notify) == [("gmail", "quiet")]
    assert W.source_check(state, now + timedelta(hours=1), beats, notify) == []
    fresh = {"gmail": now.isoformat()}
    assert W.source_check(state, now, fresh, notify) == [] and "gmail" not in state["source_alerts"]
    assert len(sent) == 1


class FakePanel:
    def __init__(self):
        self.calls = []

    def channel(self):
        return "D1"

    def post(self, ch, text):
        self.calls.append(("post", text))
        return "100.1"

    def update(self, ch, ts, text):
        self.calls.append(("update", ts))

    def delete(self, ch, ts):
        self.calls.append(("delete", ts))


def test_panel_post_update_clear(store):
    W = _load_watchdog()
    p, state = FakePanel(), {}
    assert W.panel_sync(store, state, p) == "cleared"  # nothing waiting: nothing shown
    t, _ = store.open("Spark reply", "m")
    store.progress(t["id"], "draft r-9 ready", status=S.WAITING_ON_BRIAN)
    assert W.panel_sync(store, state, p) == "posted" and "Spark reply" in p.calls[-1][1]
    assert W.panel_sync(store, state, p) == "unchanged"
    t2, _ = store.open("SBLC", "m")
    store.progress(t2["id"], "need your call on $40M vs $50M", status=S.WAITING_ON_BRIAN)
    assert W.panel_sync(store, state, p) == "updated"
    store.reopen(t["id"], "Brian replied")
    store.reopen(t2["id"], "Brian replied")
    assert W.panel_sync(store, state, p) == "cleared" and p.calls[-1] == ("delete", "100.1")
    assert "ts" not in state["panel"]


def test_weekly_metrics_due_monday_once():
    W = _load_watchdog()
    mon = datetime(2026, 11, 2, 9, 0, tzinfo=S._PT)
    assert W.weekly_metrics_due({}, mon)
    assert not W.weekly_metrics_due({"metrics_week": mon.strftime("%G-W%V")}, mon)
    assert not W.weekly_metrics_due({}, mon - timedelta(hours=2))
    assert not W.weekly_metrics_due({}, mon + timedelta(days=1))


def test_summarize_actions_keeps_last_n():
    W = _load_watchdog()
    msgs = [{"type": "ai", "content": f"step {i}", "tool_calls": [{"name": "bash", "args": {"i": i}}]} for i in range(20)]
    out = W.summarize_actions(msgs, n=15).splitlines()
    assert len(out) == 15 and out[-1].startswith("said: step 19")


# -- gateway: steering hand-off + leftover drain -------------------------------


def test_manager_steers_busy_thread_and_drains_leftovers(state_dir):
    from app.channels.manager import ChannelManager
    from app.channels.message_bus import InboundMessage

    handled = []

    async def fake_handle_chat(m, extra_context=None):
        handled.append(m.text)

    fake = SimpleNamespace(
        _thread_runs={},
        store=SimpleNamespace(get_thread_id=lambda *a, **k: "th1"),
        _handle_chat=fake_handle_chat,
    )
    fake._after_chat = lambda m: ChannelManager._after_chat(fake, m)
    first = InboundMessage(channel_name="slack", chat_id="D1", user_id="U05B5HGNCN9", text="build it", topic_id="1.1")
    second = InboundMessage(channel_name="slack", chat_id="D1", user_id="U05B5HGNCN9", text="use V2", topic_id="1.1")
    assert ChannelManager._steer_into_running(fake, "th1", second) is False  # idle: normal run
    fake._thread_runs["th1"] = id(first)
    assert ChannelManager._steer_into_running(fake, "th1", second) is True
    asyncio.run(ChannelManager._after_chat(fake, second))  # not the owner: no-op
    assert fake._thread_runs == {"th1": id(first)}
    asyncio.run(ChannelManager._after_chat(fake, first))  # run ended before consuming
    assert handled == ["use V2"] and fake._thread_runs == {}


def test_tickets_router(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.gateway.routers import tickets as TR

    monkeypatch.setenv("TICKETS_DB", str(tmp_path / "t.db"))
    st = S.TicketStore()
    t, _ = st.open("Exhibit E <CIM>", "m")
    app = FastAPI()
    app.include_router(TR.router)
    c = TestClient(app)
    assert c.get("/api/tickets").json()["count"] == 1
    page = c.get("/tickets").text
    assert t["id"] in page and "&lt;CIM&gt;" in page


# -- regressions: re-dispatch payload + judge parsing -------------------------


def test_run_body_sets_context_not_configurable():
    """LangGraph 0.6+ 400s a run that sets both configurable and context."""
    from deerflow.tickets import runner as R

    body = R.run_body("th1", "resume")
    assert "configurable" not in body["config"]
    assert body["context"]["thread_id"] == "th1" and body["context"]["thinking_enabled"] is True
    assert body["config"]["recursion_limit"] == 500


def _msg(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def test_parse_verdict_skips_thinking_blocks():
    W = _load_watchdog()
    thinking = SimpleNamespace(type="thinking", thinking="")
    text = SimpleNamespace(type="text", text='{"verdict": "looping", "reason": "same query thrice"}')
    assert W.parse_verdict(_msg(thinking, text))["verdict"] == "looping"


def test_parse_verdict_thinking_only_names_stop_reason():
    """The old failure: thinking ate max_tokens, no text, opaque JSONDecodeError."""
    W = _load_watchdog()
    with pytest.raises(ValueError, match="stop_reason=max_tokens"):
        W.parse_verdict(_msg(SimpleNamespace(type="thinking", thinking=""), stop_reason="max_tokens"))

#!/usr/bin/env python3
"""ticket-watchdog cron — Super-Agent proposal Phases 1, 3 and 4.

Every POLL_SECONDS (10 min):

1. **Stale Tickets (Phase 1).** An ``open`` Ticket with no progress for
   STALE_BUSINESS_HOURS business hours (Mon-Fri 08-18 PT) is re-dispatched on
   its own LangGraph thread and the result posted back into its Slack thread.
   After MAX_REDISPATCH attempts: a one-line DM to Brian. ``waiting_on_external``
   gets a DM after EXTERNAL_STALE_BUSINESS_HOURS. ``waiting_on_brian`` is never
   re-dispatched; it goes on the Panel. A Ticket is never closed here.
2. **Goal-level check (Phase 3).** For a busy thread that owns an open Ticket,
   a model compares the Ticket's mandate with the run's last 15 actions:
   on_track / drifting / looping. Escalation per Ticket: 1st flag = steering
   note into the run; 2nd = cancel and re-dispatch with a resume prompt;
   3rd = one-line DM to Brian, then hands off.
3. **Quiet Sources (Phase 3).** A Source with no events for longer than its
   normal gap (gmail: SOURCE_QUIET_BUSINESS_HOURS) -> one DM per business day.
4. **Panel (Phase 4).** One pinned message in Brian's bot DM, edited in place,
   listing only Tickets waiting on him. Deleted when nothing waits.
5. **Weekly metrics (Phase 0/§5).** Mondays after 08:00 PT, one DM line.

Everything here is internal. The outbound lockdown keeps external mail a draft.
Exposes ``run_loop()`` for cron_supervisor (never returns).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("ticket_watchdog")

_BACKEND = Path(__file__).resolve().parent.parent
_HARNESS = _BACKEND / "packages" / "harness"
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))

from deerflow.tickets import runner as R  # noqa: E402
from deerflow.tickets import steering as STEER  # noqa: E402
from deerflow.tickets import store as S  # noqa: E402

POLL_SECONDS = int(os.environ.get("TICKET_WATCHDOG_POLL_SECONDS", 600))
STALE_BUSINESS_HOURS = float(os.environ.get("TICKET_STALE_BUSINESS_HOURS", 4))
EXTERNAL_STALE_BUSINESS_HOURS = float(os.environ.get("TICKET_EXTERNAL_STALE_BUSINESS_HOURS", 20))
MAX_REDISPATCH = int(os.environ.get("TICKET_MAX_REDISPATCH", 2))
MODE = os.environ.get("TICKET_REDISPATCH_MODE", "auto")  # auto | ping
ALERT_COOLDOWN_BUSINESS_HOURS = 10
JUDGE_MODEL = os.environ.get("TICKET_JUDGE_MODEL", "claude-opus-5-5")
JUDGE_ENABLED = os.environ.get("TICKET_JUDGE_ENABLED", "1") == "1"
SOURCE_QUIET_BUSINESS_HOURS = {"gmail": float(os.environ.get("GMAIL_QUIET_BUSINESS_HOURS", 6))}
STATE_PATH = _BACKEND / ".deer-flow" / "ticket_watchdog_state.json"


# -- state -------------------------------------------------------------------


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _owner() -> str:
    return R.owner_id()


# -- 1. stale tickets --------------------------------------------------------


def resume_prompt(t: dict, reason: str | None = None) -> str:
    notes = "\n".join(f"- {n['ts']}: {n['note']}" for n in t["notes"][-5:]) or "- (no progress recorded)"
    why = reason or f"is still OPEN with no progress since {t['last_progress_at']}"
    return (
        f'[Ticket watchdog] Ticket {t["id"]} "{t["title"]}" {why}.\n\nMandate (Brian, verbatim):\n{t["mandate"]}\n\n'
        f"Last progress:\n{notes}\n\n"
        "Resume and DELIVER it now in this run. Check what already exists before redoing work. "
        "Record progress with ticket_update; finish with ticket_close(deliverable). If you need a decision "
        "from Brian, set status waiting_on_brian and ask one specific question. External communication stays "
        "a Gmail draft for Brian. Reply with the result only, no narration."
    )


def redispatch(store, t: dict, reason: str | None = None) -> None:
    try:
        R.run_on_ticket(t, resume_prompt(t, reason), on_start=lambda: store.mark_redispatched(t["id"]))
    except R.ThreadBusy:
        logger.info("[ticket-watchdog] %s thread busy, skip this cycle", t["id"])


def redispatch_bg(store, t: dict, reason: str | None = None) -> None:
    """Non-blocking: a 30-minute run must not hold up the panel or other Tickets."""
    import threading

    threading.Thread(target=redispatch, args=(store, t, reason), daemon=True, name=f"ticket-redispatch-{t['id']}").start()


def _alert_due(t: dict, now: datetime) -> bool:
    last = S._parse(t.get("last_alert_at"))
    return last is None or S.business_seconds_between(last, now) >= ALERT_COOLDOWN_BUSINESS_HOURS * 3600


def check_once(store, now: datetime | None = None, dispatch=redispatch_bg, notify=R.slack_send) -> list[tuple[str, str]]:
    now = now or datetime.now(UTC)
    actions: list[tuple[str, str]] = []
    for t in store.list(statuses=(S.OPEN, S.WAITING_ON_EXTERNAL)):
        if t["status"] == S.OPEN and S.is_stale(t, STALE_BUSINESS_HOURS, now):
            if MODE == "auto" and t["redispatch_count"] < MAX_REDISPATCH:
                try:
                    dispatch(store, t)
                    actions.append((t["id"], "redispatched"))
                except Exception as e:
                    logger.exception("[ticket-watchdog] re-dispatch of %s failed: %s", t["id"], e)
                    actions.append((t["id"], "redispatch_failed"))
            elif _alert_due(t, now):
                n = t["redispatch_count"]
                notify(
                    _owner(),
                    f'Ticket {t["id"]} "{t["title"]}" is still open' + (f" after {n} re-dispatch(es)" if n else "") + f"; last progress {t['last_progress_at']}.",
                )
                store.mark_alerted(t["id"], now)
                actions.append((t["id"], "alerted"))
        elif t["status"] == S.WAITING_ON_EXTERNAL and S.is_stale(t, EXTERNAL_STALE_BUSINESS_HOURS, now) and _alert_due(t, now):
            notify(_owner(), f'Ticket {t["id"]} "{t["title"]}" has been waiting on an external party since {t["last_progress_at"]}.')
            store.mark_alerted(t["id"], now)
            actions.append((t["id"], "alerted_external"))
    return actions


# -- 2. goal-level check -----------------------------------------------------


def summarize_actions(messages: list, n: int = 15) -> str:
    """Last n agent actions (tool calls and text replies) as compact lines."""
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("type") != "ai":
            continue
        for tc in m.get("tool_calls") or []:
            args = json.dumps(tc.get("args", {}), default=str)[:200]
            lines.append(f"tool {tc.get('name')}: {args}")
        c = m.get("content")
        txt = c if isinstance(c, str) else "".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
        if txt and txt.strip():
            lines.append(f"said: {txt.strip()[:200]}")
    return "\n".join(lines[-n:])


JUDGE_PROMPT = """You supervise an AI analyst working on one task for Brian.

Task mandate (Brian's words):
{mandate}

The analyst's last actions, oldest first:
{actions}

Is the analyst on track to deliver THIS mandate? Answer with JSON only:
{{"verdict": "on_track" | "drifting" | "looping", "reason": "<one sentence>"}}
drifting = working on something other than the mandate. looping = repeating work without new progress."""


def judge(mandate: str, actions: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    # Opus 5.5 always thinks; a 200-token cap was spent entirely on thinking,
    # returned no text block, and every verdict failed to parse.
    msg = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=4000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(mandate=mandate[:3000], actions=actions)}],
    )
    return parse_verdict(msg)


def parse_verdict(msg) -> dict:
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON in judge reply (stop_reason={getattr(msg, 'stop_reason', None)}): {text[:200]!r}")
    out = json.loads(text[start : end + 1])
    if out.get("verdict") not in ("on_track", "drifting", "looping"):
        raise ValueError(f"bad verdict: {text[:200]}")
    return out


def _thread_messages(thread_id: str) -> tuple[str | None, list]:
    import httpx

    with httpx.Client(timeout=30) as http:
        status = R.thread_status(http, thread_id)
        if status != "busy":
            return status, []
        r = http.get(f"{R.LANGGRAPH_URL}/threads/{thread_id}/state")
        msgs = (r.json().get("values") or {}).get("messages", []) if r.status_code == 200 else []
        return status, msgs


def goal_check(store, state: dict, *, fetch=_thread_messages, judge_fn=judge, notify=R.slack_send, dispatch=redispatch_bg, cancel=R.cancel_runs) -> list[tuple[str, str]]:
    actions: list[tuple[str, str]] = []
    flags = state.setdefault("drift_flags", {})
    for t in store.list(statuses=(S.OPEN,)):
        tid = t.get("langgraph_thread_id")
        if not tid:
            continue
        try:
            status, msgs = fetch(tid)
        except Exception as e:
            logger.warning("[ticket-watchdog] state fetch failed for %s: %s", t["id"], e)
            continue
        if status != "busy" or not msgs:
            continue
        acts = summarize_actions(msgs)
        if not acts:
            continue
        try:
            v = judge_fn(t["mandate"], acts)
        except Exception as e:
            logger.warning("[ticket-watchdog] judge failed for %s: %s", t["id"], e)
            continue
        if v["verdict"] == "on_track":
            continue
        n = flags.get(t["id"], 0) + 1
        flags[t["id"]] = n
        reason = v.get("reason", v["verdict"])
        if n == 1:
            STEER.push(tid, f"[WATCHDOG] You look {v['verdict']}: {reason} Re-read the mandate and get back to it:\n{t['mandate'][:1500]}", source="watchdog")
            actions.append((t["id"], "steered"))
        elif n == 2:
            cancel(tid)
            time.sleep(2)
            try:
                dispatch(store, store.get(t["id"]) or t, f"was cancelled by the watchdog ({v['verdict']}: {reason})")
            except Exception as e:
                logger.exception("[ticket-watchdog] post-cancel re-dispatch failed for %s: %s", t["id"], e)
            actions.append((t["id"], "cancelled_redispatched"))
        elif n == 3:
            notify(_owner(), f'Ticket {t["id"]} "{t["title"]}" keeps {v["verdict"]} after a steer and a restart: {reason}')
            actions.append((t["id"], "alerted_drift"))
    return actions


# -- 3. quiet sources --------------------------------------------------------


def source_check(state: dict, now: datetime, beats: dict | None = None, notify=R.slack_send) -> list[tuple[str, str]]:
    beats = STEER.read_beats() if beats is None else beats
    alerted = state.setdefault("source_alerts", {})
    out = []
    for src, hours in SOURCE_QUIET_BUSINESS_HOURS.items():
        last = S._parse(beats.get(src))
        if last is None:
            continue  # never seen: nothing to compare against yet
        if S.business_seconds_between(last, now) < hours * 3600:
            alerted.pop(src, None)
            continue
        prev = S._parse(alerted.get(src))
        if prev and S.business_seconds_between(prev, now) < ALERT_COOLDOWN_BUSINESS_HOURS * 3600:
            continue
        notify(_owner(), f"Source `{src}` has had no events since {beats[src]} (>{hours:g} business hours). Check the {src} watch.")
        alerted[src] = now.isoformat(timespec="seconds")
        out.append((src, "quiet"))
    return out


# -- 4. panel ----------------------------------------------------------------


def panel_text(tickets: list[dict]) -> str:
    lines = ["*Waiting on you*"]
    for t in tickets:
        last = t["notes"][-1]["note"] if t["notes"] else ""
        lines.append(f"• `{t['id']}` {t['title']}" + (f": {last[:140]}" if last else "") + f" _(since {t['last_progress_at'][:10]})_")
    return "\n".join(lines)


class SlackPanel:
    def __init__(self, client=None):
        if client is None:
            from slack_sdk import WebClient

            client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        self.c = client

    def channel(self) -> str:
        return self.c.conversations_open(users=[_owner()])["channel"]["id"]

    def post(self, ch: str, text: str) -> str:
        ts = self.c.chat_postMessage(channel=ch, text=text)["ts"]
        try:
            self.c.pins_add(channel=ch, timestamp=ts)
        except Exception as e:
            logger.warning("[ticket-watchdog] pin failed: %s", e)
        return ts

    def update(self, ch: str, ts: str, text: str) -> None:
        self.c.chat_update(channel=ch, ts=ts, text=text)

    def delete(self, ch: str, ts: str) -> None:
        self.c.chat_delete(channel=ch, ts=ts)


def panel_sync(store, state: dict, panel=None) -> str:
    rows = store.list(statuses=(S.WAITING_ON_BRIAN,))
    p = state.setdefault("panel", {})
    text = panel_text(rows) if rows else ""
    h = hashlib.sha1(text.encode()).hexdigest()
    if h == p.get("hash"):
        return "unchanged"
    panel = panel or SlackPanel()
    ch = p.get("channel") or panel.channel()
    if not rows:
        if p.get("ts"):
            try:
                panel.delete(ch, p["ts"])
            except Exception as e:
                logger.warning("[ticket-watchdog] panel delete failed: %s", e)
        state["panel"] = {"hash": h, "channel": ch}
        return "cleared"
    if p.get("ts"):
        try:
            panel.update(ch, p["ts"], text)
            state["panel"] = {"hash": h, "channel": ch, "ts": p["ts"]}
            return "updated"
        except Exception as e:
            logger.warning("[ticket-watchdog] panel update failed, reposting: %s", e)
    state["panel"] = {"hash": h, "channel": ch, "ts": panel.post(ch, text)}
    return "posted"


# -- 5. weekly metrics -------------------------------------------------------


def weekly_metrics_due(state: dict, now: datetime) -> bool:
    pt = now.astimezone(S._PT)
    week = pt.strftime("%G-W%V")
    return pt.weekday() == 0 and pt.hour >= 8 and state.get("metrics_week") != week


def weekly_metrics(store, state: dict, now: datetime, notify=R.slack_send) -> None:
    from datetime import timedelta

    since = now - timedelta(days=7)
    rows = store.created_since(since)
    accepted = len(rows)
    delivered = sum(1 for t in rows if t["status"] == S.DELIVERED)
    stale = [t for t in store.list(statuses=(S.OPEN,)) if S.is_stale(t, 10, now)]
    routed = 0
    p = _BACKEND / ".deer-flow" / "ticket_routing.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                if S._parse(json.loads(line)["ts"]) >= since:
                    routed += 1
            except Exception:
                continue
    rec = {"week_ending": now.date().isoformat(), "accepted": accepted, "delivered": delivered, "open_stale_1bd": len(stale), "replies_routed": routed}
    with (_BACKEND / ".deer-flow" / "ticket_metrics.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    notify(_owner(), f"Tickets last 7d: {delivered}/{accepted} delivered, {len(stale)} open >1 business day, {routed} email replies routed to their Ticket.")
    state["metrics_week"] = now.astimezone(S._PT).strftime("%G-W%V")


# -- loop --------------------------------------------------------------------


def run_loop() -> None:
    store = S.TicketStore()
    logger.info("[ticket-watchdog] started (poll=%ss, stale=%sbh, mode=%s, judge=%s, db=%s)", POLL_SECONDS, STALE_BUSINESS_HOURS, MODE, JUDGE_MODEL if JUDGE_ENABLED else "off", store.path)
    while True:
        now = datetime.now(UTC)
        state = load_state()
        steps = [("stale", lambda: check_once(store, now)), ("sources", lambda: source_check(state, now)), ("panel", lambda: panel_sync(store, state))]
        if JUDGE_ENABLED:
            steps.insert(1, ("goal", lambda: goal_check(store, state)))
        if weekly_metrics_due(state, now):
            steps.append(("metrics", lambda: weekly_metrics(store, state, now)))
        for name, fn in steps:
            try:
                res = fn()
                if res and res != "unchanged":
                    logger.info("[ticket-watchdog] %s: %s", name, json.dumps(res, default=str))
            except Exception as e:
                logger.exception("[ticket-watchdog] %s step failed: %s", name, e)
        try:
            save_state(state)
        except Exception as e:
            logger.warning("[ticket-watchdog] state save failed: %s", e)
        time.sleep(POLL_SECONDS)

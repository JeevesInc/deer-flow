#!/usr/bin/env python3
"""ticket-watchdog cron — no accepted commitment is silently dropped.

Proposal §3/§4 Phase 1 (approved by Brian 2026-11-02). Every POLL_SECONDS:

* ``open`` Ticket with no progress for STALE_BUSINESS_HOURS business hours
  (Mon-Fri 08-18 PT) -> re-dispatch it on its OWN LangGraph thread with a resume
  prompt, and post the result back into the Slack thread it came from.
  After MAX_REDISPATCH attempts -> one-line DM to Brian naming the Ticket.
* ``waiting_on_external`` with no progress for EXTERNAL_STALE_BUSINESS_HOURS ->
  one-line DM (at most once per business day).
* ``waiting_on_brian`` is never re-dispatched (the Panel, Phase 4, surfaces it).
* A stale Ticket is never closed by the watchdog.

Re-dispatched runs are internal work only. The outbound lockdown (PR #4) still
applies: anything external comes out as a Gmail draft for Brian to send.

Exposes ``run_loop()`` for cron_supervisor (never returns).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("ticket_watchdog")

_BACKEND = Path(__file__).resolve().parent.parent
_SKILLS = _BACKEND.parent / "skills" / "custom"

POLL_SECONDS = int(os.environ.get("TICKET_WATCHDOG_POLL_SECONDS", 600))
STALE_BUSINESS_HOURS = float(os.environ.get("TICKET_STALE_BUSINESS_HOURS", 4))
EXTERNAL_STALE_BUSINESS_HOURS = float(os.environ.get("TICKET_EXTERNAL_STALE_BUSINESS_HOURS", 20))
MAX_REDISPATCH = int(os.environ.get("TICKET_MAX_REDISPATCH", 2))
# "auto" = re-dispatch, then ping after MAX_REDISPATCH; "ping" = only ping Brian.
MODE = os.environ.get("TICKET_REDISPATCH_MODE", "auto")
LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL", "http://localhost:2024")
RUN_TIMEOUT = 30 * 60
ALERT_COOLDOWN_BUSINESS_HOURS = 10


def _load_store_module():
    path = _BACKEND / "packages" / "harness" / "deerflow" / "tickets" / "store.py"
    spec = importlib.util.spec_from_file_location("ticket_store", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _load_store_module()


def _slack_send(recipient: str, text: str, thread_ts: str | None = None) -> None:
    """Internal Slack post via slack_tool (outbound policy enforced there)."""
    try:
        p = str(_SKILLS / "slack-search")
        if p not in sys.path:
            sys.path.insert(0, p)
        import slack_tool  # type: ignore

        slack_tool.cmd_send(recipient, text, thread_ts=thread_ts)
    except Exception as e:  # never kill the loop over a notification
        logger.warning("[ticket-watchdog] slack send failed: %s", e)


def _owner() -> str:
    return os.environ.get("SLACK_OWNER_USER_ID", "U05B5HGNCN9")


def _origin_parts(origin: str | None) -> tuple[str | None, str | None]:
    if not origin or not origin.startswith("slack:"):
        return None, None
    parts = origin.split(":", 2)
    chat = parts[1] if len(parts) > 1 else None
    topic = parts[2] if len(parts) > 2 else None
    return chat, topic


def resume_prompt(t: dict) -> str:
    notes = "\n".join(f"- {n['ts']}: {n['note']}" for n in t["notes"][-5:]) or "- (no progress recorded)"
    return (
        f'[Ticket watchdog] Ticket {t["id"]} "{t["title"]}" is still OPEN with no progress since '
        f"{t['last_progress_at']}.\n\nMandate (Brian, verbatim):\n{t['mandate']}\n\nLast progress:\n{notes}\n\n"
        "Resume and DELIVER it now in this run. Check what already exists before redoing work. "
        "Record progress with ticket_update; finish with ticket_close(deliverable). If you need a decision "
        "from Brian, set status waiting_on_brian and ask one specific question. External communication stays "
        "a Gmail draft for Brian. Reply with the result only, no narration."
    )


def _thread_busy(http, thread_id: str) -> bool:
    try:
        r = http.get(f"{LANGGRAPH_URL}/threads/{thread_id}")
        return r.status_code == 200 and r.json().get("status") == "busy"
    except Exception:
        return True  # unobservable -> don't pile a run on top


def _extract_text(result) -> str:
    msgs = result.get("messages", []) if isinstance(result, dict) else []
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("type") == "ai":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
            if isinstance(c, list):
                txt = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                if txt.strip():
                    return txt.strip()
    return ""


def redispatch(store, t: dict) -> None:
    import httpx

    thread_id = t["langgraph_thread_id"]
    chat, topic = _origin_parts(t["origin"])
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=RUN_TIMEOUT + 120, write=60, pool=10)) as http:
        if not thread_id:
            r = http.post(f"{LANGGRAPH_URL}/threads", json={})
            r.raise_for_status()
            thread_id = r.json()["thread_id"]
        elif _thread_busy(http, thread_id):
            logger.info("[ticket-watchdog] %s thread busy, skip this cycle", t["id"])
            return
        store.mark_redispatched(t["id"])
        logger.info("[ticket-watchdog] re-dispatching %s on %s", t["id"], thread_id)
        body = {
            "assistant_id": "lead_agent",
            "input": {"messages": [{"role": "human", "content": resume_prompt(t)}]},
            "config": {
                "recursion_limit": 500,
                "configurable": {"thinking_enabled": True, "is_plan_mode": False, "subagent_enabled": False, "thread_id": thread_id},
            },
            "context": {"thread_id": thread_id},
            "multitask_strategy": "reject",
        }
        r = http.post(f"{LANGGRAPH_URL}/threads/{thread_id}/runs/wait", json=body)
        r.raise_for_status()
        text = _extract_text(r.json())
    after = store.get(t["id"]) or t
    if text and chat:
        _slack_send(chat, text[:3500], thread_ts=topic)
    elif text:
        _slack_send(_owner(), f'Ticket {t["id"]} "{t["title"]}" (re-dispatched):\n{text[:3300]}')
    logger.info("[ticket-watchdog] %s re-dispatch done, status now %s", t["id"], after["status"])


def _alert_due(t: dict, now: datetime) -> bool:
    last = S._parse(t.get("last_alert_at"))
    return last is None or S.business_seconds_between(last, now) >= ALERT_COOLDOWN_BUSINESS_HOURS * 3600


def check_once(store, now: datetime | None = None, dispatch=redispatch, notify=_slack_send) -> list[tuple[str, str]]:
    """One pass. Returns [(ticket_id, action)] for logging/tests."""
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
                notify(_owner(), f'Ticket {t["id"]} "{t["title"]}" is still open' + (f" after {n} re-dispatch(es)" if n else "") + f"; last progress {t['last_progress_at']}.")
                store.mark_alerted(t["id"], now)
                actions.append((t["id"], "alerted"))
        elif t["status"] == S.WAITING_ON_EXTERNAL and S.is_stale(t, EXTERNAL_STALE_BUSINESS_HOURS, now) and _alert_due(t, now):
            notify(_owner(), f'Ticket {t["id"]} "{t["title"]}" has been waiting on an external party since {t["last_progress_at"]}.')
            store.mark_alerted(t["id"], now)
            actions.append((t["id"], "alerted_external"))
    return actions


def run_loop() -> None:
    store = S.TicketStore()
    logger.info("[ticket-watchdog] started (poll=%ss, stale=%sbh, mode=%s, db=%s)", POLL_SECONDS, STALE_BUSINESS_HOURS, MODE, store.path)
    while True:
        try:
            acts = check_once(store)
            if acts:
                logger.info("[ticket-watchdog] %s", json.dumps(acts))
        except Exception as e:
            logger.exception("[ticket-watchdog] pass failed: %s", e)
        time.sleep(POLL_SECONDS)

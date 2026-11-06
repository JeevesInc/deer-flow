"""Run the agent on a Ticket's own LangGraph thread and post the answer back to
the Slack thread the Ticket came from. Shared by the ticket watchdog
(re-dispatch) and the Gmail webhook (subscription routing).

Internal only: anything external the run produces is a Gmail draft, enforced by
the outbound lockdown in slack_tool / gmail_tool / sandbox tools.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("ticket_runner")

LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL", "http://localhost:2024")
RUN_TIMEOUT = 30 * 60
_SKILLS = Path(__file__).resolve().parents[5] / "skills" / "custom"


class ThreadBusy(RuntimeError):
    pass


def owner_id() -> str:
    return os.environ.get("SLACK_OWNER_USER_ID", "U05B5HGNCN9")


def slack_send(recipient: str, text: str, thread_ts: str | None = None) -> None:
    """Internal Slack post through slack_tool (outbound policy enforced there). Never raises."""
    try:
        p = str(_SKILLS / "slack-search")
        if p not in sys.path:
            sys.path.insert(0, p)
        import slack_tool  # type: ignore

        slack_tool.cmd_send(recipient, text, thread_ts=thread_ts)
    except Exception as e:
        logger.warning("[ticket-runner] slack send failed: %s", e)


def origin_parts(origin: str | None) -> tuple[str | None, str | None]:
    """'slack:<chat_id>[:<thread_ts>]' -> (chat_id, thread_ts)."""
    if not origin or not origin.startswith("slack:"):
        return None, None
    parts = origin.split(":", 2)
    return (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)


def extract_text(result) -> str:
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


def thread_status(http, thread_id: str) -> str | None:
    try:
        r = http.get(f"{LANGGRAPH_URL}/threads/{thread_id}")
        return r.json().get("status") if r.status_code == 200 else None
    except Exception:
        return None


def run_body(thread_id: str, prompt: str) -> dict:
    """Run payload for LangGraph 0.6+: run params go in ``context`` only.

    The server copies ``context`` into ``configurable`` itself and rejects a
    body that sets both with a 400, which silently killed every re-dispatch.
    """
    return {
        "assistant_id": "lead_agent",
        "input": {"messages": [{"role": "human", "content": prompt}]},
        "config": {"recursion_limit": 500},
        "context": {"thinking_enabled": True, "is_plan_mode": False, "subagent_enabled": False, "thread_id": thread_id},
        "multitask_strategy": "reject",
    }


def run_on_ticket(ticket: dict, prompt: str, *, on_start=None, post=slack_send) -> str:
    """Blocking. Raises ThreadBusy if the Ticket's thread already has a run."""
    import httpx

    thread_id = ticket.get("langgraph_thread_id")
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=RUN_TIMEOUT + 120, write=60, pool=10)) as http:
        if thread_id:
            st = thread_status(http, thread_id)
            if st == "busy" or st is None:
                raise ThreadBusy(f"thread {thread_id} status={st}")
        else:
            r = http.post(f"{LANGGRAPH_URL}/threads", json={})
            r.raise_for_status()
            thread_id = r.json()["thread_id"]
        if on_start:
            on_start()
        body = run_body(thread_id, prompt)
        r = http.post(f"{LANGGRAPH_URL}/threads/{thread_id}/runs/wait", json=body)
        r.raise_for_status()
        text = extract_text(r.json())
    chat, topic = origin_parts(ticket.get("origin"))
    if text:
        if chat:
            post(chat, text[:3500], thread_ts=topic)
        else:
            post(owner_id(), f'Ticket {ticket["id"]} "{ticket["title"]}":\n{text[:3300]}')
    return text


def cancel_runs(thread_id: str) -> int:
    """Cancel every pending/running run on a thread. Returns the count."""
    import httpx

    n = 0
    with httpx.Client(timeout=30) as http:
        r = http.get(f"{LANGGRAPH_URL}/threads/{thread_id}/runs")
        for run in r.json() if r.status_code == 200 else []:
            if run.get("status") in ("pending", "running"):
                http.post(f"{LANGGRAPH_URL}/threads/{thread_id}/runs/{run['run_id']}/cancel")
                n += 1
    return n

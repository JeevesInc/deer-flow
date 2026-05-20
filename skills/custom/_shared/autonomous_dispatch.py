"""Autonomous dispatch — fire-and-forget agent runs from cron jobs.

Cron jobs (email monitor, analytics, etc.) can use this module to submit
work to the DeerFlow agent and post results to Slack DM.  Runs execute
in background daemon threads so the calling cron is never blocked.

Usage::

    from autonomous_dispatch import dispatch

    dispatch(
        prompt="Handle this diligence request...",
        notification="Diligence request detected — working on it.",
        category="Diligence",
    )
"""

import json
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("autonomous_dispatch")

LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL", "http://localhost:2024")
ASSISTANT_ID = "lead_agent"
MAX_CONCURRENT_RUNS = 2
RUN_TIMEOUT = 20 * 60  # 20 minutes

_active_runs = 0
_lock = threading.Lock()
_audit_lock = threading.Lock()


def _audit_path() -> Path:
    # backend/.deer-flow/dispatch_audit.jsonl — one line per dispatch event.
    # autonomous_dispatch.py lives at skills/custom/_shared, so we walk up
    # four parents to reach the repo root then into deer-flow/backend.
    here = Path(__file__).resolve()
    return here.parents[3] / "backend" / ".deer-flow" / "dispatch_audit.jsonl"


def _audit(event: str, **fields: Any) -> None:
    """Append a JSON line to the dispatch audit log. Best-effort, never raises."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("audit log write failed: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def dispatch(
    prompt: str,
    *,
    notification: str,
    category: str = "general",
    source_id: str | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> bool:
    """Submit work to the agent in a background thread.

    Posts *notification* to Slack DM immediately, then creates a LangGraph
    thread + run.  When the run finishes (or fails), posts the result to
    Slack.

    ``source_id`` and ``source_metadata`` are recorded in the audit log so
    every dispatch can be traced back to the triggering event (email id,
    subject line, etc.). Optional but recommended.

    Returns ``False`` if the system is already at capacity.
    """
    global _active_runs
    with _lock:
        if _active_runs >= MAX_CONCURRENT_RUNS:
            log.warning(
                "At capacity (%d/%d runs), dropping %s dispatch",
                _active_runs,
                MAX_CONCURRENT_RUNS,
                category,
            )
            _audit(
                "rejected_capacity",
                category=category,
                source_id=source_id,
                source_metadata=source_metadata,
                active_runs=_active_runs,
                max_runs=MAX_CONCURRENT_RUNS,
            )
            return False
        _active_runs += 1

    _audit(
        "accepted",
        category=category,
        source_id=source_id,
        source_metadata=source_metadata,
        prompt_preview=(prompt[:300] + "…") if len(prompt) > 300 else prompt,
    )

    t = threading.Thread(
        target=_run_and_report,
        args=(prompt, notification, category, source_id),
        daemon=True,
        name=f"dispatch-{category.lower().replace(' ', '-')}",
    )
    t.start()
    return True


def active_run_count() -> int:
    """Return how many autonomous runs are currently in flight."""
    return _active_runs


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _run_and_report(prompt: str, notification: str, category: str, source_id: str | None) -> None:
    global _active_runs
    thread_id: str | None = None
    try:
        # 1. Notify Slack that work is starting
        _post_slack(notification)

        # 2. Create thread + submit run via LangGraph REST API
        import httpx

        with httpx.Client(
            timeout=httpx.Timeout(
                connect=10,
                read=RUN_TIMEOUT + 120,
                write=60,
                pool=10,
            ),
        ) as http:
            # Create thread
            resp = http.post(f"{LANGGRAPH_URL}/threads", json={})
            resp.raise_for_status()
            thread_id = resp.json()["thread_id"]
            log.info("[Dispatch] created thread %s for %s", thread_id, category)
            _audit("started", category=category, source_id=source_id, thread_id=thread_id)

            # Submit run and wait for completion
            run_body: dict[str, Any] = {
                "assistant_id": ASSISTANT_ID,
                "input": {
                    "messages": [{"role": "human", "content": prompt}],
                },
                "config": {
                    "recursion_limit": 500,
                    "configurable": {
                        "thinking_enabled": True,
                        "is_plan_mode": False,
                        "subagent_enabled": False,
                        "thread_id": thread_id,
                    },
                },
            }

            resp = http.post(
                f"{LANGGRAPH_URL}/threads/{thread_id}/runs/wait",
                json=run_body,
            )
            resp.raise_for_status()
            result = resp.json()

        # 3. Extract response and post to Slack
        response = _extract_response(result)
        if response:
            summary = response[:3000]
            if len(response) > 3000:
                summary += "\n\n_(truncated)_"
            _post_slack(f"*{category} — completed:*\n\n{summary}")
        else:
            _post_slack(f"*{category} — completed* (no visible output produced).")

        log.info("[Dispatch] %s completed on thread %s", category, thread_id)
        _audit(
            "completed",
            category=category,
            source_id=source_id,
            thread_id=thread_id,
            response_chars=len(response) if response else 0,
        )

    except Exception as e:
        log.error("[Dispatch] %s failed: %s", category, e)
        traceback.print_exc()
        _post_slack(f"*{category} — failed:*\n```{str(e)[:500]}```")
        _audit(
            "failed",
            category=category,
            source_id=source_id,
            thread_id=thread_id,
            error=str(e)[:500],
        )
    finally:
        with _lock:
            _active_runs -= 1


def _extract_response(result: Any) -> str:
    """Extract the last AI message text from a ``runs/wait`` result."""
    if isinstance(result, dict):
        messages = result.get("messages", [])
    elif isinstance(result, list):
        messages = result
    else:
        return ""

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("type") or msg.get("role", "")
        if role != "ai":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            text = "\n".join(parts).strip()
            if text:
                return text
    return ""


def _post_slack(text: str) -> None:
    """Post a message to the owner's Slack DM."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    owner_id = os.environ.get("SLACK_OWNER_USER_ID")
    if not token or not owner_id:
        log.warning("Slack not configured for dispatch notifications")
        return
    try:
        from slack_sdk import WebClient

        client = WebClient(token=token)
        dm = client.conversations_open(users=[owner_id])
        channel_id = dm["channel"]["id"]
        client.chat_postMessage(channel=channel_id, text=text)
    except Exception as e:
        log.error("Dispatch Slack post failed: %s", e)

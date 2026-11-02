"""Ticket tools — the agent's handle on durable Tickets (proposal §3).

ticket_open is required whenever the agent accepts an instruction; ticket_close
refuses to close without a deliverable. Only Brian can stop a ticket.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain.tools import ToolRuntime, tool
from langgraph.typing import ContextT

from deerflow.agents.thread_state import ThreadState
from deerflow.tickets.store import AGENT_SETTABLE, TicketError, TicketStore

logger = logging.getLogger(__name__)

_store: TicketStore | None = None


def _get_store() -> TicketStore:
    global _store
    if _store is None:
        _store = TicketStore()
    return _store


def _thread_id(runtime: ToolRuntime | None) -> str | None:
    ctx = getattr(runtime, "context", None) or {}
    try:
        return ctx.get("thread_id")
    except AttributeError:
        return None


def origin_for_thread(thread_id: str | None, store_path: Path | None = None) -> str | None:
    """Reverse-map a LangGraph thread to the IM conversation that owns it, using
    the channel manager's store.json ("<channel>:<chat_id>[:<topic_id>]")."""
    if not thread_id:
        return None
    try:
        if store_path is None:
            from deerflow.config.paths import get_paths

            store_path = Path(get_paths().base_dir) / "channels" / "store.json"
        data = json.loads(Path(store_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    for key, entry in data.items():
        if isinstance(entry, dict) and entry.get("thread_id") == thread_id:
            return key
    return None


def _fmt(t: dict) -> str:
    return f"{t['id']} [{t['status']}] {t['title']}"


@tool("ticket_open", parse_docstring=True)
def ticket_open_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    title: str,
    mandate: str,
    due: str | None = None,
) -> str:
    """Open a durable Ticket for work Brian has asked for. REQUIRED whenever you accept an instruction ("do X", "build", "send me", "fix"). The Ticket outlives this run: if it goes stale, the watchdog re-dispatches it.

    Args:
        title: Short name of the work, e.g. "NB Sept BBC + cash report".
        mandate: Brian's ask quoted verbatim, then the scope you will deliver.
        due: Optional due date/time (ISO or plain text, e.g. "2026-11-09").
    """
    tid = _thread_id(runtime)
    try:
        t, created = _get_store().open(title, mandate, origin=origin_for_thread(tid), langgraph_thread_id=tid, due=due)
    except TicketError as e:
        return f"Error: {e}"
    return ("Opened " if created else "Already open: ") + _fmt(t) + ". Close it with ticket_close(deliverable) when delivered."


@tool("ticket_update", parse_docstring=True)
def ticket_update_tool(ticket_id: str, note: str, status: str | None = None) -> str:
    """Record concrete progress on a Ticket, or mark it waiting. Resets its staleness clock.

    Args:
        ticket_id: The Ticket id, e.g. "T-1a2b3c4d".
        note: What concretely changed (file built, query run, draft id created).
        status: Optional: "open", "waiting_on_brian" (needs his decision or a draft is ready for him to send) or "waiting_on_external".
    """
    if status is not None and status not in AGENT_SETTABLE:
        return f"Error: status must be one of {AGENT_SETTABLE}"
    try:
        return "Updated " + _fmt(_get_store().progress(ticket_id, note, status))
    except TicketError as e:
        return f"Error: {e}"


@tool("ticket_close", parse_docstring=True)
def ticket_close_tool(ticket_id: str, deliverable: str) -> str:
    """Close a Ticket as delivered. Refused without a real deliverable.

    Args:
        ticket_id: The Ticket id.
        deliverable: A Drive/GitHub link, a Gmail draft id, or the delivered message text.
    """
    try:
        return "Delivered " + _fmt(_get_store().close(ticket_id, deliverable))
    except TicketError as e:
        return f"Error: {e}"


@tool("ticket_stop", parse_docstring=True)
def ticket_stop_tool(ticket_id: str, brian_instruction: str) -> str:
    """Stop a Ticket. ONLY when Brian explicitly says to drop/stop/cancel that work.

    Args:
        ticket_id: The Ticket id.
        brian_instruction: Brian's instruction, quoted verbatim.
    """
    try:
        return "Stopped " + _fmt(_get_store().stop(ticket_id, brian_instruction))
    except TicketError as e:
        return f"Error: {e}"


@tool("ticket_list", parse_docstring=True)
def ticket_list_tool(runtime: ToolRuntime[ContextT, ThreadState], this_thread_only: bool = False) -> str:
    """List active Tickets (open / waiting). Check this before saying work is done or before starting a new ask.

    Args:
        this_thread_only: Only Tickets opened from the current thread.
    """
    rows = _get_store().list(thread_id=_thread_id(runtime) if this_thread_only else None)
    if not rows:
        return "No active tickets."
    return "\n".join(f"{_fmt(t)} | due {t['due'] or '-'} | last progress {t['last_progress_at']}" for t in rows)


TICKET_TOOLS = [ticket_open_tool, ticket_update_tool, ticket_close_tool, ticket_stop_tool, ticket_list_tool]

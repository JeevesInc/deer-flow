"""Durable Tickets — one row per unit of work Brian has authorized.

Proposal §3 (Super-Agent Architecture, 2026-11-01; approved by Brian
2026-11-02). A Ticket stays open until it is delivered or Brian stops it, so a
commitment survives the end of the run that accepted it (30 Oct: "since then
I've done nothing").

Stdlib only: imported by the LangGraph tools AND by the gateway cron
(``scripts/ticket_watchdog_cron.py``), which loads it by path.

Status machine::

    open -> waiting_on_brian | waiting_on_external -> open (on progress)
    any non-terminal -> delivered   (ticket_close, needs a deliverable)
    any non-terminal -> stopped     (only on Brian's explicit instruction)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo

    _PT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - tzdata missing
    _PT = timezone(timedelta(hours=-8))

OPEN = "open"
WAITING_ON_BRIAN = "waiting_on_brian"
WAITING_ON_EXTERNAL = "waiting_on_external"
DELIVERED = "delivered"
STOPPED = "stopped"

ACTIVE_STATUSES = (OPEN, WAITING_ON_BRIAN, WAITING_ON_EXTERNAL)
AGENT_SETTABLE = (OPEN, WAITING_ON_BRIAN, WAITING_ON_EXTERNAL)
TERMINAL_STATUSES = (DELIVERED, STOPPED)

# Business day used for staleness: Mon-Fri, 08:00-18:00 Pacific.
BUSINESS_START_HOUR = 8
BUSINESS_END_HOUR = 18

MIN_DELIVERABLE_CHARS = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    mandate TEXT NOT NULL,
    origin TEXT,
    langgraph_thread_id TEXT,
    status TEXT NOT NULL,
    due TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_progress_at TEXT NOT NULL,
    deliverables TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '[]',
    redispatch_count INTEGER NOT NULL DEFAULT 0,
    last_redispatch_at TEXT,
    last_alert_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_tickets_thread ON tickets(langgraph_thread_id);
CREATE TABLE IF NOT EXISTS subscriptions (
    ticket_id TEXT NOT NULL,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ticket_id, key)
);
CREATE INDEX IF NOT EXISTS ix_subs_key ON subscriptions(key);
"""

_lock = threading.Lock()


class TicketError(ValueError):
    """Raised for an invalid ticket operation; the message is shown to the agent."""


def default_db_path() -> Path:
    env = os.environ.get("TICKETS_DB")
    if env:
        return Path(env)
    # .../backend/packages/harness/deerflow/tickets/store.py -> backend/.deer-flow
    return Path(__file__).resolve().parents[4] / ".deer-flow" / "tickets.db"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class TicketStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["deliverables"] = json.loads(d.get("deliverables") or "[]")
        d["notes"] = json.loads(d.get("notes") or "[]")
        return d

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())

    def require(self, ticket_id: str) -> dict[str, Any]:
        t = self.get(ticket_id)
        if t is None:
            raise TicketError(f"No ticket with id {ticket_id!r}. Use ticket_list to see open tickets.")
        return t

    def list(self, statuses: tuple[str, ...] | None = ACTIVE_STATUSES, thread_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM tickets WHERE 1=1"
        args: list[Any] = []
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if thread_id:
            q += " AND langgraph_thread_id=?"
            args.append(thread_id)
        q += " ORDER BY created_at"
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]

    # -- writes ------------------------------------------------------------

    def open(
        self,
        title: str,
        mandate: str,
        *,
        origin: str | None = None,
        langgraph_thread_id: str | None = None,
        due: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Open a Ticket. Returns (ticket, created). Re-opening the same title on
        the same thread returns the existing active ticket instead of a duplicate."""
        title = (title or "").strip()
        mandate = (mandate or "").strip()
        if not title:
            raise TicketError("title is required")
        if not mandate:
            raise TicketError("mandate is required: Brian's ask, verbatim, plus scope")
        with _lock, self._conn() as c:
            if langgraph_thread_id:
                r = c.execute(
                    "SELECT * FROM tickets WHERE langgraph_thread_id=? AND lower(title)=lower(?) AND status IN (?,?,?)",
                    (langgraph_thread_id, title, *ACTIVE_STATUSES),
                ).fetchone()
                if r is not None:
                    return self._row(r), False
            now = _iso(_now())
            tid = "T-" + uuid.uuid4().hex[:8]
            c.execute(
                "INSERT INTO tickets (id,title,mandate,origin,langgraph_thread_id,status,due,created_at,updated_at,last_progress_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tid, title, mandate, origin, langgraph_thread_id, OPEN, due, now, now, now),
            )
            if origin:
                c.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?,?)", (tid, origin, now))
            return self._row(c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()), True

    def _append(self, c: sqlite3.Connection, ticket_id: str, field: str, item: Any) -> None:
        cur = json.loads(c.execute(f"SELECT {field} FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0] or "[]")
        cur.append(item)
        c.execute(f"UPDATE tickets SET {field}=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), ticket_id))

    def progress(self, ticket_id: str, note: str, status: str | None = None) -> dict[str, Any]:
        t = self.require(ticket_id)
        if t["status"] in TERMINAL_STATUSES:
            raise TicketError(f"{ticket_id} is {t['status']}; open a new ticket for new work.")
        if status is not None and status not in AGENT_SETTABLE:
            raise TicketError(f"status must be one of {AGENT_SETTABLE}; use ticket_close to deliver.")
        note = (note or "").strip()
        if not note:
            raise TicketError("note is required: what concretely changed")
        now = _iso(_now())
        with _lock, self._conn() as c:
            self._append(c, ticket_id, "notes", {"ts": now, "note": note})
            c.execute(
                "UPDATE tickets SET status=?, updated_at=?, last_progress_at=? WHERE id=?",
                (status or t["status"], now, now, ticket_id),
            )
        return self.require(ticket_id)

    def close(self, ticket_id: str, deliverable: str) -> dict[str, Any]:
        t = self.require(ticket_id)
        if t["status"] in TERMINAL_STATUSES:
            raise TicketError(f"{ticket_id} is already {t['status']}.")
        deliverable = (deliverable or "").strip()
        if len(deliverable) < MIN_DELIVERABLE_CHARS:
            raise TicketError("Refused: a ticket closes only with a deliverable — a Drive/GitHub link, a Gmail draft id, or the delivered message text. If the work is not done, keep the ticket open.")
        now = _iso(_now())
        with _lock, self._conn() as c:
            self._append(c, ticket_id, "deliverables", {"ts": now, "deliverable": deliverable})
            c.execute(
                "UPDATE tickets SET status=?, updated_at=?, last_progress_at=? WHERE id=?",
                (DELIVERED, now, now, ticket_id),
            )
        return self.require(ticket_id)

    def stop(self, ticket_id: str, brian_instruction: str) -> dict[str, Any]:
        t = self.require(ticket_id)
        if t["status"] in TERMINAL_STATUSES:
            raise TicketError(f"{ticket_id} is already {t['status']}.")
        brian_instruction = (brian_instruction or "").strip()
        if not brian_instruction:
            raise TicketError("Only Brian can stop a ticket: quote his instruction verbatim.")
        now = _iso(_now())
        with _lock, self._conn() as c:
            self._append(c, ticket_id, "notes", {"ts": now, "note": f"STOPPED by Brian: {brian_instruction}"})
            c.execute("UPDATE tickets SET status=?, updated_at=? WHERE id=?", (STOPPED, now, ticket_id))
        return self.require(ticket_id)

    def mark_redispatched(self, ticket_id: str) -> None:
        now = _iso(_now())
        with _lock, self._conn() as c:
            c.execute(
                "UPDATE tickets SET redispatch_count=redispatch_count+1, last_redispatch_at=?, last_progress_at=? WHERE id=?",
                (now, now, ticket_id),
            )

    def mark_alerted(self, ticket_id: str, when: datetime | None = None) -> None:
        with _lock, self._conn() as c:
            c.execute("UPDATE tickets SET last_alert_at=? WHERE id=?", (_iso(when or _now()), ticket_id))

    def subscribe(self, ticket_id: str, key: str) -> None:
        self.require(ticket_id)
        with _lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?,?)", (ticket_id, key, _iso(_now())))


# -- business-hours arithmetic ---------------------------------------------


def business_seconds_between(start: datetime, end: datetime) -> float:
    """Seconds of Mon-Fri 08:00-18:00 Pacific time between start and end."""
    if end <= start:
        return 0.0
    s = start.astimezone(_PT)
    e = end.astimezone(_PT)
    total = 0.0
    day = s.date()
    while day <= e.date():
        if day.weekday() < 5:
            open_ = datetime(day.year, day.month, day.day, BUSINESS_START_HOUR, tzinfo=_PT)
            close = datetime(day.year, day.month, day.day, BUSINESS_END_HOUR, tzinfo=_PT)
            lo = max(open_, s)
            hi = min(close, e)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def is_stale(ticket: dict[str, Any], business_hours: float, now: datetime | None = None) -> bool:
    last = _parse(ticket.get("last_progress_at"))
    if last is None:
        return False
    return business_seconds_between(last, now or _now()) >= business_hours * 3600

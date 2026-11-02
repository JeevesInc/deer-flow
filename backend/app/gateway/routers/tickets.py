"""/tickets — full Ticket list (Super-Agent proposal Phase 4).

``GET /tickets`` renders an HTML table; ``GET /api/tickets`` returns JSON.
Query: ``?status=active`` (default), ``all``, or a single status name.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from deerflow.tickets import store as S

router = APIRouter(tags=["tickets"])


def _rows(status: str) -> list[dict]:
    st = S.TicketStore()
    if status == "active":
        return st.list()
    if status == "all":
        return st.list(statuses=None)
    return st.list(statuses=(status,))


@router.get("/api/tickets")
async def list_tickets(status: str = Query("active")) -> dict:
    rows = _rows(status)
    return {"count": len(rows), "tickets": rows}


@router.get("/tickets", response_class=HTMLResponse)
async def tickets_page(status: str = Query("active")) -> str:
    rows = _rows(status)
    order = {S.WAITING_ON_BRIAN: 0, S.OPEN: 1, S.WAITING_ON_EXTERNAL: 2, S.DELIVERED: 3, S.STOPPED: 4}
    rows.sort(key=lambda t: (order.get(t["status"], 9), t["last_progress_at"]))
    e = html.escape
    body = "".join(
        "<tr><td>{id}</td><td class='s-{st}'>{st}</td><td>{title}</td><td>{due}</td><td>{last}</td><td>{note}</td><td>{deliv}</td></tr>".format(
            id=e(t["id"]),
            st=e(t["status"]),
            title=e(t["title"]),
            due=e(t["due"] or ""),
            last=e(t["last_progress_at"][:16].replace("T", " ")),
            note=e(t["notes"][-1]["note"][:200] if t["notes"] else ""),
            deliv=e(t["deliverables"][-1]["deliverable"][:200] if t["deliverables"] else ""),
        )
        for t in rows
    )
    return (
        "<!doctype html><meta charset='utf-8'><title>Tickets</title>"
        "<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse;width:100%}"
        "td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}"
        ".s-waiting_on_brian{color:#b00;font-weight:600}.s-open{color:#06c}.s-delivered{color:#080}</style>"
        f"<h2>Tickets ({e(status)}): {len(rows)}</h2>"
        "<p><a href='?status=active'>active</a> · <a href='?status=waiting_on_brian'>waiting on Brian</a> · <a href='?status=all'>all</a></p>"
        "<table><tr><th>ID</th><th>Status</th><th>Title</th><th>Due</th><th>Last progress (UTC)</th><th>Last note</th><th>Deliverable</th></tr>"
        f"{body}</table>"
    )

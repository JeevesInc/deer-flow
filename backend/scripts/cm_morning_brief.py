#!/usr/bin/env python3
"""cm_morning_brief.py
======================
Posts a daily Capital Markets brief to Brian's Slack DM (~8am weekdays).

Reads from .deer-flow/_cap_markets_state.json (written by cap_markets_metrics_writer.py).
Covers: US Bridge BB, MX SOFOM BB, CICO cash, alerts.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cm_brief")

BACKEND_DIR = Path(__file__).resolve().parent.parent
STATE_FILE   = BACKEND_DIR / ".deer-flow" / "_cap_markets_state.json"
CICO_FILE    = BACKEND_DIR / ".deer-flow" / "_cico_state.json"
SLACK_TOOL   = Path(__file__).resolve().parent.parent.parent / "deer-flow" / "skills" / "custom" / "slack-search" / "slack_tool.py"
BRIAN_ID     = "U05B5HGNCN9"


# ── Formatters ────────────────────────────────────────────────────────────────

def _m(v, decimals=1) -> str:
    """Format as $XM with sign."""
    if v is None:
        return "n/a"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:+.{decimals}f}M" if v < 0 else f"${v / 1_000_000:.{decimals}f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _pct(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:.2f}%"


def _avail_emoji(v) -> str:
    if v is None:
        return "⚪"
    if v < 0:
        return "🔴"
    if v < 3_000_000:
        return "🟡"
    return "🟢"


def _dq_emoji(v) -> str:
    if v is None:
        return "⚪"
    if v > 5:
        return "🔴"
    if v > 2:
        return "🟡"
    return "🟢"


# ── Main ─────────────────────────────────────────────────────────────────────

def build_brief() -> str:
    if not STATE_FILE.exists():
        return ":warning: Cap Markets state file not found — run dashboard update first."

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    us    = state.get("us_bridge", {})
    mx    = state.get("mx_sofom", {})
    cico  = json.loads(CICO_FILE.read_text(encoding="utf-8")) if CICO_FILE.exists() else {}

    today = date.today().strftime("%a %b %d").replace(" 0", " ")

    # ── US Bridge ──────────────────────────────────────────────────────────
    us_recv    = us.get("total_receivables")
    us_inelig  = None
    if us_recv and us.get("eligible_gross"):
        us_inelig = us_recv - us.get("eligible_gross", 0)
    elif us_recv and us.get("eligible"):
        us_inelig = us_recv - us.get("eligible", 0)
    us_elig    = us.get("eligible_gross") or us.get("eligible")
    us_bb      = us.get("borrowing_base")
    us_drawn   = us.get("total_drawn")
    us_avail   = us.get("availability")
    us_cash    = (us.get("us_cash") or 0) + (us.get("ex_us_cash") or 0)
    us_tape_dt = (us.get("tape_as_of") or us.get("source_mtime") or "")[:10]

    # ── MX SOFOM ─────────────────────────────────────────────────────────
    mx_recv_mxn   = mx.get("total_receivables_mxn")
    mx_inelig_mxn = mx.get("ineligible_mxn")
    mx_elig_mxn   = mx.get("eligible_mxn")
    mx_bb_usd     = mx.get("borrowing_base")
    mx_drawn      = mx.get("total_drawn")
    mx_avail      = mx.get("availability")
    mx_usdmxn     = mx.get("usdmxn_rate") or 17.44
    mx_tape_dt    = (mx.get("tape_as_of") or mx.get("source_mtime") or "")[:10]

    def mxn_m(v):
        if v is None:
            return "n/a"
        return f"MXN {v/1_000_000:.1f}M"

    # ── CICO ─────────────────────────────────────────────────────────────
    cico_cash       = cico.get("total_cash_usd")
    cico_daca       = cico.get("daca_pledged_usd")
    cico_restricted = cico.get("restricted_deposits_usd")
    cico_dt         = (cico.get("source_date") or "")[:16]

    # ── Alerts ───────────────────────────────────────────────────────────
    alerts = []
    if us_avail is not None and us_avail < 5_000_000:
        alerts.append(f"Bridge availability tight: {_m(us_avail)}")
    if mx_avail is not None and mx_avail < 0:
        alerts.append(f"SOFOM availability *negative*: {_m(mx_avail)}")
    # Covenant breaches
    for cov in us.get("covenants", []):
        if cov.get("excess_usd", 0) > 0:
            short = cov.get("test", "")[:60].strip()
            alerts.append(f"Bridge covenant breach: _{short}_ (+${cov['excess_usd']/1e6:.1f}M excess)")
    for cov in mx.get("covenants", []):
        if cov.get("excess_mxn", 0) > 0:
            short = cov.get("test", "")[:50].strip()
            alerts.append(f"SOFOM covenant breach: _{short}_ (+MXN {cov['excess_mxn']/1e6:.1f}M)")

    # ── Format message ────────────────────────────────────────────────────
    lines = [
        f":bar_chart: *Capital Markets · {today}*",
        "",
        f"*🏦 US Bridge* _(tape: {us_tape_dt})_",
        f"  Total Recv:   {_m(us_recv, 1)}",
        f"  Ineligible:   {_m(us_inelig, 1)}",
        f"  Eligible:     {_m(us_elig, 1)}",
        f"  Final BB:     {_m(us_bb, 1)}  _(incl. {_m(us_cash, 1)} cash)_",
        f"  Drawn:        {_m(us_drawn, 1)}   {_avail_emoji(us_avail)} {_m(us_avail, 1)} avail",
        "",
        f"*🇲🇽 MX SOFOM* _(tape: {mx_tape_dt})_",
        f"  Total Recv:   {mxn_m(mx_recv_mxn)}",
        f"  Ineligible:   {mxn_m(mx_inelig_mxn)}",
        f"  Eligible:     {mxn_m(mx_elig_mxn)}",
        f"  Final BB:     {_m(mx_bb_usd, 1)}",
        f"  Drawn:        {_m(mx_drawn, 1)}   {_avail_emoji(mx_avail)} {_m(mx_avail, 1)} avail",
        "",
        f"*💵 CICO Cash* _(Jun 8)_",
        f"  Total Cash:      {_m(cico_cash, 1)}",
        f"  DACA / Pledged:  {_m(cico_daca, 1)}",
        f"  Restricted:      {_m(cico_restricted, 1)}",
    ]

    if alerts:
        lines += ["", "*⚠️ Alerts*"]
        for a in alerts:
            lines.append(f"  • {a}")

    return "\n".join(lines)


def send_brief():
    msg = build_brief()
    log.info("Brief built (%d chars)", len(msg))

    # Send via slack_tool.py (respects audit log)
    import subprocess
    result = subprocess.run(
        [sys.executable, str(SLACK_TOOL), "send", BRIAN_ID, msg],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        log.error("Slack send failed: %s", result.stderr[:300])
        return False
    log.info("Brief sent to Brian: %s", result.stdout.strip()[:100])
    return True


if __name__ == "__main__":
    # Ensure UTF-8 stdout on Windows
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    # Print to stdout if --print flag, else send to Slack
    if "--print" in sys.argv:
        print(build_brief())
    else:
        sys.exit(0 if send_brief() else 1)

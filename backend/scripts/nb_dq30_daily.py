#!/usr/bin/env python3
"""nb_dq30_daily.py
==================
Posts a standalone, forward-ready DQ30 reading to Brian's Slack DM each morning,
right after cm_morning_brief.py (weekdays, ~8am PT).

Brian, 2026-08-19: "Can you add a daily measurement of this to the morning daily
crons? I want a simple message that I can forward internally."  Hence: no bot
chatter, no emoji noise, no internal file paths -- it reads as a clean status
note someone else can act on.

Measurement basis is deliberately IDENTICAL to what the NB borrowing base form
reports (Brian, 2026-08-19: "let's leave it as is"), i.e. total balance, all
countries, NOT the principal-only reading the Credit Agreement's "Outstanding
Receivables Balance" definition would allow:
  numerator   = balance_usd of unencumbered receivables with days_past_due > 30
  denominator = balance_usd of all unencumbered receivables
  unencumbered = NOT (MX SOFOM transfer flag on AND still-financed portion > 0)
  NB: the company_settings key is SOFOM_JPMORGAN_ENABLED, but JPMorgan is the Mexican
  card processor, NOT a lender. The encumbrance is the MX SOFOM facility.
  excluded     = charged-off accounts and accounts in a repayment plan
Query of record: cfo-org-kb/sql/nb_dq30_monthend_detail.sql (aggregate form here).

Covenant context (Credit Agreement Section 7.01(b)): the DQ30 Rate is the
3-month rolling average of month-end readings and must stay below 2.50%.  The
daily figure is a spot reading and is NOT a test point -- the message says so,
because the spot number can sit above 2.50% while the covenant is comfortable.

Writes every reading to .deer-flow/_nb_dq30_state.json so the message can show
the change since the prior reading.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nb_dq30")

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEER_FLOW_ROOT = BACKEND_DIR.parent
STATE_FILE = BACKEND_DIR / ".deer-flow" / "_nb_dq30_state.json"
SKILLS = DEER_FLOW_ROOT / "skills" / "custom"
SLACK_TOOL = SKILLS / "slack-search" / "slack_tool.py"

sys.path.insert(0, str(SKILLS / "_shared"))
from identity import owner_slack_id  # noqa: E402

COVENANT_CAP = 0.025
STEP_UP = 0.015

SQL = """
with
sofom_transfer as (
    select c.id, cs.settingValue as transfer_flag, cs.updatedat as assignment_dt
    from dms_mysql_jeeves_raw.companies c
    inner join dms_mysql_jeeves_raw.company_settings cs on cs.companyId = c.id
    where cs.settingKey = 'SOFOM_JPMORGAN_ENABLED'
),
entity_balances_base as (
    select dt, company_id,
        sum(case when assignment_dt::date = dt then balance end)
            over (partition by company_id) as jvs_transfer_balance,
        sum(case when t.assignment_dt::date < lt.dt then credit_amount end)
            over (partition by company_id order by dt rows unbounded preceding)
            as cumulative_credits
    from capital_markets_dm.loc_tape lt
    join sofom_transfer t on t.id = lt.company_id and t.assignment_dt::date <= lt.dt
),
entity_balances as (
    select dt, company_id,
        greatest(jvs_transfer_balance + coalesce(cumulative_credits, 0), 0) as jvs_remaining
    from entity_balances_base
),
tape as (
    select lt.dt, lt.balance_usd, lt.days_past_due,
        case when st.transfer_flag = 'on'
                  and (lt.balance - eb.jvs_remaining) * lt.spot_rate > 0
             then 0 else 1 end as unencumbered
    from capital_markets_dm.loc_tape lt
    left join entity_balances eb on eb.company_id = lt.company_id and eb.dt = lt.dt
    left join sofom_transfer st on st.id = lt.company_id
    where lt.dt in ({dates})
      and lt.charge_off_flag = false
      and lt.is_in_repayment = false
)
select dt
     , sum(case when unencumbered = 1 and days_past_due > 30 then balance_usd else 0 end) as dq
     , sum(case when unencumbered = 1 then balance_usd else 0 end) as total
from tape
group by 1
order by 1
"""


def month_end_window(as_of: date, n: int = 3) -> list[date]:
    """The n most recently ENDED calendar month ends as of `as_of` (oldest first)."""
    first = date(as_of.year, as_of.month, 1)
    ends = []
    for _ in range(n):
        end = first - timedelta(days=1)
        ends.append(end)
        first = date(end.year, end.month, 1)
    return list(reversed(ends))


def query(dates: list[date]) -> dict:
    sys.path.insert(0, str(SKILLS / "_shared"))
    from redshift import connect
    lits = ", ".join("date '{}'".format(d.isoformat()) for d in dates)
    cur = connect().cursor()
    cur.execute(SQL.format(dates=lits))
    out = {}
    for dt_, dq, total in cur.fetchall():
        d = dt_ if isinstance(dt_, date) else datetime.strptime(str(dt_)[:10], "%Y-%m-%d").date()
        out[d] = (float(dq or 0), float(total or 0))
    return out


def _read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("state file unreadable — starting fresh")
    return {"readings": {}}


def _write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _m(v: float) -> str:
    return "${:,.1f}M".format(v / 1_000_000) if abs(v) >= 1_000_000 else "${:,.0f}K".format(v / 1_000)


def _pct(v: float) -> str:
    return "{:.2f}%".format(v * 100)


def build_message(as_of: date | None = None) -> str:
    as_of = as_of or (date.today() - timedelta(days=1))
    months = month_end_window(as_of)
    dates = sorted(set(months + [as_of]))
    data = query(dates)

    if as_of not in data or data[as_of][1] == 0:
        raise RuntimeError("no loan tape rows for {}".format(as_of.isoformat()))

    dq, total = data[as_of]
    spot = dq / total

    # persist + day-over-day delta
    state = _read_state()
    readings = state.setdefault("readings", {})
    prior_key = max((k for k in readings if k < as_of.isoformat()), default=None)
    prior = readings.get(prior_key, {}).get("rate") if prior_key else None
    readings[as_of.isoformat()] = {"dq_usd": round(dq, 2), "total_usd": round(total, 2),
                                   "rate": spot}
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    _write_state(state)

    monthly = []
    for m in months:
        if m in data and data[m][1]:
            monthly.append((m, data[m][0] / data[m][1]))
    avg = sum(r for _, r in monthly) / len(monthly) if monthly else None

    lines = [
        "*Jeeves — NB Facility Delinquency (DQ30) Reading · {}*".format(
            as_of.strftime("%a %b %d, %Y").replace(" 0", " ")),
        "",
        "*Spot DQ30: {}*  —  {} of {} unencumbered receivables more than 30 days past due".format(
            _pct(spot), _m(dq), _m(total)),
    ]
    if prior is not None:
        delta = (spot - prior) * 10_000
        lines.append("Change since {}: {:+.0f} bps".format(prior_key, delta))

    if monthly:
        lines += [
            "",
            "Covenant test (Section 7.01(b) Maximum Delinquency): 3-month rolling average "
            "of *month-end* readings must stay below {}".format(_pct(COVENANT_CAP)),
            "   " + "   |   ".join("{}: {}".format(m.strftime("%b %d"), _pct(r))
                                   for m, r in monthly),
            "   *3-month average: {}* — {}".format(
                _pct(avg), "in compliance" if avg < COVENANT_CAP else "ABOVE THE 2.50% LIMIT"),
        ]
        if avg < STEP_UP:
            lines.append("   Advance-rate step-up prong (<1.50% for three consecutive "
                         "months): currently met")
    lines += [
        "",
        "_The daily figure is a spot reading, not a covenant test point — the covenant is "
        "measured only at month end on the three-month average. Basis: Jeeves loan tape; "
        "unencumbered excludes receivables financed under the MX SOFOM facility, "
        "charged-off accounts and accounts in a repayment plan._",
    ]
    return "\n".join(lines)


def send() -> bool:
    try:
        msg = build_message()
    except Exception as e:
        log.error("DQ30 build failed: %s", e)
        msg = ("*Jeeves — NB Facility DQ30 Reading*\nNot available this morning: {} "
               "(loan tape unreachable). I will retry on the next run.".format(
                   str(e)[:160]))
    if os.environ.get("NB_DQ30_DRY_RUN") == "1":
        log.info("DRY RUN - not sending. Message follows:")
        print(msg)
        return True
    r = subprocess.run([sys.executable, str(SLACK_TOOL), "send", owner_slack_id(), msg],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log.error("Slack send failed: %s", r.stderr[:300])
        return False
    log.info("DQ30 reading sent (%d chars)", len(msg))
    return True


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if "--print" in sys.argv:
        arg = [a for a in sys.argv[1:] if not a.startswith("-")]
        as_of = datetime.strptime(arg[0], "%Y-%m-%d").date() if arg else None
        print(build_message(as_of))
    else:
        sys.exit(0 if send() else 1)

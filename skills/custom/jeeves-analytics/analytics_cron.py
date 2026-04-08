#!/usr/bin/env python3
"""Jeeves Analytics Cron — proactive portfolio monitoring.

Behaviors:
  - Daily 07:00 local: anomaly check → Slack DM only if a threshold is breached
  - Monday 07:00 local: weekly portfolio summary → Slack DM always

Env vars required:
  - REDSHIFT_HOST, REDSHIFT_PORT, REDSHIFT_DB, REDSHIFT_USER, REDSHIFT_PASSWORD
  - SLACK_BOT_TOKEN (xoxb-...)
  - SLACK_OWNER_USER_ID

Optional:
  - ANALYTICS_CRON_INTERVAL (seconds between checks, default: 3600)
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format='[AnalyticsCron %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('analytics_cron')

CHECK_INTERVAL = int(os.environ.get('ANALYTICS_CRON_INTERVAL', '3600'))  # 1 hour

# Anomaly thresholds
DPD90_CHANGE_THRESHOLD = 0.01       # 100 bps change in 90+ DPD rate triggers alert
DPD30_CHANGE_THRESHOLD = 0.02       # 200 bps change in 30+ DPD rate
BALANCE_DROP_THRESHOLD = 0.05       # 5% single-day portfolio balance drop
DAILY_CHARGEOFF_THRESHOLD = 250_000  # $250K in new charge-offs in one day

COUNTRY_NAMES = {840: 'US', 484: 'MX', 170: 'CO', 124: 'CA', 76: 'BR'}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_path():
    backend = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.deer-flow'
    os.makedirs(backend, exist_ok=True)
    return str(backend / '_analytics_cron_state.json')


def load_state():
    path = _state_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {'last_weekly': None, 'last_daily': None}


def save_state(state):
    with open(_state_path(), 'w') as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Redshift
# ---------------------------------------------------------------------------

def _connect():
    try:
        import psycopg2
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'psycopg2-binary'])
        import psycopg2
    return psycopg2.connect(
        host=os.environ['REDSHIFT_HOST'],
        port=int(os.environ['REDSHIFT_PORT']),
        dbname=os.environ['REDSHIFT_DB'],
        user=os.environ['REDSHIFT_USER'],
        password=os.environ['REDSHIFT_PASSWORD'],
        sslmode='require',
        sslrootcert='disable',
        connect_timeout=30,
    )


def _query(sql):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()


def _scalar(sql):
    _, rows = _query(sql)
    return rows[0][0] if rows and rows[0] else None


# ---------------------------------------------------------------------------
# Portfolio queries
# ---------------------------------------------------------------------------

def get_max_dt():
    # Use the last date where balance_usd is actually populated.
    # Today's rows often exist in the tape before the ETL populates balances.
    return str(_scalar(
        "SELECT MAX(dt) FROM capital_markets_dm.loc_tape "
        "WHERE balance_usd IS NOT NULL AND charge_off_flag = false AND is_in_repayment = false"
    ))


def get_loc_snapshot(date):
    _, rows = _query(f"""
        SELECT
            SUM(balance_usd),
            SUM(CASE WHEN days_past_due >= 90 THEN balance_usd ELSE 0 END),
            SUM(CASE WHEN days_past_due >= 30 THEN balance_usd ELSE 0 END),
            SUM(CASE WHEN days_past_due >= 90 THEN balance_usd ELSE 0 END)
                / NULLIF(SUM(balance_usd), 0),
            SUM(CASE WHEN days_past_due >= 30 THEN balance_usd ELSE 0 END)
                / NULLIF(SUM(balance_usd), 0),
            COUNT(DISTINCT company_id)
        FROM capital_markets_dm.loc_tape
        WHERE dt = '{date}'
          AND charge_off_flag = false AND is_in_repayment = false
    """)
    if not rows or rows[0][0] is None:
        return None
    r = rows[0]
    return {
        'total_balance': float(r[0] or 0),
        'dpd_90plus_bal': float(r[1] or 0),
        'dpd_30plus_bal': float(r[2] or 0),
        'dpd_90plus_rate': float(r[3] or 0),
        'dpd_30plus_rate': float(r[4] or 0),
        'company_count': int(r[5] or 0),
    }


def get_gwc_snapshot(date):
    _, rows = _query(f"""
        SELECT
            SUM(balance_usd),
            SUM(CASE WHEN days_past_due >= 90 THEN balance_usd ELSE 0 END)
                / NULLIF(SUM(balance_usd), 0)
        FROM capital_markets_dm.gwc_tape
        WHERE dt = '{date}' AND charge_off_flag = false
    """)
    if not rows or rows[0][0] is None:
        return None
    r = rows[0]
    return {
        'total_balance': float(r[0] or 0),
        'dpd_90plus_rate': float(r[1] or 0),
    }


def get_chargeoffs(start, end):
    _, rows = _query(f"""
        SELECT COALESCE(SUM(balance_usd), 0), COUNT(*)
        FROM capital_markets_dm.loc_tape
        WHERE charge_off_dt BETWEEN '{start}' AND '{end}'
          AND dt = charge_off_dt
    """)
    r = rows[0] if rows else (0, 0)
    return {'amount': float(r[0] or 0), 'count': int(r[1] or 0)}


def get_disbursements(start, end):
    val = _scalar(f"""
        SELECT COALESCE(SUM(
            COALESCE(disbursement_amount_usd, 0)
            + COALESCE(jeeves_pay_disbursement_amount_usd, 0)
        ), 0)
        FROM capital_markets_dm.loc_tape
        WHERE dt BETWEEN '{start}' AND '{end}'
          AND charge_off_flag = false AND is_in_repayment = false
    """)
    return float(val or 0)


def get_country_breakdown(date):
    _, rows = _query(f"""
        SELECT
            country_code,
            SUM(balance_usd),
            SUM(CASE WHEN days_past_due >= 90 THEN balance_usd ELSE 0 END)
                / NULLIF(SUM(balance_usd), 0)
        FROM capital_markets_dm.loc_tape
        WHERE dt = '{date}'
          AND charge_off_flag = false AND is_in_repayment = false
        GROUP BY country_code
        ORDER BY 2 DESC
    """)
    return [
        {
            'country': COUNTRY_NAMES.get(int(r[0]) if r[0] else 0, str(r[0])),
            'balance': float(r[1] or 0),
            'dpd_90plus_rate': float(r[2] or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _usd(val, d=1):
    if val >= 1_000_000:
        return f"${val/1_000_000:.{d}f}M"
    if val >= 1_000:
        return f"${val/1_000:.{d}f}K"
    return f"${val:.0f}"


def _pct(val, d=2):
    return f"{val * 100:.{d}f}%"


def _delta_pct(curr, prev):
    """WoW change as +X.X% or -X.X%."""
    if not prev or prev == 0:
        return ''
    change = (curr - prev) / prev * 100
    sign = '+' if change >= 0 else ''
    return f" ({sign}{change:.1f}% WoW)"


def _delta_bps(curr, prev):
    """Rate change in basis points."""
    if prev is None:
        return ''
    bps = (curr - prev) * 10_000
    sign = '+' if bps >= 0 else ''
    return f" ({sign}{bps:.0f}bps WoW)"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _post(text, blocks=None):
    token = os.environ.get('SLACK_BOT_TOKEN')
    owner_id = os.environ.get('SLACK_OWNER_USER_ID')
    if not token or not owner_id:
        log.warning("Slack not configured (SLACK_BOT_TOKEN / SLACK_OWNER_USER_ID missing).")
        return False
    try:
        from slack_sdk import WebClient
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'slack_sdk'])
        from slack_sdk import WebClient
    try:
        client = WebClient(token=token)
        dm = client.conversations_open(users=[owner_id])
        channel_id = dm['channel']['id']
        client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        log.info(f"Slack: {text[:80]}")
        return True
    except Exception as e:
        log.error(f"Slack post failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

def run_weekly_summary():
    log.info("Running weekly summary...")
    try:
        as_of = get_max_dt()
        as_of_dt = datetime.strptime(as_of, '%Y-%m-%d').date()
        prior_week = str(as_of_dt - timedelta(days=7))
        week_start = str(as_of_dt - timedelta(days=6))

        current = get_loc_snapshot(as_of)
        prior = get_loc_snapshot(prior_week)
        chargeoffs = get_chargeoffs(week_start, as_of)
        disbursements = get_disbursements(week_start, as_of)
        countries = get_country_breakdown(as_of)
        gwc = get_gwc_snapshot(as_of)

        if not current:
            log.warning("No LOC snapshot data, skipping weekly summary.")
            return

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"Weekly Portfolio Summary — {as_of}"}},
            {"type": "divider"},
        ]

        # LOC
        bal_d = _delta_pct(current['total_balance'], prior['total_balance'] if prior else None)
        d90_d = _delta_bps(current['dpd_90plus_rate'], prior['dpd_90plus_rate'] if prior else None)
        d30_d = _delta_bps(current['dpd_30plus_rate'], prior['dpd_30plus_rate'] if prior else None)

        loc_text = (
            f"*LOC Portfolio*\n"
            f"Balance: *{_usd(current['total_balance'])}*{bal_d} | "
            f"{current['company_count']} companies\n"
            f"90+ DPD: *{_pct(current['dpd_90plus_rate'])}*{d90_d}\n"
            f"30+ DPD: *{_pct(current['dpd_30plus_rate'])}*{d30_d}\n"
            f"Charge-offs (7d): *{_usd(chargeoffs['amount'])}* ({chargeoffs['count']} accounts)\n"
            f"Disbursements (7d): *{_usd(disbursements)}*"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": loc_text}})
        blocks.append({"type": "divider"})

        # GWC
        if gwc and gwc['total_balance'] > 0:
            gwc_text = (
                f"*GWC Portfolio*\n"
                f"Balance: *{_usd(gwc['total_balance'])}* | "
                f"90+ DPD: *{_pct(gwc['dpd_90plus_rate'])}*"
            )
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": gwc_text}})
            blocks.append({"type": "divider"})

        # Country breakdown
        if countries:
            lines = [
                f"{c['country']}: {_usd(c['balance'])} | 90+ DPD: {_pct(c['dpd_90plus_rate'])}"
                for c in countries if c['balance'] >= 1000
            ]
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*By Country*\n" + "\n".join(lines)}})

        _post(text=f"Weekly Portfolio Summary — {as_of}", blocks=blocks)
        log.info("Weekly summary posted.")

    except Exception as e:
        log.error(f"Weekly summary failed: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Daily anomaly check
# ---------------------------------------------------------------------------

def run_anomaly_check():
    log.info("Running anomaly check...")
    try:
        as_of = get_max_dt()
        as_of_dt = datetime.strptime(as_of, '%Y-%m-%d').date()
        prior_day = str(as_of_dt - timedelta(days=1))

        current = get_loc_snapshot(as_of)
        prior = get_loc_snapshot(prior_day)
        chargeoffs_today = get_chargeoffs(as_of, as_of)

        if not current or not prior:
            log.info("Snapshot data unavailable, skipping anomaly check.")
            return

        alerts = []

        dpd90_change = current['dpd_90plus_rate'] - prior['dpd_90plus_rate']
        if abs(dpd90_change) >= DPD90_CHANGE_THRESHOLD:
            direction = "spiked" if dpd90_change > 0 else "dropped"
            alerts.append(
                f":rotating_light: *90+ DPD rate {direction} {abs(dpd90_change)*10000:.0f}bps DoD* — "
                f"{_pct(prior['dpd_90plus_rate'])} → {_pct(current['dpd_90plus_rate'])}"
            )

        dpd30_change = current['dpd_30plus_rate'] - prior['dpd_30plus_rate']
        if abs(dpd30_change) >= DPD30_CHANGE_THRESHOLD:
            direction = "spiked" if dpd30_change > 0 else "dropped"
            alerts.append(
                f":warning: *30+ DPD rate {direction} {abs(dpd30_change)*10000:.0f}bps DoD* — "
                f"{_pct(prior['dpd_30plus_rate'])} → {_pct(current['dpd_30plus_rate'])}"
            )

        if prior['total_balance'] > 0:
            bal_change = (current['total_balance'] - prior['total_balance']) / prior['total_balance']
            if bal_change <= -BALANCE_DROP_THRESHOLD:
                alerts.append(
                    f":chart_with_downwards_trend: *Portfolio balance dropped {abs(bal_change)*100:.1f}% DoD* — "
                    f"{_usd(prior['total_balance'])} → {_usd(current['total_balance'])}"
                )

        if chargeoffs_today['amount'] >= DAILY_CHARGEOFF_THRESHOLD:
            alerts.append(
                f":red_circle: *New charge-offs: {_usd(chargeoffs_today['amount'])} "
                f"({chargeoffs_today['count']} accounts) on {as_of}*"
            )

        if not alerts:
            log.info("No anomalies detected.")
            return

        context = (
            f"LOC balance: {_usd(current['total_balance'])} | "
            f"90+ DPD: {_pct(current['dpd_90plus_rate'])} | "
            f"30+ DPD: {_pct(current['dpd_30plus_rate'])}"
        )
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"Portfolio Alert — {as_of}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(alerts)}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
        ]
        _post(text=f"Portfolio Alert — {as_of}", blocks=blocks)
        log.info(f"Alert posted: {len(alerts)} threshold(s) breached.")

    except Exception as e:
        log.error(f"Anomaly check failed: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop():
    log.info(f"Analytics cron started. Checking every {CHECK_INTERVAL}s.")
    while True:
        try:
            state = load_state()
            now = datetime.now()
            today_str = now.date().isoformat()

            # Fire if within the 7am window OR if it's past 7am and the job
            # hasn't run yet today (handles restarts after the 7am window).
            if now.hour >= 7:
                # Weekly summary on Mondays
                if now.weekday() == 0 and state.get('last_weekly') != today_str:
                    run_weekly_summary()
                    state['last_weekly'] = today_str
                    save_state(state)

                # Daily anomaly check every day
                if state.get('last_daily') != today_str:
                    run_anomaly_check()
                    state['last_daily'] = today_str
                    save_state(state)

        except Exception as e:
            log.error(f"Cron loop error: {e}")
            traceback.print_exc()

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    # One-shot modes for testing / manual runs
    import argparse
    parser = argparse.ArgumentParser(description='Jeeves Analytics Cron')
    parser.add_argument('mode', nargs='?', choices=['weekly', 'anomaly'],
                        help='Run once in this mode instead of looping')
    args = parser.parse_args()

    if args.mode == 'weekly':
        run_weekly_summary()
    elif args.mode == 'anomaly':
        run_anomaly_check()
    else:
        run_loop()

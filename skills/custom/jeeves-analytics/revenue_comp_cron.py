#!/usr/bin/env python3
"""Revenue Comp Cron — daily MTD revenue comparison (this month vs last month).

Runs daily at 8am+. Posts a Slack message with country and product breakdowns.

Env vars required:
  - REDSHIFT_HOST, REDSHIFT_PORT, REDSHIFT_DB, REDSHIFT_USER, REDSHIFT_PASSWORD
  - SLACK_BOT_TOKEN (xoxb-...)
  - SLACK_OWNER_USER_ID
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

import psycopg2
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='[RevenueComp %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('revenue_comp')

CHECK_INTERVAL = int(os.environ.get('REVENUE_COMP_INTERVAL', '3600'))  # 1 hour

COUNTRY_NAMES = {
    840: "US", 484: "MX", 170: "CO", 124: "CA", 76: "BR",
    724: "ES", 380: "IT", 826: "UK", 620: "PT", 300: "GR",
    705: "SI", 196: "CY", 372: "IE", 233: "EE", 276: "DE",
    470: "MT", 528: "NL", 40: "AT", 250: "FR", 442: "LU",
    32: "AR", 591: "PA",
}

SQL = """
SELECT
    company_country_code
    , product
    , COALESCE(SUM(revenue_usd), 0) as revenue_usd
FROM master_transactions_dm.transactions_ssot
WHERE posted_at::date >= '{start}'
  AND posted_at::date <= '{end}'
  AND is_company_test = false
GROUP BY 1, 2
ORDER BY 1, 2
"""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_path():
    backend = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.deer-flow'
    os.makedirs(backend, exist_ok=True)
    return str(backend / '_revenue_comp_state.json')


def load_state():
    path = _state_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(_state_path(), 'w') as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _post_slack(text):
    token = os.environ.get('SLACK_BOT_TOKEN')
    owner_id = os.environ.get('SLACK_OWNER_USER_ID')
    if not token or not owner_id:
        log.warning("Slack not configured.")
        return False
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        dm = client.conversations_open(users=[owner_id])
        channel_id = dm['channel']['id']
        client.chat_postMessage(channel=channel_id, text=text, mrkdwn=True)
        return True
    except Exception as e:
        log.error(f"Slack post failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Redshift
# ---------------------------------------------------------------------------

def _query_redshift(sql):
    con = psycopg2.connect(
        host=os.environ['REDSHIFT_HOST'],
        port=int(os.environ['REDSHIFT_PORT']),
        database=os.environ['REDSHIFT_DB'],
        user=os.environ['REDSHIFT_USER'],
        password=os.environ['REDSHIFT_PASSWORD'],
        sslmode='require',
        sslrootcert='disable',
        connect_timeout=30,
        options='-c statement_timeout=120000',
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    df = pd.read_sql_query(sql, con)
    con.close()
    return df


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt(val):
    if abs(val) >= 1_000_000:
        return f"${val/1_000_000:,.1f}M"
    elif abs(val) >= 1_000:
        return f"${val/1_000:,.1f}K"
    else:
        return f"${val:,.0f}"


def _pct_change(current, prior):
    if prior == 0:
        return "N/A" if current == 0 else "+inf"
    change = (current - prior) / abs(prior) * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}%"


def _trend_emoji(current, prior):
    if prior == 0:
        return ":new:"
    change = (current - prior) / abs(prior) * 100
    if change >= 20:
        return ":rocket:"
    elif change >= 5:
        return ":chart_with_upwards_trend:"
    elif change <= -20:
        return ":chart_with_downwards_trend:"
    elif change <= -5:
        return ":small_red_triangle_down:"
    else:
        return ":arrow_right:"


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

MIN_REVENUE = 10_000


def _build_message(df_current, df_prior, yesterday, prior_end):
    total_current = df_current["revenue_usd"].sum()
    total_prior = df_prior["revenue_usd"].sum()

    month_name = yesterday.strftime("%B %Y")
    prior_month_name = prior_end.strftime("%B %Y")
    day = yesterday.day

    total_emoji = _trend_emoji(total_current, total_prior)
    lines = [
        f"*{total_emoji} MTD Revenue Comp — Day {day}*",
        f"*{month_name}:* {_fmt(total_current)}  vs  *{prior_month_name}:* {_fmt(total_prior)}  ({_pct_change(total_current, total_prior)})",
        "",
        "*:earth_americas: By Country:*",
    ]

    curr_country = df_current.groupby("company_country_code")["revenue_usd"].sum().to_dict()
    prior_country = df_prior.groupby("company_country_code")["revenue_usd"].sum().to_dict()
    all_countries = sorted(
        set(list(curr_country.keys()) + list(prior_country.keys())),
        key=lambda c: curr_country.get(c, 0), reverse=True,
    )
    for cc in all_countries:
        c = curr_country.get(cc, 0)
        p = prior_country.get(cc, 0)
        if c < MIN_REVENUE and p < MIN_REVENUE:
            continue
        name = COUNTRY_NAMES.get(cc, str(cc))
        e = _trend_emoji(c, p)
        lines.append(f"  {e} {name}: {_fmt(c)} vs {_fmt(p)} ({_pct_change(c, p)})")

    lines.append("")
    lines.append("*:label: By Product:*")

    curr_product = df_current.groupby("product")["revenue_usd"].sum().to_dict()
    prior_product = df_prior.groupby("product")["revenue_usd"].sum().to_dict()
    all_products = sorted(
        set(list(curr_product.keys()) + list(prior_product.keys())),
        key=lambda p: curr_product.get(p, 0), reverse=True,
    )
    for prod in all_products:
        c = curr_product.get(prod, 0)
        p = prior_product.get(prod, 0)
        e = _trend_emoji(c, p)
        lines.append(f"  {e} {prod}: {_fmt(c)} vs {_fmt(p)} ({_pct_change(c, p)})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run once
# ---------------------------------------------------------------------------

def run_revenue_comp():
    import calendar

    yesterday = date.today() - timedelta(days=1)
    current_start = yesterday.replace(day=1)
    current_end = yesterday

    if yesterday.month == 1:
        prior_start = date(yesterday.year - 1, 12, 1)
        prior_end = date(yesterday.year - 1, 12, yesterday.day)
    else:
        prior_start = date(yesterday.year, yesterday.month - 1, 1)
        try:
            prior_end = date(yesterday.year, yesterday.month - 1, yesterday.day)
        except ValueError:
            last_day = calendar.monthrange(yesterday.year, yesterday.month - 1)[1]
            prior_end = date(yesterday.year, yesterday.month - 1, last_day)

    log.info(f"Running revenue comp: {current_start} to {current_end} vs {prior_start} to {prior_end}")

    df_current = _query_redshift(SQL.format(start=current_start, end=current_end))
    log.info(f"  Current: {len(df_current)} rows, {_fmt(df_current['revenue_usd'].sum())}")

    df_prior = _query_redshift(SQL.format(start=prior_start, end=prior_end))
    log.info(f"  Prior: {len(df_prior)} rows, {_fmt(df_prior['revenue_usd'].sum())}")

    msg = _build_message(df_current, df_prior, yesterday, prior_end)
    _post_slack(msg)
    log.info("Revenue comp posted to Slack.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop():
    log.info(f"Revenue comp cron started. Checking every {CHECK_INTERVAL}s.")
    while True:
        try:
            state = load_state()
            now = datetime.now()
            today_str = now.date().isoformat()

            # Fire once per day after 8am
            if now.hour >= 8 and state.get('last_run') != today_str:
                run_revenue_comp()
                state['last_run'] = today_str
                save_state(state)

        except Exception as e:
            log.error(f"Revenue comp error: {e}")
            traceback.print_exc()

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    run_revenue_comp()

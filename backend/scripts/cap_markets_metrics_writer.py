#!/usr/bin/env python3
"""
cap_markets_metrics_writer.py
==============================
Reads Capital Markets dashboard data (BB state JSON, ops state files)
and writes _cap_markets_state.json for the Prometheus scrape endpoint.

Run after dashboard_full_update.py completes, or standalone.
"""
import os, sys, json, re
from datetime import date, datetime
from pathlib import Path

STATE_DIR = Path("C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow")
OUT_FILE = STATE_DIR / "_cap_markets_state.json"


def days_since(val):
    """Return float days since a date/datetime string, or None."""
    if not val:
        return None
    try:
        s = str(val)
        if 'T' in s or (' ' in s and ':' in s):
            dt = datetime.fromisoformat(s)
            return (datetime.now() - dt.replace(tzinfo=None)).total_seconds() / 86400
        else:
            dt = datetime.fromisoformat(s)
            return (date.today() - dt.date()).days
    except Exception:
        return None


def load_report_state():
    states = {}
    for fname in [
        '_analytics_cron_state',
        '_revenue_comp_state',
        '_report_scheduler_state',
        '_dreams_state',
        '_eod_review_state',
    ]:
        fp = STATE_DIR / f"{fname}.json"
        if fp.exists():
            try:
                states[fname.lstrip('_')] = json.loads(fp.read_text())
            except Exception:
                pass
    return states


def build_state(states):
    sched = states.get('report_scheduler_state', {})
    analytics = states.get('analytics_cron_state', {})
    rev = states.get('revenue_comp_state', {})
    dreams = states.get('dreams_state', {})
    eod = states.get('eod_review_state', {})

    # Cron health — days since last run (lower = healthier)
    cron_health = {
        'us_bb_days_ago':         days_since(sched.get('last_bb')),
        'mx_bb_days_ago':         days_since(sched.get('last_mx_bb')),
        'sofom_dist_days_ago':    days_since(sched.get('last_sofom_distribution')),
        'cico_days_ago':          days_since(sched.get('last_cico_dashboard')),
        'analytics_days_ago':     days_since(analytics.get('last_daily')),
        'revenue_comp_days_ago':  days_since(rev.get('last_run')),
        'dreams_days_ago':        days_since(dreams.get('last_dream')),
        'eod_review_days_ago':    days_since(eod.get('last_eod')),
    }

    # Last run timestamps (unix epoch) for timeseries
    def to_epoch(val):
        if not val:
            return None
        try:
            s = str(val)
            if 'T' in s or (' ' in s and ':' in s):
                return datetime.fromisoformat(s).timestamp()
            else:
                return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None

    cron_last_ts = {
        'us_bb_ts':        to_epoch(sched.get('last_bb')),
        'mx_bb_ts':        to_epoch(sched.get('last_mx_bb')),
        'sofom_dist_ts':   to_epoch(sched.get('last_sofom_distribution')),
        'analytics_ts':    to_epoch(analytics.get('last_daily')),
        'revenue_comp_ts': to_epoch(rev.get('last_run')),
        'dreams_ts':       to_epoch(dreams.get('last_dream')),
        'dream_count':     dreams.get('dream_count', 0),
        'eod_count':       eod.get('review_count', 0),
    }

    return {
        'cron_health': cron_health,
        'cron_last_ts': cron_last_ts,
        'updated_at': datetime.now().isoformat(),
    }


def main():
    states = load_report_state()
    state = build_state(states)

    # If a BB state file exists (written by dashboard_full_update.py), merge it
    bb_state_file = STATE_DIR / "_bb_metrics_state.json"
    if bb_state_file.exists():
        try:
            bb = json.loads(bb_state_file.read_text())
            state['us_bridge'] = bb.get('us_bridge', {})
            state['mx_sofom'] = bb.get('mx_sofom', {})
            state['portfolio'] = bb.get('portfolio', {})
        except Exception as e:
            print(f"Warning: could not read BB state: {e}", file=sys.stderr)

    OUT_FILE.write_text(json.dumps(state, indent=2))
    print(f"Wrote {OUT_FILE}")
    return state


if __name__ == '__main__':
    import pprint
    s = main()
    pprint.pprint(s)

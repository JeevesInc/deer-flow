"""Shared cron scheduling helpers (GW-F10).

Exact `now.hour == TARGET` checks give each daily/weekly cron a single
one-hour firing window: if the process is down (or the hourly tick drifts)
across that window, the run is silently skipped — a whole week for weekly
crons. These helpers express "the most recent scheduled fire time", so
callers fire whenever the last successful run predates it, catching up
after downtime instead of missing silently.
"""
from datetime import datetime, timedelta


def last_daily_due(now: datetime, hour: int) -> datetime:
    """Most recent daily fire time <= now (today at `hour`, else yesterday's)."""
    due = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if due > now:
        due -= timedelta(days=1)
    return due


def last_weekly_due(now: datetime, weekday: int, hour: int) -> datetime:
    """Most recent weekly fire time <= now for `weekday` (Mon=0..Sun=6) at `hour`."""
    days_since = (now.weekday() - weekday) % 7
    due = (now - timedelta(days=days_since)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if due > now:
        due -= timedelta(days=7)
    return due


def weekly_run_due(last_run_iso, now: datetime, weekday: int, hour: int) -> bool:
    """True when a weekly cron should fire.

    With history: fire when the last run predates the most recent scheduled
    time — this is what catches up a run missed during downtime. Without
    history (first boot, corrupt state): fire only on the scheduled day once
    the hour has passed, so a fresh install doesn't fire mid-week on boot.
    """
    fallback = now.weekday() == weekday and now.hour >= hour
    if not last_run_iso:
        return fallback
    try:
        last_dt = datetime.fromisoformat(last_run_iso)
    except (ValueError, TypeError):
        return fallback
    return last_dt < last_weekly_due(now, weekday, hour)

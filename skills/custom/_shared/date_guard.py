"""Shared date guard for Redshift-querying skill scripts.

Redshift data is only complete through yesterday — today's rows are partial
and produce silently wrong results (null credit_limit_usd, missing
eligibility flags, etc.). Every script that takes a date input should call
``reject_today_or_future()`` before issuing queries.
"""

from __future__ import annotations

import datetime as _dt
import sys


def reject_today_or_future(date_str: str, *, label: str = "query date") -> _dt.date:
    """Parse ``date_str`` (YYYY-MM-DD) and refuse today or future dates.

    Returns the parsed date on success. Exits the process with a non-zero
    status on failure so callers don't accidentally proceed with bad data.
    """
    try:
        d = _dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as e:
        print(f"ERROR: invalid {label} {date_str!r}: {e}", file=sys.stderr)
        sys.exit(2)

    today = _dt.date.today()
    if d >= today:
        print(
            f"ERROR: {label} {d.isoformat()} is today or in the future. "
            "Redshift data is only complete through yesterday "
            f"({(today - _dt.timedelta(days=1)).isoformat()}). Refusing to query.",
            file=sys.stderr,
        )
        sys.exit(2)
    return d


def yesterday() -> _dt.date:
    return _dt.date.today() - _dt.timedelta(days=1)


def last_complete_month_end() -> _dt.date:
    """Return the last day of the previous month (always fully reported)."""
    today = _dt.date.today()
    first_of_this_month = today.replace(day=1)
    return first_of_this_month - _dt.timedelta(days=1)

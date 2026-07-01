"""Tests for the stuck-run monitor's checkpoint freshness heuristic.

Regression for 2026-06-10: the monitor cancelled an actively-working run at
the 10-minute mark because run-level ``updated_at`` freezes at run start.
``checkpoint_age_seconds`` reads the latest checkpoint write time from a
``threads.get_state`` response, which advances on every graph superstep.
"""

from datetime import datetime, timedelta, timezone

from app.channels.manager_helpers import checkpoint_age_seconds

NOW = datetime(2026, 6, 10, 19, 13, 0, tzinfo=timezone.utc)


def test_fresh_checkpoint_returns_small_age():
    state = {"created_at": (NOW - timedelta(seconds=42)).isoformat()}
    age = checkpoint_age_seconds(state, NOW)
    assert age is not None
    assert abs(age - 42) < 1


def test_stale_checkpoint_returns_large_age():
    state = {"created_at": (NOW - timedelta(seconds=900)).isoformat()}
    age = checkpoint_age_seconds(state, NOW)
    assert age is not None
    assert age > 600


def test_z_suffix_timestamp_parses():
    state = {"created_at": "2026-06-10T19:12:30Z"}
    age = checkpoint_age_seconds(state, NOW)
    assert age is not None
    assert abs(age - 30) < 1


def test_naive_timestamp_assumed_utc():
    state = {"created_at": "2026-06-10T19:12:00"}
    age = checkpoint_age_seconds(state, NOW)
    assert age is not None
    assert abs(age - 60) < 1


def test_missing_created_at_returns_none():
    assert checkpoint_age_seconds({}, NOW) is None
    assert checkpoint_age_seconds({"created_at": None}, NOW) is None
    assert checkpoint_age_seconds({"created_at": ""}, NOW) is None


def test_non_mapping_state_returns_none():
    assert checkpoint_age_seconds(None, NOW) is None
    assert checkpoint_age_seconds("oops", NOW) is None
    assert checkpoint_age_seconds(["list"], NOW) is None


def test_unparseable_timestamp_returns_none():
    assert checkpoint_age_seconds({"created_at": "not-a-date"}, NOW) is None
    assert checkpoint_age_seconds({"created_at": 12345}, NOW) is None


def test_datetime_object_accepted():
    state = {"created_at": NOW - timedelta(seconds=10)}
    age = checkpoint_age_seconds(state, NOW)
    assert age is not None
    assert abs(age - 10) < 1

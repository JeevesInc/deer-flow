"""Tests for CronSupervisor observability (P1-1 / GW-F4/F5).

The supervisor previously had no tests. Covers: a clean run_loop return is
treated as abnormal (exit alert, marked not-running) rather than "recovered";
crash counting; the record_heartbeat() hook; and the cron liveness gauges.
"""
from __future__ import annotations

import pytest

from app.gateway import cron_supervisor as cs


@pytest.fixture(autouse=True)
def _isolate_supervisors():
    saved = list(cs._supervisors)
    cs._supervisors.clear()
    yield
    cs._supervisors.clear()
    cs._supervisors.extend(saved)


def test_clean_exit_is_treated_as_abnormal(monkeypatch):
    alerts: list[str] = []
    monkeypatch.setattr(cs, "_slack_alert", alerts.append)

    sv = cs.CronSupervisor("exit-cron", lambda: None, startup_delay=0)
    sv._supervised_loop()  # target returns immediately

    assert sv.exited is True
    assert sv.running is False
    assert any("exited" in a.lower() for a in alerts)
    # must NOT claim recovery on an exit
    assert not any("recovered" in a.lower() for a in alerts)


def test_crash_increments_count_and_alerts(monkeypatch):
    alerts: list[str] = []
    monkeypatch.setattr(cs, "_slack_alert", alerts.append)

    def boom():
        raise RuntimeError("kaboom")

    sv = cs.CronSupervisor("crash-cron", boom, startup_delay=0)
    # Break after the first crash without waiting out the backoff.
    monkeypatch.setattr(sv._stop_event, "wait", lambda timeout=None: True)
    sv._supervised_loop()

    assert sv.crash_count == 1
    assert sv.exited is False
    assert any("crashed" in a.lower() for a in alerts)


def test_record_heartbeat_updates_named_supervisor():
    sv = cs.CronSupervisor("hb-cron", lambda: None, startup_delay=0)
    cs._supervisors.append(sv)

    assert sv.last_heartbeat == 0.0
    cs.record_heartbeat("hb-cron")
    assert sv.last_heartbeat > 0
    cs.record_heartbeat("does-not-exist")  # must not raise


def test_refresh_crons_populates_gauges():
    from app.gateway import metrics

    sv = cs.CronSupervisor("metric-cron", lambda: None, startup_delay=0)
    sv.crash_count = 2
    sv.exited = True
    sv.last_heartbeat = 1234.0
    cs._supervisors.append(sv)

    try:
        metrics._refresh_crons()
        gsv = metrics.registry.get_sample_value
        assert gsv("deerflow_cron_exited", {"cron": "metric-cron"}) == 1.0
        assert gsv("deerflow_cron_crash_total", {"cron": "metric-cron"}) == 2.0
        assert gsv("deerflow_cron_running", {"cron": "metric-cron"}) == 0.0
        assert gsv("deerflow_cron_last_heartbeat_ts", {"cron": "metric-cron"}) == 1234.0
    finally:
        cs._supervisors.clear()
        metrics._refresh_crons()  # clear the test label from the shared registry

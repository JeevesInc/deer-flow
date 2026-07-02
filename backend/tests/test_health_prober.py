"""Tests for the WinSW health prober's decision core (P3-1, 2026-07-02).

The prober replaces the start.sh supervision loop. The decision logic must:
restart only after N CONSECUTIVE failures, honor the per-service cooldown
(incl. across sibling checks of the same service), notify once per outage +
once on recovery, and give a fresh grace period after a restart attempt.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def hp():
    spec = importlib.util.spec_from_file_location("health_prober_test", _SCRIPTS / "health_prober.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations via sys.modules[cls.__module__]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Harness:
    def __init__(self, hp, checks):
        self.up: dict[str, bool] = {}
        self.restarts: list[str] = []
        self.notices: list[str] = []
        self.now = 1000.0
        self.mon = hp.Monitor(
            checks=checks,
            probe=lambda url: self.up.get(url, True),
            restart=lambda svc: (self.restarts.append(svc), True)[1],
            notify=self.notices.append,
            clock=lambda: self.now,
            restart_cooldown=300,
        )

    def tick(self, dt=30.0):
        self.mon.tick()
        self.now += dt


def _check(hp, name="svc", service="deerflow-svc", url="http://x/ok", threshold=3):
    return hp.Check(name, service, url, threshold)


def test_no_restart_below_threshold(hp):
    h = Harness(hp, [_check(hp, threshold=3)])
    h.up["http://x/ok"] = False
    h.tick(); h.tick()
    assert h.restarts == []


def test_restart_at_threshold_with_notice(hp):
    h = Harness(hp, [_check(hp, threshold=3)])
    h.up["http://x/ok"] = False
    h.tick(); h.tick(); h.tick()
    assert h.restarts == ["deerflow-svc"]
    assert any("restart" in n for n in h.notices)


def test_recovery_resets_streak_and_notifies_once(hp):
    h = Harness(hp, [_check(hp, threshold=3)])
    h.up["http://x/ok"] = False
    for _ in range(3):
        h.tick()
    h.up["http://x/ok"] = True
    h.tick()
    assert sum("recovered" in n for n in h.notices) == 1
    # A later blip starts from zero — no immediate restart.
    h.up["http://x/ok"] = False
    h.tick()
    assert len(h.restarts) == 1


def test_intermittent_failures_never_restart(hp):
    """CONSECUTIVE means consecutive — flapping below threshold stays quiet."""
    h = Harness(hp, [_check(hp, threshold=3)])
    for _ in range(5):
        h.up["http://x/ok"] = False
        h.tick(); h.tick()
        h.up["http://x/ok"] = True
        h.tick()
    assert h.restarts == []
    assert h.notices == []


def test_cooldown_blocks_rapid_re_restarts(hp):
    h = Harness(hp, [_check(hp, threshold=2)])
    h.up["http://x/ok"] = False
    h.tick(); h.tick()          # -> restart #1 (streak reset to 0)
    h.tick(); h.tick()          # threshold again, but only ~60s since restart
    assert h.restarts == ["deerflow-svc"]
    # After the 300s cooldown expires, it may restart again.
    h.now += 400
    h.tick(); h.tick()
    assert h.restarts == ["deerflow-svc", "deerflow-svc"]


def test_sibling_checks_share_service_cooldown(hp):
    """gateway-live and gateway-deep both map to deerflow-gateway — one
    restart, not two, when both cross threshold together."""
    live = _check(hp, name="gw-live", service="deerflow-gateway", url="http://gw/livez", threshold=2)
    deep = _check(hp, name="gw-deep", service="deerflow-gateway", url="http://gw/health", threshold=2)
    h = Harness(hp, [live, deep])
    h.up["http://gw/livez"] = False
    h.up["http://gw/health"] = False
    h.tick(); h.tick()
    assert h.restarts == ["deerflow-gateway"]


def test_restart_clears_sibling_streaks(hp):
    """After a restart the whole service gets fresh grace — the deep check
    must not immediately fire a second restart post-cooldown from a stale
    streak accumulated before the restart."""
    live = _check(hp, name="gw-live", service="deerflow-gateway", url="http://gw/livez", threshold=2)
    deep = _check(hp, name="gw-deep", service="deerflow-gateway", url="http://gw/health", threshold=8)
    h = Harness(hp, [live, deep])
    h.up["http://gw/livez"] = False
    h.up["http://gw/health"] = False
    h.tick(); h.tick()           # live crosses -> restart, sibling streaks reset
    # deep re-accumulates at most the post-reset probe of the same pass — it
    # did NOT keep its pre-restart streak.
    assert deep.streak <= 1
    # And even when deep later crosses ITS threshold (8 fails = 240s), the
    # 300s service cooldown prevents a second thrash-restart.
    for _ in range(7):
        h.tick()
    assert h.restarts == ["deerflow-gateway"]


def test_default_checks_cover_all_services(hp):
    services = {c.service for c in hp.default_checks()}
    assert services == {"deerflow-langgraph", "deerflow-gateway", "deerflow-webhook",
                        "deerflow-ngrok", "deerflow-dashboard"}
    deep = [c for c in hp.default_checks() if c.name == "gateway-deep"][0]
    assert deep.fail_threshold == 8  # the ~4-min busy-grace from the old supervisor

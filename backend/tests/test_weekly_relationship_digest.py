"""Tests for the weekly relationship-health digest (PRD §7 H4, 2026-07-02).

ONA staleness ("gone quiet") + a dialectic flag pass over the most active
external counterparties, posted as a second Monday DM — ONLY when there is
signal (silent otherwise, per the dreams-silent principle).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE = (Path(__file__).resolve().parent.parent.parent
          / "skills" / "custom" / "gmail" / "weekly_open_items_cron.py")

BRIAN = "brian.mauck@tryjeeves.com"


@pytest.fixture(scope="module")
def cron():
    spec = importlib.util.spec_from_file_location("weekly_cron_digest_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _edge(a, b, weight=1, weight_90d=0, last="2026-06-01"):
    return {"source": a, "target": b, "weight": weight,
            "weight_90d": weight_90d, "last_interaction": last}


EDGES = [
    # Active external counterparty (dialectic pass candidate)
    _edge(BRIAN, "cp@cim.com", weight=80, weight_90d=12, last="2026-06-30"),
    # Gone quiet: real history, zero in 90d
    _edge(BRIAN, "quiet@vista.com", weight=45, weight_90d=0, last="2026-02-10"),
    # Thin history + quiet -> below min_weight, not surfaced
    _edge(BRIAN, "thin@x.com", weight=3, weight_90d=0, last="2026-01-01"),
    # Internal colleague quiet -> excluded (vacations are normal)
    _edge(BRIAN, "colleague@tryjeeves.com", weight=500, weight_90d=0, last="2026-03-01"),
    # Non-person -> excluded
    _edge(BRIAN, "noreply@calendar.google.com", weight=99, weight_90d=9),
    # Edge not involving Brian -> ignored
    _edge("a@x.com", "b@y.com", weight=100, weight_90d=50),
]


def test_gone_quiet_filters_correctly(cron):
    quiet = cron.gone_quiet_peers(EDGES, BRIAN)
    assert [q["email"] for q in quiet] == ["quiet@vista.com"]
    assert quiet[0]["last_interaction"] == "2026-02-10"


def test_top_active_external_only(cron):
    active = cron.top_active_peers(EDGES, BRIAN)
    assert [a["email"] for a in active] == ["cp@cim.com"]


def test_digest_silent_when_no_signal(cron):
    edges = [_edge(BRIAN, "cp@cim.com", weight=80, weight_90d=12)]
    # Dialectic says NONE and nobody's gone quiet -> no DM at all.
    assert cron.build_relationship_digest(edges, lambda e: "NONE", BRIAN) == ""


def test_digest_includes_flags_and_quiet(cron):
    digest = cron.build_relationship_digest(
        EDGES, lambda e: "Tone cooled after the pricing call; June deliverable unacknowledged.", BRIAN)
    assert "Relationship health" in digest
    assert "cp@cim.com" in digest
    assert "Tone cooled" in digest
    assert "quiet@vista.com" in digest
    assert "colleague@tryjeeves.com" not in digest


def test_digest_survives_raising_ask_fn(cron):
    def boom(email):
        raise RuntimeError("honcho down")
    digest = cron.build_relationship_digest(EDGES, boom, BRIAN)
    # Quiet section still reports even when the dialectic is down.
    assert "quiet@vista.com" in digest
    assert "Flags" not in digest


def test_digest_treats_none_and_empty_as_no_flag(cron):
    digest = cron.build_relationship_digest(
        [_edge(BRIAN, "cp@cim.com", weight=80, weight_90d=12)],
        lambda e: "  none  ".upper(), BRIAN)
    assert digest == ""

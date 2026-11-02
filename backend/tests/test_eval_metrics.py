"""Tests for the jeeves_eval_* Prometheus gauges.

The property that matters here is the NaN-vs-zero distinction. If a missing eval
state published 0.0, the dashboard would show "accuracy 0.000" in red and read
as "the classifier is broken" when the truth is "nobody has run the eval". The
one gauge that must stay numeric is days_since_run — it is what you alert on,
so "never run" has to trip the same alert as "months stale".
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "packages" / "harness"))

from app.gateway import metrics  # noqa: E402


def _value(gauge, **labels):
    return gauge.labels(**labels)._value.get() if labels else gauge._value.get()


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_state_dir", lambda: tmp_path)
    return tmp_path


def _write(state_dir: Path, latest: dict) -> None:
    (state_dir / "_eval_state.json").write_text(
        json.dumps({"runs": [latest], "latest": latest}), encoding="utf-8"
    )


GOOD = {
    "accuracy": 0.9,
    "precision": 0.85,
    "recall": 0.8,
    "f1": 0.82,
    "true_positives": 17,
    "false_positives": 3,
    "false_negatives": 4,
    "true_negatives": 26,
    "action_score_mean": 1.7,
    "action_score_n": 12,
    "golden_set_size": 60,
    "latency_p50_ms": 7000.0,
    "unix_ts": 1787766542.0,
}


def test_publishes_all_metrics(state_dir):
    _write(state_dir, GOOD)
    metrics._refresh_eval()
    assert _value(metrics.g_eval_score, metric="accuracy") == 0.9
    assert _value(metrics.g_eval_score, metric="recall") == 0.8
    assert _value(metrics.g_eval_confusion, cell="fn") == 4
    assert _value(metrics.g_eval_action_score) == 1.7
    assert _value(metrics.g_eval_action_judged) == 12
    assert _value(metrics.g_eval_set_size) == 60


def test_missing_state_publishes_nan_not_zero(state_dir):
    metrics._refresh_eval()
    assert math.isnan(_value(metrics.g_eval_score, metric="accuracy"))
    assert math.isnan(_value(metrics.g_eval_confusion, cell="tp"))
    assert math.isnan(_value(metrics.g_eval_action_score))


def test_missing_state_still_trips_the_staleness_alert(state_dir):
    """'Never run' must alert exactly like 'ran once, months ago'."""
    metrics._refresh_eval()
    assert _value(metrics.g_eval_age_days) == 9999


def test_corrupt_state_degrades_to_unknown(state_dir):
    (state_dir / "_eval_state.json").write_text("{ not json", encoding="utf-8")
    metrics._refresh_eval()
    assert math.isnan(_value(metrics.g_eval_score, metric="f1"))
    assert _value(metrics.g_eval_age_days) == 9999


def test_state_without_latest_key_degrades(state_dir):
    (state_dir / "_eval_state.json").write_text(json.dumps({"runs": []}), encoding="utf-8")
    metrics._refresh_eval()
    assert math.isnan(_value(metrics.g_eval_score, metric="precision"))


def test_unjudged_action_score_is_nan(state_dir):
    """action_score_mean is legitimately None when no case had ground truth."""
    _write(state_dir, dict(GOOD, action_score_mean=None, action_score_n=0))
    metrics._refresh_eval()
    assert math.isnan(_value(metrics.g_eval_action_score))
    assert _value(metrics.g_eval_action_judged) == 0


def test_age_is_computed_from_the_run_timestamp(state_dir, monkeypatch):
    monkeypatch.setattr(metrics.time, "time", lambda: GOOD["unix_ts"] + 3 * 86400)
    _write(state_dir, GOOD)
    metrics._refresh_eval()
    assert _value(metrics.g_eval_age_days) == pytest.approx(3.0, abs=1e-6)


def test_missing_timestamp_is_treated_as_never_run(state_dir):
    _write(state_dir, dict(GOOD, unix_ts=None))
    metrics._refresh_eval()
    assert _value(metrics.g_eval_age_days) == 9999


def test_refresh_all_includes_eval(state_dir, monkeypatch):
    """A gauge nobody calls is a gauge nobody sees."""
    called = []
    monkeypatch.setattr(metrics, "_refresh_eval", lambda: called.append(True))
    for name in ("_refresh_db_size", "_refresh_store_sizes", "_refresh_active_runs",
                 "_refresh_dispatch_audit", "_refresh_thread_map", "_refresh_cap_markets",
                 "_refresh_crons"):
        monkeypatch.setattr(metrics, name, lambda *a, **k: None)
    monkeypatch.setattr(metrics, "_refresh_langgraph_up", lambda *a, **k: None)
    monkeypatch.setattr(metrics, "_refresh_langgraph_threads", lambda *a, **k: None)
    metrics.refresh_all()
    assert called == [True]

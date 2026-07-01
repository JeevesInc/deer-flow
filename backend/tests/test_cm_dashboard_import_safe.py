"""Regression tests for the 2026-06-16 gateway-flap incident.

Root cause: cron_supervisor loaded scripts/cm_credit_health_app.py via
spec.loader.exec_module() to obtain run_loop(). That Streamlit script had no
`if __name__ == "__main__"` guard, so importing it executed the entire
dashboard body — including three synchronous Redshift queries — on the
gateway's main thread at every startup. Slow Redshift then blocked /health
past the supervisor's readiness gate, causing a kill/restart flap.

Two invariants protect against regression:
  1. The dashboard module must be importable WITHOUT running its render body
     or any Redshift query (defense-in-depth: the __main__ guard).
  2. cron_supervisor must not load cm-dashboard in-process anymore (it is now
     launched as a supervised service from start.sh).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
DASHBOARD = BACKEND_DIR / "scripts" / "cm_credit_health_app.py"
CRON_SUPERVISOR = BACKEND_DIR / "app" / "gateway" / "cron_supervisor.py"


@pytest.mark.skipif(not DASHBOARD.exists(), reason="dashboard script not present")
def test_dashboard_import_does_not_query_redshift():
    """Importing the dashboard under a non-__main__ name must NOT run the body.

    Before the guard, exec_module() called load_dq_history() -> pd.read_sql(),
    which is exactly what blocked the gateway. We assert pd.read_sql is never
    invoked during import; the guard makes the body run only under
    `streamlit run` (__name__ == "__main__").
    """
    pytest.importorskip("streamlit")
    pytest.importorskip("pandas")

    spec = importlib.util.spec_from_file_location("cm_dashboard_import_probe", DASHBOARD)
    mod = importlib.util.module_from_spec(spec)
    assert mod.__name__ != "__main__"

    with mock.patch(
        "pandas.read_sql",
        side_effect=AssertionError("Redshift query ran at import time — __main__ guard missing"),
    ):
        spec.loader.exec_module(mod)

    # The supervisor only needs these symbols; they must still be exported.
    assert hasattr(mod, "run_loop")
    assert hasattr(mod, "_kill_stale_dashboards")


def test_cron_supervisor_does_not_load_cm_dashboard_in_process():
    src = CRON_SUPERVISOR.read_text(encoding="utf-8")
    # Any non-commented call would re-import the heavy script into the gateway.
    active = [
        ln for ln in src.splitlines()
        if '_load_and_start("cm-dashboard"' in ln and not ln.lstrip().startswith("#")
    ]
    assert active == [], f"cm-dashboard is still loaded in-process: {active}"

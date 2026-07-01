"""Tests for scripts/cm_morning_brief.py (P0-8 / GW-F1 regression).

Two real bugs the morning brief had (both path/formatting, NOT the NameError the
first-pass review guessed — the code uses the imported Path):
  1. SLACK_TOOL resolved to a nonexistent `deer-flow/deer-flow/skills/...` path
     (spurious extra segment), so even a reachable brief could never send.
  2. The CICO date label was hardcoded "Jun 8" instead of the state's source_date.
(The caller in report_scheduler_cron.py also pointed at a nonexistent
skills/custom/scripts/ path — fixed there and guarded below.)
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
BRIEF = BACKEND_DIR / "scripts" / "cm_morning_brief.py"
CRON = BACKEND_DIR.parent / "skills" / "custom" / "jeeves-borrowing-base" / "report_scheduler_cron.py"


def _load():
    spec = importlib.util.spec_from_file_location("cm_morning_brief_under_test", BRIEF)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cmb():
    return _load()


def test_slack_tool_path_exists(cmb):
    """SLACK_TOOL must resolve to the real slack_tool.py (the send path)."""
    assert cmb.SLACK_TOOL.exists(), f"slack_tool not found at {cmb.SLACK_TOOL}"


def test_caller_script_path_exists():
    """report_scheduler_cron must point run_morning_brief at the real script.

    Mirrors the (fixed) path computation: parents[3]/backend/scripts.
    """
    resolved = CRON.resolve().parents[3] / "backend" / "scripts" / "cm_morning_brief.py"
    assert resolved.exists(), f"caller resolves to nonexistent {resolved}"


def test_build_brief_uses_cico_source_date(cmb, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    cico = tmp_path / "cico.json"
    state.write_text(json.dumps({"us_bridge": {}, "mx_sofom": {}}), encoding="utf-8")
    cico.write_text(json.dumps({"source_date": "2026-06-15", "total_cash_usd": 1234567}), encoding="utf-8")
    monkeypatch.setattr(cmb, "STATE_FILE", state)
    monkeypatch.setattr(cmb, "CICO_FILE", cico)

    out = cmb.build_brief()

    assert "2026-06-15" in out       # dynamic date from state
    assert "Jun 8" not in out        # no hardcoded date


def test_build_brief_handles_missing_cico_date(cmb, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    cico = tmp_path / "cico.json"
    state.write_text(json.dumps({"us_bridge": {}, "mx_sofom": {}}), encoding="utf-8")
    cico.write_text(json.dumps({"total_cash_usd": 1}), encoding="utf-8")  # no source_date
    monkeypatch.setattr(cmb, "STATE_FILE", state)
    monkeypatch.setattr(cmb, "CICO_FILE", cico)

    out = cmb.build_brief()

    assert "_(n/a)_" in out  # graceful fallback, not a crash or "Jun 8"

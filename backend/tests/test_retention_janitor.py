"""Tests for the retention janitor (P2-7)."""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "retention_janitor_cron.py"


@pytest.fixture()
def janitor(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("retention_janitor_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DEER", tmp_path)
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "_retention_janitor_state.json")
    return mod


def _age(path: Path, days: int) -> None:
    import os
    ts = (datetime.now() - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


def test_prune_intel_by_filename_date(janitor, tmp_path):
    k = tmp_path / "knowledge"
    k.mkdir()
    old_date = (datetime.now() - timedelta(days=90)).strftime("intel_%Y-%m-%d.md")
    new_date = (datetime.now() - timedelta(days=5)).strftime("intel_%Y-%m-%d.md")
    (k / old_date).write_text("old", encoding="utf-8")
    (k / new_date).write_text("new", encoding="utf-8")

    count, _ = janitor.prune_intel(datetime.now())

    assert count == 1
    assert not (k / old_date).exists()
    assert (k / new_date).exists()


def test_scratch_prune_only_touches_workspace(janitor, tmp_path):
    thread = tmp_path / "threads" / "abc" / "user-data"
    ws = thread / "workspace" / "sub"
    outs = thread / "outputs"
    ws.mkdir(parents=True)
    outs.mkdir(parents=True)
    stale_ws = ws / "temp.csv"
    fresh_ws = thread / "workspace" / "fresh.csv"
    stale_out = outs / "deliverable.xlsx"
    for f in (stale_ws, fresh_ws, stale_out):
        f.write_text("x", encoding="utf-8")
    _age(stale_ws, 45)
    _age(stale_out, 45)  # outputs are deliverables — must survive

    count, _ = janitor.prune_workspace_scratch(datetime.now())

    assert count == 1
    assert not stale_ws.exists()
    assert fresh_ws.exists()
    assert stale_out.exists()
    assert not stale_ws.parent.exists()          # emptied subdir swept
    assert (thread / "workspace").exists()       # workspace root kept


def test_audit_rotation_thresholds(janitor, tmp_path, monkeypatch):
    audit = tmp_path / "dispatch_audit.jsonl"
    audit.write_text("small\n", encoding="utf-8")
    assert janitor.rotate_dispatch_audit() is False  # under the cap

    monkeypatch.setattr(janitor, "AUDIT_ROTATE_MB", 0)
    assert janitor.rotate_dispatch_audit() is True
    assert not audit.exists()
    assert audit.with_suffix(".jsonl.1").exists()


def test_vacuum_reclaims_space(janitor, tmp_path):
    db = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x BLOB)")
    conn.executemany("INSERT INTO t VALUES (?)", [(b"0" * 10_000,)] * 200)
    conn.commit()
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()
    before = db.stat().st_size

    assert janitor.vacuum_checkpoints() is True
    assert db.stat().st_size < before


def test_run_once_stamps_state_and_vacuums_once_per_month(janitor, tmp_path):
    (tmp_path / "checkpoints.db").write_bytes(b"")  # empty file, vacuum trivially ok
    summary = janitor.run_once()
    assert summary.get("vacuumed") is True

    state = janitor.load_json_state(janitor.STATE_PATH, {})
    assert state["last_vacuum_month"] == datetime.now().strftime("%Y-%m")
    assert janitor._ran_today(state, datetime.now()) is True

    # Second run same month: no re-vacuum
    summary2 = janitor.run_once()
    assert "vacuumed" not in summary2

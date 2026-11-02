"""Tests for P1-4 — correct-state-on-failure across crons (GW-F3/F8/F9/F10).

Covers:
  - GW-F3: dreams/eod/weekly/latent must NOT stamp their "ran" marker when
    dispatch is capacity-rejected or raises — stamping made one blip a silent
    daily/weekly miss.
  - GW-F10: cron_schedule helpers — `hour >= target` daily windows and weekly
    catch-up when downtime spans the exact scheduled hour.
  - GW-F8: dispatch queue drains persisted items at boot, reports stale drops,
    and a dispatch exception during drain requeues instead of losing the item.
  - GW-F9: the SOFOM direct-call path finally-stamps its state so an exception
    can't leave `{today}_running` stuck until the next restart.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BACKEND_DIR.parent / "skills" / "custom"
SHARED_DIR = SKILLS_DIR / "_shared"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_dispatch_module(result=True, exc=None, calls=None):
    """A stand-in for skills/custom/_shared/autonomous_dispatch.py."""
    fake = types.ModuleType("autonomous_dispatch")

    def dispatch(prompt, *, notification, category="general", source_id=None, source_metadata=None):
        if calls is not None:
            calls.append({"prompt": prompt, "category": category})
        if exc is not None:
            raise exc
        return result

    fake.dispatch = dispatch
    fake.active_run_count = lambda: 0
    fake.MAX_CONCURRENT_RUNS = 2
    return fake


# ---------------------------------------------------------------------------
# GW-F10 — cron_schedule helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cs():
    return _load("cron_schedule_under_test", SHARED_DIR / "cron_schedule.py")


def test_last_daily_due_today_when_hour_passed(cs):
    now = datetime(2026, 7, 1, 19, 30)
    assert cs.last_daily_due(now, 17) == datetime(2026, 7, 1, 17, 0)


def test_last_daily_due_yesterday_when_hour_not_reached(cs):
    now = datetime(2026, 7, 1, 9, 0)
    assert cs.last_daily_due(now, 17) == datetime(2026, 6, 30, 17, 0)


def test_last_weekly_due_same_day_after_hour(cs):
    # 2026-06-29 is a Monday
    now = datetime(2026, 6, 29, 10, 0)
    assert cs.last_weekly_due(now, 0, 8) == datetime(2026, 6, 29, 8, 0)


def test_last_weekly_due_same_day_before_hour_wraps_a_week(cs):
    now = datetime(2026, 6, 29, 7, 0)  # Monday 07:00, due 08:00
    assert cs.last_weekly_due(now, 0, 8) == datetime(2026, 6, 22, 8, 0)


def test_last_weekly_due_midweek(cs):
    now = datetime(2026, 7, 1, 12, 0)  # Wednesday
    assert cs.last_weekly_due(now, 0, 8) == datetime(2026, 6, 29, 8, 0)


def test_weekly_run_due_catches_up_after_downtime(cs):
    """The GW-F10 scenario: gateway down over Monday 08:00, boots Tuesday."""
    now = datetime(2026, 6, 30, 9, 0)  # Tuesday
    last_run = datetime(2026, 6, 22, 8, 5).isoformat()  # previous Monday's run
    assert cs.weekly_run_due(last_run, now, 0, 8) is True


def test_weekly_run_due_false_after_this_weeks_run(cs):
    now = datetime(2026, 6, 30, 9, 0)  # Tuesday
    last_run = datetime(2026, 6, 29, 8, 5).isoformat()  # ran Monday
    assert cs.weekly_run_due(last_run, now, 0, 8) is False


def test_weekly_run_due_no_history_fires_only_on_scheduled_day(cs):
    monday_after = datetime(2026, 6, 29, 9, 0)
    monday_before = datetime(2026, 6, 29, 7, 0)
    tuesday = datetime(2026, 6, 30, 9, 0)
    assert cs.weekly_run_due(None, monday_after, 0, 8) is True
    assert cs.weekly_run_due(None, monday_before, 0, 8) is False
    assert cs.weekly_run_due(None, tuesday, 0, 8) is False


def test_weekly_run_due_corrupt_timestamp_falls_back(cs):
    monday_after = datetime(2026, 6, 29, 9, 0)
    tuesday = datetime(2026, 6, 30, 9, 0)
    assert cs.weekly_run_due("not-a-date", monday_after, 0, 8) is True
    assert cs.weekly_run_due("not-a-date", tuesday, 0, 8) is False


# ---------------------------------------------------------------------------
# GW-F3 — dreams: stamp last_dream only on successful dispatch
# ---------------------------------------------------------------------------

@pytest.fixture()
def dreams(tmp_path, monkeypatch):
    mod = _load("dreams_cron_under_test", SKILLS_DIR / "gmail" / "dreams_cron.py")
    state_file = tmp_path / "_dreams_state.json"
    monkeypatch.setattr(mod, "_state_path", lambda: state_file)
    # Neutralize the heavy inputs run_dream gathers before dispatching.
    monkeypatch.setattr(mod, "_read_recent_audit", lambda **k: [])
    monkeypatch.setattr(mod, "_fetch_recent_transcripts", lambda **k: [])
    monkeypatch.setattr(mod, "_build_day_review", lambda: "")
    monkeypatch.setattr(mod, "_build_dream_prompt", lambda *a, **k: "prompt")
    return mod, state_file


def test_dreams_save_state_does_not_auto_stamp(dreams):
    mod, state_file = dreams
    mod.save_state({"dream_count": 3})
    saved = json.loads(state_file.read_text())
    assert "last_dream" not in saved


def test_dreams_rejection_leaves_last_dream_unstamped(dreams, monkeypatch):
    mod, state_file = dreams
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=False))
    mod.run_dream()
    assert not state_file.exists() or "last_dream" not in json.loads(state_file.read_text())
    assert mod._dreamed_today_pst(mod.load_state()) is False


def test_dreams_success_stamps_last_dream_and_count(dreams, monkeypatch):
    mod, state_file = dreams
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=True))
    mod.run_dream()
    saved = json.loads(state_file.read_text())
    assert saved["dream_count"] == 1
    assert saved["last_dream"]
    assert mod._dreamed_today_pst(mod.load_state()) is True


def test_dreams_uses_plain_dispatch_not_queue():
    """enqueue_or_dispatch + the window-loop retry would double-dispatch."""
    src = (SKILLS_DIR / "gmail" / "dreams_cron.py").read_text(encoding="utf-8")
    assert "from dispatch_queue import enqueue_or_dispatch" not in src


# ---------------------------------------------------------------------------
# GW-F3/F10 — eod / weekly / latent
# ---------------------------------------------------------------------------

@pytest.fixture()
def eod(tmp_path, monkeypatch):
    mod = _load("eod_review_cron_under_test", SKILLS_DIR / "gmail" / "eod_review_cron.py")
    state_file = tmp_path / "_eod_state.json"
    monkeypatch.setattr(mod, "_state_path", lambda: state_file)
    monkeypatch.setattr(mod, "_run_proposal_learner", lambda: None)
    monkeypatch.setattr(mod, "_build_eod_prompt", lambda n: "prompt")
    return mod, state_file


def test_eod_rejection_leaves_state_unstamped(eod, monkeypatch):
    mod, state_file = eod
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=False))
    mod.run_eod_review()
    assert not state_file.exists()
    assert mod._already_ran_today(mod.load_state()) is False


def test_eod_dispatch_exception_leaves_state_unstamped(eod, monkeypatch):
    mod, state_file = eod
    monkeypatch.setitem(
        sys.modules, "autonomous_dispatch", _fake_dispatch_module(exc=RuntimeError("boom"))
    )
    mod.run_eod_review()
    assert not state_file.exists()


def test_eod_success_stamps_last_eod(eod, monkeypatch):
    mod, state_file = eod
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=True))
    mod.run_eod_review()
    saved = json.loads(state_file.read_text())
    assert saved["review_count"] == 1
    assert saved["last_eod"]
    assert mod._already_ran_today(mod.load_state()) is True


def test_eod_loop_uses_gte_hour_window():
    """== EOD_HOUR gave a single one-hour window; downtime across it lost the day."""
    src = (SKILLS_DIR / "gmail" / "eod_review_cron.py").read_text(encoding="utf-8")
    assert "now.hour >= EOD_HOUR" in src
    assert "now.hour == EOD_HOUR" not in src
    assert "from dispatch_queue import enqueue_or_dispatch" not in src


@pytest.fixture()
def weekly(tmp_path, monkeypatch):
    mod = _load(
        "weekly_open_items_cron_under_test", SKILLS_DIR / "gmail" / "weekly_open_items_cron.py"
    )
    state_file = tmp_path / "_weekly_state.json"
    monkeypatch.setattr(mod, "_state_path", lambda: state_file)
    monkeypatch.setattr(mod, "_build_prompt", lambda n: "prompt")
    return mod, state_file


def test_weekly_rejection_leaves_state_unstamped(weekly, monkeypatch):
    mod, state_file = weekly
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=False))
    mod.run_review()
    assert not state_file.exists()


def test_weekly_success_stamps_last_run(weekly, monkeypatch):
    mod, state_file = weekly
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=True))
    mod.run_review()
    saved = json.loads(state_file.read_text())
    assert saved["review_count"] == 1
    assert saved["last_run"]


def test_weekly_loop_uses_catchup_scheduling():
    src = (SKILLS_DIR / "gmail" / "weekly_open_items_cron.py").read_text(encoding="utf-8")
    assert "weekly_run_due" in src
    assert "now.hour == REVIEW_HOUR" not in src
    assert "from dispatch_queue import enqueue_or_dispatch" not in src


@pytest.fixture()
def latent(tmp_path, monkeypatch):
    mod = _load(
        "latent_learning_cron_under_test",
        SKILLS_DIR / "latent-learning" / "latent_learning_cron.py",
    )
    state_file = tmp_path / "_latent_state.json"
    monkeypatch.setattr(mod, "_state_path", lambda: state_file)
    monkeypatch.setattr(mod, "_build_prompt", lambda n: "prompt")
    return mod, state_file


def test_latent_rejection_leaves_state_unstamped(latent, monkeypatch):
    mod, state_file = latent
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=False))
    mod.run_review()
    assert not state_file.exists()


def test_latent_success_stamps_last_run(latent, monkeypatch):
    mod, state_file = latent
    monkeypatch.setitem(sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=True))
    mod.run_review()
    saved = json.loads(state_file.read_text())
    assert saved["run_count"] == 1
    assert saved["last_run"]


def test_latent_loop_uses_catchup_scheduling():
    src = (SKILLS_DIR / "latent-learning" / "latent_learning_cron.py").read_text(encoding="utf-8")
    assert "weekly_run_due" in src
    assert "now.hour == LEARN_HOUR" not in src
    assert "from dispatch_queue import enqueue_or_dispatch" not in src


# ---------------------------------------------------------------------------
# GW-F8 — dispatch queue boot drain + stale-drop reporting + requeue-on-error
# ---------------------------------------------------------------------------

def _fresh_item(category="test"):
    from datetime import timezone

    return {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "prompt": "p",
        "notification": "n",
        "category": category,
        "source_id": None,
        "source_metadata": {},
    }


def _stale_item(category="old"):
    from datetime import timedelta, timezone

    return {
        "queued_at": (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(),
        "prompt": "p",
        "notification": "n",
        "category": category,
        "source_id": None,
        "source_metadata": {},
    }


@pytest.fixture()
def dq(tmp_path, monkeypatch):
    mod = _load("dispatch_queue_under_test", SHARED_DIR / "dispatch_queue.py")
    monkeypatch.setattr(mod, "QUEUE_PATH", tmp_path / "dispatch_queue.jsonl")
    # Keep the (daemon) drain thread from actually spinning up in tests.
    monkeypatch.setattr(mod, "_ensure_drain_thread", lambda: None)
    return mod


def _write_items(mod, items):
    mod._write_queue(items)


def test_boot_drain_starts_thread_when_items_pending(dq, monkeypatch):
    started = []
    monkeypatch.setattr(dq, "_ensure_drain_thread", lambda: started.append(True))
    _write_items(dq, [_fresh_item()])
    dq.ensure_drain_on_boot()
    assert started == [True]


def test_boot_drain_noop_when_queue_empty(dq, monkeypatch):
    started = []
    monkeypatch.setattr(dq, "_ensure_drain_thread", lambda: started.append(True))
    dq.ensure_drain_on_boot()
    assert started == []


def test_boot_drain_drops_and_reports_stale(dq, monkeypatch):
    reported = []
    monkeypatch.setattr(dq, "_report_stale_drops", lambda stale, where: reported.extend(stale))
    _write_items(dq, [_stale_item(), _fresh_item()])
    dq.ensure_drain_on_boot()
    remaining = dq._read_queue()
    assert len(remaining) == 1
    assert remaining[0]["category"] == "test"
    assert len(reported) == 1
    assert reported[0]["category"] == "old"


def test_drain_once_requeues_on_dispatch_exception(dq, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "autonomous_dispatch", _fake_dispatch_module(exc=RuntimeError("LG down"))
    )
    _write_items(dq, [_fresh_item()])
    assert dq._drain_once() == 0
    assert len(dq._read_queue()) == 1  # not lost


def test_drain_once_success_removes_item(dq, monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules, "autonomous_dispatch", _fake_dispatch_module(result=True, calls=calls)
    )
    _write_items(dq, [_fresh_item()])
    assert dq._drain_once() == 1
    assert dq._read_queue() == []
    assert len(calls) == 1


def test_gateway_boot_wires_ensure_drain():
    src = (BACKEND_DIR / "app" / "gateway" / "cron_supervisor.py").read_text(encoding="utf-8")
    assert "ensure_drain_on_boot" in src


# ---------------------------------------------------------------------------
# GW-F9 — SOFOM direct path finally-stamps its state
# ---------------------------------------------------------------------------

def test_sofom_direct_path_stamps_in_finally():
    src = (SKILLS_DIR / "jeeves-borrowing-base" / "report_scheduler_cron.py").read_text(
        encoding="utf-8"
    )
    idx = src.index("run_sofom_distribution(eml['message_id']")
    window = src[idx - 300 : idx + 400]
    assert "try:" in window and "finally:" in window

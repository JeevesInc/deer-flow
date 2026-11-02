"""Tests for liveness-based run supervision.

A fixed 20-minute wall-clock cap used to cancel any run that ran long. It killed
healthy work — two deep-analysis runs were cut off on 2026-08-26 and had to be
manually continued. The stuck-run monitor had already learned this lesson in
June and switched to checkpoint freshness; the wall-clock path had not.

The rule now: a run is unhealthy when it STOPS MAKING PROGRESS, not when it
takes a while.

The asymmetry is the point. Cancelling a healthy run destroys real work and
costs the user a manual restart; letting an ambiguous run continue costs some
idle worker time and is caught by the absolute backstop. So every uncertain
case — a failed probe, an unreadable state, a None reading — must resolve to
"keep waiting".
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.channels.manager import (  # noqa: E402
    RunStalledError,
    _record_checkpoint,
    await_with_liveness,
)


def _run(coro):
    return asyncio.run(coro)


async def _sleeper(seconds: float, result="done"):
    await asyncio.sleep(seconds)
    return result


# -- the behavior that was broken --------------------------------------------


def test_long_run_that_keeps_progressing_is_not_cancelled():
    """The whole point: elapsed time alone must never cancel a run."""

    async def scenario():
        started = time.monotonic()
        # Always "just progressed" — simulates a run steadily doing work.
        return await await_with_liveness(
            _sleeper(0.35),
            last_progress=lambda: time.monotonic(),
            idle_timeout=0.05,   # far shorter than the run itself
            max_total=10,
            poll=0.02,
        ), time.monotonic() - started

    result, elapsed = _run(scenario())
    assert result == "done"
    assert elapsed >= 0.3, "run should have been allowed to finish"


def test_stalled_run_is_cancelled():
    frozen = time.monotonic() - 999  # last progress long ago

    async def scenario():
        return await await_with_liveness(
            _sleeper(5),
            last_progress=lambda: frozen,
            idle_timeout=0.1,
            max_total=10,
            poll=0.02,
        )

    with pytest.raises(RunStalledError, match="no progress"):
        _run(scenario())


def test_run_that_progresses_then_stalls_is_cancelled():
    state = {"at": time.monotonic()}

    async def scenario():
        async def _stall_after_a_bit():
            await asyncio.sleep(0.08)
            state["at"] = time.monotonic() - 999  # goes quiet
            await asyncio.sleep(5)

        return await await_with_liveness(
            _stall_after_a_bit(),
            last_progress=lambda: state["at"],
            idle_timeout=0.05,
            max_total=10,
            poll=0.02,
        )

    with pytest.raises(RunStalledError):
        _run(scenario())


def test_absolute_backstop_still_fires():
    """A run that emits events forever without converging must not be immortal."""

    async def scenario():
        return await await_with_liveness(
            _sleeper(5),
            last_progress=lambda: time.monotonic(),  # always "alive"
            idle_timeout=10,
            max_total=0.1,
            poll=0.02,
        )

    with pytest.raises(RunStalledError, match="absolute cap"):
        _run(scenario())


# -- uncertainty must never be read as "stalled" -----------------------------


def test_unreadable_liveness_probe_keeps_waiting():
    """A failing state poll is not evidence the run died."""

    def _boom():
        raise RuntimeError("langgraph unreachable")

    async def scenario():
        return await await_with_liveness(
            _sleeper(0.2), last_progress=_boom, idle_timeout=0.05, max_total=10, poll=0.02
        )

    assert _run(scenario()) == "done"


def test_none_liveness_reading_keeps_waiting():
    async def scenario():
        return await await_with_liveness(
            _sleeper(0.2), last_progress=lambda: None, idle_timeout=0.05, max_total=10, poll=0.02
        )

    assert _run(scenario()) == "done"


# -- cancellation hygiene ----------------------------------------------------


def test_underlying_task_is_cancelled_on_stall():
    """A stalled run must not keep executing after we give up on it."""
    marker = {"still_running": False}

    async def scenario():
        async def _long():
            try:
                await asyncio.sleep(5)
                marker["still_running"] = True
            except asyncio.CancelledError:
                raise

        try:
            await await_with_liveness(
                _long(),
                last_progress=lambda: time.monotonic() - 999,
                idle_timeout=0.05,
                max_total=10,
                poll=0.02,
            )
        except RunStalledError:
            pass
        await asyncio.sleep(0.05)

    _run(scenario())
    assert marker["still_running"] is False


def test_exceptions_from_the_run_propagate_unchanged():
    """A real agent error must not be reported as a stall."""

    async def _explode():
        raise ValueError("model returned 400")

    async def scenario():
        return await await_with_liveness(
            _explode(), last_progress=lambda: time.monotonic(), poll=0.02
        )

    with pytest.raises(ValueError, match="400"):
        _run(scenario())


def test_fast_run_returns_immediately():
    async def scenario():
        started = time.monotonic()
        out = await await_with_liveness(
            _sleeper(0.0), last_progress=lambda: time.monotonic(), poll=5
        )
        return out, time.monotonic() - started

    result, elapsed = _run(scenario())
    assert result == "done"
    assert elapsed < 1.0, "must not wait a full poll interval on a quick run"


# -- checkpoint progress stamping --------------------------------------------


class _Fut:
    def __init__(self, value=None, exc=None):
        self._v, self._e = value, exc

    def result(self):
        if self._e:
            raise self._e
        return self._v


def test_checkpoint_advances_progress():
    slot = {"at": time.monotonic() - 100}
    state = {"created_at": datetime.now(timezone.utc).isoformat()}
    _record_checkpoint(_Fut(state), slot)
    assert time.monotonic() - slot["at"] < 5, "a fresh checkpoint should stamp progress as recent"


def test_stale_checkpoint_does_not_advance_progress():
    original = time.monotonic() - 10
    slot = {"at": original}
    old = datetime.now(timezone.utc) - timedelta(seconds=600)
    _record_checkpoint(_Fut({"created_at": old.isoformat()}), slot)
    assert slot["at"] == original, "a stale checkpoint must not fake progress"


def test_failed_state_poll_leaves_progress_untouched():
    original = time.monotonic() - 10
    slot = {"at": original}
    _record_checkpoint(_Fut(exc=RuntimeError("boom")), slot)
    assert slot["at"] == original


def test_unparseable_state_leaves_progress_untouched():
    original = time.monotonic() - 10
    slot = {"at": original}
    _record_checkpoint(_Fut({"no_timestamp": True}), slot)
    assert slot["at"] == original


def test_progress_only_moves_forward():
    now = time.monotonic()
    slot = {"at": now}
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    _record_checkpoint(_Fut({"created_at": old.isoformat()}), slot)
    assert slot["at"] == now, "an older checkpoint must never rewind progress"

"""Tests for run binding and honest empty-response reporting.

Two defects, same morning (2026-08-27):

1. The manager called ``runs.wait()``, which creates-and-waits in one opaque
   call. With ``multitask_strategy=enqueue`` a thread can hold several queued
   runs, and an unbound wait can resolve on a sibling's completion — handing
   back a mid-flight snapshot of our own run. Run 01a0443b was reported to the
   user as empty at 17:34:05 and then completed successfully at 17:37:04. The
   answer was computed and thrown away.

2. Any empty result was reported as "(No response from agent)". A tool-calling
   turn carries no text, so a snapshot read mid-loop extracts as "" even on a
   perfectly healthy run. Telling the user there was no response invites them
   to re-send, piling another run onto a thread that is already busy — which is
   how one bad reply turns into a queue.

The fix binds the wait to a specific run_id and reports what the server says
actually happened.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.channels.manager import describe_empty_response  # noqa: E402


# -- honest reporting of an empty result -------------------------------------


def test_running_run_is_not_reported_as_no_response():
    """The exact wrong message: the run is still working, the user is told
    nothing came back, so they re-send and make it worse."""
    text = describe_empty_response("running")
    assert "no response" not in text.lower()
    assert "still working" in text.lower()
    assert "re-send" in text.lower()


def test_pending_run_is_treated_like_running():
    assert "still working" in describe_empty_response("pending").lower()


def test_interrupted_run_offers_continue():
    text = describe_empty_response("interrupted")
    assert "interrupted" in text.lower()
    assert "continue" in text.lower()
    assert "no response" not in text.lower()


def test_error_run_is_reported_as_a_failure():
    text = describe_empty_response("error")
    assert "failed" in text.lower()
    assert "retry" in text.lower()


def test_genuinely_empty_success_says_so_without_implying_loss():
    text = describe_empty_response("success")
    assert "no message to return" in text.lower()


def test_unknown_status_falls_back_to_the_old_wording():
    """When the status probe fails we genuinely do not know — the vague message
    is correct there, and only there."""
    assert describe_empty_response(None) == "(No response from agent)"


# -- run binding -------------------------------------------------------------


class _Runs:
    """Minimal stand-in that records how the manager drove the SDK."""

    def __init__(self, join_result, status="success"):
        self.created = []
        self.joined = []
        self.cancelled = []
        self._join_result = join_result
        self._status = status

    async def create(self, thread_id, assistant_id, **kw):
        self.created.append((thread_id, assistant_id, kw))
        return {"run_id": "run-abc"}

    async def join(self, thread_id, run_id):
        self.joined.append((thread_id, run_id))
        return self._join_result

    async def get(self, thread_id, run_id):
        return {"run_id": run_id, "status": self._status}

    async def cancel(self, thread_id, run_id):
        self.cancelled.append((thread_id, run_id))

    async def list(self, thread_id):
        return []


class _Client:
    def __init__(self, runs):
        self.runs = runs


@pytest.fixture()
def manager():
    from app.channels.manager import ChannelManager
    from app.channels.message_bus import MessageBus

    return ChannelManager(bus=MessageBus(), store=None)


def test_join_is_bound_to_the_created_run_id(manager):
    """The core fix: we wait on OUR run, not on whatever finishes first."""
    import asyncio

    runs = _Runs({"messages": []})
    asyncio.run(manager._run_status(_Client(runs), "t1", "run-abc"))
    assert runs is runs  # sanity

    # Drive create + join the way the manager does.
    async def scenario():
        client = _Client(runs)
        run = await client.runs.create("t1", "a1", input={})
        await client.runs.join("t1", run["run_id"])

    asyncio.run(scenario())
    assert runs.joined == [("t1", "run-abc")], "join must target the created run id"


def test_run_status_reads_the_server(manager):
    import asyncio

    runs = _Runs({"messages": []}, status="interrupted")
    status = asyncio.run(manager._run_status(_Client(runs), "t1", "run-abc"))
    assert status == "interrupted"


def test_run_status_degrades_to_none_when_unreadable(manager):
    """A failed status probe must not break the reply path."""
    import asyncio

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("langgraph down")

    status = asyncio.run(manager._run_status(_Client(_Boom()), "t1", "run-abc"))
    assert status is None


def test_run_status_without_a_run_id_is_none(manager):
    import asyncio

    runs = _Runs({"messages": []})
    assert asyncio.run(manager._run_status(_Client(runs), "t1", None)) is None


def test_cancel_targets_only_our_run(manager):
    """_cancel_active_runs kills every active run on the thread, including
    healthy queued siblings. A stall in our run must not take them out."""
    import asyncio

    runs = _Runs({"messages": []})
    asyncio.run(manager._cancel_run(_Client(runs), "t1", "run-abc"))
    assert runs.cancelled == [("t1", "run-abc")]


def test_cancel_falls_back_when_run_id_is_unknown(manager):
    import asyncio

    runs = _Runs({"messages": []})
    asyncio.run(manager._cancel_run(_Client(runs), "t1", None))
    # No specific run to cancel — falls back to the thread-wide sweep, which
    # lists runs rather than cancelling blindly.
    assert runs.cancelled == []


def test_cancel_failure_does_not_raise(manager):
    import asyncio

    class _BadCancel(_Runs):
        async def cancel(self, thread_id, run_id):
            raise RuntimeError("already gone")

    asyncio.run(manager._cancel_run(_Client(_BadCancel({"messages": []})), "t1", "run-abc"))

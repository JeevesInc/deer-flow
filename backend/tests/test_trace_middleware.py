"""Tests for the local run-trace sink and its middleware.

Two properties matter more than the record contents:

  1. Tracing NEVER breaks a run. A sink that can raise is worse than no sink —
     it converts an observability gap into an outage. Every test that exercises
     a failure path asserts the model call still returns (or re-raises the
     model's own error, not ours).
  2. Content blocks are flattened correctly. Adaptive thinking is always on for
     the Claude 5 models, so message content is a list whose first block is a
     ThinkingBlock. Naive [0].text extraction silently recorded empty output —
     the exact bug that made the proposal synthesizer a no-op for weeks.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR / "packages" / "harness"))

from deerflow.agents.tracing import trace_sink  # noqa: E402


@pytest.fixture()
def sink(tmp_path, monkeypatch):
    """Point the sink at a temp dir so tests never touch live .deer-flow."""
    monkeypatch.setenv("DEERFLOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DEERFLOW_TRACING", "1")
    monkeypatch.setattr(trace_sink, "_step_counters", {})
    return trace_sink


def _records(sink_mod) -> list[dict]:
    files = sorted(sink_mod.trace_dir().glob("*.jsonl"))
    out = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# -- sink ---------------------------------------------------------------------


def test_write_and_read_roundtrip(sink):
    sink.write({"run_id": "t1", "step": 1, "model": "claude-opus-5"})
    recs = list(sink.read_traces(days=1))
    assert len(recs) == 1
    assert recs[0]["run_id"] == "t1"
    assert recs[0]["ts"], "writer must stamp a timestamp"


def test_disabled_writes_nothing(sink, monkeypatch):
    monkeypatch.setenv("DEERFLOW_TRACING", "0")
    sink.write({"run_id": "t1"})
    assert not list(sink.read_traces(days=1))


def test_write_never_raises_on_bad_path(sink, monkeypatch):
    monkeypatch.setattr(sink, "_today_file", lambda: (_ for _ in ()).throw(OSError("disk full")))
    sink.write({"run_id": "t1"})  # must not raise


def test_read_skips_malformed_lines(sink):
    sink.write({"run_id": "good"})
    path = sorted(sink.trace_dir().glob("*.jsonl"))[0]
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert [r["run_id"] for r in sink.read_traces(days=1)] == ["good"]


def test_steps_are_monotonic_per_run(sink):
    assert [sink.next_step("a"), sink.next_step("a"), sink.next_step("b")] == [1, 2, 1]


def test_truncate_marks_the_clip(sink):
    out = sink.truncate("x" * 100, limit=10)
    assert out.startswith("x" * 10) and "truncated" in out


def test_prune_removes_old_day_files(sink):
    d = sink.trace_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    (d / "not-a-date.jsonl").write_text("{}\n", encoding="utf-8")
    sink.write({"run_id": "today"})
    assert sink.prune(retention_days=30) == 1
    names = {p.name for p in d.glob("*.jsonl")}
    assert "2020-01-01.jsonl" not in names
    assert "not-a-date.jsonl" in names, "unparseable names must be left alone, not deleted"


def test_stats_aggregates(sink):
    sink.write({"run_id": "a", "usage": {"input_tokens": 10, "output_tokens": 5},
                "latency_ms": 100, "output": {"tool_calls": [{"name": "bash"}]}})
    sink.write({"run_id": "a", "error": "boom", "latency_ms": 50, "output": {"tool_calls": []}})
    s = sink.stats(days=1)
    assert s["model_calls"] == 2
    assert s["errors"] == 1
    assert s["tool_calls"] == 1
    assert s["input_tokens"] == 10


# -- middleware ---------------------------------------------------------------


def _request(messages=None, tools=None, thread_id="thread-1"):
    return SimpleNamespace(
        model=SimpleNamespace(model_name="claude-opus-5"),
        messages=messages if messages is not None else [],
        system_message=SimpleNamespace(content="you are an analyst"),
        tools=tools or [SimpleNamespace(name="bash"), SimpleNamespace(name="read_file")],
        runtime=SimpleNamespace(context={"thread_id": thread_id}),
        state={},
    )


def _human(text):
    return SimpleNamespace(type="human", content=text)


def _ai(content, tool_calls=None, usage=None, stop="end_turn"):
    return SimpleNamespace(
        type="ai",
        content=content,
        tool_calls=tool_calls or [],
        usage_metadata=usage,
        response_metadata={"stop_reason": stop},
    )


@pytest.fixture()
def mw(sink):
    from deerflow.agents.middlewares.trace_middleware import TraceMiddleware

    return TraceMiddleware(agent_name="lead")


def test_records_a_successful_call(mw, sink):
    request = _request(messages=[_human("what is the BB headroom?")])
    response = SimpleNamespace(
        result=[_ai("Headroom is $4.2M.", usage={"input_tokens": 900, "output_tokens": 30})]
    )
    assert mw.wrap_model_call(request, lambda r: response) is response

    (rec,) = _records(sink)
    assert rec["run_id"] == "thread-1"
    assert rec["model"] == "claude-opus-5"
    assert rec["output"]["text"] == "Headroom is $4.2M."
    assert rec["usage"]["input_tokens"] == 900
    assert rec["input"]["last_user"] == "what is the BB headroom?"
    assert rec["input"]["tools"] == ["bash", "read_file"]
    assert rec["error"] is None
    assert rec["latency_ms"] >= 0


def test_flattens_thinking_block_content(mw, sink):
    """A ThinkingBlock first element must not swallow the real text."""
    content = [
        {"type": "thinking", "thinking": "let me check the tape"},
        {"type": "text", "text": "Eligible balance is $12.1M."},
    ]
    mw.wrap_model_call(_request(), lambda r: SimpleNamespace(result=[_ai(content)]))
    (rec,) = _records(sink)
    assert rec["output"]["text"] == "Eligible balance is $12.1M."


def test_records_tool_calls(mw, sink):
    response = SimpleNamespace(
        result=[_ai("", tool_calls=[{"name": "bash", "args": {"command": "ls"}}])]
    )
    mw.wrap_model_call(_request(), lambda r: response)
    (rec,) = _records(sink)
    assert rec["output"]["tool_calls"][0]["name"] == "bash"
    assert "ls" in rec["output"]["tool_calls"][0]["args"]


def test_model_error_is_traced_and_reraised(mw, sink):
    def _boom(_r):
        raise RuntimeError("529 overloaded")

    with pytest.raises(RuntimeError, match="529 overloaded"):
        mw.wrap_model_call(_request(), _boom)

    (rec,) = _records(sink)
    assert "529 overloaded" in rec["error"]
    assert rec["output"]["stop_reason"] == "error"


def test_tracing_failure_does_not_break_the_call(mw, sink, monkeypatch):
    """The whole point: a broken sink must be invisible to the agent."""
    monkeypatch.setattr(trace_sink, "write", lambda rec: (_ for _ in ()).throw(RuntimeError("nope")))
    sentinel = SimpleNamespace(result=[_ai("fine")])
    assert mw.wrap_model_call(_request(), lambda r: sentinel) is sentinel


def test_unknown_thread_id_does_not_crash(mw, sink):
    request = _request()
    request.runtime = None
    mw.wrap_model_call(request, lambda r: SimpleNamespace(result=[_ai("ok")]))
    (rec,) = _records(sink)
    assert rec["run_id"] == "unknown"


def test_bare_message_result_is_handled(mw, sink):
    """Some langchain versions hand back a message rather than a list."""
    mw.wrap_model_call(_request(), lambda r: SimpleNamespace(result=_ai("bare")))
    (rec,) = _records(sink)
    assert rec["output"]["text"] == "bare"


# The async hook is the one production actually uses (the gateway handlers are
# async), so it needs real coverage. Driven with asyncio.run rather than
# pytest-asyncio, which is not in the dev dependency set.


def test_async_path_records(mw, sink):
    async def _handler(_r):
        return SimpleNamespace(result=[_ai("async answer")])

    asyncio.run(mw.awrap_model_call(_request(), _handler))
    (rec,) = _records(sink)
    assert rec["output"]["text"] == "async answer"


def test_async_error_is_traced_and_reraised(mw, sink):
    async def _handler(_r):
        raise ValueError("timeout")

    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(mw.awrap_model_call(_request(), _handler))
    (rec,) = _records(sink)
    assert "timeout" in rec["error"]

"""Tests for LoopDetectionMiddleware."""

from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.middlewares.loop_detection_middleware import (
    _HARD_STOP_MSG,
    LoopDetectionMiddleware,
    _hash_tool_calls,
)


def _make_runtime(thread_id="test-thread"):
    """Build a minimal Runtime mock with context."""
    runtime = MagicMock()
    runtime.context = {"thread_id": thread_id}
    return runtime


def _make_state(tool_calls=None, content=""):
    """Build a minimal AgentState dict with an AIMessage."""
    msg = AIMessage(content=content, tool_calls=tool_calls or [])
    return {"messages": [msg]}


def _bash_call(cmd="ls"):
    return {"name": "bash", "id": f"call_{cmd}", "args": {"command": cmd}}


def _step(mw, state, runtime):
    """Simulate one full graph step: after_model then before_model.

    Returns whatever ``before_model`` would emit (the warning, if any), since
    ``after_model`` only queues the warning and returns None in the warning path.
    Hard-stop short-circuits and returns from ``after_model`` directly.
    """
    after_result = mw._after(state, runtime)
    if after_result is not None:
        return after_result
    return mw._before(state, runtime)


class TestHashToolCalls:
    def test_same_calls_same_hash(self):
        a = _hash_tool_calls([_bash_call("ls")])
        b = _hash_tool_calls([_bash_call("ls")])
        assert a == b

    def test_different_calls_different_hash(self):
        a = _hash_tool_calls([_bash_call("ls")])
        b = _hash_tool_calls([_bash_call("pwd")])
        assert a != b

    def test_order_independent(self):
        a = _hash_tool_calls([_bash_call("ls"), {"name": "read_file", "args": {"path": "/tmp"}}])
        b = _hash_tool_calls([{"name": "read_file", "args": {"path": "/tmp"}}, _bash_call("ls")])
        assert a == b

    def test_empty_calls(self):
        h = _hash_tool_calls([])
        assert isinstance(h, str)
        assert len(h) > 0


class TestLoopDetection:
    def test_no_tool_calls_returns_none(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        state = {"messages": [AIMessage(content="hello")]}
        result = _step(mw, state, runtime)
        assert result is None

    def test_below_threshold_returns_none(self):
        mw = LoopDetectionMiddleware(warn_threshold=3)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # First two identical calls — no warning
        for _ in range(2):
            result = _step(mw, _make_state(tool_calls=call), runtime)
            assert result is None

    def test_warn_at_threshold(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        for _ in range(2):
            _step(mw, _make_state(tool_calls=call), runtime)

        # Third identical call triggers warning
        result = _step(mw, _make_state(tool_calls=call), runtime)
        assert result is not None
        msgs = result["messages"]
        assert len(msgs) == 1
        assert isinstance(msgs[0], HumanMessage)
        assert "LOOP DETECTED" in msgs[0].content

    def test_warn_only_injected_once(self):
        """Warning for the same hash should only be injected once per thread."""
        mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=10)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # First two — no warning
        for _ in range(2):
            _step(mw, _make_state(tool_calls=call), runtime)

        # Third — warning injected
        result = _step(mw, _make_state(tool_calls=call), runtime)
        assert result is not None
        assert "LOOP DETECTED" in result["messages"][0].content

        # Fourth — warning already injected, should return None
        result = _step(mw, _make_state(tool_calls=call), runtime)
        assert result is None

    def test_hard_stop_at_limit(self):
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=4)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        for _ in range(3):
            _step(mw, _make_state(tool_calls=call), runtime)

        # Fourth call triggers hard stop
        result = _step(mw, _make_state(tool_calls=call), runtime)
        assert result is not None
        msgs = result["messages"]
        assert len(msgs) == 1
        # Hard stop strips tool_calls
        assert isinstance(msgs[0], AIMessage)
        assert msgs[0].tool_calls == []
        assert _HARD_STOP_MSG in msgs[0].content

    def test_hard_stop_with_list_content_thinking_enabled(self):
        """Regression: with thinking on, AIMessage.content is a list of blocks.
        The old `(content or "") + str` raised TypeError exactly when the hard
        stop fired, and leftover tool_use blocks would 400 the next call.
        """
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=4)
        runtime = _make_runtime()
        call = [_bash_call("ls")]
        list_content = [
            {"type": "thinking", "thinking": "let me try ls again"},
            {"type": "text", "text": "partial progress so far"},
            {"type": "tool_use", "id": "call_ls", "name": "bash", "input": {"command": "ls"}},
        ]

        result = None
        for _ in range(4):
            result = _step(mw, _make_state(tool_calls=call, content=list_content), runtime)

        assert result is not None
        msg = result["messages"][0]
        assert isinstance(msg, AIMessage)
        assert msg.tool_calls == []
        assert isinstance(msg.content, str)          # normalized, no more list
        assert _HARD_STOP_MSG in msg.content
        assert "partial progress so far" in msg.content   # text block preserved
        assert "tool_use" not in msg.content and "let me try ls" not in msg.content

    def test_different_calls_dont_trigger(self):
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime = _make_runtime()

        # Each call is different
        for i in range(10):
            result = _step(mw, _make_state(tool_calls=[_bash_call(f"cmd_{i}")]), runtime)
            assert result is None

    def test_window_sliding(self):
        mw = LoopDetectionMiddleware(warn_threshold=3, window_size=5)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # Fill with 2 identical calls
        _step(mw, _make_state(tool_calls=call), runtime)
        _step(mw, _make_state(tool_calls=call), runtime)

        # Push them out of the window with different calls
        for i in range(5):
            _step(mw, _make_state(tool_calls=[_bash_call(f"other_{i}")]), runtime)

        # Now the original call should be fresh again — no warning
        result = _step(mw, _make_state(tool_calls=call), runtime)
        assert result is None

    def test_reset_clears_state(self):
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        _step(mw, _make_state(tool_calls=call), runtime)
        _step(mw, _make_state(tool_calls=call), runtime)

        # Would trigger warning, but reset first
        mw.reset()
        result = _step(mw, _make_state(tool_calls=call), runtime)
        assert result is None

    def test_non_ai_message_ignored(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        state = {"messages": [SystemMessage(content="hello")]}
        result = _step(mw, state, runtime)
        assert result is None

    def test_empty_messages_ignored(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        result = _step(mw, {"messages": []}, runtime)
        assert result is None

    def test_thread_id_from_runtime_context(self):
        """Thread ID should come from runtime.context, not state."""
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime_a = _make_runtime("thread-A")
        runtime_b = _make_runtime("thread-B")
        call = [_bash_call("ls")]

        # One call on thread A
        _step(mw, _make_state(tool_calls=call), runtime_a)
        # One call on thread B
        _step(mw, _make_state(tool_calls=call), runtime_b)

        # Second call on thread A — triggers warning (2 >= warn_threshold)
        result = _step(mw, _make_state(tool_calls=call), runtime_a)
        assert result is not None
        assert "LOOP DETECTED" in result["messages"][0].content

        # Second call on thread B — also triggers (independent tracking)
        result = _step(mw, _make_state(tool_calls=call), runtime_b)
        assert result is not None
        assert "LOOP DETECTED" in result["messages"][0].content

    def test_lru_eviction(self):
        """Old threads should be evicted when max_tracked_threads is exceeded."""
        mw = LoopDetectionMiddleware(warn_threshold=2, max_tracked_threads=3)
        call = [_bash_call("ls")]

        # Fill up 3 threads
        for i in range(3):
            runtime = _make_runtime(f"thread-{i}")
            _step(mw, _make_state(tool_calls=call), runtime)

        # Add a 4th thread — should evict thread-0
        runtime_new = _make_runtime("thread-new")
        _step(mw, _make_state(tool_calls=call), runtime_new)

        assert "thread-0" not in mw._history
        assert "thread-new" in mw._history
        assert len(mw._history) == 3

    def test_thread_safe_mutations(self):
        """Verify lock is used for mutations (basic structural test)."""
        mw = LoopDetectionMiddleware()
        # The middleware should have a lock attribute
        assert hasattr(mw, "_lock")
        assert isinstance(mw._lock, type(mw._lock))

    def test_fallback_thread_id_when_missing(self):
        """When runtime context has no thread_id, should use 'default'."""
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime = MagicMock()
        runtime.context = {}
        call = [_bash_call("ls")]

        _step(mw, _make_state(tool_calls=call), runtime)
        assert "default" in mw._history


class TestWarningOrdering:
    """Regression: warning HumanMessage must NOT split a tool_use/tool_result pair.

    Anthropic's API requires every tool_use block to be immediately followed
    by its matching tool_result. The pre-fix behavior emitted the warning
    from ``after_model``, producing the sequence
    ``[AI tool_use] [Human warning] [Tool result]`` which the API rejected
    with HTTP 400 on the next call.

    The fix defers the warning to ``before_model``, so it lands AFTER the
    tool_result of the offending call: ``[AI tool_use] [Tool result] [Human warning]``.
    """

    def test_after_model_does_not_emit_warning_immediately(self):
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=10)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        # First call: tracked, no warning yet
        assert mw._after(_make_state(tool_calls=call), runtime) is None
        # Second call hits warn_threshold — but after_model still returns None;
        # warning is queued for the next before_model.
        assert mw._after(_make_state(tool_calls=call), runtime) is None
        # Pending slot should now hold the warning
        assert "test-thread" in mw._pending_warning

    def test_warning_emitted_on_next_before_model(self):
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=10)
        runtime = _make_runtime()
        call = [_bash_call("ls")]

        mw._after(_make_state(tool_calls=call), runtime)
        mw._after(_make_state(tool_calls=call), runtime)

        result = mw._before({"messages": []}, runtime)
        assert result is not None
        msgs = result["messages"]
        assert len(msgs) == 1
        assert isinstance(msgs[0], HumanMessage)
        assert "LOOP DETECTED" in msgs[0].content
        # Slot drained
        assert "test-thread" not in mw._pending_warning

    def test_before_model_no_pending_returns_none(self):
        mw = LoopDetectionMiddleware()
        runtime = _make_runtime()
        assert mw._before({"messages": []}, runtime) is None

    def test_warning_lands_after_tool_result_in_simulated_graph(self):
        """End-to-end: simulate the agent loop and assert message ordering."""
        mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=10)
        runtime = _make_runtime()
        call_args = {"command": "ls"}
        ai_msg_1 = AIMessage(content="", tool_calls=[{"name": "bash", "id": "tc_1", "args": call_args}])
        tool_msg_1 = ToolMessage(content="output", tool_call_id="tc_1")
        ai_msg_2 = AIMessage(content="", tool_calls=[{"name": "bash", "id": "tc_2", "args": call_args}])
        tool_msg_2 = ToolMessage(content="output", tool_call_id="tc_2")

        # Turn 1: model emits ai_msg_1; after_model runs (just tracks).
        history = [ai_msg_1]
        after = mw._after({"messages": list(history)}, runtime)
        assert after is None
        # Tool node runs and appends tool_msg_1.
        history.append(tool_msg_1)

        # Turn 2: before_model runs. No pending warning yet (only 1 hit so far).
        before = mw._before({"messages": list(history)}, runtime)
        assert before is None
        # Model emits ai_msg_2 (second identical call).
        history.append(ai_msg_2)
        after = mw._after({"messages": list(history)}, runtime)
        # Warning queued, NOT emitted between ai_msg_2 and the upcoming tool result.
        assert after is None
        # Tool node runs and appends tool_msg_2.
        history.append(tool_msg_2)

        # Turn 3: before_model drains the pending warning.
        before = mw._before({"messages": list(history)}, runtime)
        assert before is not None
        history.extend(before["messages"])

        # Final ordering: every AI tool_use must be IMMEDIATELY followed by its
        # tool_result (Anthropic adjacency rule).
        for i, msg in enumerate(history):
            if isinstance(msg, AIMessage) and msg.tool_calls:
                next_msg = history[i + 1]
                assert isinstance(next_msg, ToolMessage), (
                    f"AIMessage with tool_calls at index {i} not followed by ToolMessage; "
                    f"got {type(next_msg).__name__}. Full sequence: "
                    f"{[type(m).__name__ for m in history]}"
                )
                assert next_msg.tool_call_id == msg.tool_calls[0]["id"]

        # And the warning is present, after both tool results.
        assert isinstance(history[-1], HumanMessage)
        assert "LOOP DETECTED" in history[-1].content

    def test_reset_clears_pending_warning(self):
        mw = LoopDetectionMiddleware(warn_threshold=2)
        runtime = _make_runtime("thread-X")
        call = [_bash_call("ls")]

        mw._after(_make_state(tool_calls=call), runtime)
        mw._after(_make_state(tool_calls=call), runtime)
        assert "thread-X" in mw._pending_warning

        mw.reset("thread-X")
        assert "thread-X" not in mw._pending_warning
        # before_model now finds nothing to drain
        assert mw._before({"messages": []}, runtime) is None

    def test_lru_eviction_drops_pending_warning(self):
        mw = LoopDetectionMiddleware(warn_threshold=2, max_tracked_threads=2)
        call = [_bash_call("ls")]

        # Queue a warning on thread-0
        rt0 = _make_runtime("thread-0")
        mw._after(_make_state(tool_calls=call), rt0)
        mw._after(_make_state(tool_calls=call), rt0)
        assert "thread-0" in mw._pending_warning

        # Touch two more threads to evict thread-0
        for tid in ("thread-1", "thread-2"):
            mw._after(_make_state(tool_calls=call), _make_runtime(tid))

        assert "thread-0" not in mw._history
        assert "thread-0" not in mw._pending_warning

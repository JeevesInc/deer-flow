"""Tests for StepBudgetMiddleware (P1-6 / Core-H1 — previously had no tests).

Focus: the wrap-up path must not crash on list-content messages (thinking
enabled), and must only fire when the budget is exhausted and tools were called.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.step_budget_middleware import (
    _WRAP_UP_MSG,
    StepBudgetMiddleware,
)


def _runtime():
    r = MagicMock()
    r.context = {}
    return r


def _state(content, tool_calls):
    return {"messages": [AIMessage(content=content, tool_calls=tool_calls)]}


def _bash_call():
    return [{"name": "bash", "id": "c1", "args": {"command": "ls"}}]


def test_wrap_up_with_list_content_thinking_enabled(monkeypatch):
    """Regression: list content + `list + str` raised TypeError exactly when
    the budget wrap-up fired."""
    mw = StepBudgetMiddleware()
    monkeypatch.setattr(mw, "_get_budget", lambda: 1)  # exhaust on first call
    list_content = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": "work completed so far"},
        {"type": "tool_use", "id": "c1", "name": "bash", "input": {"command": "ls"}},
    ]
    result = mw._after(_state(list_content, _bash_call()), _runtime())

    assert result is not None
    msg = result["messages"][0]
    assert msg.tool_calls == []
    assert isinstance(msg.content, str)
    assert _WRAP_UP_MSG in msg.content
    assert "work completed so far" in msg.content
    assert "internal reasoning" not in msg.content  # thinking dropped
    assert "tool_use" not in msg.content


def test_wrap_up_with_string_content(monkeypatch):
    mw = StepBudgetMiddleware()
    monkeypatch.setattr(mw, "_get_budget", lambda: 1)
    result = mw._after(_state("partial answer", _bash_call()), _runtime())
    msg = result["messages"][0]
    assert msg.content.startswith("partial answer")
    assert _WRAP_UP_MSG in msg.content


def test_no_wrap_up_when_no_tool_calls(monkeypatch):
    mw = StepBudgetMiddleware()
    monkeypatch.setattr(mw, "_get_budget", lambda: 1)  # exhausted...
    result = mw._after(_state("text only", []), _runtime())  # ...but nothing to strip
    assert result is None


def test_under_budget_returns_none(monkeypatch):
    mw = StepBudgetMiddleware()
    monkeypatch.setattr(mw, "_get_budget", lambda: 100)
    result = mw._after(_state("x", _bash_call()), _runtime())
    assert result is None

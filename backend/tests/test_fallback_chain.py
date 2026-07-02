"""Tests for FallbackChatModel step-down semantics (P2-9 / Core MED).

The chain used to catch (Exception,): a 400 bad request (OUR bug — e.g. an
invalid thinking config) was silently replayed at the fallback tier, masking
the bug at Opus prices. Now only transient failures (429, >=500 incl. 529,
connection/timeout) and model-404s step down; 400/401/403/422 surface.
"""
from __future__ import annotations

from typing import Any

import anthropic
import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deerflow.models.fallback import FallbackChatModel, _default_fallback_exceptions


def _api_error(cls, status: int, message: str = "boom"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req)
    return cls(message, response=resp, body=None)


class FakeModel(BaseChatModel):
    """Minimal chat model that raises `exc` or answers with `reply`."""

    name_tag: str = "fake"
    exc: Any = None
    reply: str = "ok"
    seen_messages: list = []

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        # record what this model was actually asked (list identity per instance)
        self.seen_messages = list(messages)
        if self.exc is not None:
            raise self.exc
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _chain(primary_exc=None, fallback_reply="from-fallback"):
    primary = FakeModel(name_tag="primary", exc=primary_exc)
    fallback = FakeModel(name_tag="fallback", reply=fallback_reply)
    return FallbackChatModel(primary=primary, fallbacks=[fallback]), primary, fallback


# ---------------------------------------------------------------------------
# Classification: what steps down vs what surfaces
# ---------------------------------------------------------------------------

def test_default_exceptions_are_narrow():
    excs = _default_fallback_exceptions()
    assert anthropic.NotFoundError in excs
    assert anthropic.RateLimitError in excs
    assert anthropic.InternalServerError in excs
    assert anthropic.APIConnectionError in excs
    assert Exception not in excs
    assert anthropic.BadRequestError not in excs


@pytest.mark.parametrize("cls,status", [
    (anthropic.NotFoundError, 404),        # decommissioned model id
    (anthropic.RateLimitError, 429),
    (anthropic.InternalServerError, 500),
    (anthropic.InternalServerError, 529),  # overloaded
])
def test_transient_and_404_step_down(cls, status):
    fb, _, _ = _chain(primary_exc=_api_error(cls, status))
    out = fb.invoke([HumanMessage(content="hi")])
    assert out.content == "from-fallback"


def test_connection_error_steps_down():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=req)
    fb, _, _ = _chain(primary_exc=exc)
    out = fb.invoke([HumanMessage(content="hi")])
    assert out.content == "from-fallback"


@pytest.mark.parametrize("cls,status", [
    (anthropic.BadRequestError, 400),          # our bug — don't mask it
    (anthropic.AuthenticationError, 401),      # same key down-chain
    (anthropic.PermissionDeniedError, 403),
    (anthropic.UnprocessableEntityError, 422),
])
def test_caller_and_auth_errors_surface(cls, status):
    fb, _, fallback = _chain(primary_exc=_api_error(cls, status))
    with pytest.raises(cls):
        fb.invoke([HumanMessage(content="hi")])
    assert fallback.seen_messages == []  # fallback never consulted


def test_non_api_exception_surfaces():
    fb, _, fallback = _chain(primary_exc=ValueError("logic bug in our code"))
    with pytest.raises(ValueError):
        fb.invoke([HumanMessage(content="hi")])
    assert fallback.seen_messages == []


def test_exhausted_chain_reraises_last_transient():
    primary = FakeModel(exc=_api_error(anthropic.InternalServerError, 529))
    fallback = FakeModel(exc=_api_error(anthropic.InternalServerError, 529))
    fb = FallbackChatModel(primary=primary, fallbacks=[fallback])
    with pytest.raises(anthropic.InternalServerError):
        fb.invoke([HumanMessage(content="hi")])


# ---------------------------------------------------------------------------
# Mid-tool-loop step-down with thinking blocks (P2-9 explicit ask)
# ---------------------------------------------------------------------------

def test_mid_tool_loop_stepdown_passes_thinking_history_unchanged():
    """A 529 mid tool-loop: the fallback must receive the FULL history —
    including thinking blocks and tool_use/tool_result pairs — unmodified."""
    history = [
        HumanMessage(content="pull the borrowing base"),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "need to query redshift", "signature": "sig=="},
                {"type": "text", "text": "querying now"},
            ],
            tool_calls=[{"name": "bash", "args": {"cmd": "sql"}, "id": "tu_1"}],
        ),
        ToolMessage(content="42 rows", tool_call_id="tu_1"),
    ]
    fb, _, fallback = _chain(primary_exc=_api_error(anthropic.InternalServerError, 529))

    out = fb.invoke(history)

    assert out.content == "from-fallback"
    assert fallback.seen_messages == history  # nothing dropped or rewritten


def test_success_path_never_touches_fallback():
    fb, primary, fallback = _chain(primary_exc=None)
    out = fb.invoke([HumanMessage(content="hi")])
    assert out.content == "ok"
    assert fallback.seen_messages == []


# ---------------------------------------------------------------------------
# bind_tools path uses the same narrowed exception set
# ---------------------------------------------------------------------------

def test_bind_tools_passes_narrowed_exceptions(monkeypatch):
    captured = {}

    class BindableFake(FakeModel):
        def bind_tools(self, tools, **kwargs):
            class _Bound:
                def with_fallbacks(self, fallbacks, exceptions_to_handle=None):
                    captured["exceptions"] = exceptions_to_handle
                    return self
            return _Bound()

    primary = BindableFake()
    fallback = BindableFake()
    fb = FallbackChatModel(primary=primary, fallbacks=[fallback])
    fb.bind_tools([{"name": "t"}])

    assert captured["exceptions"] == fb.exceptions_to_handle
    assert Exception not in captured["exceptions"]

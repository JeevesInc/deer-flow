"""Tests for ContinuationMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.continuation_middleware import (
    ContinuationMiddleware,
    _looks_like_abandoned_intent,
)


def _ai(text: str, tool_calls: list | None = None) -> AIMessage:
    return AIMessage(content=text, tool_calls=tool_calls or [])


def _tc(name: str = "bash", tc_id: str = "call_1") -> dict:
    return {"name": name, "id": tc_id, "args": {}, "type": "tool_call"}


class TestLooksLikeAbandonedIntent:
    def test_with_tool_calls_returns_false(self):
        msg = _ai("Now let me check the database:", tool_calls=[_tc()])
        assert not _looks_like_abandoned_intent(msg)

    def test_empty_content_returns_false(self):
        msg = _ai("")
        assert not _looks_like_abandoned_intent(msg)

    def test_trailing_colon_with_intent_verb(self):
        assert _looks_like_abandoned_intent(_ai("Now let me check the customer table:"))

    def test_trailing_ellipsis_with_intent_verb(self):
        assert _looks_like_abandoned_intent(_ai("I'll query the data..."))

    def test_intent_phrase_at_end(self):
        assert _looks_like_abandoned_intent(_ai("Got it. Now let me pull the recent activity"))

    def test_let_me_check_in_tail(self):
        assert _looks_like_abandoned_intent(
            _ai("Found the company. Next, let me look at their borrowing history")
        )

    def test_legitimate_closing_remark_let_me_know(self):
        assert not _looks_like_abandoned_intent(_ai("All done. Let me know if you need anything else."))

    def test_legitimate_admission(self):
        assert not _looks_like_abandoned_intent(_ai("I'll need more information from you to proceed."))

    def test_final_answer_without_intent(self):
        assert not _looks_like_abandoned_intent(_ai("The answer is 42."))

    def test_trailing_colon_without_intent_verb(self):
        # A heading or quoted text ending with ":" shouldn't trigger
        assert not _looks_like_abandoned_intent(_ai("Here are the results:"))

    def test_structured_content_blocks(self):
        msg = AIMessage(
            content=[
                {"type": "text", "text": "Some preamble"},
                {"type": "text", "text": "Now let me query the database:"},
            ]
        )
        assert _looks_like_abandoned_intent(msg)


class TestWrapModelCallSync:
    def test_passthrough_when_no_abandonment(self):
        mw = ContinuationMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]
        response = ModelResponse(result=[_ai("Done.")])
        handler = MagicMock(return_value=response)

        result = mw.wrap_model_call(request, handler)

        assert result is response
        assert handler.call_count == 1

    def test_passthrough_when_tool_call_present(self):
        mw = ContinuationMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]
        response = ModelResponse(result=[_ai("Now let me check:", tool_calls=[_tc()])])
        handler = MagicMock(return_value=response)

        result = mw.wrap_model_call(request, handler)

        assert result is response
        assert handler.call_count == 1

    def test_reinvokes_on_abandoned_intent(self):
        mw = ContinuationMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]

        first = ModelResponse(result=[_ai("Now let me check the table:")])
        second = ModelResponse(result=[_ai("The table has 100 rows.", tool_calls=[_tc()])])
        handler = MagicMock(side_effect=[first, second])

        # request.override should reflect the second request's messages
        new_request = MagicMock()
        request.override.return_value = new_request

        result = mw.wrap_model_call(request, handler)

        assert result is second
        assert handler.call_count == 2
        # Verify the reminder was appended in the override
        override_kwargs = request.override.call_args.kwargs
        appended = override_kwargs["messages"]
        assert len(appended) == 3  # original + dangling AI + reminder
        assert isinstance(appended[-1], HumanMessage)
        assert "tool call" in appended[-1].content.lower()

    def test_bare_aimessage_response_is_coerced(self):
        mw = ContinuationMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]
        # Handler returns a bare AIMessage (allowed by ModelCallResult union)
        handler = MagicMock(return_value=_ai("Final answer."))

        result = mw.wrap_model_call(request, handler)

        assert isinstance(result, ModelResponse)
        assert handler.call_count == 1

    def test_does_not_recurse_on_double_abandonment(self):
        """If the model abandons intent again on retry, do NOT loop — accept the response."""
        mw = ContinuationMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]

        first = ModelResponse(result=[_ai("Now let me check:")])
        second = ModelResponse(result=[_ai("Now let me really check:")])
        handler = MagicMock(side_effect=[first, second])
        request.override.return_value = MagicMock()

        result = mw.wrap_model_call(request, handler)

        # Exactly two calls — no third re-invocation
        assert handler.call_count == 2
        assert result is second


class TestAwrapModelCallAsync:
    @pytest.mark.anyio
    async def test_async_passthrough(self):
        mw = ContinuationMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]
        response = ModelResponse(result=[_ai("Done.")])
        handler = AsyncMock(return_value=response)

        result = await mw.awrap_model_call(request, handler)

        assert result is response
        assert handler.await_count == 1

    @pytest.mark.anyio
    async def test_async_reinvokes_on_abandoned_intent(self):
        mw = ContinuationMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]

        first = ModelResponse(result=[_ai("Now let me run the query:")])
        second = ModelResponse(result=[_ai("Result is 42.", tool_calls=[_tc()])])
        handler = AsyncMock(side_effect=[first, second])
        request.override.return_value = MagicMock()

        result = await mw.awrap_model_call(request, handler)

        assert result is second
        assert handler.await_count == 2

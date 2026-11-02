"""Middleware that re-invokes the model when it abandons stated intent.

Problem: Claude sometimes emits a transition message like *"Now let me check X:"*
without attaching the tool call to the same turn. LangGraph treats any AIMessage
without ``tool_calls`` as a final answer and terminates the run, leaving the
user with a half-finished response.

Strategy: in ``wrap_model_call``, inspect the model's response. If the last
AIMessage has no ``tool_calls`` but its text matches an "I'm about to do X"
pattern, re-invoke the model **once** with a one-shot reminder appended to the
request. The reminder is not persisted to state — only the corrected response
is returned, so the user only ever sees the final message.

Bounded to one retry per turn (no recursion inside ``wrap_model_call``).
"""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)


# Patterns that signal the model intended to take an action.
# Verbs are deliberately narrow to avoid matching legitimate closing remarks
# like "let me know if..." or "I'll need more info from you".
_ACTION_VERBS = (
    r"check|look|get|find|fetch|query|run|pull|grab|see|search|"
    r"investigate|examine|verify|confirm|try|continue|proceed|"
    r"do that|gather|inspect|analy[sz]e|review|read|load|dig|explore"
)

_INTENT_PATTERNS = [
    re.compile(rf"\b(?:let me|let's|i'?ll|i will|now i'?ll|now let me|next,?\s*i'?ll|"
               rf"next,?\s*let me|going to)\s+(?:{_ACTION_VERBS})\b", re.IGNORECASE),
]

# Trailing punctuation that almost always indicates an unfinished thought.
_TRAILING_UNFINISHED = re.compile(r"[:…]\s*$|\.\.\.\s*$")

_REMINDER = (
    "Your previous response indicated you were about to take an action but did not "
    "include a tool call. If you intended to continue, call the appropriate tool now "
    "in this same turn. If you are actually finished, give your final answer directly "
    "without 'let me' or 'I'll' phrasing."
)


def _extract_text(msg: BaseMessage) -> str:
    """Return the plain-text portion of a message, handling list-of-blocks content."""
    content = getattr(msg, "content", "") or ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _last_ai_message(messages: list[BaseMessage]) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _looks_like_abandoned_intent(ai_msg: AIMessage) -> bool:
    """True if the message has no tool calls but text suggests an action was intended."""
    if getattr(ai_msg, "tool_calls", None):
        return False

    text = _extract_text(ai_msg).strip()
    if not text:
        return False

    # Strong signal: ends with colon or ellipsis (e.g. "Now let me query the db:")
    if _TRAILING_UNFINISHED.search(text):
        # Only flag if there's also an intent verb anywhere — avoids flagging
        # headings/quotes that happen to end with ":".
        return any(p.search(text) for p in _INTENT_PATTERNS)

    # Otherwise, look for intent phrases in the last ~400 chars (tail of response).
    tail = text[-400:]
    return any(p.search(tail) for p in _INTENT_PATTERNS)


def _coerce_response(result: ModelCallResult) -> ModelResponse:
    """Normalize ``ModelCallResult`` (either a ``ModelResponse`` or bare ``AIMessage``)."""
    if isinstance(result, ModelResponse):
        return result
    return ModelResponse(result=[result])


class ContinuationMiddleware(AgentMiddleware[AgentState]):
    """Re-invokes the model once when it abandons stated intent without a tool call."""

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        response = _coerce_response(handler(request))
        ai_msg = _last_ai_message(response.result)
        if ai_msg is None or not _looks_like_abandoned_intent(ai_msg):
            return response

        logger.warning(
            "ContinuationMiddleware: detected abandoned intent in model output; "
            "re-invoking with reminder. Preview: %r",
            _extract_text(ai_msg)[-200:],
        )
        new_request = request.override(
            messages=[*request.messages, ai_msg, HumanMessage(content=_REMINDER)]
        )
        return handler(new_request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        response = _coerce_response(await handler(request))
        ai_msg = _last_ai_message(response.result)
        if ai_msg is None or not _looks_like_abandoned_intent(ai_msg):
            return response

        logger.warning(
            "ContinuationMiddleware: detected abandoned intent in model output; "
            "re-invoking with reminder. Preview: %r",
            _extract_text(ai_msg)[-200:],
        )
        new_request = request.override(
            messages=[*request.messages, ai_msg, HumanMessage(content=_REMINDER)]
        )
        return await handler(new_request)

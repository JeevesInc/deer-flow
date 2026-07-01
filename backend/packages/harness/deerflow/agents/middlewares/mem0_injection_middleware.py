"""Middleware that injects mem0 semantic memories before each LLM call.

Uses ``wrap_model_call`` to prepend recalled memories as a SystemMessage
directly into the model request — without mutating thread state.  This
avoids the Anthropic API error from multiple non-consecutive system
messages and prevents HumanMessage accumulation across turns.

Search query is the latest user message, so memories are contextually
relevant to what the user is actually asking about.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


class Mem0InjectionMiddleware(AgentMiddleware[AgentState]):
    """Injects semantically relevant mem0 memories via wrap_model_call."""

    state_schema = AgentState

    def __init__(self, top_k: int = 10):
        super().__init__()
        self._top_k = top_k

    def _get_latest_user_query(self, messages: list) -> str | None:
        """Extract the latest human message text from the request messages."""
        for msg in reversed(messages):
            if getattr(msg, "type", None) == "human":
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    parts = []
                    for p in content:
                        if isinstance(p, str):
                            parts.append(p)
                        elif isinstance(p, dict) and "text" in p:
                            parts.append(p["text"])
                    return " ".join(parts).strip() or None
                return str(content).strip() or None
        return None

    def _build_injection(self, query: str) -> str | None:
        """Search mem0 and format results for injection."""
        try:
            from deerflow.agents.memory.mem0_store import search_memories

            memories = search_memories(query, top_k=self._top_k)
            if not memories:
                return None

            lines = []
            for mem in memories:
                text = mem.get("memory", "") or mem.get("text", "") or ""
                text = text.strip()
                if text:
                    lines.append(f"- {text}")

            if not lines:
                return None

            return (
                "<recalled_memories>\n"
                "The following are long-term memories relevant to the user's "
                "current message. Use them to personalize your response, but "
                "verify any specific numbers or claims before repeating them:\n"
                + "\n".join(lines)
                + "\n</recalled_memories>"
            )
        except Exception as e:
            logger.warning("mem0 injection failed: %s", e)
            return None

    def _inject(self, request: ModelRequest) -> ModelRequest:
        """Build injected request if mem0 is enabled and has relevant memories."""
        config = get_memory_config()
        if not config.enabled or not config.injection_enabled:
            return request

        query = self._get_latest_user_query(request.messages)
        if not query:
            return request

        injection = self._build_injection(query)
        if not injection:
            return request

        logger.info("Injecting %d mem0 memories for: %s", injection.count("\n- "), query[:80])

        # Merge into the existing leading SystemMessage if present, otherwise
        # prepend a new one at index 0. We intentionally never *insert* a
        # second SystemMessage mid-list: when `create_agent` passes its static
        # system_prompt separately (not as a list message), an injected
        # SystemMessage after a HumanMessage becomes non-consecutive once the
        # static prompt is re-added at index 0, and langchain_anthropic's
        # `_format_messages` raises "Received multiple non-consecutive system
        # messages."
        injected = list(request.messages)
        if injected and getattr(injected[0], "type", None) == "system":
            existing = injected[0]
            existing_content = existing.content
            if isinstance(existing_content, str):
                merged_content = existing_content + "\n\n" + injection
            elif isinstance(existing_content, list):
                merged_content = list(existing_content) + [{"type": "text", "text": injection}]
            else:
                merged_content = str(existing_content) + "\n\n" + injection
            injected[0] = existing.model_copy(update={"content": merged_content})
        else:
            injected.insert(0, SystemMessage(content=injection))
        return request.override(messages=injected)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._inject(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._inject(request))

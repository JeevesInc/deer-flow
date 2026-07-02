"""Middleware that injects semantically-relevant strategic context per turn.

STRATEGIC_CONTEXT.md is a large living document. Rather than loading it whole
into every system prompt (bloat), this middleware retrieves only the sections
relevant to the user's current message and injects them as a
``<strategic_context>`` block — and only when they clear a similarity
threshold, so off-topic turns inject nothing.

Uses ``wrap_model_call`` (like ``Mem0InjectionMiddleware``) so the injection
lives in the model request, not thread state, avoiding HumanMessage
accumulation and Anthropic's "multiple non-consecutive system messages" error.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


class StrategicContextMiddleware(AgentMiddleware[AgentState]):
    """Injects threshold-gated strategic-context sections via wrap_model_call."""

    state_schema = AgentState

    def __init__(self) -> None:
        super().__init__()
        # Per-run memoization: the factory rebuilds this middleware every run, so
        # a plain instance cache never leaks across threads/runs. Within one run
        # the query (latest human message) is constant across all the tool-loop
        # model calls, so we retrieve/build the injection once and reuse it.
        self._injection_cache: dict[str, str | None] = {}
        self._synced = False

    def _get_latest_user_query(self, messages: list) -> str | None:
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
        from deerflow.agents.memory.strategic_context_store import search_strategic_context, sync_if_changed

        # Cheap hash check, once per run. Crons re-index eagerly after writing
        # (see crawler.py); this inline sync is the safety net for overnight
        # dreams edits and manual edits. Gating to once-per-run keeps the file
        # read/hash off every model call in the tool loop.
        if not self._synced:
            self._synced = True
            try:
                sync_if_changed()
            except Exception as e:
                logger.warning("strategic_context sync failed: %s", e)

        results = search_strategic_context(query)
        if not results:
            return None

        blocks = []
        for r in results:
            text = (r.get("memory") or r.get("text") or "").strip()
            if text:
                blocks.append(text)
        if not blocks:
            return None

        return (
            "<strategic_context>\n"
            "The following strategic-context sections are relevant to the user's "
            "current message. Treat them as authoritative background on active "
            "deals, stakeholders, and priorities, but verify specific numbers or "
            "dates before repeating them:\n\n"
            + "\n\n---\n\n".join(blocks)
            + "\n</strategic_context>"
        )

    def _inject(self, request: ModelRequest) -> ModelRequest:
        from deerflow.agents.memory.strategic_context_store import is_enabled

        if not is_enabled():
            return request

        query = self._get_latest_user_query(request.messages)
        if not query:
            return request

        if query not in self._injection_cache:
            self._injection_cache[query] = self._build_injection(query)
        injection = self._injection_cache[query]
        if not injection:
            return request

        logger.info("Injecting strategic context for: %s", query[:80])

        # NOTE on mechanism (verified against langchain 1.x + langchain_anthropic):
        # `request.messages` does NOT contain the static system prompt — that
        # lives in `request.system_message` and is prepended only at model-invoke
        # time (`_execute_model_sync`: [system_message, *messages]). So messages[0]
        # is normally the first Human message. We insert our injection as a
        # SystemMessage at index 0 of `messages`; the final list becomes
        # [static_system, injected_system, human, ...] — two CONSECUTIVE system
        # messages, which langchain_anthropic's `_merge_messages` collapses into
        # one (so no "multiple non-consecutive system messages" error).
        #
        # When Mem0InjectionMiddleware runs before us (it's earlier in the chain)
        # it has already inserted its own SystemMessage at index 0 — in that case
        # messages[0] IS a system message and we merge into it, coalescing both
        # injected blocks into a single message. Keeping the injection as a
        # message distinct from `system_message` also keeps the static prompt a
        # stable prefix (cache-friendly) while our volatile block follows it.
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

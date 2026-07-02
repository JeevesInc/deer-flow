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

    def __init__(self, top_k: int = 10, lessons_top_k: int = 4):
        super().__init__()
        self._top_k = top_k
        self._lessons_top_k = lessons_top_k
        # Per-run memoization: this middleware is rebuilt by the graph factory
        # every run, so a plain instance cache never leaks across threads/runs.
        # Within one run the query (latest human message) is constant across all
        # tool-loop model calls, so we search mem0 once and reuse the injection.
        self._injection_cache: dict[str, str | None] = {}

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

    def _build_injection(self, query: str, min_score: float = 0.0) -> str | None:
        """Search mem0 and format results for injection.

        Results scoring below ``min_score`` are dropped (Mem-H1): mem0 search
        always returns top_k rows however weak the match, so an ungated
        injection pads every turn with off-topic memories.
        """
        try:
            from deerflow.agents.memory.mem0_store import search_memories

            memories = search_memories(query, top_k=self._top_k) or []

            if memories and min_score > 0:
                kept = [m for m in memories
                        if float(m.get("score", 0.0) or 0.0) >= min_score]
                if len(kept) < len(memories):
                    logger.info(
                        "mem0 injection gate: kept %d/%d memories (min_score=%.2f)",
                        len(kept), len(memories), min_score)
                memories = kept

            lines = []
            for mem in memories:
                text = mem.get("memory", "") or mem.get("text", "") or ""
                text = text.strip()
                if text:
                    lines.append(f"- {text}")

            lessons_block = self._build_lessons_block(query, min_score)
            if not lines and not lessons_block:
                return None

            parts = []
            if lines:
                parts.append(
                    "<recalled_memories>\n"
                    "The following are long-term memories relevant to the user's "
                    "current message. Use them to personalize your response, but "
                    "verify any specific numbers or claims before repeating them:\n"
                    + "\n".join(lines)
                    + "\n</recalled_memories>"
                )
            if lessons_block:
                parts.append(lessons_block)
            return "\n\n".join(parts)
        except Exception as e:
            logger.warning("mem0 injection failed: %s", e)
            return None

    def _build_lessons_block(self, query: str, min_score: float) -> str | None:
        """Search the mem0 `lessons` namespace and format matches (P2-1 §4.1).

        This replaces the old SOUL.md mandate to run the SLL inject script by
        hand every turn (verifiably unfollowed): lessons captured by sll_score
        are synced into mem0 and retrieved here, score-gated like facts.
        """
        try:
            from deerflow.agents.memory.mem0_store import search_memories

            lessons = search_memories(query, top_k=self._lessons_top_k, user_id="lessons")
            if min_score > 0:
                lessons = [m for m in lessons
                           if float(m.get("score", 0.0) or 0.0) >= min_score]
            lines = []
            for mem in lessons:
                text = (mem.get("memory", "") or "").strip()
                if text:
                    lines.append(f"- {text}")
            if not lines:
                return None
            self._log_lesson_injection(lessons, query)
            return (
                "<learned_lessons>\n"
                "Hard-won lessons from your own past work, relevant to this "
                "task. Treat [!] AVOID entries as prohibitions and [+] DO "
                "entries as required patterns:\n"
                + "\n".join(lines)
                + "\n</learned_lessons>"
            )
        except Exception as e:
            logger.debug("lessons injection skipped: %s", e)
            return None

    @staticmethod
    def _log_lesson_injection(lessons: list[dict], query: str) -> None:
        """Append injected-lesson events for the efficacy metric (best-effort).

        Dreams/the SLL dashboard can correlate injections with outcomes —
        without this, lesson usage is invisible and efficacy unprovable (§8).
        """
        try:
            import json
            import os
            import time as _time

            if os.environ.get("PYTEST_CURRENT_TEST"):
                return  # keep test runs out of the live efficacy data

            from deerflow.config.paths import get_paths

            path = get_paths().base_dir / "sll" / "injection_log.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": _time.time(),
                "query": query[:150],
                "lesson_ids": [m.get("id", "") for m in lessons],
                "scores": [round(float(m.get("score", 0.0) or 0.0), 3) for m in lessons],
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _inject(self, request: ModelRequest) -> ModelRequest:
        """Build injected request if mem0 is enabled and has relevant memories."""
        config = get_memory_config()
        if not config.enabled or not config.injection_enabled:
            return request

        query = self._get_latest_user_query(request.messages)
        if not query:
            return request

        if query not in self._injection_cache:
            raw = getattr(config, "mem0_min_injection_score", None)
            min_score = float(raw) if isinstance(raw, (int, float)) else 0.0
            self._injection_cache[query] = self._build_injection(query, min_score=min_score)
        injection = self._injection_cache[query]
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

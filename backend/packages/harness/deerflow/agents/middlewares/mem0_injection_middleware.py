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


def _jev_filter(query: str, items: list[dict], kind: str) -> list[dict]:
    """Jev relevance pass over score-gated candidates (2026-09-23).

    Loads skills/custom/_shared/jev_memfilter.py by path (the LangGraph
    process does not have _shared on sys.path). Never raises; any failure
    returns ``items`` unchanged. MEM0_JEV_FILTER=0 disables.
    """
    import os
    import sys
    from pathlib import Path

    if not items or os.environ.get("MEM0_JEV_FILTER", "1") == "0":
        return items
    try:
        shared = Path(__file__).resolve().parents[6] / "skills" / "custom" / "_shared"
        if str(shared) not in sys.path:
            sys.path.insert(0, str(shared))
        from jev_memfilter import filter_memories

        kept = filter_memories(query, items, kind=kind)
        logger.info("mem0 jev filter (%s): kept %d/%d", kind, len(kept), len(items))
        return kept
    except Exception as e:
        logger.warning("mem0 jev filter skipped: %s", e)
        return items


class Mem0InjectionMiddleware(AgentMiddleware[AgentState]):
    """Injects semantically relevant mem0 memories via wrap_model_call."""

    state_schema = AgentState

    def __init__(self, top_k: int = 10, lessons_top_k: int = 4,
                 behavior_top_k: int = 5, scenarios_top_k: int = 3,
                 atoms_always_keep: int = 3):
        super().__init__()
        self._top_k = top_k
        self._scenarios_top_k = scenarios_top_k
        self._atoms_always_keep = atoms_always_keep
        self._lessons_top_k = lessons_top_k
        self._behavior_top_k = behavior_top_k
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

            memories = _jev_filter(query, memories, "fact")

            # L2 (memory layers, 2026-10-12): topic pages synthesized nightly
            # from clusters of L1 facts by backend/scripts/memory_layers.py.
            # Atoms already summarized by an injected page are dropped, except
            # the top few, which keep exact detail for the specific question.
            scenarios_block, covered_ids = self._build_scenarios_block(query, min_score)
            if covered_ids:
                keep = memories[: self._atoms_always_keep]
                rest = [m for m in memories[self._atoms_always_keep:]
                        if str(m.get("id", "")) not in covered_ids]
                if len(keep) + len(rest) < len(memories):
                    logger.info("mem0 L2 dedupe: dropped %d atoms covered by scenarios",
                                len(memories) - len(keep) - len(rest))
                memories = keep + rest

            lines = []
            for mem in memories:
                text = mem.get("memory", "") or mem.get("text", "") or ""
                text = text.strip()
                if text:
                    lines.append(f"- {text}")

            lessons_block = self._build_lessons_block(query, min_score)
            behavior_block = self._build_behavior_block(query, min_score)
            if not lines and not lessons_block and not behavior_block and not scenarios_block:
                return None

            parts = []
            if scenarios_block:
                parts.append(scenarios_block)
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
            if behavior_block:
                parts.append(behavior_block)
            return "\n\n".join(parts)
        except Exception as e:
            logger.warning("mem0 injection failed: %s", e)
            return None

    def _build_scenarios_block(self, query: str, min_score: float) -> tuple[str | None, set[str]]:
        """Search the L2 ``memory-scenarios`` namespace (memory_layers.py).

        Returns the formatted block and the set of L1 fact ids those pages
        summarize (for atom dedupe). Never raises.
        """
        try:
            from deerflow.agents.memory.mem0_store import search_memories

            pages = search_memories(query, top_k=self._scenarios_top_k,
                                    user_id="memory-scenarios")
            if min_score > 0:
                pages = [m for m in pages
                         if float(m.get("score", 0.0) or 0.0) >= min_score]
            pages = _jev_filter(query, pages, "fact")
            lines, covered = [], set()
            for m in pages:
                text = (m.get("memory", "") or "").strip()
                if not text:
                    continue
                meta = m.get("metadata") or {}
                asof = str(meta.get("last_fact_at", ""))[:10]
                lines.append(f"- {text}" + (f" [latest fact {asof}]" if asof else ""))
                covered.update(str(i) for i in (meta.get("member_ids") or []))
            if not lines:
                return None, set()
            return (
                "<memory_topics>\n"
                "Consolidated topic summaries built from many of your long-term "
                "memories (newer facts supersede older). Use them for context; "
                "verify specific numbers before repeating them:\n"
                + "\n".join(lines)
                + "\n</memory_topics>"
            ), covered
        except Exception as e:
            logger.debug("scenarios injection skipped: %s", e)
            return None, set()

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
            lessons = _jev_filter(query, lessons, "lesson")
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

    def _build_behavior_block(self, query: str, min_score: float) -> str | None:
        """Search the mem0 `brian-behavior` namespace (action_learner.py).

        These are observed patterns in how Brian himself triages and writes,
        synthesized from his own sent mail, the threads he left unanswered, and
        his Slack sends. Injected so drafting and triage imitate his actual
        behavior instead of a generic assistant register.
        """
        try:
            from deerflow.agents.memory.mem0_store import search_memories

            patterns = search_memories(query, top_k=self._behavior_top_k,
                                       user_id="brian-behavior")
            if min_score > 0:
                patterns = [m for m in patterns
                            if float(m.get("score", 0.0) or 0.0) >= min_score]
            lines = []
            for mem in patterns:
                text = (mem.get("memory", "") or "").strip()
                if text:
                    lines.append(f"- {text}")
            if not lines:
                return None
            return (
                "<observed_owner_behavior>\n"
                "Patterns in how Brian himself triages and writes, learned from "
                "his own sent mail, the threads he ignored, and his Slack sends. "
                "[TRIAGE] = what earns a response and how fast; [VOICE] = how to "
                "write it; [SUBSTANCE] = what he holds, concedes, or delegates. "
                "Match these when drafting on his behalf or deciding what "
                "deserves action — they describe HIS behavior, not facts to "
                "repeat:\n"
                + "\n".join(lines)
                + "\n</observed_owner_behavior>"
            )
        except Exception as e:
            logger.debug("behavior injection skipped: %s", e)
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

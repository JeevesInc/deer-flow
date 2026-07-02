"""Middleware for memory mechanism.

Uses mem0 for long-term fact storage (semantic vector search) and keeps
the lightweight FileMemoryStorage for profile sections only.
"""

import logging
import re
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from deerflow.agents.memory.queue import get_memory_queue
from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


class MemoryMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    pass


# Ops-noise exclusion (Mem-H2): cron-dispatched runs (dreams, EOD review,
# report pipelines, ...) used to flow through memory capture like user
# conversations, storing near-identical daily run trivia as permanent facts —
# the #1 retrieval-pollution source ("User ran both borrowing bases on May
# 25/26/27..."). Threads whose FIRST message opens with a known cron header,
# or contains the explicit sentinel, are skipped entirely.
MEMORY_OPT_OUT_SENTINEL = "<memory:off>"
_OPS_PROMPT_HEADERS = (
    "DREAM SESSION #",
    "END OF DAY REVIEW #",
    "WEEKLY OPEN ITEMS REVIEW #",
    "LATENT-LEARNING REVIEW #",
)


def _message_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


def _is_ops_thread(user_messages: list[Any]) -> bool:
    """True when this thread is autonomous ops work, not a user conversation."""
    if not user_messages:
        return False
    first = _message_text(user_messages[0])
    head = first.lstrip()[:100]
    if any(head.startswith(h) for h in _OPS_PROMPT_HEADERS):
        return True
    return MEMORY_OPT_OUT_SENTINEL in first


def _filter_messages_for_memory(messages: list[Any]) -> list[Any]:
    """Filter messages to keep only user inputs and final assistant responses.

    This filters out:
    - Tool messages (intermediate tool call results)
    - AI messages with tool_calls (intermediate steps, not final responses)
    - The <uploaded_files> block injected by UploadsMiddleware into human messages
      (file paths are session-scoped and must not persist in long-term memory).
      The user's actual question is preserved; only turns whose content is entirely
      the upload block (nothing remains after stripping) are dropped along with
      their paired assistant response.

    Only keeps:
    - Human messages (with the ephemeral upload block removed)
    - AI messages without tool_calls (final assistant responses), unless the
      paired human turn was upload-only and had no real user text.
    """
    _UPLOAD_BLOCK_RE = re.compile(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", re.IGNORECASE)

    filtered = []
    skip_next_ai = False
    for msg in messages:
        msg_type = getattr(msg, "type", None)

        if msg_type == "human":
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            content_str = str(content)
            if "<uploaded_files>" in content_str:
                stripped = _UPLOAD_BLOCK_RE.sub("", content_str).strip()
                if not stripped:
                    skip_next_ai = True
                    continue
                from copy import copy

                clean_msg = copy(msg)
                clean_msg.content = stripped
                filtered.append(clean_msg)
                skip_next_ai = False
            else:
                filtered.append(msg)
                skip_next_ai = False
        elif msg_type == "ai":
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                if skip_next_ai:
                    skip_next_ai = False
                    continue
                filtered.append(msg)
        # Skip tool messages and AI messages with tool_calls

    return filtered


def _messages_to_mem0_format(messages: list[Any]) -> list[dict[str, str]]:
    """Convert LangChain messages to mem0's expected format.

    Args:
        messages: Filtered LangChain message objects.

    Returns:
        List of {"role": "user"|"assistant", "content": "..."} dicts.
    """
    result = []
    for msg in messages:
        msg_type = getattr(msg, "type", None)
        content = getattr(msg, "content", "")

        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict):
                    text_val = p.get("text")
                    if isinstance(text_val, str):
                        text_parts.append(text_val)
            content = " ".join(text_parts) if text_parts else str(content)

        content = str(content).strip()
        if not content:
            continue

        # Truncate very long messages before sending to mem0
        if len(content) > 2000:
            content = content[:2000] + "..."

        if msg_type == "human":
            result.append({"role": "user", "content": content})
        elif msg_type == "ai":
            result.append({"role": "assistant", "content": content})

    return result


class MemoryMiddleware(AgentMiddleware[MemoryMiddlewareState]):
    """Middleware that queues conversation for memory update after agent execution.

    This middleware:
    1. After each agent execution, queues the conversation for memory update
    2. Only includes user inputs and final assistant responses (ignores tool calls)
    3. The queue uses debouncing to batch multiple updates together
    4. Long-term facts are stored via mem0 (semantic vector search)
    5. Profile sections (workContext etc.) are still updated via the legacy updater
    """

    state_schema = MemoryMiddlewareState

    def __init__(self, agent_name: str | None = None):
        super().__init__()
        self._agent_name = agent_name

    @override
    def after_agent(self, state: MemoryMiddlewareState, runtime: Runtime) -> dict | None:
        config = get_memory_config()
        if not config.enabled:
            return None

        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if not thread_id:
            logger.debug("No thread_id in context, skipping memory update")
            return None

        messages = state.get("messages", [])
        if not messages:
            logger.debug("No messages in state, skipping memory update")
            return None

        # Filter to only keep user inputs and final assistant responses
        filtered_messages = _filter_messages_for_memory(messages)

        user_messages = [m for m in filtered_messages if getattr(m, "type", None) == "human"]
        assistant_messages = [m for m in filtered_messages if getattr(m, "type", None) == "ai"]

        if not user_messages or not assistant_messages:
            return None

        if _is_ops_thread(user_messages):
            logger.info("Ops-dispatched thread — skipping memory capture (Mem-H2)")
            return None

        # Queue for mem0 storage (debounced) — replaces old fact extraction
        queue = get_memory_queue()
        queue.add(thread_id=thread_id, messages=filtered_messages, agent_name=self._agent_name)

        self._capture_sll(user_messages, assistant_messages)

        return None

    @staticmethod
    def _capture_sll(user_messages: list[Any], assistant_messages: list[Any]) -> None:
        """Fire-and-forget SLL scoring for this turn (P2-1 / §4.1).

        Replaces the SOUL.md "run sll_score.py at the start and end of every
        turn" mandate the agent verifiably did not follow. Runs the existing
        scorer as a detached subprocess: first applies pending sentiment using
        the user's current message (their reaction to the PREVIOUS turn), then
        scores this turn. Lessons land in the SLL JSONL and are synced into
        the mem0 `lessons` namespace by the daily maintenance pass, where
        Mem0InjectionMiddleware retrieves them. Kill switch:
        SLL_CAPTURE_DISABLE=1.
        """
        import os

        if os.environ.get("SLL_CAPTURE_DISABLE", "").strip() == "1":
            return
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return

        task = _message_text(user_messages[-1]).strip()[:1500]
        response = _message_text(assistant_messages[-1]).strip()[:1500]
        if not task or not response:
            return

        try:
            import subprocess
            import sys
            import threading

            from deerflow.config.paths import get_paths

            # base_dir = backend/.deer-flow → repo root is two levels up.
            script = (get_paths().base_dir.parent.parent
                      / "skills" / "custom" / "sll" / "sll_score.py")
            if not script.exists():
                return

            def _run() -> None:
                try:
                    subprocess.run(
                        [sys.executable, str(script), "--apply-sentiment",
                         "--user-reply", task],
                        capture_output=True, timeout=120)
                    subprocess.run(
                        [sys.executable, str(script), "--task", task,
                         "--response", response],
                        capture_output=True, timeout=120)
                except Exception as exc:  # noqa: BLE001 - background best-effort
                    logger.debug("SLL capture failed: %s", exc)

            threading.Thread(target=_run, name="sll-capture", daemon=True).start()
        except Exception as exc:
            logger.debug("SLL capture not started: %s", exc)

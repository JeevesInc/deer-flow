"""Middleware to detect and break repetitive tool call loops.

P0 safety: prevents the agent from calling the same tool with the same
arguments indefinitely until the recursion limit kills the run.

Detection strategy:
  1. After each model response, hash the tool calls (name + args).
  2. Track recent hashes in a sliding window.
  3. If the same hash appears >= warn_threshold times, queue a warning
     for the next ``before_model`` cycle (once per hash). Deferring to
     ``before_model`` ensures the warning HumanMessage lands AFTER the
     tool_result for the offending call — Anthropic's API requires every
     tool_use to be immediately followed by its tool_result, so injecting
     the warning right after ``after_model`` would split that pair.
  4. If a hash appears >= hard_limit times, strip all tool_calls from the
     response so the agent is forced to produce a final text answer.
     Hard-stop happens in ``after_model`` because it removes the tool_use
     entirely, so there is no adjacency pair to preserve.
"""

import hashlib
import json
import logging
import threading
from collections import OrderedDict, defaultdict
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from deerflow.utils.text import extract_text

logger = logging.getLogger(__name__)

# Defaults — can be overridden via constructor
_DEFAULT_WARN_THRESHOLD = 3  # inject warning after 3 identical calls
_DEFAULT_HARD_LIMIT = 5  # force-stop after 5 identical calls
_DEFAULT_WINDOW_SIZE = 20  # track last N tool calls
_DEFAULT_MAX_TRACKED_THREADS = 100  # LRU eviction limit


def _normalize_args(args: dict) -> dict:
    """Normalize tool call arguments for consistent hashing.

    Strips leading/trailing whitespace only (preserves internal spacing)
    and sorts keys for deterministic ordering.  This avoids false positives
    where semantically different strings (e.g. SQL with different column
    lists) would hash the same after aggressive whitespace collapsing.
    """
    normalized = {}
    for k, v in sorted(args.items()):
        if isinstance(v, str):
            normalized[k] = v.strip()
        elif isinstance(v, dict):
            normalized[k] = _normalize_args(v)
        elif isinstance(v, list):
            normalized[k] = [i.strip() if isinstance(i, str) else i for i in v]
        else:
            normalized[k] = v
    return normalized


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    """Deterministic hash of a set of tool calls (name + normalized args).

    Order-independent: the same multiset of tool calls always produces
    the same hash, regardless of input order or trivial whitespace differences.
    """
    normalized: list[dict] = []
    for tc in tool_calls:
        normalized.append(
            {
                "name": tc.get("name", ""),
                "args": _normalize_args(tc.get("args", {})),
            }
        )

    normalized.sort(
        key=lambda tc: (
            tc["name"],
            json.dumps(tc["args"], sort_keys=True, default=str),
        )
    )
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


_WARNING_MSG = "[LOOP DETECTED] You are repeating the same tool calls. Stop calling tools and produce your final answer now. If you cannot complete the task, summarize what you accomplished so far."

_HARD_STOP_MSG = "[FORCED STOP] Repeated tool calls exceeded the safety limit. Producing final answer with results collected so far."


class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    """Detects and breaks repetitive tool call loops.

    Args:
        warn_threshold: Number of identical tool call sets before injecting
            a warning message. Default: 3.
        hard_limit: Number of identical tool call sets before stripping
            tool_calls entirely. Default: 5.
        window_size: Size of the sliding window for tracking calls.
            Default: 20.
        max_tracked_threads: Maximum number of threads to track before
            evicting the least recently used. Default: 100.
    """

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
    ):
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self._lock = threading.Lock()
        # Per-thread tracking using OrderedDict for LRU eviction
        self._history: OrderedDict[str, list[str]] = OrderedDict()
        self._warned: dict[str, set[str]] = defaultdict(set)
        # Pending warnings queued in after_model, drained in before_model
        self._pending_warning: dict[str, str] = {}

    def _get_thread_id(self, runtime: Runtime) -> str:
        """Extract thread_id from runtime context for per-thread tracking."""
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id:
            return thread_id
        return "default"

    def _evict_if_needed(self) -> None:
        """Evict least recently used threads if over the limit.

        Must be called while holding self._lock.
        """
        while len(self._history) > self.max_tracked_threads:
            evicted_id, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_id, None)
            self._pending_warning.pop(evicted_id, None)
            logger.debug("Evicted loop tracking for thread %s (LRU)", evicted_id)

    def _track_and_check(self, state: AgentState, runtime: Runtime) -> tuple[str | None, bool]:
        """Track tool calls and check for loops.

        Returns:
            (warning_message_or_none, should_hard_stop)
        """
        messages = state.get("messages", [])
        if not messages:
            return None, False

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, False

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, False

        thread_id = self._get_thread_id(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        with self._lock:
            # Touch / create entry (move to end for LRU)
            if thread_id in self._history:
                self._history.move_to_end(thread_id)
            else:
                self._history[thread_id] = []
                self._evict_if_needed()

            history = self._history[thread_id]
            history.append(call_hash)
            if len(history) > self.window_size:
                history[:] = history[-self.window_size :]

            count = history.count(call_hash)
            tool_names = [tc.get("name", "?") for tc in tool_calls]

            if count >= self.hard_limit:
                logger.error(
                    "Loop hard limit reached — forcing stop",
                    extra={
                        "thread_id": thread_id,
                        "call_hash": call_hash,
                        "count": count,
                        "tools": tool_names,
                    },
                )
                return _HARD_STOP_MSG, True

            if count >= self.warn_threshold:
                warned = self._warned[thread_id]
                if call_hash not in warned:
                    warned.add(call_hash)
                    logger.warning(
                        "Repetitive tool calls detected — injecting warning",
                        extra={
                            "thread_id": thread_id,
                            "call_hash": call_hash,
                            "count": count,
                            "tools": tool_names,
                        },
                    )
                    return _WARNING_MSG, False
                # Warning already injected for this hash — suppress
                return None, False

        return None, False

    def _after(self, state: AgentState, runtime: Runtime) -> dict | None:
        """Track tool calls; hard-stop immediately, queue warnings for next ``before_model``."""
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # Strip tool_calls from the last AIMessage to force text output.
            # Safe to mutate now: no tool will run, so no tool_use/tool_result
            # adjacency pair to preserve. content is normalized to a plain string
            # via extract_text: with thinking enabled it is a list of blocks
            # (thinking/text/tool_use), so `list + str` would raise TypeError
            # exactly when the breaker fires, and leftover tool_use blocks would
            # 400 the next call. extract_text drops both and keeps only text.
            messages = state.get("messages", [])
            last_msg = messages[-1]
            new_content = (extract_text(last_msg.content) + f"\n\n{_HARD_STOP_MSG}").strip()
            stripped_msg = last_msg.model_copy(
                update={
                    "tool_calls": [],
                    "content": new_content,
                }
            )
            return {"messages": [stripped_msg]}

        if warning:
            # Defer to before_model so the warning HumanMessage lands AFTER
            # the tool_result for the offending tool_use. Injecting here
            # would split the tool_use/tool_result pair and Anthropic would
            # reject every subsequent request with a 400.
            thread_id = self._get_thread_id(runtime)
            with self._lock:
                self._pending_warning[thread_id] = warning

        return None

    def _before(self, state: AgentState, runtime: Runtime) -> dict | None:
        """Drain any queued warning from a prior ``after_model`` cycle."""
        thread_id = self._get_thread_id(runtime)
        with self._lock:
            warning = self._pending_warning.pop(thread_id, None)
        if warning is None:
            return None
        # HumanMessage (not SystemMessage) — Anthropic only accepts system
        # messages at the start of the conversation; mid-stream system
        # messages crash langchain_anthropic's _format_messages(). See #1299.
        return {"messages": [HumanMessage(content=warning)]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._after(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._after(state, runtime)

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._before(state, runtime)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._before(state, runtime)

    def reset(self, thread_id: str | None = None) -> None:
        """Clear tracking state. If thread_id given, clear only that thread."""
        with self._lock:
            if thread_id:
                self._history.pop(thread_id, None)
                self._warned.pop(thread_id, None)
                self._pending_warning.pop(thread_id, None)
            else:
                self._history.clear()
                self._warned.clear()
                self._pending_warning.clear()

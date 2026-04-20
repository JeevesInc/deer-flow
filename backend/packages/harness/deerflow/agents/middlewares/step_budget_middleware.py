"""Middleware to enforce a soft step budget and force graceful wrap-up.

When the agent approaches the recursion limit, this middleware strips
tool_calls from the model response and injects a wrap-up instruction,
giving the agent a chance to summarize its progress before the hard
limit kills the run and rolls back all state.

Each model invocation (``after_model`` call) corresponds to roughly
``STEPS_PER_MODEL_CALL`` graph-level steps (model node + tool node +
middleware before/after nodes).  The middleware converts the run's
``recursion_limit`` into a model-call budget, reserving a few calls
at the end for the final text response.
"""

import logging
import threading
from collections import OrderedDict
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_config
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# Approximate graph steps consumed per model→tool round-trip.
# 4 middleware nodes + model node + tool node ≈ 6.  Conservative
# estimate to avoid triggering too early.
STEPS_PER_MODEL_CALL = 6

# Reserve this many model calls for the agent to produce a final
# text response after being told to wrap up.
_DEFAULT_RESERVE_CALLS = 2

_MAX_TRACKED_THREADS = 100

_WRAP_UP_MSG = (
    "[STEP BUDGET] You are running low on execution steps. "
    "Stop calling tools and produce your final answer NOW. "
    "Summarize what you accomplished and what remains to be done. "
    "The user can reply 'continue' to pick up where you left off."
)


class StepBudgetMiddleware(AgentMiddleware[AgentState]):
    """Counts model invocations per run and forces wrap-up near the limit.

    Args:
        reserve_calls: Model calls to reserve for the final response.
            Default: 2.
    """

    def __init__(self, reserve_calls: int = _DEFAULT_RESERVE_CALLS):
        super().__init__()
        self.reserve_calls = reserve_calls
        self._lock = threading.Lock()
        # Per-thread step counters: thread_id -> count of after_model calls
        self._counts: OrderedDict[str, int] = OrderedDict()

    def _get_thread_id(self, runtime: Runtime) -> str:
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        return thread_id or "default"

    def _get_budget(self) -> int:
        """Derive model-call budget from the run's recursion_limit."""
        try:
            config: RunnableConfig = get_config()
            limit = config.get("recursion_limit", 500)
        except Exception:
            limit = 500
        # Convert graph steps → model calls, then subtract reserve
        max_calls = limit // STEPS_PER_MODEL_CALL
        return max(max_calls - self.reserve_calls, 5)

    def _increment_and_check(self, runtime: Runtime) -> bool:
        """Increment the step count. Returns True if budget is exhausted."""
        thread_id = self._get_thread_id(runtime)
        budget = self._get_budget()

        with self._lock:
            if thread_id in self._counts:
                self._counts.move_to_end(thread_id)
            else:
                self._counts[thread_id] = 0
                # LRU eviction
                while len(self._counts) > _MAX_TRACKED_THREADS:
                    self._counts.popitem(last=False)

            self._counts[thread_id] += 1
            count = self._counts[thread_id]

        if count >= budget:
            logger.warning(
                "Step budget exhausted: %d/%d model calls used (thread=%s, recursion_limit≈%d)",
                count, budget, thread_id, budget * STEPS_PER_MODEL_CALL,
            )
            return True
        return False

    def _before(self, state: AgentState, runtime: Runtime) -> dict | None:
        """Reset counter on the first model call of a new run.

        Detect a new run by checking if the last message is a HumanMessage
        (the user just sent something) and the counter is non-zero.
        """
        thread_id = self._get_thread_id(runtime)
        messages = state.get("messages", [])
        if not messages:
            return None
        # If the most recent non-system message is human, this is a fresh run
        last_msg = messages[-1]
        if getattr(last_msg, "type", None) == "human":
            with self._lock:
                if thread_id in self._counts and self._counts[thread_id] > 0:
                    logger.info("Step budget counter reset for thread %s (new run detected)", thread_id)
                    self._counts[thread_id] = 0
        return None

    def _after(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._increment_and_check(runtime):
            return None

        # Budget exhausted — strip tool calls and inject wrap-up message.
        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            # Already a text-only response — nothing to strip.
            return None

        stripped_msg = last_msg.model_copy(
            update={
                "tool_calls": [],
                "content": (last_msg.content or "") + f"\n\n{_WRAP_UP_MSG}",
            }
        )
        return {"messages": [stripped_msg]}

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._before(state, runtime)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._before(state, runtime)

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._after(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._after(state, runtime)

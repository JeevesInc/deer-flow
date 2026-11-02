"""SteeringMiddleware — mid-run steering + cheap goal-level rule checks.

Super-Agent proposal §3, Phase 3. Runs ``before_model`` (after tool results,
so a HumanMessage never splits a tool_use/tool_result pair):

1. **Steering inbox.** Messages Brian sent while this run was working (dropped
   into ``.deer-flow/steering/<thread_id>/`` by the gateway) are injected now,
   so a correction lands within one model call instead of after the run.
2. **Auto-subscription.** When a tool result shows a Gmail draft was created
   (``gmail_tool`` prints ``Thread: <id>``), every active Ticket on this thread
   subscribes to ``gmail:<id>``, so the counterparty's reply routes back to the
   Ticket instead of the classifier.
3. **Repeated-error rule.** The same tool error more than 3 times (our retry
   limit) injects one stop-and-reassess note. Same-tool/same-args loops are
   LoopDetectionMiddleware's job.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from deerflow.tickets import steering

logger = logging.getLogger(__name__)

_GMAIL_THREAD_RE = re.compile(r"Draft created successfully![\s\S]{0,200}?Thread:\s+([0-9a-f]{10,})", re.I)
ERROR_REPEAT_LIMIT = 3
_SCAN_WINDOW = 40

_ERROR_NOTE = '[WATCHDOG] The same error has now occurred {n} times: "{sig}". Stop retrying this approach. Diagnose the cause or switch method; if you cannot, tell Brian exactly what failed and why.'


def _text(m) -> str:
    c = getattr(m, "content", "")
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c if isinstance(c, str) else str(c)


def _error_signature(m) -> str | None:
    if getattr(m, "type", None) != "tool":
        return None
    txt = _text(m).strip()
    is_err = getattr(m, "status", None) == "error" or txt.lower().startswith(("error", "traceback")) or "\nError:" in txt[:400]
    if not is_err:
        return None
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    errs = [ln for ln in lines if re.match(r"^[\w.]*(Error|Exception)\b|^Error", ln)]
    sig = (errs[-1] if errs else lines[0] if lines else "")[:160]
    sig = re.sub(r"\d{3,}", "#", sig)  # ids/ports/line numbers vary between retries
    return f"{getattr(m, 'name', '')}: {sig}"


class SteeringMiddleware(AgentMiddleware[AgentState]):
    def __init__(self, max_tracked_threads: int = 200):
        super().__init__()
        self._lock = threading.Lock()
        self._seen_tool_ids: OrderedDict[str, set[str]] = OrderedDict()
        self._warned: OrderedDict[str, set[str]] = OrderedDict()
        self._max = max_tracked_threads

    def _remember(self, store: OrderedDict, thread_id: str) -> set[str]:
        s = store.setdefault(thread_id, set())
        store.move_to_end(thread_id)
        while len(store) > self._max:
            store.popitem(last=False)
        return s

    def _subscribe_drafts(self, thread_id: str, messages: list) -> None:
        with self._lock:
            seen = self._remember(self._seen_tool_ids, thread_id)
            fresh = []
            for m in messages[-_SCAN_WINDOW:]:
                if getattr(m, "type", None) != "tool":
                    continue
                mid = getattr(m, "tool_call_id", None) or getattr(m, "id", None) or str(id(m))
                if mid in seen:
                    continue
                seen.add(mid)
                fresh.append(m)
        ids = {g for m in fresh for g in _GMAIL_THREAD_RE.findall(_text(m))}
        if not ids:
            return
        try:
            from deerflow.tools.builtins.ticket_tools import _get_store

            store = _get_store()
            for t in store.list(thread_id=thread_id):
                for gid in ids:
                    store.subscribe(t["id"], f"gmail:{gid}")
                    logger.info("[Steering] %s subscribed to gmail:%s", t["id"], gid)
        except Exception as e:
            logger.warning("[Steering] auto-subscribe failed: %s", e)

    def _error_note(self, thread_id: str, messages: list) -> str | None:
        counts: dict[str, int] = {}
        for m in messages[-_SCAN_WINDOW:]:
            sig = _error_signature(m)
            if sig:
                counts[sig] = counts.get(sig, 0) + 1
        with self._lock:
            warned = self._remember(self._warned, thread_id)
            for sig, n in counts.items():
                if n > ERROR_REPEAT_LIMIT and sig not in warned:
                    warned.add(sig)
                    return _ERROR_NOTE.format(n=n, sig=sig)
        return None

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if not thread_id:
            return None
        messages = state.get("messages", [])
        # Never inject between an AI tool_use and its tool_result.
        if messages and getattr(messages[-1], "type", None) == "ai" and getattr(messages[-1], "tool_calls", None):
            return None
        new: list[HumanMessage] = []
        try:
            for item in steering.pop_all(thread_id):
                new.append(HumanMessage(content=f"[Brian, sent while you were working — {item.get('ts', '')}]\n{item.get('text', '')}"))
                logger.info("[Steering] injected inbox message into thread %s", thread_id)
        except Exception as e:
            logger.warning("[Steering] inbox read failed for %s: %s", thread_id, e)
        self._subscribe_drafts(thread_id, messages)
        note = self._error_note(thread_id, messages)
        if note:
            new.append(HumanMessage(content=note))
            logger.warning("[Steering] repeated-error note injected on thread %s", thread_id)
        return {"messages": new} if new else None

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

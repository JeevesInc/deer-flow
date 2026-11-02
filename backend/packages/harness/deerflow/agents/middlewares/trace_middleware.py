"""Records one trace record per model call.

Wraps the model invocation so both sides are captured together — what went in
(prompt shape, tool menu, last user turn) and what came out (text, tool calls,
usage, latency, errors). `after_model` could only see the result, and would miss
calls that raised.

Placement matters. This sits OUTSIDE AnthropicRetryMiddleware so one logical
model call produces one trace record with the retry cost folded into its
latency, rather than N records that make the run look busier than it was. It
sits INSIDE the context-building middlewares (mem0, strategic context,
summarization) so the prompt it records is the prompt the model actually saw.

Everything here is best-effort: a tracing failure is logged at debug and
swallowed. The agent must never fail because we could not write a log line.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse

from deerflow.agents.tracing import trace_sink

logger = logging.getLogger(__name__)


class TraceMiddleware(AgentMiddleware):
    """Persist per-model-call traces to the local JSONL sink."""

    def __init__(self, agent_name: str | None = None) -> None:
        super().__init__()
        self._agent_name = agent_name or "lead"

    # -- extraction helpers -------------------------------------------------

    @staticmethod
    def _run_id(request: ModelRequest) -> str:
        """Best-effort stable id for the run. thread_id is what every other
        subsystem keys on (checkpoints, channels/store.json), so traces join
        against them for free."""
        try:
            runtime = getattr(request, "runtime", None)
            ctx = getattr(runtime, "context", None) if runtime else None
            if ctx:
                tid = ctx.get("thread_id")
                if tid:
                    return str(tid)
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    @staticmethod
    def _model_name(request: ModelRequest) -> str:
        model = getattr(request, "model", None)
        for attr in ("model_name", "model", "name"):
            value = getattr(model, attr, None)
            if isinstance(value, str) and value:
                return value
        return type(model).__name__ if model is not None else "unknown"

    @staticmethod
    def _last_user_text(messages: list) -> str:
        for msg in reversed(messages or []):
            if getattr(msg, "type", "") == "human":
                return trace_sink.truncate(
                    _content_text(getattr(msg, "content", "")), trace_sink.MAX_PROMPT_CHARS
                )
        return ""

    def _describe_request(self, request: ModelRequest) -> dict:
        messages = list(getattr(request, "messages", []) or [])
        tools = getattr(request, "tools", None) or []
        tool_names = []
        for t in tools:
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if name:
                tool_names.append(str(name))
        system = getattr(request, "system_message", None)
        system_text = _content_text(getattr(system, "content", system)) if system else ""
        return {
            "message_count": len(messages),
            "system_chars": len(system_text),
            "tool_count": len(tool_names),
            "tools": tool_names[:60],
            "last_user": self._last_user_text(messages),
        }

    @staticmethod
    def _describe_response(response: Any) -> tuple[dict, dict]:
        """Return (output_summary, usage). Tolerates the several shapes a
        ModelResponse result can take across langchain versions."""
        out: dict = {"text": "", "tool_calls": [], "stop_reason": ""}
        usage: dict = {}

        result = getattr(response, "result", response)
        # `result` is usually a list of messages; sometimes a bare message.
        messages = result if isinstance(result, list) else [result]
        for msg in messages:
            if msg is None:
                continue
            content = getattr(msg, "content", None)
            if content is not None:
                text = _content_text(content)
                if text:
                    out["text"] = trace_sink.truncate(text)
            for call in getattr(msg, "tool_calls", None) or []:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
                out["tool_calls"].append(
                    {
                        "name": str(name or ""),
                        "args": trace_sink.truncate(args, trace_sink.MAX_TOOL_ARGS_CHARS),
                    }
                )
            meta = getattr(msg, "usage_metadata", None)
            if meta:
                usage = {
                    "input_tokens": meta.get("input_tokens"),
                    "output_tokens": meta.get("output_tokens"),
                    "total_tokens": meta.get("total_tokens"),
                }
            rmeta = getattr(msg, "response_metadata", None) or {}
            stop = rmeta.get("stop_reason") or rmeta.get("finish_reason")
            if stop:
                out["stop_reason"] = str(stop)
        return out, usage

    def _emit(
        self,
        request: ModelRequest,
        response: Any,
        started: float,
        error: BaseException | None = None,
    ) -> None:
        try:
            run_id = self._run_id(request)
            record = {
                "run_id": run_id,
                "step": trace_sink.next_step(run_id),
                "agent": self._agent_name,
                "model": self._model_name(request),
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "input": self._describe_request(request),
                "error": None,
            }
            if error is not None:
                record["error"] = f"{type(error).__name__}: {error}"
                record["output"] = {"text": "", "tool_calls": [], "stop_reason": "error"}
                record["usage"] = {}
            else:
                out, usage = self._describe_response(response)
                record["output"] = out
                record["usage"] = usage
            trace_sink.write(record)
        except Exception:  # noqa: BLE001 — never fail a run over a trace
            logger.debug("trace emit failed", exc_info=True)

    # -- middleware hooks ---------------------------------------------------

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if not trace_sink.enabled():
            return handler(request)
        started = time.monotonic()
        try:
            response = handler(request)
        except BaseException as e:
            self._emit(request, None, started, error=e)
            raise
        self._emit(request, response, started)
        return response

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if not trace_sink.enabled():
            return await handler(request)
        started = time.monotonic()
        try:
            response = await handler(request)
        except BaseException as e:
            self._emit(request, None, started, error=e)
            raise
        self._emit(request, response, started)
        return response


def _content_text(content: Any) -> str:
    """Flatten LangChain message content to plain text.

    Content is a str for simple turns but a list of typed blocks once thinking
    or tool use is involved — and with adaptive thinking always on for the
    Claude 5 models, the list shape is the common case here. Indexing [0] the
    way older code did lands on a ThinkingBlock and yields nothing.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)

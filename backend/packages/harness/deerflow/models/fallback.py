"""Model fallback wrapper.

Wraps a primary chat model plus an ordered list of fallback chat models so that
if the primary model fails at invocation time (e.g. Anthropic returns a 404
not_found for a decommissioned model, a 429 rate-limit, a 5xx, or an
"overloaded" error), the call automatically steps down to the next model in the
chain instead of surfacing an internal error.

Design notes
------------
* This is a real ``BaseChatModel`` subclass (not a ``RunnableWithFallbacks``)
  because ``langchain.agents.create_agent`` requires ``model.bind_tools(...)``
  and ``model.profile`` -- neither of which ``RunnableWithFallbacks`` exposes.
* The MAIN agent path calls ``bind_tools``. We bind tools to every model in the
  chain and then hand off to LangChain's battle-tested
  ``Runnable.with_fallbacks`` so streaming + retries are handled natively.
* The SECONDARY direct-invoke paths (summarization / title / memory / subagent
  middlewares call ``.invoke`` / ``.ainvoke`` without binding tools) are served
  by ``_generate`` / ``_agenerate``, which loop over the chain themselves.
* Any attribute not defined here (``model``, ``model_name``, ``model_id`` ...)
  is delegated to the primary model via ``__getattr__``.

Fallback semantics (P2-9)
-------------------------
Only *transient* failures and *model-404* step down the chain:
404 not_found (decommissioned model id), 429 rate-limit, >=500 (incl. 529
overloaded), and connection/timeout errors. Caller bugs (400 bad request,
422) and auth problems (401/403 — the same key is used down-chain, so a
fallback cannot help) surface immediately: replaying a 400 at the fallback
tier used to re-run our own bugs at Opus prices while masking them.

Retry-before-fallback ordering: the Anthropic SDK already retries 429/5xx/
connection errors against the SAME model with backoff (``max_retries``,
default 2) before the exception ever reaches this wrapper. So by the time we
see a transient error the primary has been retried; stepping down is the
correct next move, not another primary retry.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field

logger = logging.getLogger(__name__)


def _default_fallback_exceptions() -> tuple[type[BaseException], ...]:
    """Transient + model-404 exception classes that justify a step-down.

    Anthropic SDK mapping: NotFoundError=404, RateLimitError=429,
    InternalServerError=>500 (incl. 529 overloaded), APIConnectionError
    covers timeouts (APITimeoutError subclasses it). Deliberately excluded:
    BadRequestError(400), UnprocessableEntityError(422) — our bug, surface
    it; AuthenticationError(401)/PermissionDeniedError(403) — same key
    down-chain, fallback can't help.
    """
    try:
        import anthropic

        return (
            anthropic.NotFoundError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        )
    except ImportError:  # pragma: no cover - non-anthropic deployments
        return (ConnectionError, TimeoutError)


class FallbackChatModel(BaseChatModel):
    """A chat model that steps down to fallback models on failure."""

    primary: BaseChatModel
    fallbacks: list[BaseChatModel]
    # Exception types that trigger a step-down. Default: transient + 404 only
    # (see module docstring) — NOT (Exception,), which replayed caller bugs
    # at fallback-tier prices and masked them.
    exceptions_to_handle: tuple[type[BaseException], ...] = Field(
        default_factory=_default_fallback_exceptions
    )

    model_config = {"arbitrary_types_allowed": True}

    def model_post_init(self, __context: Any) -> None:
        # ``profile`` is a pydantic FIELD on the modern ``BaseChatModel`` (it can
        # be passed as ``ChatModel(..., profile=...)``). Declaring a plain
        # ``@property profile`` here collided with that field, so instance access
        # returned the descriptor object instead of a value — which broke
        # ``SummarizationMiddleware`` (it reads ``model.profile['max_input_tokens']``
        # to evaluate a fractional trigger). Copy the primary's profile onto this
        # instance so the wrapper is transparent to profile-reading middlewares.
        try:
            self.profile = self.primary.profile
        except Exception:  # pragma: no cover - defensive
            pass

    @property
    def _llm_type(self) -> str:
        return f"fallback:{getattr(self.primary, '_llm_type', 'chat')}"

    @property
    def _chain(self) -> list[BaseChatModel]:
        return [self.primary, *self.fallbacks]

    # -- Attribute delegation --------------------------------------
    def __getattr__(self, item: str) -> Any:
        # Only called when normal attribute lookup fails. Delegate unknown
        # attributes (model, model_name, model_id, etc.) to the primary model.
        if item.startswith("__") or item in ("primary", "fallbacks", "exceptions_to_handle"):
            raise AttributeError(item)
        primary = object.__getattribute__(self, "__dict__").get("primary")
        if primary is None:
            raise AttributeError(item)
        return getattr(primary, item)

    # -- Tool binding (main agent path) ----------------------------
    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable:
        bound_primary = self.primary.bind_tools(tools, **kwargs)
        bound_fallbacks = []
        for fb in self.fallbacks:
            try:
                bound_fallbacks.append(fb.bind_tools(tools, **kwargs))
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Fallback model %r could not bind tools: %s", getattr(fb, "model", fb), e)
        if not bound_fallbacks:
            return bound_primary
        return bound_primary.with_fallbacks(
            bound_fallbacks, exceptions_to_handle=self.exceptions_to_handle
        )

    # -- Direct invocation paths (no tools) ------------------------
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_exc: BaseException | None = None
        for idx, model in enumerate(self._chain):
            try:
                msg = model.invoke(messages, stop=stop, **kwargs)
                return ChatResult(generations=[ChatGeneration(message=msg)])
            except self.exceptions_to_handle as e:
                last_exc = e
                _log_stepdown(idx, model, e, self._chain)
        assert last_exc is not None
        raise last_exc

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_exc: BaseException | None = None
        for idx, model in enumerate(self._chain):
            try:
                msg = await model.ainvoke(messages, stop=stop, **kwargs)
                return ChatResult(generations=[ChatGeneration(message=msg)])
            except self.exceptions_to_handle as e:
                last_exc = e
                _log_stepdown(idx, model, e, self._chain)
        assert last_exc is not None
        raise last_exc


def _log_stepdown(idx: int, failed: BaseChatModel, exc: BaseException, chain: list[BaseChatModel]) -> None:
    failed_name = getattr(failed, "model", repr(failed))
    if idx + 1 < len(chain):
        next_name = getattr(chain[idx + 1], "model", repr(chain[idx + 1]))
        logger.warning(
            "Model %r failed (%s: %s) -- stepping down to %r",
            failed_name, type(exc).__name__, exc, next_name,
        )
    else:
        logger.error(
            "Model %r failed (%s: %s) and no fallback remains in the chain",
            failed_name, type(exc).__name__, exc,
        )

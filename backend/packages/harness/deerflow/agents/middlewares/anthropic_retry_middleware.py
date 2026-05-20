"""Middleware to retry transient Anthropic API errors with exponential backoff.

Wraps the model call and retries on transient failures: HTTP 500/502/503/504/529,
connection errors, request timeouts, and rate limits. Non-transient errors
(bad request, auth, etc.) propagate immediately.

Placed near the bottom of the middleware list so it sits closest to the actual
model invocation — outer middlewares' request mutations are preserved across
retries because we only re-invoke ``handler(request)`` itself.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


_TRANSIENT_STATUS_CODES = frozenset({500, 502, 503, 504, 529})


def _is_transient_error(exc: BaseException) -> bool:
    """True if ``exc`` is a transient Anthropic API error that warrants retry."""
    try:
        import anthropic
    except ImportError:
        return False

    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError, anthropic.InternalServerError)):
        return True

    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", None) in _TRANSIENT_STATUS_CODES

    return False


class AnthropicRetryMiddleware(AgentMiddleware[AgentState]):
    """Retries the wrapped model call on transient Anthropic API errors.

    Exponential backoff with jitter: delay = min(base * 2**attempt, max_delay) + uniform jitter.
    """

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 16.0) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def _compute_delay(self, attempt: int) -> float:
        backoff = min(self.base_delay * (2 ** attempt), self.max_delay)
        jitter = random.uniform(0, backoff * 0.25)
        return backoff + jitter

    def _log_retry(self, attempt: int, exc: BaseException, delay: float) -> None:
        logger.warning(
            "Transient Anthropic API error on attempt %d/%d: %s: %s. Retrying in %.2fs.",
            attempt + 1,
            self.max_retries + 1,
            type(exc).__name__,
            exc,
            delay,
        )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        for attempt in range(self.max_retries + 1):
            try:
                return handler(request)
            except Exception as exc:
                if not _is_transient_error(exc) or attempt == self.max_retries:
                    raise
                delay = self._compute_delay(attempt)
                self._log_retry(attempt, exc, delay)
                time.sleep(delay)
        # Unreachable — loop above always returns or raises
        raise RuntimeError("AnthropicRetryMiddleware: retry loop exited without return")

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        for attempt in range(self.max_retries + 1):
            try:
                return await handler(request)
            except Exception as exc:
                if not _is_transient_error(exc) or attempt == self.max_retries:
                    raise
                delay = self._compute_delay(attempt)
                self._log_retry(attempt, exc, delay)
                await asyncio.sleep(delay)
        raise RuntimeError("AnthropicRetryMiddleware: retry loop exited without return")

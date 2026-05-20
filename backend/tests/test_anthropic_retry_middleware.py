"""Tests for AnthropicRetryMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import anthropic

from deerflow.agents.middlewares.anthropic_retry_middleware import (
    AnthropicRetryMiddleware,
    _is_transient_error,
)


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=_request())


def _internal_server_error() -> anthropic.InternalServerError:
    return anthropic.InternalServerError("boom", response=_response(500), body=None)


def _bad_request_error() -> anthropic.BadRequestError:
    return anthropic.BadRequestError("nope", response=_response(400), body=None)


def _rate_limit_error() -> anthropic.RateLimitError:
    return anthropic.RateLimitError("slow down", response=_response(429), body=None)


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_request())


def _overloaded_error() -> anthropic.APIStatusError:
    """Simulate a 529 Overloaded — constructed as a generic APIStatusError."""
    return anthropic.APIStatusError("overloaded", response=_response(529), body=None)


def _service_unavailable_error() -> anthropic.APIStatusError:
    return anthropic.APIStatusError("unavailable", response=_response(503), body=None)


class TestIsTransientError:
    def test_internal_server_error_is_transient(self):
        assert _is_transient_error(_internal_server_error())

    def test_overloaded_529_is_transient(self):
        assert _is_transient_error(_overloaded_error())

    def test_service_unavailable_503_is_transient(self):
        assert _is_transient_error(_service_unavailable_error())

    def test_rate_limit_is_transient(self):
        assert _is_transient_error(_rate_limit_error())

    def test_connection_error_is_transient(self):
        assert _is_transient_error(_connection_error())

    def test_bad_request_is_not_transient(self):
        assert not _is_transient_error(_bad_request_error())

    def test_value_error_is_not_transient(self):
        assert not _is_transient_error(ValueError("oops"))


@pytest.fixture
def fast_mw(monkeypatch):
    """Middleware with zero backoff so tests don't actually sleep."""
    mw = AnthropicRetryMiddleware(max_retries=3, base_delay=0.0, max_delay=0.0)
    monkeypatch.setattr(mw, "_compute_delay", lambda attempt: 0.0)
    return mw


class TestWrapModelCallSync:
    def test_success_first_try_no_retry(self, fast_mw):
        handler = MagicMock(return_value="ok")
        result = fast_mw.wrap_model_call(MagicMock(), handler)
        assert result == "ok"
        assert handler.call_count == 1

    def test_retries_then_succeeds(self, fast_mw):
        handler = MagicMock(side_effect=[_internal_server_error(), _internal_server_error(), "ok"])
        result = fast_mw.wrap_model_call(MagicMock(), handler)
        assert result == "ok"
        assert handler.call_count == 3

    def test_gives_up_after_max_retries(self, fast_mw):
        handler = MagicMock(side_effect=_internal_server_error())
        with pytest.raises(anthropic.InternalServerError):
            fast_mw.wrap_model_call(MagicMock(), handler)
        # 1 initial + 3 retries = 4 attempts
        assert handler.call_count == 4

    def test_non_transient_error_not_retried(self, fast_mw):
        handler = MagicMock(side_effect=_bad_request_error())
        with pytest.raises(anthropic.BadRequestError):
            fast_mw.wrap_model_call(MagicMock(), handler)
        assert handler.call_count == 1


class TestAwrapModelCallAsync:
    @pytest.mark.anyio
    async def test_async_success_no_retry(self, fast_mw):
        handler = AsyncMock(return_value="ok")
        result = await fast_mw.awrap_model_call(MagicMock(), handler)
        assert result == "ok"
        assert handler.await_count == 1

    @pytest.mark.anyio
    async def test_async_retries_then_succeeds(self, fast_mw):
        handler = AsyncMock(side_effect=[_overloaded_error(), "ok"])
        result = await fast_mw.awrap_model_call(MagicMock(), handler)
        assert result == "ok"
        assert handler.await_count == 2

    @pytest.mark.anyio
    async def test_async_gives_up_after_max_retries(self, fast_mw):
        handler = AsyncMock(side_effect=_internal_server_error())
        with pytest.raises(anthropic.InternalServerError):
            await fast_mw.awrap_model_call(MagicMock(), handler)
        assert handler.await_count == 4

    @pytest.mark.anyio
    async def test_async_non_transient_error_not_retried(self, fast_mw):
        handler = AsyncMock(side_effect=_bad_request_error())
        with pytest.raises(anthropic.BadRequestError):
            await fast_mw.awrap_model_call(MagicMock(), handler)
        assert handler.await_count == 1


class TestComputeDelay:
    def test_exponential_growth_capped(self):
        mw = AnthropicRetryMiddleware(max_retries=5, base_delay=1.0, max_delay=8.0)
        # Lower bound = base*2**attempt (no jitter); upper = bound + 25% jitter.
        # Cap kicks in at attempt 3: 1*2**3 = 8 == max_delay.
        d0 = mw._compute_delay(0)
        d1 = mw._compute_delay(1)
        d4 = mw._compute_delay(4)
        assert 1.0 <= d0 <= 1.25
        assert 2.0 <= d1 <= 2.5
        # Capped: bound=8, +<=25% jitter
        assert 8.0 <= d4 <= 10.0

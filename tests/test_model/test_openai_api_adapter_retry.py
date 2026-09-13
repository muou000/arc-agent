"""Verify the ARC-level retry behaviour added to ``agents/model/openai_api_adapter.py``.

The model layer used to only normalize provider exceptions: a single exhausted
429 escaped as ``ARCModelAPIError`` and killed the running node. These tests pin
the new contract:

* transient failures (429/408/409/5xx, connection and timeout errors) are
  retried with exponential backoff, honoring ``Retry-After`` up to the cap;
* non-transient 4xx failures fail fast with the normalized error;
* quota/billing exhaustion fails fast even when the provider returns it as a
  429: the error text is deterministic, so retries would only burn backoff time;
* retries are configurable via ``ARC_MODEL_MAX_RETRIES`` and the delay env
  vars (``0`` restores the old no-retry behaviour);
* anything that is not a model API exception propagates unwrapped.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from agents.model import openai_api_adapter as adapter
from agents.model.openai_api_adapter import (
    ARCModelAPIError,
    _acall_model_with_retries,
    _call_model_with_retries,
    _resolve_retry_policy,
)


def _request() -> httpx.Request:
    return httpx.Request("POST", "http://model.test/v1/chat/completions")


def _status_error(status_code: int, *, headers: dict[str, str] | None = None) -> APIStatusError:
    response = httpx.Response(status_code, request=_request(), headers=headers or {})
    if status_code == 429:
        return RateLimitError("rate limited", response=response, body=None)
    return APIStatusError(f"HTTP {status_code}", response=response, body=None)


def _rate_limit_error(message: str, *, body: object | None = None) -> RateLimitError:
    response = httpx.Response(429, request=_request(), headers={})
    return RateLimitError(message, response=response, body=body)


def _connection_error() -> APIConnectionError:
    return APIConnectionError(message="connection refused", request=_request())


class _FlakyCall:
    """Zero-arg callable raising the queued exceptions before returning a value."""

    def __init__(self, exceptions: list[Exception], final: str = "ok") -> None:
        self._exceptions = list(exceptions)
        self._final = final
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._exceptions:
            raise self._exceptions.pop(0)
        return self._final


class _AsyncFlakyCall(_FlakyCall):
    """Awaitable variant used by the async retry loop."""

    async def __call__(self) -> str:  # type: ignore[override]
        return _FlakyCall.__call__(self)


@pytest.fixture(autouse=True)
def _deterministic_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove jitter so asserted delays are exact."""
    monkeypatch.setattr(adapter.random, "uniform", lambda low, high: high)


# ---------------------------------------------------------------------------
# Retry policy resolution
# ---------------------------------------------------------------------------


def test_resolve_retry_policy_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ARC_MODEL_MAX_RETRIES", "ARC_MODEL_RETRY_INITIAL_DELAY", "ARC_MODEL_RETRY_MAX_DELAY"):
        monkeypatch.delenv(name, raising=False)
    policy = _resolve_retry_policy()
    assert policy.max_retries == 3
    assert policy.initial_delay == 2.0
    assert policy.max_delay == 30.0


def test_resolve_retry_policy_honors_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "5")
    monkeypatch.setenv("ARC_MODEL_RETRY_INITIAL_DELAY", "0.5")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "9")
    policy = _resolve_retry_policy()
    assert policy.max_retries == 5
    assert policy.initial_delay == 0.5
    assert policy.max_delay == 9.0


def test_resolve_retry_policy_falls_back_on_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "banana")
    monkeypatch.setenv("ARC_MODEL_RETRY_INITIAL_DELAY", "soon")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "-5")
    policy = _resolve_retry_policy()
    assert policy.max_retries == 3
    assert policy.initial_delay == 2.0
    assert policy.max_delay == 30.0


# ---------------------------------------------------------------------------
# Retryable classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (_status_error(429), True),
        (_status_error(408), True),
        (_status_error(409), True),
        (_status_error(500), True),
        (_status_error(503), True),
        (_status_error(400), False),
        (_status_error(401), False),
        (_status_error(403), False),
        (_status_error(404), False),
        (_status_error(422), False),
        (_connection_error(), True),
        (APITimeoutError(_request()), True),
        (RuntimeError("not a model error"), False),
    ],
)
def test_is_retryable_model_api_exception(exc: Exception, retryable: bool) -> None:
    assert adapter._is_retryable_model_api_exception(exc) is retryable


@pytest.mark.parametrize(
    "message",
    [
        # OpenAI quota/billing exhaustion wording.
        "You exceeded your current quota, please check your plan and billing details",
        "Error code: 429 - {'error': {'code': 'insufficient_quota'}}",
        # Subscription/gateway usage limits returned as 429.
        "Monthly usage limit reached. Your plan will reset on 2026-10-01",
        "FreeUsageLimitError: enable available balance usage to continue",
        # Common gateway balance/budget wording.
        "Insufficient Balance: please top up your account",
        "Request failed: out of budget",
        "Quota exceeded for this subscription",
        # Explicit billing-limit/payment phrases, not generic billing mentions.
        "Billing hard limit reached: usage is blocked until the cycle resets",
        "Payment required to continue usage this month",
    ],
)
def test_quota_exhaustion_429_is_not_retryable(message: str) -> None:
    assert adapter._is_retryable_model_api_exception(_rate_limit_error(message)) is False


def test_quota_error_code_in_body_is_not_retryable() -> None:
    exc = _rate_limit_error(
        "Error code: 429",
        body={"error": {"message": "request failed", "code": "insufficient_quota"}},
    )
    assert adapter._is_retryable_model_api_exception(exc) is False


def test_error_as_string_in_body_is_not_retryable() -> None:
    exc = _rate_limit_error("Error code: 429", body={"error": "insufficient_quota"})
    assert adapter._is_retryable_model_api_exception(exc) is False


def test_error_as_list_in_body_is_not_retryable() -> None:
    exc = _rate_limit_error(
        "Error code: 429", body={"error": [{"message": "insufficient quota remaining"}]}
    )
    assert adapter._is_retryable_model_api_exception(exc) is False


def test_string_body_is_not_retryable() -> None:
    exc = _rate_limit_error("Error code: 429", body="insufficient_quota")
    assert adapter._is_retryable_model_api_exception(exc) is False


def test_quota_text_wins_over_retryable_status() -> None:
    response = httpx.Response(503, request=_request(), headers={})
    exc = APIStatusError("Service Unavailable: billing hard limit reached", response=response, body=None)
    assert adapter._is_retryable_model_api_exception(exc) is False


@pytest.mark.parametrize(
    "message",
    [
        # Transient throttle wording must not be mistaken for quota exhaustion.
        "Rate limit reached for gpt-4 on requests per minute (RPM): Limit 500, Used 500",
        "Too many requests, please slow down",
    ],
)
def test_transient_throttle_429_stays_retryable(message: str) -> None:
    assert adapter._is_retryable_model_api_exception(_rate_limit_error(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        # Generic billing mentions are not quota exhaustion.
        "Please update your billing email on file",
        "Billing details verified, no action needed",
    ],
)
def test_generic_billing_mention_stays_retryable(message: str) -> None:
    assert adapter._is_retryable_model_api_exception(_rate_limit_error(message)) is True


def test_throttle_language_wins_over_quota_text() -> None:
    exc = _rate_limit_error("Quota exceeded for requests per minute under your rate limit")
    assert adapter._is_retryable_model_api_exception(exc) is True


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


def test_sync_call_retries_transient_429_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    monkeypatch.setenv("ARC_MODEL_RETRY_INITIAL_DELAY", "2")
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(429), _status_error(429)])
    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert call.calls == 3
    assert sleeps == [2.0, 4.0]


def test_async_call_retries_transient_429_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    monkeypatch.setenv("ARC_MODEL_RETRY_INITIAL_DELAY", "2")
    sleeps: list[float] = []

    async def fake_asleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(adapter, "_asleep", fake_asleep)

    call = _AsyncFlakyCall([_status_error(429), _connection_error()])
    result = asyncio.run(
        _acall_model_with_retries(call, api_mode="chat_completions", model="test-model")
    )

    assert result == "ok"
    assert call.calls == 3
    assert sleeps == [2.0, 4.0]


def test_non_retryable_401_fails_fast_without_wrapping_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(401)])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert excinfo.value.status_code == 401
    assert call.calls == 1
    assert sleeps == []


def test_quota_429_fails_fast_without_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall(
        [_rate_limit_error("You exceeded your current quota, please check your plan and billing details")]
    )
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert excinfo.value.status_code == 429
    assert call.calls == 1
    assert sleeps == []


def test_async_quota_429_fails_fast_without_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    sleeps: list[float] = []

    async def fake_asleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(adapter, "_asleep", fake_asleep)

    call = _AsyncFlakyCall([_rate_limit_error("Monthly usage limit reached")])
    with pytest.raises(ARCModelAPIError) as excinfo:
        asyncio.run(_acall_model_with_retries(call, api_mode="chat_completions", model="test-model"))

    assert excinfo.value.status_code == 429
    assert call.calls == 1
    assert sleeps == []


def test_retry_exhaustion_raises_normalized_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "2")
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)

    call = _FlakyCall([_status_error(429)] * 10)
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert excinfo.value.status_code == 429
    assert call.calls == 3  # one original attempt plus two retries


def test_zero_max_retries_restores_no_retry_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")

    call = _FlakyCall([_status_error(429)] * 5)
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert excinfo.value.status_code == 429
    assert call.calls == 1


def test_non_model_exception_propagates_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _FlakyCall([ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        _call_model_with_retries(call, api_mode="chat_completions", model="test-model")
    assert call.calls == 1


def test_retry_after_header_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(429, headers={"retry-after": "7"})])
    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert sleeps == [7.0]


def test_retry_after_header_is_capped_at_max_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "10")
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(429, headers={"retry-after": "999"})])
    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert sleeps == [10.0]


def test_backoff_delay_is_capped_at_max_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "5")
    monkeypatch.setenv("ARC_MODEL_RETRY_INITIAL_DELAY", "8")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "20")
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(500)] * 5)
    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert call.calls == 6
    assert sleeps == [8.0, 16.0, 20.0, 20.0, 20.0]


# ---------------------------------------------------------------------------
# Wiring inside the ARC model classes
# ---------------------------------------------------------------------------


def test_arc_chat_openai_agenerate_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_openai import ChatOpenAI
    from langchain_core.outputs import ChatResult

    attempts = {"count": 0}

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _status_error(429)
        return ChatResult(generations=[])

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_agenerate)

    async def run() -> ChatResult:
        model = adapter.ARCChatOpenAI(
            model="test-model",
            api_key="test-key",
            arc_api_mode="chat_completions",
            arc_model_name="test-model",
        )
        return await model._agenerate([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert attempts["count"] == 2
    assert isinstance(result, ChatResult)


def test_arc_compatible_chat_openai_agenerate_retries_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.model.compatible_openai import CompatibleChatOpenAI
    from langchain_core.outputs import ChatResult

    attempts = {"count": 0}

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise _status_error(503)
        return ChatResult(generations=[])

    monkeypatch.setattr(CompatibleChatOpenAI, "_agenerate", fake_agenerate)

    async def run() -> ChatResult:
        model = adapter.ARCCompatibleChatOpenAI(
            model="test-model",
            api_key="test-key",
            arc_api_mode="responses",
            arc_model_name="test-model",
        )
        return await model._agenerate([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert attempts["count"] == 3
    assert isinstance(result, ChatResult)

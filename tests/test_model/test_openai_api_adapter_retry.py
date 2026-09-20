"""Verify the ARC-level retry behaviour added to ``agents/model/openai_api_adapter.py``.

The model layer used to only normalize provider exceptions: a single exhausted
429 escaped as ``ARCModelAPIError`` and killed the running node. These tests pin
the current contract:

* transient failures (429/408/409/5xx, connection and timeout errors) are
  retried with a short fixed delay, honoring ``Retry-After`` up to the cap;
* after a connection-class failure the retry waits on a cheap GET /models
  reachability probe instead of re-hanging until the full request timeout;
* non-transient 4xx failures fail fast with the normalized error;
* quota/billing exhaustion fails fast even when the provider returns it as a
  429: the error text is deterministic, so retries would only burn backoff time;
* retries are configurable via ``ARC_MODEL_MAX_RETRIES`` and the delay env
  vars (``0`` restores the old no-retry behaviour);
* a cross-call consecutive-failure budget (default 5) stops re-entering the
  retry chain against a dead endpoint; any success resets it;
* anything that is not a model API exception propagates unwrapped.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from langchain_core.messages import HumanMessage
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from agents.model import openai_api_adapter as adapter
from agents.model.openai_api_adapter import (
    ARCModelAPIError,
    _acall_model_with_retries,
    _call_model_with_retries,
    _resolve_retry_policy,
    probe_endpoint_reachable,
    reset_consecutive_failure_budget_for_tests,
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
def _reset_failure_budget() -> None:
    reset_consecutive_failure_budget_for_tests()
    yield
    reset_consecutive_failure_budget_for_tests()


@pytest.fixture(autouse=True)
def _always_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe outcomes are asserted in dedicated tests; elsewhere answer yes."""

    monkeypatch.setattr(adapter, "_endpoint_reachable", lambda base_url, api_key: True)

    async def fake_areachable(base_url: str, api_key: str) -> bool:
        return True

    monkeypatch.setattr(adapter, "_aendpoint_reachable", fake_areachable)


@pytest.fixture(autouse=True)
def _reset_streaming_support_cache() -> None:
    """The streaming-unsupported cache is process-global; isolate every test."""

    adapter.reset_streaming_support_cache_for_tests()
    yield
    adapter.reset_streaming_support_cache_for_tests()


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ARC_MODEL_MAX_RETRIES",
        "ARC_MODEL_RETRY_DELAY",
        "ARC_MODEL_RETRY_MAX_DELAY",
        "ARC_MODEL_MAX_CONSECUTIVE_FAILURES",
        "ARC_MODEL_TIMEOUT",
        "ARC_MODEL_CONNECT_TIMEOUT",
        "ARC_MODEL_STREAM_TRANSPORT",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Retry policy resolution
# ---------------------------------------------------------------------------


def test_resolve_retry_policy_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    policy = _resolve_retry_policy()
    assert policy.max_retries == 3
    assert policy.retry_delay == 5.0
    assert policy.max_delay == 60.0
    assert policy.max_consecutive_failures == 5


def test_resolve_retry_policy_honors_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "5")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "3")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "9")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")
    policy = _resolve_retry_policy()
    assert policy.max_retries == 5
    assert policy.retry_delay == 3.0
    assert policy.max_delay == 9.0
    assert policy.max_consecutive_failures == 2


def test_resolve_retry_policy_falls_back_on_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "banana")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "soon")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "-5")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "nope")
    policy = _resolve_retry_policy()
    assert policy.max_retries == 3
    assert policy.retry_delay == 5.0
    assert policy.max_delay == 60.0
    assert policy.max_consecutive_failures == 5


# ---------------------------------------------------------------------------
# Request timeout resolution
# ---------------------------------------------------------------------------


def test_request_timeout_defaults_to_sdk_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    timeout = adapter.resolve_model_request_timeout()
    assert timeout.connect == 15.0
    assert timeout.read == 600.0
    assert timeout.write == 600.0


def test_request_timeout_honors_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_TIMEOUT", "120")
    monkeypatch.setenv("ARC_MODEL_CONNECT_TIMEOUT", "5")
    timeout = adapter.resolve_model_request_timeout()
    assert timeout.connect == 5.0
    assert timeout.read == 120.0


def test_build_openai_chat_model_sets_timeout_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.model.openai_api_adapter import build_openai_chat_model, reset_model_cache_for_tests

    monkeypatch.setenv("ARC_MODEL_TIMEOUT", "120")
    reset_model_cache_for_tests()
    try:
        model = build_openai_chat_model(
            "test-model",
            api_mode="chat_completions",
            base_url="https://model.test/v1",
            api_key="test-key",
        )
        client = model.root_client
        assert client.timeout is not None
        assert client.timeout.read == 120.0
        assert client.max_retries == 0
    finally:
        reset_model_cache_for_tests()


def test_built_model_streams_with_usage_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The zero-cache billing blind spot: streamed chat.completions requests
    must ask for usage (stream_options.include_usage) or every call falls to
    tiktoken estimation whose cache_read is 0 by definition (arc-output4 was
    fully billed as zero-cache because of this)."""

    from agents.model.openai_api_adapter import build_openai_chat_model, reset_model_cache_for_tests

    monkeypatch.delenv("ARC_MODEL_STREAM_USAGE", raising=False)
    reset_model_cache_for_tests()
    try:
        model = build_openai_chat_model(
            "test-model",
            api_mode="chat_completions",
            base_url="https://model.test/v1",
            api_key="test-key",
        )
        assert model.stream_usage is True
        payload = model._get_request_payload(
            [HumanMessage("hi")], stream=True, stream_options={"include_usage": True}
        )
        assert payload["stream_options"] == {"include_usage": True}
    finally:
        reset_model_cache_for_tests()


def test_stream_usage_env_restores_pre_fix_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARC_MODEL_STREAM_USAGE=0 is the escape hatch for gateways that reject
    stream_options with a 4xx (which would otherwise mark the endpoint
    streaming-unsupported and lose the stream transport's idle-timeout
    protection)."""

    from agents.model.openai_api_adapter import build_openai_chat_model, reset_model_cache_for_tests

    monkeypatch.setenv("ARC_MODEL_STREAM_USAGE", "0")
    reset_model_cache_for_tests()
    try:
        model = build_openai_chat_model(
            "test-model",
            api_mode="chat_completions",
            base_url="https://model.test/v1",
            api_key="test-key",
        )
        assert model.stream_usage is False
        assert model._should_stream_usage() is False
    finally:
        reset_model_cache_for_tests()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", True),  # unset: the fix's default
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        (" Off ", False),  # surrounding whitespace + case folded
        # A typo or unrecognized value must not silently disable the fix
        # (default-on, same invalid->default convention as _env_int/_env_float;
        # check_config surfaces the typo as a doctor warning).
        ("flase", True),
        ("maybe", True),
    ],
)
def test_resolve_stream_usage_parse_matrix(raw: str, expected: bool) -> None:
    from agents.model.openai_api_adapter import resolve_stream_usage

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("ARC_MODEL_STREAM_USAGE", raw)
    try:
        assert resolve_stream_usage() is expected
    finally:
        monkeypatch.undo()


def test_responses_mode_streaming_never_sends_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Responses streaming path builds its payload from _astream_responses
    (which never reads stream_usage), and _construct_responses_api_payload does
    not strip unknown keys — so instance-level stream_usage must not leak a
    chat.completions-only option into a responses request."""

    from agents.model.openai_api_adapter import build_openai_chat_model, reset_model_cache_for_tests

    monkeypatch.delenv("ARC_MODEL_STREAM_USAGE", raising=False)
    reset_model_cache_for_tests()
    try:
        model = build_openai_chat_model(
            "test-model",
            api_mode="responses",
            base_url="https://model.test/v1",
            api_key="test-key",
        )
        assert model.stream_usage is True
        # The chat.completions branch of _should_stream_usage stays on...
        assert model._should_stream_usage() is True
        # ...but the payload the responses streaming path would send carries
        # no stream_options: _astream_responses builds from kwargs directly.
        payload = model._get_request_payload([HumanMessage("hi")], stream=True)
        assert "input" in payload and "messages" not in payload
        assert "stream_options" not in payload
    finally:
        reset_model_cache_for_tests()


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
# Reachability probe
# ---------------------------------------------------------------------------


def test_probe_reports_reachable_on_any_http_status() -> None:
    # Even a 401 answers the question this probe exists for: the endpoint's
    # TCP stack is alive. Only transport errors mean "unreachable".
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "auth"}))
    assert probe_endpoint_reachable(
        base_url="https://model.test/v1", api_key="k", transport=transport
    )


def test_probe_reports_unreachable_on_transport_error() -> None:
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    assert not probe_endpoint_reachable(
        base_url="https://model.test/v1", api_key="k", transport=transport
    )


def test_probe_targets_the_models_listing() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    assert probe_endpoint_reachable(
        base_url="https://model.test/v1", api_key="k", transport=transport
    )
    assert seen == ["/v1/models"]


def test_probe_without_custom_base_url_assumes_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # No OPENAI_API_BASE/OPENAI_BASE_URL configured: the probe would only
    # duplicate the real attempt against the official default endpoint.
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert probe_endpoint_reachable(base_url="", api_key="") is True


def test_connection_failure_triggers_probe_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection failure must not be re-attempted blind: the loop probes.
    A totally-down endpoint gives up after the probe-round cap, not after
    burning the full real-attempt budget."""

    probes: list[str] = []
    monkeypatch.setattr(
        adapter, "_endpoint_reachable", lambda base_url, api_key: probes.append(base_url) or False
    )
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)

    call = _FlakyCall([_connection_error()] * 10)
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(
            call,
            api_mode="chat_completions",
            model="test-model",
            base_url="https://model.test/v1",
            api_key="test-key",
        )

    # One real attempt, then probe rounds: cap re-probes plus the final
    # confirming probe whose failure crosses the cap and raises (no further
    # real calls at any point).
    assert call.calls == 1
    assert probes == ["https://model.test/v1"] * (adapter._PROBE_ROUNDS_PER_ATTEMPT + 1)
    assert "unreachable" in str(excinfo.value).lower()


def test_probe_rounds_do_not_consume_the_model_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the endpoint stays unreachable, only probes fly — no model calls,
    and the default retry budget is untouched by probe failures (a connection
    blip that recovers still gets its full budget of real retries)."""

    monkeypatch.setattr(adapter, "_endpoint_reachable", lambda base_url, api_key: False)
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    # Default policy (max_retries=3): one real failure, then probe rounds
    # until the probe cap. The real-attempt count must stay at 1.
    call = _FlakyCall([_connection_error()] * 10)
    with pytest.raises(ARCModelAPIError):
        _call_model_with_retries(
            call,
            api_mode="chat_completions",
            model="test-model",
            base_url="https://model.test/v1",
            api_key="test-key",
        )
    assert call.calls == 1
    # Post-failure delay plus one delay per probe round.
    assert sleeps == [5.0] * (1 + adapter._PROBE_ROUNDS_PER_ATTEMPT)


def test_probe_recovery_resumes_real_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the endpoint answers the probe again, the real call is retried —
    and a recovered probe round does not shorten the real-attempt budget."""

    # Probe answers: first round unreachable, then recovered, then (after the
    # second real failure) recovered again immediately.
    probe_answers = iter([False, True, True])
    monkeypatch.setattr(
        adapter, "_endpoint_reachable", lambda base_url, api_key: next(probe_answers)
    )
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")

    call = _FlakyCall([_connection_error(), _connection_error()])
    result = _call_model_with_retries(
        call,
        api_mode="chat_completions",
        model="test-model",
        base_url="https://model.test/v1",
        api_key="test-key",
    )

    assert result == "ok"
    # All four budgeted real attempts were available; three were needed.
    assert call.calls == 3
    # One delay after each real failure, one between the two probe rounds.
    assert sleeps == [5.0, 5.0, 5.0]


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


def test_sync_call_retries_transient_429_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "2")
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(429), _status_error(429)])
    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert call.calls == 3
    assert sleeps == [2.0, 2.0]


def test_async_call_retries_transient_429_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "2")
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
    assert sleeps == [2.0, 2.0]


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


def test_retry_delay_is_the_fixed_short_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "5")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "20")
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(500)] * 3)
    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert call.calls == 4
    # Fixed delay: no exponential growth between attempts.
    assert sleeps == [5.0, 5.0, 5.0]


def test_retry_delay_is_capped_at_max_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "30")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "20")
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)

    call = _FlakyCall([_status_error(500)])
    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert sleeps == [20.0]


# ---------------------------------------------------------------------------
# Cross-call consecutive-failure budget
# ---------------------------------------------------------------------------


def test_consecutive_failures_fail_fast_on_the_next_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default budget 5: after five consecutive failed calls, the sixth does
    not enter the retry loop at all."""

    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "5")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    for _ in range(5):
        call = _FlakyCall([_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_model_with_retries(
                call, api_mode="chat_completions", model="test-model", **endpoint
            )

    # Budget exhausted: the next call fails fast with the budget message,
    # without touching the model.
    call = _FlakyCall([_connection_error()])
    with pytest.raises(ARCModelAPIError, match="consecutive failed model calls"):
        _call_model_with_retries(call, api_mode="chat_completions", model="test-model", **endpoint)
    assert call.calls == 0


def test_success_resets_the_consecutive_failure_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    failing = _FlakyCall([_connection_error()])
    with pytest.raises(ARCModelAPIError):
        _call_model_with_retries(failing, api_mode="chat_completions", model="test-model", **endpoint)

    # A success in between resets the counter.
    ok = _FlakyCall([], final="ok")
    assert (
        _call_model_with_retries(ok, api_mode="chat_completions", model="test-model", **endpoint)
        == "ok"
    )

    failing2 = _FlakyCall([_connection_error()])
    with pytest.raises(ARCModelAPIError):
        _call_model_with_retries(failing2, api_mode="chat_completions", model="test-model", **endpoint)
    # Only one consecutive failure so far: this call still entered the loop.
    assert failing2.calls == 1


def test_zero_budget_disables_the_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "0")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    for _ in range(6):
        call = _FlakyCall([_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_model_with_retries(call, api_mode="chat_completions", model="test-model", **endpoint)
        assert call.calls == 1


def test_failure_budget_is_scoped_per_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")

    for _ in range(2):
        call = _FlakyCall([_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_model_with_retries(
                call,
                api_mode="chat_completions",
                model="test-model",
                base_url="https://a.test/v1",
                api_key="key-a",
            )

    # Same failure count on a different endpoint: unaffected.
    call = _FlakyCall([_connection_error()])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(
            call,
            api_mode="chat_completions",
            model="test-model",
            base_url="https://b.test/v1",
            api_key="key-b",
        )
    # Normalized per-attempt error, not the budget fail-fast message.
    assert "consecutive" not in str(excinfo.value)
    assert call.calls == 1


def test_endpoint_key_never_contains_the_raw_api_key() -> None:
    key = adapter._model_endpoint_key("m", "https://model.test/v1", "sk-secret")
    assert "sk-secret" not in key


def test_stale_failures_leave_the_recovery_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failures older than the recovery window stop counting, so a tripped
    breaker unblocks itself once the outage is plausibly over instead of
    failing calls forever, and stale entries do not leak into later runs
    of the same process."""

    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    for _ in range(2):
        call = _FlakyCall([_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_model_with_retries(call, api_mode="chat_completions", model="test-model", **endpoint)

    # Budget tripped: the next call fails fast without touching the model.
    tripped = _FlakyCall([_connection_error()])
    with pytest.raises(ARCModelAPIError, match="consecutive failed model calls"):
        _call_model_with_retries(tripped, api_mode="chat_completions", model="test-model", **endpoint)
    assert tripped.calls == 0

    # Age both failures past the recovery window: the breaker must release,
    # letting the call enter the loop again (and fail per-attempt, not via
    # the budget message).
    with adapter._CONSECUTIVE_FAILURES_LOCK:
        for ep_key, records in adapter._CONSECUTIVE_FAILURES.items():
            stale = adapter.time.monotonic() - (adapter._FAILURE_RECOVERY_WINDOW_SECONDS + 60.0)
            adapter._CONSECUTIVE_FAILURES[ep_key] = [(stale, exc) for _, exc in records]
    aged = _FlakyCall([_connection_error()])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(aged, api_mode="chat_completions", model="test-model", **endpoint)
    assert "consecutive" not in str(excinfo.value)
    assert aged.calls == 1


def test_endpoint_key_normalizes_explicit_and_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller passing the env credential explicitly and one relying on the
    fallback must share one failure counter (same endpoint identity)."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.test/v1")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)

    explicit = adapter._model_endpoint_key("m", "https://model.test/v1", "sk-env")
    via_env = adapter._model_endpoint_key("m", "", "")
    assert explicit == via_env


# ---------------------------------------------------------------------------
# Wiring inside the ARC model classes
# ---------------------------------------------------------------------------


def test_arc_chat_openai_agenerate_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_openai import ChatOpenAI
    from langchain_core.outputs import ChatResult

    # These tests exercise the plain-attempt retry chain; keep them off the
    # stream-first path so the unpatched _astream never touches the network.
    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "0")
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

    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "0")
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


# ---------------------------------------------------------------------------
# Streaming transport retry (gateway idle-timeout recovery)
# ---------------------------------------------------------------------------


class _TransportRecorder:
    """Records which transport served each attempt; plain fails, streamed answers."""

    def __init__(self, exceptions: list[Exception] | None = None) -> None:
        self.exceptions = list(exceptions or [])
        self.plain_calls = 0
        self.streamed_calls = 0

    def plain(self) -> str:
        self.plain_calls += 1
        if self.exceptions:
            raise self.exceptions.pop(0)
        return "plain-ok"

    def streamed(self) -> str:
        self.streamed_calls += 1
        if self.exceptions:
            raise self.exceptions.pop(0)
        return "streamed-ok"


class _AsyncTransportRecorder(_TransportRecorder):
    async def plain(self) -> str:  # type: ignore[override]
        return _TransportRecorder.plain(self)

    async def streamed(self) -> str:  # type: ignore[override]
        return _TransportRecorder.streamed(self)


def _clear_stream_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_MODEL_STREAM_TRANSPORT", raising=False)


def test_connection_failure_switches_next_attempt_to_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed failure mode: a non-streaming POST dies mid-generation on a
    gateway idle timeout (connection reset) while the endpoint stays reachable;
    re-issuing the same request over the streaming transport keeps SSE chunks
    flowing and survives."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder([_connection_error()])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
    )

    assert result == "streamed-ok"
    assert recorder.plain_calls == 1
    assert recorder.streamed_calls == 1


def test_streamed_retry_failure_falls_back_to_plain_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streamed attempt that still fails with a connection error must be
    followed by a plain attempt (alternate transports), and a non-connection
    failure inside a streamed attempt returns to plain attempts."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder([_connection_error(), _connection_error()])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
    )

    assert result == "plain-ok"
    assert recorder.plain_calls == 2
    assert recorder.streamed_calls == 1


def test_non_connection_failure_never_switches_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder([_status_error(429)])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
    )

    assert result == "plain-ok"
    assert recorder.plain_calls == 2
    assert recorder.streamed_calls == 0


def test_stream_transport_env_flag_forces_plain_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "0")
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder([_connection_error()])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
    )

    assert result == "plain-ok"
    assert recorder.plain_calls == 2
    assert recorder.streamed_calls == 0


def test_async_connection_failure_switches_to_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_stream_env(monkeypatch)

    async def fake_asleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_asleep", fake_asleep)
    recorder = _AsyncTransportRecorder([_connection_error()])

    async def run() -> str:
        return await _acall_model_with_retries(
            recorder.plain,
            api_mode="chat_completions",
            model="test-model",
            streamed_retry=recorder.streamed,
        )

    result = asyncio.run(run())

    assert result == "streamed-ok"
    assert recorder.plain_calls == 1
    assert recorder.streamed_calls == 1


def test_streamed_retry_without_hook_stays_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that predate the streaming hook (or a model class without a
    stream implementation) keep the original plain retry behaviour."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    call = _FlakyCall([_connection_error()])

    result = _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    assert result == "ok"
    assert call.calls == 2


def test_arc_chat_openai_uses_streaming_after_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end at the model-class layer in retry mode: the plain _agenerate
    dies on a connection error (the gateway idle-timeout signature), the retry
    loop re-issues the request through _astream, and the accumulated stream result
    is returned to the agent layer. The real ``agenerate_from_stream`` runs so
    the accumulated result is produced exactly as in production."""

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from langchain_openai import ChatOpenAI

    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "retry")

    async def fake_asleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_asleep", fake_asleep)
    calls = {"plain": 0, "streamed": 0}

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        calls["plain"] += 1
        raise _connection_error()

    async def fake_astream(self, messages, stop=None, run_manager=None, **kwargs):
        calls["streamed"] += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content="recovered"))

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_agenerate)
    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream)

    async def run() -> ChatResult:
        model = adapter.ARCChatOpenAI(
            model="test-model",
            api_key="test-key",
            arc_api_mode="chat_completions",
            arc_model_name="test-model",
        )
        return await model._agenerate([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert calls == {"plain": 1, "streamed": 1}
    assert result.generations[0].message.content == "recovered"


# ---------------------------------------------------------------------------
# Underlying-cause visibility in error messages
# ---------------------------------------------------------------------------


def test_short_error_text_includes_the_cause_chain() -> None:
    transport = httpx.ConnectError("[Errno 111] Connect call failed")
    sdk_error = APIConnectionError(message="Connection error.", request=_request())
    sdk_error.__cause__ = transport

    text = adapter._short_error_text(sdk_error)

    assert "Connection error." in text
    assert "caused by ConnectError" in text
    assert "[Errno 111] Connect call failed" in text


def test_short_error_text_handles_cycle_and_missing_cause() -> None:
    first = APIConnectionError(message="Connection error.", request=_request())
    second = httpx.ReadError("peer closed connection without response")
    first.__cause__ = second
    second.__cause__ = first  # defensive: a cycle must not loop forever

    text = adapter._short_error_text(first)
    assert "peer closed connection" in text

    bare = APIConnectionError(message="Connection error.", request=_request())
    assert adapter._short_error_text(bare) == "Connection error."


def test_raised_error_carries_the_cause_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    transport = httpx.ReadError("peer closed connection without response")
    sdk_error = APIConnectionError(message="Connection error.", request=_request())
    sdk_error.__cause__ = transport
    call = _FlakyCall([sdk_error])

    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_model_with_retries(call, api_mode="chat_completions", model="test-model")

    message = str(excinfo.value)
    assert "type=APIConnectionError" in message
    assert "caused by ReadError: peer closed connection" in message


# ---------------------------------------------------------------------------
# Stream-first transport (ARC_MODEL_STREAM_TRANSPORT=stream, the default)
# ---------------------------------------------------------------------------


def test_stream_first_serves_the_first_attempt_streamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode: the very first attempt already streams, so a gateway
    idle-timeout drop never gets a chance to kill the call."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder()

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
        stream_first=True,
    )

    assert result == "streamed-ok"
    assert recorder.plain_calls == 0
    assert recorder.streamed_calls == 1


def test_stream_first_client_error_falls_back_without_spending_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider answering the streamed request with 4xx does not support
    streaming: the loop must re-attempt plain immediately (no retry delay, no
    budget consumption) and remember the endpoint for the rest of the process."""

    _clear_stream_env(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)
    adapter.reset_streaming_support_cache_for_tests()
    recorder = _TransportRecorder([_status_error(400)])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="sf-model",
        base_url="https://sf.test/v1",
        streamed_retry=recorder.streamed,
        stream_first=True,
    )

    assert result == "plain-ok"
    assert recorder.streamed_calls == 1
    assert recorder.plain_calls == 1
    assert sleeps == []  # the fallback re-attempt is immediate

    # The endpoint is remembered: a second call goes plain from the start.
    recorder2 = _TransportRecorder()
    result2 = _call_model_with_retries(
        recorder2.plain,
        api_mode="chat_completions",
        model="sf-model",
        base_url="https://sf.test/v1",
        streamed_retry=recorder2.streamed,
        stream_first=True,
    )
    assert result2 == "plain-ok"
    assert recorder2.streamed_calls == 0
    assert recorder2.plain_calls == 1
    adapter.reset_streaming_support_cache_for_tests()


def test_stream_first_server_error_keeps_streaming_on_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx from the streamed attempt is transient: retry with the streamed
    transport still selected (unlike a connection failure, which alternates)."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder([_status_error(503)])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
        stream_first=True,
    )

    assert result == "streamed-ok"
    assert recorder.streamed_calls == 2
    assert recorder.plain_calls == 0


def test_stream_first_connection_failure_alternates_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection failure on the streamed first attempt probes, then
    alternates to plain - and back to streamed on a further connection
    failure."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder([_connection_error(), _connection_error()])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
        stream_first=True,
    )

    assert result == "streamed-ok"
    assert recorder.streamed_calls == 2
    assert recorder.plain_calls == 1


def test_stream_first_env_retry_mode_keeps_plain_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARC_MODEL_STREAM_TRANSPORT=retry restores the PR #44 behaviour: plain
    first, streaming only after a connection-class failure."""

    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "retry")
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    recorder = _TransportRecorder()

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
        stream_first=adapter.ARCChatOpenAI(
            model="t", api_key="k", arc_api_mode="chat_completions", arc_model_name="t"
        )._arc_should_stream_first(),
    )

    assert result == "plain-ok"
    assert recorder.plain_calls == 1
    assert recorder.streamed_calls == 0


def test_stream_first_mode_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARC_MODEL_STREAM_TRANSPORT=0: the model class reports no stream-first,
    so the first attempt is plain and stays plain."""

    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "0")
    model = adapter.ARCChatOpenAI(
        model="t", api_key="k", arc_api_mode="chat_completions", arc_model_name="t"
    )
    assert model._arc_should_stream_first() is False


def test_stream_first_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_stream_env(monkeypatch)
    model = adapter.ARCChatOpenAI(
        model="t", api_key="k", arc_api_mode="chat_completions", arc_model_name="t"
    )
    assert model._arc_should_stream_first() is True


def test_empty_stream_is_treated_as_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that closes without any generation chunk must retry (switching
    transport), not surface a bogus empty result. ``agenerate_from_stream``
    raises ValueError("No generations found in stream.") for such streams; the
    model-class streamed hook converts it into a connection-class error."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)

    def streamed() -> "ChatResult":
        # Simulate what the model-class hook does when the underlying
        # agenerate_from_stream raises its no-generations ValueError.
        raise adapter._arc_empty_stream_error()

    def plain() -> str:
        return "plain-ok"

    result = _call_model_with_retries(
        plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=streamed,
        stream_first=True,
    )

    assert result == "plain-ok"


def test_arc_chat_openai_streams_first_attempt_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-class wiring: with the default env, _agenerate's first attempt
    goes through _astream (via the streamed hook), no plain attempt needed."""

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from langchain_openai import ChatOpenAI

    _clear_stream_env(monkeypatch)
    adapter.reset_streaming_support_cache_for_tests()

    async def fake_asleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_asleep", fake_asleep)
    calls = {"plain": 0, "streamed": 0}

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        calls["plain"] += 1
        return ChatResult(generations=[])

    async def fake_astream(self, messages, stop=None, run_manager=None, **kwargs):
        calls["streamed"] += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content="first-attempt-streamed"))

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_agenerate)
    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream)

    async def run() -> ChatResult:
        model = adapter.ARCChatOpenAI(
            model="test-model",
            api_key="test-key",
            arc_api_mode="chat_completions",
            arc_model_name="test-model",
        )
        return await model._agenerate([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert calls == {"plain": 0, "streamed": 1}
    assert result.generations[0].message.content == "first-attempt-streamed"
    adapter.reset_streaming_support_cache_for_tests()


def test_arc_streamed_hook_converts_no_generations_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model-class streamed hook must translate the accumulator's
    ValueError into a connection-class error the retry loop understands."""

    from langchain_openai import ChatOpenAI

    async def fake_astream(self, messages, stop=None, run_manager=None, **kwargs):
        return
        yield  # pragma: no cover - empty async generator

    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream)

    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
    )

    import asyncio
    from openai import APIConnectionError

    async def run():
        try:
            await model._arc_streamed_agenerate(
                [{"role": "user", "content": "hi"}], stop=None, run_manager=None
            )
        except APIConnectionError as exc:
            return str(exc)
        return "no-error"

    message = asyncio.run(run())
    assert "without any generation chunks" in message


# ---------------------------------------------------------------------------
# Review fixes: streaming-unsupported TTL and empty-stream probe skip
# ---------------------------------------------------------------------------


def test_streaming_unsupported_mark_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx mark must not route the endpoint to plain forever: after the TTL
    the next call rediscovers streaming (a gateway misconfiguration can be
    transient, and a permanent mark would re-expose large-output turns to the
    idle-timeout drop for the rest of a long run)."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    adapter.reset_streaming_support_cache_for_tests()

    # First call: streamed attempt answers 4xx -> plain fallback + mark.
    recorder = _TransportRecorder([_status_error(400)])
    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="ttl-model",
        base_url="https://ttl.test/v1",
        streamed_retry=recorder.streamed,
        stream_first=True,
    )
    assert result == "plain-ok"

    # Still within the TTL: the mark holds, second call starts plain.
    recorder2 = _TransportRecorder()
    result2 = _call_model_with_retries(
        recorder2.plain,
        api_mode="chat_completions",
        model="ttl-model",
        base_url="https://ttl.test/v1",
        streamed_retry=recorder2.streamed,
        stream_first=True,
    )
    assert result2 == "plain-ok"
    assert recorder2.streamed_calls == 0

    # Age the mark past the TTL: streaming is retried.
    with adapter._STREAMING_UNSUPPORTED_LOCK:
        adapter._STREAMING_UNSUPPORTED[("ttl-model", "https://ttl.test/v1")] = (
            adapter.time.monotonic() - (adapter._STREAMING_UNSUPPORTED_TTL_SECONDS + 1.0)
        )
    recorder3 = _TransportRecorder()
    result3 = _call_model_with_retries(
        recorder3.plain,
        api_mode="chat_completions",
        model="ttl-model",
        base_url="https://ttl.test/v1",
        streamed_retry=recorder3.streamed,
        stream_first=True,
    )
    assert result3 == "streamed-ok"
    assert recorder3.streamed_calls == 1
    adapter.reset_streaming_support_cache_for_tests()


def test_empty_stream_skips_the_reachability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty stream proves the endpoint just answered with HTTP 200, so the
    retry must switch transport directly instead of waiting on probe rounds."""

    _clear_stream_env(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)
    probe_calls = {"count": 0}
    real_probe = adapter._endpoint_reachable

    def counting_probe(base_url: str, api_key: str) -> bool:
        probe_calls["count"] += 1
        return real_probe(base_url, api_key)

    monkeypatch.setattr(adapter, "_endpoint_reachable", counting_probe)

    empty_results = [adapter._arc_empty_stream_error()]

    def streamed() -> str:
        raise empty_results.pop(0)

    def plain() -> str:
        return "plain-ok"

    result = _call_model_with_retries(
        plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=streamed,
        stream_first=True,
    )

    assert result == "plain-ok"
    assert probe_calls["count"] == 0  # no probe round before the plain retry
    assert sleeps == [5.0]  # only the retry delay, no probe-wait sleeps


# ---------------------------------------------------------------------------
# StreamChunkTimeoutError: the streamed transport's inter-chunk gap watchdog
# ---------------------------------------------------------------------------


class _StreamChunkTimeoutError(TimeoutError):
    """Local stand-in for langchain_openai's exception (name-matched)."""

    def __init__(self, timeout_s: float = 120.0) -> None:
        super().__init__(f"No streaming chunk received for {timeout_s:.1f}s")
        self.timeout_s = timeout_s


# The adapter classifies the watchdog by class NAME (importing the symbol would
# pin one that older langchain_openai versions lack), so the stand-in must
# carry the production name.
_StreamChunkTimeoutError.__name__ = "StreamChunkTimeoutError"


def test_stream_chunk_timeout_detected_direct_and_wrapped() -> None:
    """The watchdog error is recognized both raw and wrapped in the ValueError
    that agenerate_from_stream re-raises mid-iteration; unrelated timeouts and
    other exceptions are not."""

    direct = _StreamChunkTimeoutError()
    wrapped = ValueError("No generations found in stream.")
    wrapped.__cause__ = _StreamChunkTimeoutError()
    plain_timeout = TimeoutError("unrelated asyncio timeout")

    assert adapter._is_stream_chunk_timeout(direct) is True
    assert adapter._is_stream_chunk_timeout(wrapped) is True
    assert adapter._is_stream_chunk_timeout(plain_timeout) is False
    assert adapter._is_stream_chunk_timeout(_connection_error()) is False
    assert adapter._is_stream_chunk_timeout(_status_error(500)) is False


def test_stream_first_chunk_timeout_switches_transport_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The online-run failure mode: a streamed attempt stalls between chunks.
    The loop must re-attempt over plain immediately — no probe round, no retry
    delay, no budget burn — instead of letting the error escape to the agent
    layer, whose ainvoke fallback would replay the whole session (and stream
    again, hitting the same stall)."""

    _clear_stream_env(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", sleeps.append)
    probe_calls = {"count": 0}
    monkeypatch.setattr(
        adapter, "_endpoint_reachable", lambda *args, **kwargs: probe_calls.__setitem__("count", probe_calls["count"] + 1) or True
    )
    recorder = _TransportRecorder([_StreamChunkTimeoutError()])

    result = _call_model_with_retries(
        recorder.plain,
        api_mode="chat_completions",
        model="test-model",
        streamed_retry=recorder.streamed,
        stream_first=True,
    )

    assert result == "plain-ok"
    assert recorder.streamed_calls == 1
    assert recorder.plain_calls == 1
    assert probe_calls["count"] == 0  # endpoint provably alive: no probe round
    assert sleeps == []  # transport switch is immediate


def test_async_stream_chunk_timeout_switches_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async variant: the chunk watchdog on a streamed attempt retries over
    plain on the same call, without probe rounds."""

    _clear_stream_env(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(adapter, "_asleep", sleeps.append)
    recorder = _AsyncTransportRecorder([_StreamChunkTimeoutError()])

    result = asyncio.run(
        _acall_model_with_retries(
            recorder.plain,
            api_mode="chat_completions",
            model="test-model",
            streamed_retry=recorder.streamed,
            stream_first=True,
        )
    )

    assert result == "plain-ok"
    assert recorder.streamed_calls == 1
    assert recorder.plain_calls == 1
    assert sleeps == []


def test_plain_attempt_chunk_timeout_shape_still_retries_via_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog-shaped error raised by a *plain* attempt (no streamed
    transport configured) is not an OpenAI/httpx exception, so the retry
    budget contract keeps its old behaviour: it propagates unwrapped instead
    of being silently retried forever."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)

    def plain() -> str:
        raise _StreamChunkTimeoutError()

    with pytest.raises(_StreamChunkTimeoutError):
        _call_model_with_retries(
            plain,
            api_mode="chat_completions",
            model="test-model",
        )


def test_resolve_stream_chunk_timeout_env_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARC_MODEL_STREAM_CHUNK_TIMEOUT: unset -> 90 (below the library's 120 so
    stalls surface earlier), explicit values pass through, 0 disables the
    watchdog, invalid/negative values fall back to the default."""

    cases = {
        "": 90.0,
        "45": 45.0,
        "0": None,
        "0.0": None,
        "300": 300.0,
        "abc": 90.0,
        "-5": 90.0,
    }
    for raw, expected in cases.items():
        monkeypatch.setenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", raw) if raw else monkeypatch.delenv(
            "ARC_MODEL_STREAM_CHUNK_TIMEOUT", raising=False
        )
        assert adapter.resolve_stream_chunk_timeout() == expected, f"env={raw!r}"


def test_resolve_stream_chunk_timeout_clamped_to_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog firing later than the request's read timeout could never
    trigger, so small ARC_MODEL_TIMEOUT configurations clamp the chunk
    timeout down instead of silently disabling it."""

    monkeypatch.delenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", raising=False)
    monkeypatch.setenv("ARC_MODEL_TIMEOUT", "30")
    assert adapter.resolve_stream_chunk_timeout() == 30.0

    monkeypatch.setenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", "60")
    assert adapter.resolve_stream_chunk_timeout() == 30.0

    monkeypatch.setenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", "0")  # explicit disable wins
    assert adapter.resolve_stream_chunk_timeout() is None

    monkeypatch.delenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", raising=False)
    monkeypatch.setenv("ARC_MODEL_TIMEOUT", "300")
    assert adapter.resolve_stream_chunk_timeout() == 90.0  # no clamp below default


def test_build_openai_chat_model_passes_stream_chunk_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The constructed ChatOpenAI carries ARC's chunk-timeout so the library
    default (120s, not tunable from ARC before) no longer governs stall
    detection."""

    adapter.reset_model_cache_for_tests()
    monkeypatch.delenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", raising=False)
    model = adapter.build_openai_chat_model("chunk-timeout-model", api_key="k")
    assert model.stream_chunk_timeout == 90.0

    monkeypatch.setenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", "33")
    adapter.reset_model_cache_for_tests()
    model33 = adapter.build_openai_chat_model("chunk-timeout-model-33", api_key="k")
    assert model33.stream_chunk_timeout == 33.0
    adapter.reset_model_cache_for_tests()


def test_successful_attempt_records_transport_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry loop publishes which transport answered and how many attempts
    it took, so llm_usage events can attribute latency per transport."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)

    token = adapter._last_call_meta.set(None)
    try:
        recorder = _TransportRecorder([_StreamChunkTimeoutError()])
        result = _call_model_with_retries(
            recorder.plain,
            api_mode="chat_completions",
            model="test-model",
            streamed_retry=recorder.streamed,
            stream_first=True,
        )
        assert result == "plain-ok"
        meta = adapter._last_call_meta.get()
        assert meta == {"transport": "plain", "attempts": 2}

        recorder2 = _TransportRecorder()
        result2 = _call_model_with_retries(
            recorder2.plain,
            api_mode="chat_completions",
            model="test-model",
            streamed_retry=recorder2.streamed,
            stream_first=True,
        )
        assert result2 == "streamed-ok"
        assert adapter._last_call_meta.get() == {"transport": "streamed", "attempts": 1}
    finally:
        adapter._last_call_meta.reset(token)


def test_repeated_chunk_timeouts_are_bounded_by_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both transports stalling must terminate through the retry budget,
    never spin in a zero-delay retry loop. The watchdog error is not an
    OpenAI/httpx exception, so it surfaces raw (the historical contract for
    non-model-API exceptions), but only after the budget is spent."""

    _clear_stream_env(monkeypatch)
    monkeypatch.setattr(adapter, "_sleep", lambda seconds: None)
    adapter.reset_consecutive_failure_budget_for_tests()
    # Every attempt on either transport raises the watchdog error.
    recorder = _TransportRecorder([_StreamChunkTimeoutError()] * 20)

    with pytest.raises(TimeoutError):
        _call_model_with_retries(
            recorder.plain,
            api_mode="chat_completions",
            model="test-model",
            streamed_retry=recorder.streamed,
            stream_first=True,
        )

    # 1 free switch + (max_retries + 1) budgeted attempts (the last one is
    # the attempt that trips the budget and raises) = 5 with defaults.
    max_retries = adapter._resolve_retry_policy().max_retries
    assert recorder.streamed_calls + recorder.plain_calls == 1 + max_retries + 1

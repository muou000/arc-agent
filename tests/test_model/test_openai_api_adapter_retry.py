"""Verify the single retry engine behind ARC's model call seam.

The model layer used to carry two parallel retry engines (a sync and an async
copy) and two copies of every model-class helper, and its tests imported the
private retry functions while swapping the module-level probe. These tests pin
the current contract through the public seam only — a fake ``ModelTransport``
drives the real engine, and the reachability probe / retry delay are injected
parameters:

* transient failures (429/408/409/5xx, connection and timeout errors) are
  retried with a short fixed delay, honoring ``Retry-After`` up to the cap;
* after a connection-class failure the retry waits on a cheap GET /models
  reachability probe instead of re-hanging until the full request timeout;
* non-transient 4xx failures fail fast with the normalized error;
* quota/billing exhaustion fails fast even when the provider returns it as a
  429: the error text is deterministic, so retries would only burn backoff;
* retries are configurable via ``ARC_MODEL_MAX_RETRIES`` and the delay env
  vars (``0`` restores the old no-retry behaviour);
* a cross-call consecutive-failure budget (default 5) stops re-entering the
  retry chain against a dead endpoint; any success resets it;
* every stream→plain fallback (trigger attempt + reason) is recorded on the
  returned ``ModelCallOutcome`` — the fallback decision is assertable at the
  seam without reaching into module internals;
* the sync entry point drives the same engine — no second implementation.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

import httpx
import pytest
from langchain_core.messages import HumanMessage
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from agents.model import openai_api_adapter as adapter
from agents.model.openai_api_adapter import (
    FALLBACK_CHUNK_TIMEOUT,
    FALLBACK_CLIENT_ERROR,
    FALLBACK_CONNECTION_FAILURE,
    ARCModelAPIError,
    ModelCallOutcome,
    ModelTransport,
    StreamFallback,
    acall_model_with_retries,
    call_model_with_retries,
    empty_stream_error,
    is_provider_outage_error,
    model_api_error_category,
    model_api_error_details,
    probe_endpoint_reachable,
    resolve_retry_policy,
    reset_consecutive_failure_budget_for_tests,
    reset_streaming_support_cache_for_tests,
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


class _Recorder:
    """Scripted two-transport fake.

    Each leg raises its queued exceptions before returning its value, and the
    per-leg call counts stay observable. Both legs are plain callables — the
    engine awaits non-awaitable results as-is, so the same fake serves the
    engine and both model classes; awaitable legs are covered explicitly in
    :func:`test_async_engine_awaits_the_streamed_leg`.
    """

    def __init__(
        self,
        plain_errors: list[Exception] | None = None,
        streamed_errors: list[Exception] | None = None,
        plain_value: object = "plain-ok",
        streamed_value: object = "streamed-ok",
    ) -> None:
        self._plain_errors = list(plain_errors or [])
        self._streamed_errors = list(streamed_errors or [])
        self.plain_value = plain_value
        self.streamed_value = streamed_value
        self.calls = {"plain": 0, "streamed": 0}

    def plain(self) -> object:
        self.calls["plain"] += 1
        if self._plain_errors:
            raise self._plain_errors.pop(0)
        return self.plain_value

    def streamed(self) -> object:
        self.calls["streamed"] += 1
        if self._streamed_errors:
            raise self._streamed_errors.pop(0)
        return self.streamed_value

    @property
    def transport(self) -> ModelTransport:
        return ModelTransport(plain=self.plain, streamed=self.streamed)


def _call_engine(recorder: _Recorder, *, plain_only: bool = False, **overrides: object):
    """Drive the real async engine with the fake transport and offline knobs.

    ``plain_only`` drops the streamed leg, mirroring transports that cannot
    stream — the plain-path tests then behave like the pre-seam plain-call
    tests instead of alternating transports on connection failures.
    """

    transport = (
        ModelTransport(plain=recorder.plain, streamed=None) if plain_only else recorder.transport
    )
    kwargs: dict[str, object] = {
        "api_mode": "chat_completions",
        "model": "test-model",
        "base_url": "https://model.test/v1",
        "api_key": "test-key",
        "prober": lambda base_url, api_key: True,
        "sleeper": lambda seconds: None,
    }
    kwargs.update(overrides)
    return asyncio.run(acall_model_with_retries(transport, **kwargs))


@pytest.fixture(autouse=True)
def _reset_engine_state() -> Iterator[None]:
    """The failure budget and the streaming-support cache are process-global."""

    reset_consecutive_failure_budget_for_tests()
    reset_streaming_support_cache_for_tests()
    yield
    reset_consecutive_failure_budget_for_tests()
    reset_streaming_support_cache_for_tests()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic policy defaults: ambient env must not skew the engine.

    The OPENAI_* endpoint vars are cleared too: model-class tests without an
    explicit base_url hit the engine's default prober, and an ambient
    OPENAI_API_BASE would turn that instant reachability answer into a real
    network call.
    """

    for name in (
        "ARC_MODEL_MAX_RETRIES",
        "ARC_MODEL_RETRY_DELAY",
        "ARC_MODEL_RETRY_MAX_DELAY",
        "ARC_MODEL_MAX_CONSECUTIVE_FAILURES",
        "ARC_MODEL_TIMEOUT",
        "ARC_MODEL_CONNECT_TIMEOUT",
        "ARC_MODEL_STREAM_TRANSPORT",
        "ARC_MODEL_STREAM_CHUNK_TIMEOUT",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Retry policy resolution
# ---------------------------------------------------------------------------


def test_resolve_retry_policy_defaults() -> None:
    policy = resolve_retry_policy()
    assert policy.max_retries == 3
    assert policy.retry_delay == 5.0
    assert policy.max_delay == 60.0
    assert policy.max_consecutive_failures == 5


def test_resolve_retry_policy_honors_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "5")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "3")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "9")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")
    policy = resolve_retry_policy()
    assert policy.max_retries == 5
    assert policy.retry_delay == 3.0
    assert policy.max_delay == 9.0
    assert policy.max_consecutive_failures == 2


def test_resolve_retry_policy_falls_back_on_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "banana")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "soon")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "-5")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "nope")
    policy = resolve_retry_policy()
    assert policy.max_retries == 3
    assert policy.retry_delay == 5.0
    assert policy.max_delay == 60.0
    assert policy.max_consecutive_failures == 5


# ---------------------------------------------------------------------------
# Request timeout resolution
# ---------------------------------------------------------------------------


def test_request_timeout_defaults_to_sdk_semantics() -> None:
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
    ("raw", "expected", "warns"),
    [
        ("", True, False),  # unset: the fix's default, not a config mistake
        ("1", True, False),
        ("true", True, False),
        ("yes", True, False),
        ("on", True, False),
        ("0", False, False),
        ("false", False, False),
        ("no", False, False),
        ("off", False, False),
        (" Off ", False, False),  # surrounding whitespace + case folded
        # A typo or unrecognized value must not silently disable the fix
        # (default-on, same invalid->default convention as the env int/float
        # parsers) but is logged once; check_config surfaces it too.
        ("flase", True, True),
        ("maybe", True, True),
    ],
)
def test_resolve_stream_usage_parse_matrix(raw: str, expected: bool, warns: bool) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("ARC_MODEL_STREAM_USAGE", raw)
    adapter._ENV_WARNED.clear()
    try:
        with caplog_context(adapter) as records:
            assert adapter.resolve_stream_usage() is expected
            # A repeat resolve (every model build) must not re-log the typo.
            adapter.resolve_stream_usage()
        warning_records = [r for r in records if r.levelno >= logging.WARNING]
        assert len(warning_records) == (1 if warns else 0)
    finally:
        adapter._ENV_WARNED.clear()
        monkeypatch.undo()


@contextlib.contextmanager
def caplog_context(module):
    """Capture this module's logger output without pytest's caplog fixture
    (the parse matrix drives resolve directly, not through a test function
    that can request it)."""

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    capture = _Capture()
    module_logger = logging.getLogger(module.__name__)
    module_logger.addHandler(capture)
    try:
        yield capture.records
    finally:
        module_logger.removeHandler(capture)


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
# Retryable classification, driven through the real engine
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
    ],
)
def test_engine_retries_transient_and_fails_fast_otherwise(
    exc: Exception, retryable: bool
) -> None:
    """Classification is asserted by behaviour: a retryable failure comes back
    for a second attempt (after the reachability probe for connection-class
    errors), a non-retryable one stops after one attempt."""

    recorder = _Recorder(plain_errors=[exc])
    if retryable:
        result = _call_engine(recorder, plain_only=True)
        assert result.value == "plain-ok"
        assert recorder.calls["plain"] == 2
        return
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, plain_only=True)
    assert excinfo.value.status_code == getattr(exc, "status_code", None)
    assert recorder.calls["plain"] == 1


def test_non_model_exception_propagates_unwrapped() -> None:
    recorder = _Recorder(plain_errors=[ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        _call_engine(recorder)
    assert recorder.calls["plain"] == 1


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
def test_quota_exhaustion_429_fails_fast_in_engine(message: str) -> None:
    recorder = _Recorder(plain_errors=[_rate_limit_error(message)])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder)
    assert excinfo.value.status_code == 429
    assert recorder.calls["plain"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "request failed", "code": "insufficient_quota"}},
        {"error": "insufficient_quota"},
        {"error": [{"message": "insufficient quota remaining"}]},
        "insufficient_quota",
    ],
)
def test_quota_error_in_body_fails_fast_in_engine(body: object) -> None:
    recorder = _Recorder(plain_errors=[_rate_limit_error("Error code: 429", body=body)])
    with pytest.raises(ARCModelAPIError):
        _call_engine(recorder)
    assert recorder.calls["plain"] == 1


def test_quota_text_wins_over_retryable_status_in_engine() -> None:
    response = httpx.Response(503, request=_request(), headers={})
    exc = APIStatusError("Service Unavailable: billing hard limit reached", response=response, body=None)
    recorder = _Recorder(plain_errors=[exc])
    with pytest.raises(ARCModelAPIError):
        _call_engine(recorder)
    assert recorder.calls["plain"] == 1


@pytest.mark.parametrize(
    "message",
    [
        # Transient throttle wording must not be mistaken for quota exhaustion.
        "Rate limit reached for gpt-4 on requests per minute (RPM): Limit 500, Used 500",
        "Too many requests, please slow down",
        # Generic billing mentions are not quota exhaustion.
        "Please update your billing email on file",
        "Billing details verified, no action needed",
        # Throttle language wins over quota text.
        "Quota exceeded for requests per minute under your rate limit",
    ],
)
def test_transient_throttle_429_is_retried_in_engine(message: str) -> None:
    recorder = _Recorder(plain_errors=[_rate_limit_error(message)])
    result = _call_engine(recorder)
    assert result.value == "plain-ok"
    assert recorder.calls["plain"] == 2


# ---------------------------------------------------------------------------
# Reachability probe (the public probe helper keeps its transport injection)
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


# ---------------------------------------------------------------------------
# Probe rounds inside the engine (injected fake prober, no module swaps)
# ---------------------------------------------------------------------------

# The engine's probe-round cap per real attempt (module constant); a totally
# down endpoint is abandoned after one real attempt + cap rounds.
_PROBE_ROUNDS = 3


def test_connection_failure_triggers_probe_rounds() -> None:
    """A connection failure must not be re-attempted blind: the loop probes.
    A totally-down endpoint gives up after the probe-round cap, not after
    burning the full real-attempt budget."""

    probed: list[str] = []

    def prober(base_url: str, api_key: str) -> bool:
        probed.append(base_url)
        return False

    recorder = _Recorder(plain_errors=[_connection_error()] * 10)
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, prober=prober)

    # One real attempt, then probe rounds: cap re-probes plus the final
    # confirming probe whose failure crosses the cap and raises (no further
    # real calls at any point).
    assert recorder.calls["plain"] == 1
    assert probed == ["https://model.test/v1"] * (_PROBE_ROUNDS + 1)
    assert "unreachable" in str(excinfo.value).lower()


def test_probe_rounds_do_not_consume_the_model_attempt_budget() -> None:
    """While the endpoint stays unreachable, only probes fly — no model calls,
    and the default retry budget is untouched by probe failures (a connection
    blip that recovers still gets its full budget of real retries)."""

    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_connection_error()] * 10)
    with pytest.raises(ARCModelAPIError):
        _call_engine(recorder, prober=lambda base_url, api_key: False, sleeper=sleeps.append)
    assert recorder.calls["plain"] == 1
    # Post-failure delay plus one delay per probe round.
    assert sleeps == [5.0] * (1 + _PROBE_ROUNDS)


def test_probe_recovery_resumes_real_attempts() -> None:
    """After the endpoint answers the probe again, the real call is retried —
    and a recovered probe round does not shorten the real-attempt budget."""

    answers = iter([False, True, True])
    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_connection_error(), _connection_error()])
    result = _call_engine(
        recorder,
        plain_only=True,
        prober=lambda base_url, api_key: next(answers),
        sleeper=sleeps.append,
    )

    assert result.value == "plain-ok"
    # All four budgeted real attempts were available; three were needed.
    assert recorder.calls["plain"] == 3
    # One delay after each real failure, one between the two probe rounds.
    assert sleeps == [5.0, 5.0, 5.0]


# ---------------------------------------------------------------------------
# Retry loop (async engine)
# ---------------------------------------------------------------------------


def test_async_engine_retries_transient_429_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "2")
    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_status_error(429), _status_error(429)])
    result = _call_engine(recorder, sleeper=sleeps.append)

    assert result.value == "plain-ok"
    assert recorder.calls["plain"] == 3
    assert sleeps == [2.0, 2.0]


def test_sync_engine_entry_drives_the_same_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync entry point is a thin adapter over the async engine: same
    retry sequence, same outcome shape, no second implementation."""

    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "2")
    recorder = _Recorder(plain_errors=[_status_error(429), _status_error(429)])
    result = call_model_with_retries(
        recorder.transport,
        api_mode="chat_completions",
        model="test-model",
        base_url="https://model.test/v1",
        api_key="test-key",
        sleeper=lambda seconds: None,
        prober=lambda base_url, api_key: True,
    )

    assert result.value == "plain-ok"
    assert recorder.calls["plain"] == 3
    assert result.outcome == ModelCallOutcome(
        transport="plain", attempts=3, stream_fallbacks=()
    )


def test_sync_engine_entry_runs_from_inside_a_running_loop() -> None:
    """Sync callers on a loop thread (streaming runtimes) are served by the
    background engine loop instead of crashing inside asyncio.run."""

    async def main():
        return call_model_with_retries(
            ModelTransport(plain=lambda: "plain-ok"),
            api_mode="chat_completions",
            model="loop-model",
            base_url="https://loop.test/v1",
            api_key="test-key",
            sleeper=lambda seconds: None,
            prober=lambda base_url, api_key: True,
        )

    result = asyncio.run(main())
    assert result.value == "plain-ok"
    assert result.outcome.attempts == 1


def test_non_retryable_401_fails_fast_without_wrapping_delay() -> None:
    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_status_error(401)])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, sleeper=sleeps.append)

    assert excinfo.value.status_code == 401
    assert recorder.calls["plain"] == 1
    assert sleeps == []


def test_quota_429_fails_fast_without_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "3")
    sleeps: list[float] = []
    recorder = _Recorder(
        plain_errors=[
            _rate_limit_error("You exceeded your current quota, please check your plan and billing details")
        ]
    )
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, sleeper=sleeps.append)

    assert excinfo.value.status_code == 429
    assert recorder.calls["plain"] == 1
    assert sleeps == []


def test_retry_exhaustion_raises_normalized_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "2")
    recorder = _Recorder(plain_errors=[_status_error(429)] * 10)
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder)

    assert excinfo.value.status_code == 429
    assert recorder.calls["plain"] == 3  # one original attempt plus two retries


def test_zero_max_retries_restores_no_retry_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    recorder = _Recorder(plain_errors=[_status_error(429)] * 5)
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder)

    assert excinfo.value.status_code == 429
    assert recorder.calls["plain"] == 1


def test_retry_after_header_is_honored() -> None:
    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_status_error(429, headers={"retry-after": "7"})])
    result = _call_engine(recorder, sleeper=sleeps.append)

    assert result.value == "plain-ok"
    assert sleeps == [7.0]


def test_retry_after_header_is_capped_at_max_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "10")
    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_status_error(429, headers={"retry-after": "999"})])
    result = _call_engine(recorder, sleeper=sleeps.append)

    assert result.value == "plain-ok"
    assert sleeps == [10.0]


def test_retry_delay_is_the_fixed_short_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "5")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "20")
    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_status_error(500)] * 3)
    result = _call_engine(recorder, sleeper=sleeps.append)

    assert result.value == "plain-ok"
    assert recorder.calls["plain"] == 4
    # Fixed delay: no exponential growth between attempts.
    assert sleeps == [5.0, 5.0, 5.0]


def test_retry_delay_is_capped_at_max_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "30")
    monkeypatch.setenv("ARC_MODEL_RETRY_MAX_DELAY", "20")
    sleeps: list[float] = []
    recorder = _Recorder(plain_errors=[_status_error(500)])
    result = _call_engine(recorder, sleeper=sleeps.append)

    assert result.value == "plain-ok"
    assert sleeps == [20.0]


# ---------------------------------------------------------------------------
# Cross-call consecutive-failure budget
# ---------------------------------------------------------------------------


def test_consecutive_failures_fail_fast_on_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default budget 5: after five consecutive failed calls, the sixth does
    not enter the retry loop at all."""

    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "5")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    for _ in range(5):
        recorder = _Recorder(plain_errors=[_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_engine(recorder, **endpoint)

    # Budget exhausted: the next call fails fast with the budget message,
    # without touching the model.
    recorder = _Recorder(plain_errors=[_connection_error()])
    with pytest.raises(ARCModelAPIError, match="consecutive failed model calls"):
        _call_engine(recorder, **endpoint)
    assert recorder.calls["plain"] == 0


def test_success_resets_the_consecutive_failure_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    recorder = _Recorder(plain_errors=[_connection_error()])
    with pytest.raises(ARCModelAPIError):
        _call_engine(recorder, **endpoint)

    # A success in between resets the counter.
    ok = _Recorder()
    assert _call_engine(ok, **endpoint).value == "plain-ok"

    recorder2 = _Recorder(plain_errors=[_connection_error()])
    with pytest.raises(ARCModelAPIError):
        _call_engine(recorder2, **endpoint)
    # Only one consecutive failure so far: this call still entered the loop.
    assert recorder2.calls["plain"] == 1


def test_zero_budget_disables_the_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "0")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    for _ in range(6):
        recorder = _Recorder(plain_errors=[_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_engine(recorder, **endpoint)
        assert recorder.calls["plain"] == 1


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_authentication_and_rate_limit_failures_do_not_trip_outage_budget(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    """Only endpoint reachability failures feed the cross-call outage budget.

    Authentication and throttling are provider responses, not evidence that
    the endpoint is unreachable. They must keep their own error semantics even
    when the outage budget is configured to trip after one failure.
    """

    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "1")
    endpoint = {"base_url": "https://model.test/v1", "api_key": "test-key"}

    recorder = _Recorder(plain_errors=[_status_error(status_code)])
    with pytest.raises(ARCModelAPIError):
        _call_engine(recorder, **endpoint)

    second = _Recorder(plain_errors=[_status_error(status_code)])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(second, **endpoint)
    assert "consecutive failed model calls" not in str(excinfo.value)
    assert second.calls["plain"] == 1


@pytest.mark.parametrize("status_code", [400, 401, 429, 503])
def test_provider_responses_with_outage_word_are_not_reclassified_as_endpoint_outage(
    status_code: int,
) -> None:
    error = ARCModelAPIError(
        "endpoint unreachable was reported by the provider response",
        api_mode="chat_completions",
        model="test-model",
        status_code=status_code,
        error_type="provider_response",
    )

    assert not is_provider_outage_error(error)
    assert model_api_error_category(error) != "provider_outage"


def test_outage_details_use_canonical_default_endpoint_without_leaking_credentials() -> None:
    error = ARCModelAPIError(
        "Model API endpoint unreachable; Bearer sk-secret-value",
        api_mode="chat_completions",
        model="test-model",
        error_type="EndpointUnreachable",
    )

    details = model_api_error_details(error)

    assert details["base_url"] == "https://api.openai.com/v1"
    assert details["provider"] == "api.openai.com"
    assert "sk-secret-value" not in details["message"]


def test_failure_budget_is_scoped_per_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")

    for _ in range(2):
        recorder = _Recorder(plain_errors=[_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_engine(
                recorder,
                base_url="https://a.test/v1",
                api_key="key-a",
            )

    # Same failure count on a different endpoint: unaffected.
    recorder = _Recorder(plain_errors=[_connection_error()])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, base_url="https://b.test/v1", api_key="key-b")
    # Normalized per-attempt error, not the budget fail-fast message.
    assert "consecutive" not in str(excinfo.value)
    assert recorder.calls["plain"] == 1


def test_fail_fast_message_never_contains_the_raw_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "1")

    recorder = _Recorder(plain_errors=[_connection_error()])
    with pytest.raises(ARCModelAPIError):
        _call_engine(recorder)

    recorder2 = _Recorder()
    with pytest.raises(ARCModelAPIError, match="consecutive failed model calls") as excinfo:
        _call_engine(recorder2)
    assert "test-key" not in str(excinfo.value)


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
        recorder = _Recorder(plain_errors=[_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_engine(recorder, **endpoint)

    # Budget tripped: the next call fails fast without touching the model.
    tripped = _Recorder(plain_errors=[_connection_error()])
    with pytest.raises(ARCModelAPIError, match="consecutive failed model calls"):
        _call_engine(tripped, **endpoint)
    assert tripped.calls["plain"] == 0

    # Age both failures past the recovery window: the breaker must release,
    # letting the call enter the loop again (and fail per-attempt, not via
    # the budget message).
    with adapter._CONSECUTIVE_FAILURES_LOCK:
        for ep_key, records in adapter._CONSECUTIVE_FAILURES.items():
            stale = adapter.time.monotonic() - (adapter._FAILURE_RECOVERY_WINDOW_SECONDS + 60.0)
            adapter._CONSECUTIVE_FAILURES[ep_key] = [(stale, exc) for _, exc in records]
    aged = _Recorder(plain_errors=[_connection_error()])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(aged, **endpoint)
    assert "consecutive" not in str(excinfo.value)
    assert aged.calls["plain"] == 1


def test_endpoint_key_normalizes_explicit_and_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller passing the env credential explicitly and one relying on the
    fallback must share one failure counter (same endpoint identity)."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.test/v1")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    monkeypatch.setenv("ARC_MODEL_MAX_CONSECUTIVE_FAILURES", "2")

    for _ in range(2):
        recorder = _Recorder(plain_errors=[_connection_error()])
        with pytest.raises(ARCModelAPIError):
            _call_engine(recorder, api_key="sk-env")

    # The same identity reached with the env fallback (empty explicit key):
    # the budget is already tripped.
    recorder = _Recorder()
    with pytest.raises(ARCModelAPIError, match="consecutive failed model calls"):
        _call_engine(recorder, api_key="")


# ---------------------------------------------------------------------------
# Wiring inside the ARC model classes (injected fake transport, no patching)
# ---------------------------------------------------------------------------


def test_arc_chat_openai_agenerate_retries_transient_failures() -> None:
    from langchain_core.outputs import ChatResult

    errors = [_status_error(429), _status_error(503)]
    calls = {"count": 0}

    def plain() -> ChatResult:
        calls["count"] += 1
        if errors:
            raise errors.pop(0)
        return ChatResult(generations=[])

    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
        arc_transport=ModelTransport(plain=plain),
    )
    result = asyncio.run(model._agenerate([{"role": "user", "content": "hi"}]))
    assert calls["count"] == 3
    assert isinstance(result, ChatResult)


def test_arc_compatible_chat_openai_agenerate_retries_transient_failures() -> None:
    from langchain_core.outputs import ChatResult

    errors = [_status_error(503), _status_error(503)]
    calls = {"count": 0}

    def plain() -> ChatResult:
        calls["count"] += 1
        if errors:
            raise errors.pop(0)
        return ChatResult(generations=[])

    model = adapter.ARCCompatibleChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="responses",
        arc_model_name="test-model",
        arc_transport=ModelTransport(plain=plain),
    )
    result = asyncio.run(model._agenerate([{"role": "user", "content": "hi"}]))
    assert calls["count"] == 3
    assert isinstance(result, ChatResult)


def test_arc_chat_openai_generate_sync_path_retries_via_the_same_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync model-class path plugs its sync transport into the same engine
    (delay env at 0 keeps the test off the real clock)."""

    from langchain_core.outputs import ChatResult

    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "0")
    calls = {"count": 0}

    def plain() -> ChatResult:
        calls["count"] += 1
        if calls["count"] == 1:
            raise _status_error(429)
        return ChatResult(generations=[])

    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
        arc_transport=ModelTransport(plain=plain),
    )
    result = model._generate([{"role": "user", "content": "hi"}])
    assert calls["count"] == 2
    assert isinstance(result, ChatResult)


# ---------------------------------------------------------------------------
# Streaming transport retry (gateway idle-timeout recovery)
# ---------------------------------------------------------------------------


def test_connection_failure_switches_next_attempt_to_streaming() -> None:
    """The observed failure mode: a non-streaming POST dies mid-generation on a
    gateway idle timeout (connection reset) while the endpoint stays reachable;
    re-issuing the same request over the streaming transport keeps SSE chunks
    flowing and survives."""

    recorder = _Recorder(plain_errors=[_connection_error()])
    result = _call_engine(recorder)

    assert result.value == "streamed-ok"
    assert recorder.calls == {"plain": 1, "streamed": 1}
    # plain→streamed is a transport switch, not a stream fallback.
    assert result.outcome.stream_fallbacks == ()
    assert result.outcome == ModelCallOutcome(transport="streamed", attempts=2)


def test_async_engine_awaits_the_streamed_leg() -> None:
    """The engine must await awaitable transport legs (the async model class
    hands it coroutines)."""

    recorder = _Recorder(plain_errors=[_connection_error()])

    async def streamed() -> str:
        recorder.calls["streamed"] += 1
        return "streamed-ok"

    transport = ModelTransport(plain=recorder.plain, streamed=streamed)
    result = asyncio.run(
        acall_model_with_retries(
            transport,
            api_mode="chat_completions",
            model="test-model",
            base_url="https://model.test/v1",
            api_key="test-key",
            prober=lambda base_url, api_key: True,
            sleeper=lambda seconds: None,
        )
    )

    assert result.value == "streamed-ok"
    assert recorder.calls == {"plain": 1, "streamed": 1}


def test_streamed_retry_failure_falls_back_to_plain_attempts() -> None:
    """A streamed attempt that still fails with a connection error must be
    followed by a plain attempt (alternate transports), and the fallback is
    recorded with its trigger and reason."""

    recorder = _Recorder(plain_errors=[_connection_error()], streamed_errors=[_connection_error()])
    result = _call_engine(recorder)

    assert result.value == "plain-ok"
    assert recorder.calls == {"plain": 2, "streamed": 1}
    assert result.outcome.stream_fallbacks == (
        StreamFallback(FALLBACK_CONNECTION_FAILURE, 2),
    )


def test_non_connection_failure_never_switches_transport() -> None:
    recorder = _Recorder(plain_errors=[_status_error(429)])
    result = _call_engine(recorder)

    assert result.value == "plain-ok"
    assert recorder.calls == {"plain": 2, "streamed": 0}
    assert result.outcome.stream_fallbacks == ()


def test_stream_transport_env_off_strips_even_injected_stream_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARC_MODEL_STREAM_TRANSPORT=0 is authoritative: the streamed leg is
    stripped from every transport, injected or not, so no alternation can
    reintroduce streaming (the model is built without a base_url, so the
    engine's default prober answers instantly without network)."""

    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "0")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "0")
    recorder = _Recorder(plain_errors=[_connection_error()] * 10)
    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
        arc_transport=recorder.transport,
    )

    # Connection failures probe (instantly reachable: no base_url) and then
    # re-attempt plain until the retry budget is spent; streaming never
    # rejoins because the env switch stripped the streamed leg.
    with pytest.raises(ARCModelAPIError):
        asyncio.run(model._agenerate([{"role": "user", "content": "hi"}]))
    assert recorder.calls["plain"] == 1 + resolve_retry_policy().max_retries
    assert recorder.calls["streamed"] == 0


def _plain_only_engine_call(recorder: _Recorder):
    return asyncio.run(
        acall_model_with_retries(
            ModelTransport(plain=recorder.plain, streamed=None),
            api_mode="chat_completions",
            model="test-model",
            base_url="https://model.test/v1",
            api_key="test-key",
            prober=lambda base_url, api_key: True,
            sleeper=lambda seconds: None,
        )
    )


def test_transport_without_stream_leg_retries_plain() -> None:
    recorder = _Recorder(plain_errors=[_connection_error()])
    result = _plain_only_engine_call(recorder)

    assert result.value == "plain-ok"
    assert recorder.calls == {"plain": 2, "streamed": 0}


# ---------------------------------------------------------------------------
# Stream-first transport (ARC_MODEL_STREAM_TRANSPORT=stream, the default)
# ---------------------------------------------------------------------------


def test_stream_first_serves_the_first_attempt_streamed() -> None:
    """Default mode: the very first attempt already streams, so a gateway
    idle-timeout drop never gets a chance to kill the call."""

    recorder = _Recorder()
    result = _call_engine(recorder, stream_first=True)

    assert result.value == "streamed-ok"
    assert recorder.calls == {"plain": 0, "streamed": 1}
    assert result.outcome == ModelCallOutcome(transport="streamed", attempts=1)


def test_stream_first_client_error_falls_back_without_spending_budget() -> None:
    """A provider answering the streamed request with 4xx does not support
    streaming: the loop must re-attempt plain immediately (no retry delay, no
    budget consumption) and remember the endpoint for the rest of the process.
    The fallback decision — trigger attempt and reason — lands on the outcome."""

    sleeps: list[float] = []
    recorder = _Recorder(streamed_errors=[_status_error(400)])
    result = _call_engine(
        recorder, stream_first=True, model="sf-model", sleeper=sleeps.append
    )

    assert result.value == "plain-ok"
    assert recorder.calls == {"plain": 1, "streamed": 1}
    assert sleeps == []  # the fallback re-attempt is immediate
    assert result.outcome.stream_fallbacks == (
        StreamFallback(FALLBACK_CLIENT_ERROR, 1),
    )

    # The endpoint is remembered: a second call goes plain from the start.
    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True, model="sf-model")
    assert result2.value == "plain-ok"
    assert recorder2.calls["streamed"] == 0
    assert result2.outcome.stream_fallbacks == ()


def test_streaming_unsupported_mark_expires_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx mark must not route the endpoint to plain forever: after the TTL
    the next call rediscovers streaming (a gateway misconfiguration can be
    transient, and a permanent mark would re-expose large-output turns to the
    idle-timeout drop for the rest of a long run)."""

    recorder = _Recorder(streamed_errors=[_status_error(400)])
    result = _call_engine(
        recorder, stream_first=True, model="ttl-model"
    )
    assert result.value == "plain-ok"
    assert result.outcome.stream_fallbacks == (StreamFallback(FALLBACK_CLIENT_ERROR, 1),)

    # Still within the TTL: the mark holds, second call starts plain.
    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True, model="ttl-model")
    assert result2.value == "plain-ok"
    assert recorder2.calls["streamed"] == 0

    # Shrink the TTL to zero: the mark expires immediately and streaming is
    # retried.
    monkeypatch.setattr(adapter, "_STREAMING_UNSUPPORTED_TTL_SECONDS", 0.0)
    recorder3 = _Recorder()
    result3 = _call_engine(recorder3, stream_first=True, model="ttl-model")
    assert result3.value == "streamed-ok"
    assert recorder3.calls["streamed"] == 1
    assert result3.outcome.stream_fallbacks == ()


# ---------------------------------------------------------------------------
# Streamed 4xx classification: only capability-proof statuses mark the cache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 404, 405, 415, 422])
def test_stream_first_capability_status_still_falls_back_and_marks(
    status_code: int,
) -> None:
    """400/404/405/415/422 prove the endpoint rejects the streamed request
    shape itself: immediate plain re-attempt, FALLBACK_CLIENT_ERROR, and the
    streaming-unsupported mark (later calls start plain)."""

    recorder = _Recorder(streamed_errors=[_status_error(status_code)])
    result = _call_engine(recorder, stream_first=True, model=f"cap-{status_code}")

    assert result.value == "plain-ok"
    assert recorder.calls == {"plain": 1, "streamed": 1}
    assert result.outcome.stream_fallbacks == (StreamFallback(FALLBACK_CLIENT_ERROR, 1),)

    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True, model=f"cap-{status_code}")
    assert result2.value == "plain-ok"
    assert recorder2.calls["streamed"] == 0


@pytest.mark.parametrize("status_code", [401, 403])
def test_stream_first_auth_status_fails_fast_without_mark(status_code: int) -> None:
    """401/403 say nothing about streaming capability: no plain re-attempt
    (which would bypass auth handling with a duplicate request), no
    FALLBACK_CLIENT_ERROR, no cache mark — the normalized auth error raises."""

    recorder = _Recorder(streamed_errors=[_status_error(status_code)])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, stream_first=True, model=f"auth-{status_code}")

    assert excinfo.value.status_code == status_code
    assert recorder.calls == {"plain": 0, "streamed": 1}

    # No mark: a later call still tries streaming first.
    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True, model=f"auth-{status_code}")
    assert result2.value == "streamed-ok"
    assert recorder2.calls["streamed"] == 1


@pytest.mark.parametrize("status_code", [408, 409])
def test_stream_first_transient_status_retries_streamed_without_mark(
    status_code: int,
) -> None:
    """408/409 are transient classes the generic classification already owns:
    retry on the streamed transport after the policy delay — no plain switch,
    no cache mark."""

    sleeps: list[float] = []
    recorder = _Recorder(streamed_errors=[_status_error(status_code)])
    result = _call_engine(
        recorder, stream_first=True, model=f"transient-{status_code}", sleeper=sleeps.append
    )

    assert result.value == "streamed-ok"
    assert recorder.calls == {"plain": 0, "streamed": 2}
    assert sleeps == [5.0]  # the generic classification's fixed retry delay
    assert result.outcome.stream_fallbacks == ()

    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True, model=f"transient-{status_code}")
    assert result2.value == "streamed-ok"
    assert recorder2.calls["streamed"] == 1


def test_stream_first_429_retries_streamed_with_retry_after() -> None:
    """A streamed 429 must keep the plain path's Retry-After semantics: wait
    the advertised delay, retry streamed (no duplicate plain request fired
    past the throttle), no cache mark."""

    sleeps: list[float] = []
    recorder = _Recorder(streamed_errors=[_status_error(429, headers={"retry-after": "7"})])
    result = _call_engine(
        recorder, stream_first=True, model="throttle-stream", sleeper=sleeps.append
    )

    assert result.value == "streamed-ok"
    assert recorder.calls == {"plain": 0, "streamed": 2}
    assert sleeps == [7.0]
    assert result.outcome.stream_fallbacks == ()

    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True, model="throttle-stream")
    assert result2.value == "streamed-ok"
    assert recorder2.calls["streamed"] == 1


def test_stream_first_quota_429_fails_fast_without_mark() -> None:
    """A streamed 429 carrying quota-exhaustion text is deterministic: fail
    fast exactly like the plain path — no plain re-attempt, no cache mark."""

    recorder = _Recorder(
        streamed_errors=[
            _rate_limit_error("Error code: 429 - {'error': {'code': 'insufficient_quota'}}")
        ]
    )
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, stream_first=True, model="quota-stream")

    assert excinfo.value.status_code == 429
    assert recorder.calls == {"plain": 0, "streamed": 1}

    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True, model="quota-stream")
    assert result2.value == "streamed-ok"
    assert recorder2.calls["streamed"] == 1


def test_stream_first_server_error_keeps_streaming_on_retries() -> None:
    """A 5xx from the streamed attempt is transient: retry with the streamed
    transport still selected (unlike a connection failure, which alternates)."""

    recorder = _Recorder(streamed_errors=[_status_error(503)])
    result = _call_engine(recorder, stream_first=True)

    assert result.value == "streamed-ok"
    assert recorder.calls == {"plain": 0, "streamed": 2}
    assert result.outcome.stream_fallbacks == ()


def test_stream_first_connection_failure_alternates_transports() -> None:
    """A connection failure on the streamed first attempt probes, then
    alternates to plain — and back to streamed on a further connection
    failure. Only the stream→plain direction is a recorded fallback."""

    recorder = _Recorder(plain_errors=[_connection_error()], streamed_errors=[_connection_error()])
    result = _call_engine(recorder, stream_first=True)

    assert result.value == "streamed-ok"
    assert recorder.calls == {"plain": 1, "streamed": 2}
    assert result.outcome.stream_fallbacks == (
        StreamFallback(FALLBACK_CONNECTION_FAILURE, 1),
    )


def test_stream_first_env_retry_mode_keeps_plain_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARC_MODEL_STREAM_TRANSPORT=retry restores the PR #44 behaviour: plain
    first, streaming only after a connection-class failure. Asserted through
    the model class so the env→engine mapping is what is under test."""

    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "retry")
    recorder = _Recorder()
    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
        arc_transport=recorder.transport,
    )
    result = asyncio.run(model._agenerate([{"role": "user", "content": "hi"}]))

    assert result == "plain-ok"
    assert recorder.calls == {"plain": 1, "streamed": 0}


def test_arc_chat_openai_uses_streaming_after_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end at the model-class layer in retry mode: the plain langchain
    generate dies on a connection error (the gateway idle-timeout signature),
    the engine re-issues the request through the real streamed helper
    (accumulating _astream), and the accumulated stream result is returned to
    the agent layer exactly as in production."""

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk, ChatResult
    from langchain_openai import ChatOpenAI

    monkeypatch.setenv("ARC_MODEL_STREAM_TRANSPORT", "retry")
    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "0")
    calls = {"plain": 0, "streamed": 0}

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        calls["plain"] += 1
        raise _connection_error()

    async def fake_astream(self, messages, stop=None, run_manager=None, **kwargs):
        calls["streamed"] += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content="recovered"))

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_agenerate)
    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream)

    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
    )
    result = asyncio.run(model._agenerate([{"role": "user", "content": "hi"}]))
    assert calls == {"plain": 1, "streamed": 1}
    assert result.generations[0].message.content == "recovered"


def test_arc_chat_openai_streams_first_attempt_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-class wiring: with the default env, _agenerate's first attempt
    goes through the real streamed helper, and the plain langchain generate is
    never reached."""

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk, ChatResult
    from langchain_openai import ChatOpenAI

    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "0")
    calls = {"plain": 0, "streamed": 0}

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        calls["plain"] += 1
        return ChatResult(generations=[])

    async def fake_astream(self, messages, stop=None, run_manager=None, **kwargs):
        calls["streamed"] += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content="first-attempt-streamed"))

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_agenerate)
    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream)

    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
    )
    result = asyncio.run(model._agenerate([{"role": "user", "content": "hi"}]))
    assert calls == {"plain": 0, "streamed": 1}
    assert result.generations[0].message.content == "first-attempt-streamed"


def test_empty_stream_via_the_real_streamed_helper_recovers_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real streamed helper must translate the accumulator's "No
    generations" ValueError into a connection-class error the engine
    understands: an empty SSE body must not surface as a bogus empty result —
    the call recovers over plain instead."""

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_openai import ChatOpenAI

    monkeypatch.setenv("ARC_MODEL_RETRY_DELAY", "0")

    async def fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessageChunk(content="plain-recovery"))]
        )

    async def fake_astream(self, messages, stop=None, run_manager=None, **kwargs):
        return
        yield  # pragma: no cover - empty async generator

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_agenerate)
    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream)

    model = adapter.ARCChatOpenAI(
        model="test-model",
        api_key="test-key",
        arc_api_mode="chat_completions",
        arc_model_name="test-model",
    )
    result = asyncio.run(model._agenerate([{"role": "user", "content": "hi"}]))
    assert result.generations[0].message.content == "plain-recovery"


def test_empty_stream_is_treated_as_connection_failure() -> None:
    """A stream that closes without any generation chunk must retry (switching
    transport), not surface a bogus empty result. The streamed leg raises the
    contract's ``empty_stream_error``; the engine skips the reachability probe
    (the endpoint just answered HTTP 200) and switches directly."""

    sleeps: list[float] = []
    probes = {"count": 0}

    def prober(base_url: str, api_key: str) -> bool:
        probes["count"] += 1
        return True

    def streamed() -> object:
        raise empty_stream_error()

    transport = ModelTransport(plain=lambda: "plain-ok", streamed=streamed)
    result = asyncio.run(
        acall_model_with_retries(
            transport,
            api_mode="chat_completions",
            model="test-model",
            base_url="https://model.test/v1",
            api_key="test-key",
            stream_first=True,
            sleeper=sleeps.append,
            prober=prober,
        )
    )

    assert result.value == "plain-ok"
    assert probes["count"] == 0  # no probe round before the plain retry
    assert sleeps == [5.0]  # only the retry delay, no probe-wait sleeps
    assert result.outcome.stream_fallbacks == (
        StreamFallback(FALLBACK_CONNECTION_FAILURE, 1),
    )


# ---------------------------------------------------------------------------
# Underlying-cause visibility in error messages
# ---------------------------------------------------------------------------


def test_raised_error_carries_the_cause_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    transport_error = httpx.ReadError("peer closed connection without response")
    sdk_error = APIConnectionError(message="Connection error.", request=_request())
    sdk_error.__cause__ = transport_error

    recorder = _Recorder(plain_errors=[sdk_error])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder)

    message = str(excinfo.value)
    assert "type=APIConnectionError" in message
    assert "caused by ReadError: peer closed connection" in message


def test_cyclic_cause_chain_does_not_hang_the_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    first = APIConnectionError(message="Connection error.", request=_request())
    second = httpx.ReadError("peer closed connection without response")
    first.__cause__ = second
    second.__cause__ = first  # defensive: a cycle must not loop forever

    recorder = _Recorder(plain_errors=[first])
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, plain_only=True)
    assert "peer closed connection" in str(excinfo.value)


def test_bare_error_message_has_no_cause_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_MODEL_MAX_RETRIES", "0")
    recorder = _Recorder(
        plain_errors=[APIConnectionError(message="Connection error.", request=_request())]
    )
    with pytest.raises(ARCModelAPIError) as excinfo:
        _call_engine(recorder, plain_only=True)
    assert "error=Connection error." in str(excinfo.value)


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


def test_stream_first_chunk_timeout_switches_transport_without_probe() -> None:
    """The online-run failure mode: a streamed attempt stalls between chunks.
    The loop must re-attempt over plain immediately — no probe round, no retry
    delay, no budget burn — instead of letting the error escape to the agent
    layer, whose ainvoke fallback would replay the whole session (and stream
    again, hitting the same stall)."""

    sleeps: list[float] = []
    probes = {"count": 0}

    def prober(base_url: str, api_key: str) -> bool:
        probes["count"] += 1
        return True

    recorder = _Recorder(streamed_errors=[_StreamChunkTimeoutError()])
    result = _call_engine(recorder, stream_first=True, sleeper=sleeps.append, prober=prober)

    assert result.value == "plain-ok"
    assert recorder.calls == {"plain": 1, "streamed": 1}
    assert probes["count"] == 0  # endpoint provably alive: no probe round
    assert sleeps == []  # transport switch is immediate
    assert result.outcome.stream_fallbacks == (
        StreamFallback(FALLBACK_CHUNK_TIMEOUT, 1),
    )


def test_async_engine_chunk_timeout_switches_transport() -> None:
    """Async engine on awaitable legs: the chunk watchdog on a streamed attempt
    retries over plain on the same call, without probe rounds."""

    calls = {"plain": 0, "streamed": 0}
    sleeps: list[float] = []

    async def plain() -> str:
        calls["plain"] += 1
        return "plain-ok"

    async def streamed() -> str:
        calls["streamed"] += 1
        raise _StreamChunkTimeoutError()

    transport = ModelTransport(plain=plain, streamed=streamed)
    result = asyncio.run(
        acall_model_with_retries(
            transport,
            api_mode="chat_completions",
            model="test-model",
            base_url="https://model.test/v1",
            api_key="test-key",
            stream_first=True,
            sleeper=sleeps.append,
            prober=lambda base_url, api_key: True,
        )
    )

    assert result.value == "plain-ok"
    assert calls == {"plain": 1, "streamed": 1}
    assert sleeps == []


def test_wrapped_chunk_timeout_is_still_classified() -> None:
    """``agenerate_from_stream`` re-raises mid-iteration errors wrapped in
    ValueError; the engine must still see the watchdog in ``__cause__`` and
    take the transport-switch path."""

    wrapped = ValueError("No generations found in stream.")
    wrapped.__cause__ = _StreamChunkTimeoutError()
    recorder = _Recorder(streamed_errors=[wrapped])
    result = _call_engine(recorder, stream_first=True)

    assert result.value == "plain-ok"
    assert recorder.calls == {"plain": 1, "streamed": 1}
    assert result.outcome.stream_fallbacks == (
        StreamFallback(FALLBACK_CHUNK_TIMEOUT, 1),
    )


def test_plain_attempt_chunk_timeout_shape_still_propagates_raw() -> None:
    """A watchdog-shaped error raised by a *plain* attempt (no streamed
    transport configured) is not an OpenAI/httpx exception, so the retry
    budget contract keeps its old behaviour: it propagates unwrapped instead
    of being silently retried forever."""

    recorder = _Recorder(plain_errors=[_StreamChunkTimeoutError()])
    with pytest.raises(_StreamChunkTimeoutError):
        _call_engine(recorder)
    assert recorder.calls["plain"] == 1


def test_repeated_chunk_timeouts_are_bounded_by_the_retry_budget() -> None:
    """Both transports stalling must terminate through the retry budget,
    never spin in a zero-delay retry loop. The watchdog error is not an
    OpenAI/httpx exception, so it surfaces raw (the historical contract for
    non-model-API exceptions), but only after the budget is spent."""

    watchdogs = [_StreamChunkTimeoutError()] * 20
    recorder = _Recorder(plain_errors=list(watchdogs), streamed_errors=list(watchdogs))
    with pytest.raises(TimeoutError):
        _call_engine(recorder, stream_first=True)

    # 1 free switch + (max_retries + 1) budgeted attempts (the last one is
    # the attempt that trips the budget and raises) = 5 with defaults.
    max_retries = resolve_retry_policy().max_retries
    assert recorder.calls["plain"] + recorder.calls["streamed"] == 1 + max_retries + 1


# ---------------------------------------------------------------------------
# Stream chunk timeout resolution
# ---------------------------------------------------------------------------


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
        if raw:
            monkeypatch.setenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", raw)
        else:
            monkeypatch.delenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", raising=False)
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

    from agents.model.openai_api_adapter import reset_model_cache_for_tests

    reset_model_cache_for_tests()
    try:
        monkeypatch.delenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", raising=False)
        model = adapter.build_openai_chat_model("chunk-timeout-model", api_key="k")
        assert model.stream_chunk_timeout == 90.0

        monkeypatch.setenv("ARC_MODEL_STREAM_CHUNK_TIMEOUT", "33")
        reset_model_cache_for_tests()
        model33 = adapter.build_openai_chat_model("chunk-timeout-model-33", api_key="k")
        assert model33.stream_chunk_timeout == 33.0
    finally:
        reset_model_cache_for_tests()


# ---------------------------------------------------------------------------
# Outcome observability at the seam
# ---------------------------------------------------------------------------


def test_outcome_reports_which_transport_answered() -> None:
    """The engine's outcome is the seam's verdict: which transport answered,
    how many real attempts it took, and every stream→plain fallback with its
    trigger and reason."""

    recorder = _Recorder(streamed_errors=[_StreamChunkTimeoutError()])
    result = _call_engine(recorder, stream_first=True)
    assert result.outcome == ModelCallOutcome(
        transport="plain",
        attempts=2,
        stream_fallbacks=(StreamFallback(FALLBACK_CHUNK_TIMEOUT, 1),),
    )

    recorder2 = _Recorder()
    result2 = _call_engine(recorder2, stream_first=True)
    assert result2.outcome == ModelCallOutcome(
        transport="streamed", attempts=1, stream_fallbacks=()
    )

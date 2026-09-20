from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAIError
from langchain_core.language_models.chat_models import agenerate_from_stream, generate_from_stream
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from agents.model.compatible_openai import CompatibleChatOpenAI
from agents.model.usage_capture import record_chat_result_usage


logger = logging.getLogger(__name__)

OpenAIAPIMode = Literal["responses", "chat_completions"]
_TRUTHY = {"1", "true", "yes", "on", "responses", "response", "responses_api"}
_FALSY = {"0", "false", "no", "off", "chat", "chat_completion", "chat_completions", "chat/completions"}

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 5.0
_DEFAULT_RETRY_MAX_DELAY = 60.0
_DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
_DEFAULT_REQUEST_TIMEOUT = 600.0
_DEFAULT_CONNECT_TIMEOUT = 15.0
# langchain-openai defaults the inter-chunk gap timeout to 120s. A stalled
# stream is usually a dead gateway connection (TCP alive, zero bytes), so the
# sooner it surfaces the less tail latency a call accumulates before the
# transport switch. 90s sits above real inter-chunk gaps of slow reasoners
# (long tool-call arguments still arrive as separate chunks) while cutting
# ~30s of dead waiting per stalled attempt. 0 disables the watchdog.
_DEFAULT_STREAM_CHUNK_TIMEOUT = 90.0
_CHUNK_TIMEOUT_ENV = "ARC_MODEL_STREAM_CHUNK_TIMEOUT"
# Whether streamed chat.completions requests ask the endpoint to return usage
# (stream_options.include_usage). Without it every streamed call reports no
# usage and ARC falls back to tiktoken estimation, whose cache_read is 0 by
# definition — arc-output4's whole run was billed as zero-cache because of
# this. The flag also gates a compatibility escape hatch: a gateway that
# 400-rejects stream_options would otherwise mark the endpoint
# streaming-unsupported and silently lose the stream transport's
# idle-timeout protection, so keep it possible to turn the option off.
_STREAM_USAGE_ENV = "ARC_MODEL_STREAM_USAGE"
# One-shot dedupe for unrecognized-env-value warnings, keyed by (name, value).
_ENV_WARNED: set[tuple[str, str]] = set()
_ENV_WARN_LOCK = threading.Lock()
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
_REACHABILITY_PROBE_TIMEOUT = 5.0
# A dead endpoint is re-probed up to this many times per real attempt before
# the loop gives up on the call. Probe rounds never consume the real-attempt
# budget (a connection blip that recovers still gets its full retry budget),
# so the cap is what bounds a totally-down endpoint: 1 real failure + 3
# probe rounds ~= 4 x (5s probe + 5s wait) before the call raises.
_PROBE_ROUNDS_PER_ATTEMPT = 3

# Streaming transport for model calls. A non-streaming chat/completions POST
# carries zero response bytes while the model thinks; gateways in front of
# OpenAI-compatible endpoints commonly drop such idle connections after ~120s
# (observed against the arc-bench endpoint: TestGenerator's large-output turns
# died as OpenAIConnectionError at almost exactly 120s per attempt, four times
# in a row, while short calls on the same endpoint kept succeeding). Streaming
# keeps SSE chunks flowing, so the same generation survives the gateway idle
# window. The stage agents always consume the accumulated ChatResult, so this
# only changes the HTTP transport, not the agent-facing behaviour.
#
# Modes (ARC_MODEL_STREAM_TRANSPORT):
#   stream (default) - first attempt already streams; a provider that rejects
#                      streaming with a 4xx permanently falls back to plain
#                      non-streaming for the process (cached per endpoint),
#                      without consuming the retry budget.
#   retry            - first attempt stays non-streaming; only retries after a
#                      connection-class failure switch transport (the original
#                      PR #44 behaviour, useful for providers whose streaming
#                      path is flakier than their plain path).
#   0/false/no/off   - never stream (pre-PR behaviour).
_STREAM_TRANSPORT_ENV = "ARC_MODEL_STREAM_TRANSPORT"


def _stream_transport_mode() -> str:
    raw = os.environ.get(_STREAM_TRANSPORT_ENV, "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return "off"
    if raw in {"retry", "retry-only", "on-failure", "on_failure"}:
        return "retry"
    return "stream"


def _stream_transport_enabled() -> bool:
    return _stream_transport_mode() != "off"


# Providers that answered a streamed request with a client error: streaming is
# unsupported there, so every later call goes plain without paying the failed
# streamed attempt again. Keyed by (model, base_url); a process-wide cache
# like the model-client cache because the capability is an endpoint property.
# The mark expires after ``_STREAMING_UNSUPPORTED_TTL_SECONDS``: a 4xx can also
# come from a transient gateway misconfiguration, and a permanent mark would
# re-expose large-output turns to the gateway idle-timeout drop for the rest
# of a long run even after the endpoint recovers streaming.
_STREAMING_UNSUPPORTED: dict[tuple[str, str], float] = {}
_STREAMING_UNSUPPORTED_LOCK = threading.Lock()
_STREAMING_UNSUPPORTED_TTL_SECONDS = 300.0


def _mark_streaming_unsupported(model: str, base_url: str) -> None:
    with _STREAMING_UNSUPPORTED_LOCK:
        _STREAMING_UNSUPPORTED[(model, base_url)] = time.monotonic()


def _streaming_marked_unsupported(model: str, base_url: str) -> bool:
    with _STREAMING_UNSUPPORTED_LOCK:
        marked_at = _STREAMING_UNSUPPORTED.get((model, base_url))
    if marked_at is None:
        return False
    if time.monotonic() - marked_at > _STREAMING_UNSUPPORTED_TTL_SECONDS:
        # Expired: forget the mark so the next call rediscovers streaming.
        with _STREAMING_UNSUPPORTED_LOCK:
            _STREAMING_UNSUPPORTED.pop((model, base_url), None)
        return False
    return True


def reset_streaming_support_cache_for_tests() -> None:
    with _STREAMING_UNSUPPORTED_LOCK:
        _STREAMING_UNSUPPORTED.clear()

# Quota/billing exhaustion is deterministic: retrying only burns backoff time.
# A status carrying these texts is an account or subscription limit, not a
# transient throttle, so it must fail fast instead of entering the retry loop.
# The list mirrors pi's NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN
# (packages/ai/src/utils/retry.ts), minus bare "billing": that also matches
# non-limit mentions (billing email prompts) and transient provider-side
# billing-subsystem outages.
_NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN = re.compile(
    "|".join(
        (
            r"insufficient[ _-]?quota",
            r"exceeded your (?:current )?quota",
            r"insufficient (?:credits?|funds|balance)",
            r"out of budget",
            r"quota exceeded",
            r"usage.?limit",
            r"available balance",
            r"billing (?:hard )?limit",
            r"payment required",
        )
    ),
    re.IGNORECASE,
)

# Explicit throttle wording marks the error as transient even when it also
# mentions quota/limit text (e.g. "quota exceeded for requests per minute").
_TRANSIENT_THROTTLE_ERROR_PATTERN = re.compile(r"rate.?limit|too many requests|throttl", re.IGNORECASE)

# ARC builds one agent per stage invocation, so ``build_openai_chat_model`` used
# to construct a brand-new ChatOpenAI (and therefore a brand-new httpx client)
# for every node, phase and TDD retry. Each new client discarded the previous
# connection pool, so every model call paid a fresh TCP/TLS handshake. Caching
# the client by its construction inputs lets one pool serve the whole run;
# ``bind_tools`` returns a new bound runnable and never mutates the cached
# instance, so sharing it across agents is safe.
_MODEL_CACHE: dict[tuple[str, str, str, str, bool], ChatOpenAI] = {}
_MODEL_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class OpenAIAdapterConfig:
    model_name: str
    api_mode: OpenAIAPIMode
    base_url: str = ""
    api_key: str = ""
    sse_text_compat: bool = False


@dataclass(frozen=True)
class _ModelRetryPolicy:
    max_retries: int
    retry_delay: float
    max_delay: float
    max_consecutive_failures: int


def _resolve_retry_policy() -> _ModelRetryPolicy:
    return _ModelRetryPolicy(
        max_retries=_env_int("ARC_MODEL_MAX_RETRIES", _DEFAULT_MAX_RETRIES),
        retry_delay=_env_float("ARC_MODEL_RETRY_DELAY", _DEFAULT_RETRY_DELAY),
        max_delay=_env_float("ARC_MODEL_RETRY_MAX_DELAY", _DEFAULT_RETRY_MAX_DELAY),
        max_consecutive_failures=_env_int(
            "ARC_MODEL_MAX_CONSECUTIVE_FAILURES", _DEFAULT_MAX_CONSECUTIVE_FAILURES
        ),
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class ARCModelAPIError(RuntimeError):
    """Normalized exception for OpenAI-compatible model API failures."""

    def __init__(
        self,
        message: str,
        *,
        api_mode: OpenAIAPIMode,
        model: str,
        status_code: int | None = None,
        error_type: str = "",
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.api_mode = api_mode
        self.model = model
        self.status_code = status_code
        self.error_type = error_type
        self.original = original


def _arc_empty_stream_error() -> APIConnectionError:
    """A stream that closed without any generation chunk, as a connection error.

    The retry loop treats connection-class failures as "transport suspect" —
    the stream transport gets marked not-first-choice — which is exactly the
    handling an empty SSE body deserves. The ``_arc_empty_stream`` marker
    additionally skips the reachability probe round: the endpoint just
    answered the request with HTTP 200, so it is provably alive and waiting on
    ``GET /models`` only burns wall-clock time before the transport switch.
    """

    request = httpx.Request("POST", "stream://chat/completions")
    error = APIConnectionError(
        message="streamed response closed without any generation chunks", request=request
    )
    error._arc_empty_stream = True
    return error


def _is_empty_stream_error(exc: BaseException) -> bool:
    return bool(getattr(exc, "_arc_empty_stream", False))


def _is_stream_chunk_timeout(exc: BaseException) -> bool:
    """Whether the failure is the streamed transport's inter-chunk gap timeout.

    langchain-openai wraps every ``__anext__`` of the streamed response in
    ``asyncio.wait_for``; a gateway that drops the connection without RST
    (idle timeout, proxy hiccup) surfaces as ``StreamChunkTimeoutError`` — a
    plain ``TimeoutError`` subclass, *not* an OpenAI/httpx error, so without
    this check it escapes the adapter retry loop entirely and the agent layer
    replays the whole session through ``ainvoke`` (which streams again and
    hits the same stall: observed as multi-minute tail latency per call).
    The check also looks at ``__cause__`` because ``agenerate_from_stream``
    re-raises mid-iteration errors wrapped in ``ValueError``/``RuntimeError``.
    """

    if _has_stream_chunk_timeout_type(exc):
        return True
    cause = getattr(exc, "__cause__", None)
    return cause is not exc and _has_stream_chunk_timeout_type(cause)


def _has_stream_chunk_timeout_type(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    # Match by name: importing the class from langchain_openai would pin a
    # symbol that older installed versions do not have.
    return type(exc).__name__ == "StreamChunkTimeoutError" and isinstance(exc, TimeoutError)


# Transport metadata of the most recent retry-loop call on this execution
# context ({"transport": "streamed"|"plain", "attempts": int}). The retry
# helpers keep returning the bare result — dozens of tests call them directly
# — so the model classes read this after the call to attribute llm_usage
# events with which transport actually answered and how many attempts it took.
_last_call_meta: ContextVar[dict[str, Any] | None] = ContextVar(
    "arc_model_last_call_meta", default=None
)


def _note_successful_attempt(*, transport: str, attempts: int) -> None:
    _last_call_meta.set({"transport": transport, "attempts": attempts})


def _record_call_usage(result: Any, *, model: str, api_mode: OpenAIAPIMode, messages, started_at: float) -> None:
    """Emit one usage record with the call's latency/transport telemetry."""

    meta = _last_call_meta.get() or {}
    try:
        record_chat_result_usage(
            result,
            model=model,
            api_mode=api_mode,
            messages=messages,
            duration_s=time.monotonic() - started_at,
            transport=str(meta.get("transport") or ""),
            attempts=meta.get("attempts"),
        )
    finally:
        # Reset even on capture failure: a stale transport marker must not
        # attribute the next call to this call's transport.
        _last_call_meta.set(None)


class ARCChatOpenAI(ChatOpenAI):
    """ChatOpenAI with ARC-level API error normalization and transient-failure retries."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="chat_completions")
    _arc_model_name: str = PrivateAttr(default="")
    _arc_base_url: str = PrivateAttr(default="")
    _arc_api_key: str = PrivateAttr(default="")

    def __init__(
        self,
        *args: Any,
        arc_api_mode: OpenAIAPIMode,
        arc_model_name: str,
        arc_base_url: str = "",
        arc_api_key: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name
        self._arc_base_url = arc_base_url
        self._arc_api_key = arc_api_key

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        started_at = time.monotonic()
        result = await _acall_model_with_retries(
            lambda: parent._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
            base_url=self._arc_base_url,
            api_key=self._arc_api_key,
            streamed_retry=lambda: self._arc_streamed_agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ),
            stream_first=self._arc_should_stream_first(),
        )
        _record_call_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages,
            started_at=started_at,
        )
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        started_at = time.monotonic()
        result = _call_model_with_retries(
            lambda: parent._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
            base_url=self._arc_base_url,
            api_key=self._arc_api_key,
            streamed_retry=lambda: self._arc_streamed_generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ),
            stream_first=self._arc_should_stream_first(),
        )
        _record_call_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages,
            started_at=started_at,
        )
        return result

    def _arc_should_stream_first(self) -> bool:
        """Whether the first attempt of a call may go over the streaming transport."""

        return (
            _stream_transport_mode() == "stream"
            and not _streaming_marked_unsupported(self._arc_model_name, self._arc_base_url)
        )

    async def _arc_streamed_agenerate(self, messages, *, stop, run_manager, **kwargs):
        """One attempt over the streaming HTTP transport.

        The instance is built with ``disable_streaming=True``, so langchain-core
        routes every agent-level call to ``(a)generate``; calling the SDK-level
        ``(a)stream`` directly here bypasses that switch while keeping request
        payload construction, chunk parsing and usage extraction on the
        langchain-openai code path. An empty stream (no generations at all,
        surfaced by ``agenerate_from_stream`` as a ValueError) is treated as a
        connection failure: some gateways accept stream=true but return an
        empty body, which must not surface as a bogus "no generations" result.
        """

        try:
            result = await agenerate_from_stream(
                super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)
            )
        except ValueError as exc:
            if "No generations" in str(exc):
                raise _arc_empty_stream_error() from exc
            raise
        if not result.generations:
            raise _arc_empty_stream_error()
        return result

    def _arc_streamed_generate(self, messages, *, stop, run_manager, **kwargs):
        """Sync counterpart of ``_arc_streamed_agenerate``."""

        try:
            result = generate_from_stream(
                super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            )
        except ValueError as exc:
            if "No generations" in str(exc):
                raise _arc_empty_stream_error() from exc
            raise
        if not result.generations:
            raise _arc_empty_stream_error()
        return result


class ARCCompatibleChatOpenAI(CompatibleChatOpenAI):
    """Responses-compatible ChatOpenAI with ARC-level API error normalization and transient-failure retries."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="responses")
    _arc_model_name: str = PrivateAttr(default="")
    _arc_base_url: str = PrivateAttr(default="")
    _arc_api_key: str = PrivateAttr(default="")

    def __init__(
        self,
        *args: Any,
        arc_api_mode: OpenAIAPIMode,
        arc_model_name: str,
        arc_base_url: str = "",
        arc_api_key: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name
        self._arc_base_url = arc_base_url
        self._arc_api_key = arc_api_key

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        started_at = time.monotonic()
        result = await _acall_model_with_retries(
            lambda: parent._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
            base_url=self._arc_base_url,
            api_key=self._arc_api_key,
            streamed_retry=lambda: self._arc_streamed_agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ),
            stream_first=self._arc_should_stream_first(),
        )
        _record_call_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages,
            started_at=started_at,
        )
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        started_at = time.monotonic()
        result = _call_model_with_retries(
            lambda: parent._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
            base_url=self._arc_base_url,
            api_key=self._arc_api_key,
            streamed_retry=lambda: self._arc_streamed_generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ),
            stream_first=self._arc_should_stream_first(),
        )
        _record_call_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages,
            started_at=started_at,
        )
        return result

    def _arc_should_stream_first(self) -> bool:
        """Whether the first attempt of a call may go over the streaming transport."""

        return (
            _stream_transport_mode() == "stream"
            and not _streaming_marked_unsupported(self._arc_model_name, self._arc_base_url)
        )

    async def _arc_streamed_agenerate(self, messages, *, stop, run_manager, **kwargs):
        try:
            result = await agenerate_from_stream(
                super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)
            )
        except ValueError as exc:
            if "No generations" in str(exc):
                raise _arc_empty_stream_error() from exc
            raise
        if not result.generations:
            raise _arc_empty_stream_error()
        return result

    def _arc_streamed_generate(self, messages, *, stop, run_manager, **kwargs):
        try:
            result = generate_from_stream(
                super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            )
        except ValueError as exc:
            if "No generations" in str(exc):
                raise _arc_empty_stream_error() from exc
            raise
        if not result.generations:
            raise _arc_empty_stream_error()
        return result


def build_openai_chat_model(
    model_name: str,
    *,
    api_mode: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    config = resolve_openai_adapter_config(
        model_name=model_name,
        api_mode=api_mode,
        base_url=base_url,
        api_key=api_key,
    )
    cache_key = (
        config.model_name,
        config.api_mode,
        config.base_url,
        config.api_key,
        config.sse_text_compat,
    )
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    kwargs: dict[str, Any] = {
        "model": config.model_name,
        "disable_streaming": True,
        # Chat-completions streaming only sends stream_options.include_usage
        # (the real token counts incl. cache hits in the final SSE chunk) when
        # this is on; the Responses streaming path ignores it, and the
        # non-streaming paths never read it, so it is safe for every mode.
        "stream_usage": resolve_stream_usage(),
        "use_responses_api": config.api_mode == "responses",
        "output_version": "responses/v1" if config.api_mode == "responses" else "v0",
        "arc_api_mode": config.api_mode,
        "arc_model_name": config.model_name,
        "arc_base_url": config.base_url,
        "arc_api_key": config.api_key,
        # The openai SDK defaults to a 600s timeout with 2 hidden internal
        # retries, so one ARC attempt could silently stretch to ~30 min on a
        # dead endpoint (run7: 25 min; run8: 2x6 min hangs). Retrying is ARC's
        # own job (the adapter retry loop sees the real attempt count), so the
        # SDK layer is disabled here and the timeout is set explicitly.
        "max_retries": 0,
        "request_timeout": resolve_model_request_timeout(),
        # Binds the inter-chunk gap watchdog of streamed attempts to ARC's env
        # (the library default of 120s is neither configurable from ARC nor
        # aligned with the retry loop's transport-switch latency budget).
        "stream_chunk_timeout": resolve_stream_chunk_timeout(),
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.api_key:
        kwargs["api_key"] = config.api_key

    model_class = ARCCompatibleChatOpenAI if config.sse_text_compat else ARCChatOpenAI
    model = model_class(**kwargs)
    with _MODEL_CACHE_LOCK:
        # Another thread may have built the same client while we were constructing.
        return _MODEL_CACHE.setdefault(cache_key, model)


def resolve_model_request_timeout() -> httpx.Timeout:
    """Explicit per-request timeouts for model calls (env-tunable).

    ``ARC_MODEL_TIMEOUT`` bounds a full non-streaming request (read timeout =
    generation time; the SDK default 600s is kept because benchmark DESIGN
    calls legitimately run for minutes). ``ARC_MODEL_CONNECT_TIMEOUT`` bounds
    connection establishment, where a silently dropped connection must fail
    fast instead of waiting out the full request timeout.
    """

    request_timeout = _env_float("ARC_MODEL_TIMEOUT", _DEFAULT_REQUEST_TIMEOUT)
    connect_timeout = _env_float("ARC_MODEL_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT)
    return httpx.Timeout(request_timeout, connect=min(connect_timeout, request_timeout))


def resolve_stream_usage() -> bool:
    """Whether streamed requests ask the provider for usage (env-tunable).

    Defaults to on: ``stream_options.include_usage`` is what makes the final
    SSE chunk carry the real token counts (including cache hits), turning
    llm_usage events from tiktoken estimates (cache_read=0 by construction)
    into reported values. ``ARC_MODEL_STREAM_USAGE=0/false/no/off`` restores
    the pre-fix behaviour for gateways that reject the option. Unset or
    unrecognized values fall back to the default (on) — a typo silently
    disabling the fix would re-open the zero-cache blind spot — but an
    unrecognized value is logged once (the structured_output_supported
    convention), and the doctor surfaces it too.
    """

    raw = os.environ.get(_STREAM_USAGE_ENV, "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw and raw not in {"1", "true", "yes", "on"}:
        _warn_unrecognized_env_once(
            _STREAM_USAGE_ENV, raw, "expected 1/true/yes/on or 0/false/no/off; defaulting to on"
        )
    return True


def _warn_unrecognized_env_once(name: str, value: str, expected: str) -> None:
    """Log an unrecognized env value once per process (not per call site).

    Resolvers run on every model build; without the dedupe the same typo would
    re-log for each cached-client rebuild. A warning, not an error: the
    invalid->default fallback keeps the run alive.
    """

    with _ENV_WARN_LOCK:
        if (name, value) in _ENV_WARNED:
            return
        _ENV_WARNED.add((name, value))
    logger.warning("Invalid %s=%r; %s.", name, value, expected)


def resolve_stream_chunk_timeout() -> float | None:
    """Inter-chunk gap timeout for streamed model responses (env-tunable).

    ``ARC_MODEL_STREAM_CHUNK_TIMEOUT`` bounds how long a streamed call may wait
    for the next SSE chunk. A silent gateway drop mid-generation (TCP alive,
    no bytes) surfaces as ``StreamChunkTimeoutError`` after this many seconds
    instead of holding the attempt for the full ``ARC_MODEL_TIMEOUT``. ``0``
    disables the watchdog; invalid values fall back to the default. The
    effective value is clamped to ``ARC_MODEL_TIMEOUT``: a watchdog that fires
    later than the request's read timeout could never trigger, and silently
    keeping it above would just re-create the pre-fix behaviour for
    small-timeout configurations.
    """

    raw = os.getenv(_CHUNK_TIMEOUT_ENV, "").strip()
    if not raw:
        value: float | None = _DEFAULT_STREAM_CHUNK_TIMEOUT
    else:
        try:
            parsed = float(raw)
        except ValueError:
            return _DEFAULT_STREAM_CHUNK_TIMEOUT
        if parsed < 0:
            return _DEFAULT_STREAM_CHUNK_TIMEOUT
        value = parsed or None
    if value is None:
        return None
    request_timeout = _env_float("ARC_MODEL_TIMEOUT", _DEFAULT_REQUEST_TIMEOUT)
    if request_timeout > 0:
        value = min(value, request_timeout)
    return value or None


def reset_model_cache_for_tests() -> None:
    """Drop cached model clients so a test can assert construction behaviour."""

    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()


def resolve_openai_adapter_config(
    *,
    model_name: str,
    api_mode: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> OpenAIAdapterConfig:
    resolved_base_url = (base_url if base_url is not None else _get_openai_base_url()).strip()
    resolved_api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
    resolved_mode = resolve_openai_api_mode(api_mode)
    return OpenAIAdapterConfig(
        model_name=model_name,
        api_mode=resolved_mode,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        sse_text_compat=_should_use_sse_text_compat(resolved_base_url, resolved_mode),
    )


def resolve_openai_api_mode(api_mode: str | None = None) -> OpenAIAPIMode:
    requested = str(api_mode or os.getenv("ARC_OPENAI_API_MODE", "")).strip().lower()
    if not requested:
        return "chat_completions"
    if requested in _TRUTHY or requested in {"responses"}:
        return "responses"
    if requested in _FALSY or requested in {"chat_completions", "chat"}:
        return "chat_completions"
    raise ValueError(
        "Invalid OpenAI API mode. Use `responses` or `chat_completions` "
        "(aliases: true/false for responses, chat for chat_completions)."
    )


def should_disable_streaming_for_openai_mode(api_mode: str | None = None) -> bool:
    if resolve_openai_api_mode(api_mode) != "responses":
        return False
    force = os.environ.get("ARC_AGENT_FORCE_RESPONSES_STREAM", "").strip().lower()
    if force in {"1", "true", "yes", "on"}:
        return False
    base_url = _get_openai_base_url()
    if not base_url:
        return False
    return not _is_official_openai_base_url(base_url)


# Structured output (pydantic ``response_format`` on stage agents) is implemented
# by langchain as a tool-calling strategy, so the only endpoint capability it
# needs is standard tool calling. Custom ``OPENAI_BASE_URL`` endpoints are probed
# once per process instead of being disabled by hostname whitelist.
_STRUCTURED_OUTPUT_ON_VALUES = {"1", "true", "yes", "on", "force"}
_STRUCTURED_OUTPUT_OFF_VALUES = {"0", "false", "no", "off"}
_STRUCTURED_OUTPUT_PROBE_TIMEOUT = 10.0
# A definitive rejection must name the tool-calling surface; a bare 400 could be
# an unrelated request problem (bad model name, malformed payload).
_TOOL_CALL_ERROR_PATTERN = re.compile(r"tool|function", re.IGNORECASE)
# Provider-native structured outputs (chat_completions ``response_format`` of
# type ``json_schema`` with ``strict: true``) power the DESIGN stage's dynamic
# semantic floor. A definitive rejection must name the response_format surface.
_JSON_SCHEMA_ERROR_PATTERN = re.compile(r"response_format|json_schema|structured.?output", re.IGNORECASE)
# Probe decisions are scoped to the endpoint AND the credential that produced
# them (gateways may answer differently per key), keyed by a key fingerprint
# so the raw secret never lands in cache contents or debug dumps.
_STRUCTURED_OUTPUT_SUPPORT_CACHE: dict[tuple[str, str, str, str], bool] = {}
_JSON_SCHEMA_SUPPORT_CACHE: dict[tuple[str, str, str, str], bool] = {}
# One in-flight probe per cache key: concurrent first-time callers wait for the
# probe instead of each hitting the network (per-key, so unrelated endpoints
# never block each other).
_STRUCTURED_OUTPUT_SUPPORT_LOCKS: dict[tuple[str, str, str, str], threading.Lock] = {}
_STRUCTURED_OUTPUT_SUPPORT_LOCK = threading.Lock()


def _structured_output_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def probe_tool_call_support(
    *,
    base_url: str,
    model: str,
    api_mode: OpenAIAPIMode,
    api_key: str = "",
    timeout: float = _STRUCTURED_OUTPUT_PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> bool | None:
    """Send one minimal forced tool call to ``base_url`` and classify the result.

    Returns True when the endpoint answers with the forced tool call, False when
    it definitively rejects tool calling, and None when the capability cannot be
    determined (auth failures, throttling, outages, ambiguous request errors).
    ``transport`` is an injection point for offline tests.
    """

    if api_mode == "responses":
        url = base_url.rstrip("/") + "/responses"
        payload: dict[str, Any] = {
            "model": model,
            "input": "Call the arc_capability_ping tool.",
            "tools": [
                {
                    "type": "function",
                    "name": "arc_capability_ping",
                    "description": "No-op capability probe tool.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": {"type": "function", "name": "arc_capability_ping"},
        }
    else:
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Call the arc_capability_ping tool."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "arc_capability_ping",
                        "description": "No-op capability probe tool.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "arc_capability_ping"}},
        }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(url, json=payload, headers=headers)
    except (httpx.HTTPError, OSError):
        return None

    if response.status_code != 200:
        if response.status_code in {400, 404, 422}:
            try:
                body = response.text
            except Exception:
                # Undecodable gateway error page: classification is impossible.
                return None
            if _TOOL_CALL_ERROR_PATTERN.search(body):
                return False
        return None

    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        # A 200 whose body is not a JSON object says nothing about tool support.
        return None

    if api_mode == "responses":
        output = data.get("output")
        if not isinstance(output, list):
            return False
        return any(isinstance(item, dict) and item.get("type") == "function_call" for item in output)

    choices = data.get("choices")
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    if not isinstance(message, dict):
        return False
    return bool(message.get("tool_calls"))


_JSON_SCHEMA_PROBE_SCHEMA = {
    "name": "arc_capability_ping",
    "schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
    "strict": True,
}


def probe_json_schema_support(
    *,
    base_url: str,
    model: str,
    api_mode: OpenAIAPIMode,
    api_key: str = "",
    timeout: float = _STRUCTURED_OUTPUT_PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> bool | None:
    """Probe chat_completions ``response_format: json_schema`` (strict) support.

    Returns True when the endpoint accepts the strict json_schema request and
    answers with conforming JSON, False when it definitively rejects the
    response_format surface, and None when the capability cannot be determined
    (auth failures, throttling, outages, ambiguous request errors, non-object
    bodies). Only ``chat_completions`` mode is probed: the Responses API path
    is not used for the dynamic semantic floor, so callers treat any other
    mode as unsupported. ``transport`` is an injection point for offline tests.
    """

    if api_mode != "chat_completions":
        return None

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Return the JSON object {\"ok\": true}."}],
        "response_format": {"type": "json_schema", "json_schema": _JSON_SCHEMA_PROBE_SCHEMA},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(url, json=payload, headers=headers)
    except (httpx.HTTPError, OSError):
        return None

    if response.status_code != 200:
        if response.status_code in {400, 404, 422}:
            try:
                body = response.text
            except Exception:
                return None
            if _JSON_SCHEMA_ERROR_PATTERN.search(body):
                return False
        return None

    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return False
    try:
        parsed = json.loads(content)
    except ValueError:
        return False
    return isinstance(parsed, dict)


def json_schema_structured_output_supported(model: str | object) -> bool:
    """Decide whether provider-native strict json_schema output may be used.

    Same resolution order as ``structured_output_supported``: an explicit
    ``ARC_STRUCTURED_OUTPUT`` override forces the decision without probing
    (off means no structured output at all, hence no json_schema either);
    direct/official-OpenAI hosts are supported; custom endpoints get one
    cached probe per (base URL, model, API mode, credential fingerprint),
    failing open when inconclusive. Independent cache: an endpoint may accept
    plain tool calling (the agent channel) while rejecting native json_schema.
    """

    override = os.getenv("ARC_STRUCTURED_OUTPUT", "").strip().lower()
    if override in _STRUCTURED_OUTPUT_OFF_VALUES:
        return False

    base_url = _get_openai_base_url()
    if not base_url or _is_official_openai_base_url(base_url):
        return True

    model_name = model if isinstance(model, str) else str(getattr(model, "model_name", "") or "")
    if not model_name:
        return True

    api_mode = resolve_openai_api_mode(None)
    if api_mode != "chat_completions":
        return False
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    cache_key = (
        base_url,
        model_name,
        api_mode,
        _structured_output_key_fingerprint(api_key),
    )
    with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
        key_lock = _STRUCTURED_OUTPUT_SUPPORT_LOCKS.get(cache_key)
        if key_lock is None:
            key_lock = threading.Lock()
            _STRUCTURED_OUTPUT_SUPPORT_LOCKS[cache_key] = key_lock
    with key_lock:
        with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
            cached = _JSON_SCHEMA_SUPPORT_CACHE.get(cache_key)
        if cached is not None:
            return cached

        probe_result = probe_json_schema_support(
            base_url=base_url,
            model=model_name,
            api_mode=api_mode,
            api_key=api_key,
        )
        if probe_result is False:
            supported = False
            logger.info(
                "Native json_schema structured output disabled: endpoint %s rejected response_format (model=%s).",
                base_url,
                model_name,
            )
        else:
            # True = probe succeeded; None = inconclusive, fail open.
            supported = True
            logger.info(
                "Native json_schema structured output %s for %s (model=%s).",
                "enabled" if probe_result is True else "assumed (probe inconclusive)",
                base_url,
                model_name,
            )

        with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
            _JSON_SCHEMA_SUPPORT_CACHE[cache_key] = supported
        return supported


def structured_output_supported(model: str | object) -> bool:
    """Decide whether a pydantic ``response_format`` may be passed to agents.

    Resolution order:
    1. ``ARC_STRUCTURED_OUTPUT``=on/off forces the decision without probing.
    2. No custom base URL, an official OpenAI host, or a non-string model
       object: supported.
    3. Otherwise one cached probe per (base URL, model, API mode, credential
       fingerprint); concurrent first-time callers share a single probe.

    The probe fails open: when capability is inconclusive (auth, throttling,
    outage), structured output stays enabled because ARC agents already require
    tool calling to function at all.
    """

    override = os.getenv("ARC_STRUCTURED_OUTPUT", "").strip().lower()
    if override in _STRUCTURED_OUTPUT_ON_VALUES:
        return True
    if override in _STRUCTURED_OUTPUT_OFF_VALUES:
        return False
    if override:
        logger.warning(
            "Invalid ARC_STRUCTURED_OUTPUT=%r; expected on/off. Falling back to capability probing.",
            override,
        )

    base_url = _get_openai_base_url()
    if not base_url or _is_official_openai_base_url(base_url):
        return True

    model_name = model if isinstance(model, str) else str(getattr(model, "model_name", "") or "")
    if not model_name:
        return True

    api_mode = resolve_openai_api_mode(None)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    cache_key = (
        base_url,
        model_name,
        api_mode,
        _structured_output_key_fingerprint(api_key),
    )
    # Per-key single-flight: the first caller probes while holding the key's
    # lock; concurrent callers for the same key wait and then read the cached
    # decision instead of issuing duplicate probes.
    with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
        key_lock = _STRUCTURED_OUTPUT_SUPPORT_LOCKS.get(cache_key)
        if key_lock is None:
            key_lock = threading.Lock()
            _STRUCTURED_OUTPUT_SUPPORT_LOCKS[cache_key] = key_lock
    with key_lock:
        with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
            cached = _STRUCTURED_OUTPUT_SUPPORT_CACHE.get(cache_key)
        if cached is not None:
            return cached

        probe_result = probe_tool_call_support(
            base_url=base_url,
            model=model_name,
            api_mode=api_mode,
            api_key=api_key,
        )
        if probe_result is False:
            supported = False
            logger.warning(
                "Structured output disabled: endpoint %s rejected tool calling (model=%s).",
                base_url,
                model_name,
            )
        else:
            # True = probe succeeded; None = inconclusive, fail open.
            supported = True
            logger.info(
                "Structured output enabled for %s (model=%s, probe %s).",
                base_url,
                model_name,
                "succeeded" if probe_result is True else "inconclusive",
            )

        with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
            _STRUCTURED_OUTPUT_SUPPORT_CACHE[cache_key] = supported
        return supported


def reset_structured_output_support_cache_for_tests() -> None:
    """Drop cached probe decisions so a test can assert probing behaviour."""

    with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
        _STRUCTURED_OUTPUT_SUPPORT_CACHE.clear()
        _JSON_SCHEMA_SUPPORT_CACHE.clear()
        _STRUCTURED_OUTPUT_SUPPORT_LOCKS.clear()


def normalize_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    """Return ARC's normalized API exception when `exc` is model-provider related."""

    return _wrap_model_api_exception(exc, api_mode=api_mode, model=model)


def _raise_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> NoReturn:
    wrapped = _wrap_model_api_exception(exc, api_mode=api_mode, model=model)
    if wrapped is exc:
        raise exc
    raise wrapped from exc


def _call_model_with_retries(
    call: Callable[[], Any],
    *,
    api_mode: OpenAIAPIMode,
    model: str,
    base_url: str = "",
    api_key: str = "",
    streamed_retry: Callable[[], Any] | None = None,
    stream_first: bool = False,
) -> Any:
    """Invoke a model call, retrying transient failures with a short fixed delay.

    Failure detection has three layers. The per-call loop bounds one
    invocation (default 1 original + 3 real retries). After a connection-class
    failure the next attempt waits on a cheap GET /models reachability probe:
    probe rounds are bounded separately by ``_PROBE_ROUNDS_PER_ATTEMPT`` and
    never consume the real-attempt budget, so a dead endpoint is detected in
    seconds without shortening the retry budget for a connection blip that
    recovers. The cross-call counter bounds a whole run: when the same
    endpoint accumulates ``max_consecutive_failures`` consecutive failed
    attempts, later calls fail fast instead of re-burning the retry chain.
    Any success resets the counter.

    Streaming transport (``streamed_retry`` provided by the ARC model classes):
    with ``stream_first`` (ARC_MODEL_STREAM_TRANSPORT=stream, the default) the
    very first attempt streams — a non-streaming response carries zero bytes
    while the model thinks, which gateways with an idle timeout (observed
    ~120s) drop mid generation, while SSE chunks keep the connection alive.
    A streamed attempt answered by a client error (4xx: the provider rejects
    streaming) marks the endpoint streaming-unsupported for the whole process
    and immediately re-attempts plain, without consuming the retry budget.
    Without ``stream_first`` (mode ``retry``) the first attempt stays plain and
    only a connection-class failure switches transport; transports alternate
    on further connection failures and any non-connection error returns to
    plain attempts.
    """

    policy = _resolve_retry_policy()
    endpoint_key = _model_endpoint_key(model, base_url, api_key)
    _check_consecutive_failure_budget(endpoint_key, api_mode=api_mode, model=model, base_url=base_url, api_key=api_key)
    failed_attempts = 0
    probe_rounds = 0
    probe_next = False
    attempt_count = 0
    chunk_timeout_switches = 0
    stream_retry = bool(
        stream_first and streamed_retry is not None and not _streaming_marked_unsupported(model, base_url)
    )
    while True:
        if probe_next:
            if not _endpoint_reachable(base_url, api_key):
                # The endpoint is down: probing is cheap, a real attempt is
                # not, and the unreachable window already counts towards the
                # consecutive-failure budget, so re-probe (up to the cap)
                # instead of burning a real attempt against it.
                _record_model_failure(endpoint_key)
                probe_rounds += 1
                if probe_rounds > _PROBE_ROUNDS_PER_ATTEMPT:
                    _raise_endpoint_unreachable(
                        api_mode=api_mode,
                        model=model,
                        base_url=base_url,
                        attempts=failed_attempts,
                    )
                _log_unreachable_probe(probe_rounds=probe_rounds, policy=policy)
                _sleep(policy.retry_delay)
                continue
            # The endpoint answers again; fall through to the real attempt.
            probe_next = False
            probe_rounds = 0
        attempt: Callable[[], Any]
        if stream_retry and streamed_retry is not None:
            attempt = streamed_retry
        else:
            attempt = call
        attempt_count += 1
        try:
            result = attempt()
        except Exception as exc:
            if stream_retry and _is_client_error(exc):
                # The provider rejected the streamed request itself (4xx):
                # not transient, and not the plain transport's fault. Switch
                # the endpoint to plain for the rest of the process and
                # re-attempt immediately without spending the retry budget.
                _mark_streaming_unsupported(model, base_url)
                logger.warning(
                    "Provider rejected the streamed request (%s); "
                    "falling back to non-streaming requests for this endpoint.",
                    _short_error_text(exc, limit=200),
                )
                stream_retry = False
                continue
            if _is_stream_chunk_timeout(exc) and (
                stream_retry or chunk_timeout_switches > 0
            ):
                # The attempt stalled between chunks (watchdog error). The
                # first stall on the streamed transport gets one immediate
                # free transport switch (no probe round, no delay, no budget
                # burn — the common case recovers here). Every further stall,
                # on either transport, counts as a budgeted attempt so a
                # fully stalled endpoint cannot loop for free forever.
                # The guard routes a watchdog-shaped error from a plain
                # attempt (no streamed transport configured, no stall seen
                # yet) to the generic path below: there it is not a
                # retryable model-API exception, so the budget check fails
                # immediately and _raise_model_api_exception re-raises it
                # as-is (the exception is not OpenAI/httpx, so the wrapper
                # passes it through untouched — the pre-watchdog contract).
                chunk_timeout_switches += 1
                if chunk_timeout_switches == 1:
                    _record_model_failure(endpoint_key)
                    _log_model_retry(
                        exc,
                        failed_attempts=failed_attempts + 1,
                        policy=policy,
                        delay=0.0,
                        stream_retry=not stream_retry or not _stream_transport_enabled(),
                    )
                    if streamed_retry is not None and _stream_transport_enabled():
                        stream_retry = not stream_retry
                    continue
                failed_attempts += 1
                _record_model_failure(endpoint_key, exc=exc)
                if failed_attempts > policy.max_retries:
                    _raise_model_api_exception(exc, api_mode=api_mode, model=model)
                _log_model_retry(
                    exc,
                    failed_attempts=failed_attempts,
                    policy=policy,
                    delay=0.0,
                    stream_retry=not stream_retry or not _stream_transport_enabled(),
                )
                if streamed_retry is not None and _stream_transport_enabled():
                    stream_retry = not stream_retry
                continue
            failed_attempts += 1
            _record_model_failure(endpoint_key, exc=exc)
            if not _should_retry_model_exception(exc, failed_attempts=failed_attempts, policy=policy):
                _raise_model_api_exception(exc, api_mode=api_mode, model=model)
            delay = _compute_retry_delay(policy, exc)
            next_stream = (
                _is_connection_failure(exc)
                and streamed_retry is not None
                and _stream_transport_enabled()
                and not stream_retry
            )
            _log_model_retry(
                exc, failed_attempts=failed_attempts, policy=policy, delay=delay, stream_retry=next_stream
            )
            _sleep(delay)
            if _is_connection_failure(exc):
                # An empty stream proves the endpoint just answered with HTTP
                # 200, so the reachability probe would only burn its wait;
                # switch transport and re-attempt directly.
                probe_next = not _is_empty_stream_error(exc)
                # Only a connection-class failure is transport-suspicious:
                # alternate. Any other retryable error keeps the current
                # transport (a 5xx says nothing about stream vs plain).
                if streamed_retry is not None and _stream_transport_enabled():
                    stream_retry = not stream_retry
            continue
        _reset_model_failures(endpoint_key)
        _note_successful_attempt(
            transport="streamed" if stream_retry else "plain", attempts=attempt_count
        )
        return result


async def _acall_model_with_retries(
    call: Callable[[], Any],
    *,
    api_mode: OpenAIAPIMode,
    model: str,
    base_url: str = "",
    api_key: str = "",
    streamed_retry: Callable[[], Any] | None = None,
    stream_first: bool = False,
) -> Any:
    """Async variant of ``_call_model_with_retries``; see its docstring for the retry contract."""

    policy = _resolve_retry_policy()
    endpoint_key = _model_endpoint_key(model, base_url, api_key)
    _check_consecutive_failure_budget(
        endpoint_key, api_mode=api_mode, model=model, base_url=base_url, api_key=api_key
    )
    failed_attempts = 0
    probe_rounds = 0
    probe_next = False
    attempt_count = 0
    chunk_timeout_switches = 0
    stream_retry = bool(
        stream_first and streamed_retry is not None and not _streaming_marked_unsupported(model, base_url)
    )
    while True:
        if probe_next:
            if not await _aendpoint_reachable(base_url, api_key):
                # The endpoint is down: probing is cheap, a real attempt is
                # not, and the unreachable window already counts towards the
                # consecutive-failure budget, so re-probe (up to the cap)
                # instead of burning a real attempt against it.
                _record_model_failure(endpoint_key)
                probe_rounds += 1
                if probe_rounds > _PROBE_ROUNDS_PER_ATTEMPT:
                    _raise_endpoint_unreachable(
                        api_mode=api_mode,
                        model=model,
                        base_url=base_url,
                        attempts=failed_attempts,
                    )
                _log_unreachable_probe(probe_rounds=probe_rounds, policy=policy)
                await _asleep(policy.retry_delay)
                continue
            # The endpoint answers again; fall through to the real attempt.
            probe_next = False
            probe_rounds = 0
        if stream_retry and streamed_retry is not None:
            attempt = streamed_retry
        else:
            attempt = call
        attempt_count += 1
        try:
            result = await _await_if_needed(attempt())
        except Exception as exc:
            if stream_retry and _is_client_error(exc):
                # The provider rejected the streamed request itself (4xx):
                # not transient, and not the plain transport's fault. Switch
                # the endpoint to plain for the rest of the process and
                # re-attempt immediately without spending the retry budget.
                _mark_streaming_unsupported(model, base_url)
                logger.warning(
                    "Provider rejected the streamed request (%s); "
                    "falling back to non-streaming requests for this endpoint.",
                    _short_error_text(exc, limit=200),
                )
                stream_retry = False
                continue
            if _is_stream_chunk_timeout(exc) and (
                stream_retry or chunk_timeout_switches > 0
            ):
                # The attempt stalled between chunks (watchdog error). The
                # first stall on the streamed transport gets one immediate
                # free transport switch (no probe round, no delay, no budget
                # burn — the common case recovers here). Every further stall,
                # on either transport, counts as a budgeted attempt so a
                # fully stalled endpoint cannot loop for free forever.
                # The guard routes a watchdog-shaped error from a plain
                # attempt (no streamed transport configured, no stall seen
                # yet) to the generic path below: there it is not a
                # retryable model-API exception, so the budget check fails
                # immediately and _raise_model_api_exception re-raises it
                # as-is (the exception is not OpenAI/httpx, so the wrapper
                # passes it through untouched — the pre-watchdog contract).
                chunk_timeout_switches += 1
                if chunk_timeout_switches == 1:
                    _record_model_failure(endpoint_key)
                    _log_model_retry(
                        exc,
                        failed_attempts=failed_attempts + 1,
                        policy=policy,
                        delay=0.0,
                        stream_retry=not stream_retry or not _stream_transport_enabled(),
                    )
                    if streamed_retry is not None and _stream_transport_enabled():
                        stream_retry = not stream_retry
                    continue
                failed_attempts += 1
                _record_model_failure(endpoint_key, exc=exc)
                if failed_attempts > policy.max_retries:
                    _raise_model_api_exception(exc, api_mode=api_mode, model=model)
                _log_model_retry(
                    exc,
                    failed_attempts=failed_attempts,
                    policy=policy,
                    delay=0.0,
                    stream_retry=not stream_retry or not _stream_transport_enabled(),
                )
                if streamed_retry is not None and _stream_transport_enabled():
                    stream_retry = not stream_retry
                continue
            failed_attempts += 1
            _record_model_failure(endpoint_key, exc=exc)
            if not _should_retry_model_exception(exc, failed_attempts=failed_attempts, policy=policy):
                _raise_model_api_exception(exc, api_mode=api_mode, model=model)
            delay = _compute_retry_delay(policy, exc)
            next_stream = (
                _is_connection_failure(exc)
                and streamed_retry is not None
                and _stream_transport_enabled()
                and not stream_retry
            )
            _log_model_retry(
                exc, failed_attempts=failed_attempts, policy=policy, delay=delay, stream_retry=next_stream
            )
            await _asleep(delay)
            if _is_connection_failure(exc):
                # An empty stream proves the endpoint just answered with HTTP
                # 200, so the reachability probe would only burn its wait;
                # switch transport and re-attempt directly.
                probe_next = not _is_empty_stream_error(exc)
                # Only a connection-class failure is transport-suspicious:
                # alternate. Any other retryable error keeps the current
                # transport (a 5xx says nothing about stream vs plain).
                if streamed_retry is not None and _stream_transport_enabled():
                    stream_retry = not stream_retry
            continue
        _reset_model_failures(endpoint_key)
        _note_successful_attempt(
            transport="streamed" if stream_retry else "plain", attempts=attempt_count
        )
        return result


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _should_retry_model_exception(
    exc: Exception,
    *,
    failed_attempts: int,
    policy: _ModelRetryPolicy,
) -> bool:
    if not _is_model_api_exception(exc):
        return False
    if not _is_retryable_model_api_exception(exc):
        return False
    return failed_attempts <= policy.max_retries


def _is_retryable_model_api_exception(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, httpx.TransportError)):
        return True
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if _is_provider_limit_error(exc):
            return False
        return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500
    return False


def _is_provider_limit_error(exc: Exception) -> bool:
    """Return True when the error text indicates quota/billing exhaustion."""
    text = _provider_error_text(exc)
    if _TRANSIENT_THROTTLE_ERROR_PATTERN.search(text):
        return False
    return bool(_NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.search(text))


def _provider_error_text(exc: Exception) -> str:
    """Combine the exception text with structured body fields for classification."""
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for candidate in (body.get("error"), body):
            if isinstance(candidate, dict):
                for key in ("message", "type", "code"):
                    value = candidate.get(key)
                    if value:
                        parts.append(str(value))
            elif candidate:
                parts.append(str(candidate))
    elif isinstance(body, (str, list)) and body:
        parts.append(str(body))
    return "\n".join(parts)


def _compute_retry_delay(policy: _ModelRetryPolicy, exc: Exception) -> float:
    retry_after = _parse_retry_after_header(exc)
    if retry_after is not None:
        return min(retry_after, policy.max_delay)
    # A fixed short delay (pi uses 2s base with a 60s cap): the failure modes
    # this loop targets (connection loss, throttling, provider 5xx) recover on
    # their own schedule, and exponential growth mostly burns wall-clock time
    # while the run is already blocked.
    return min(policy.retry_delay, policy.max_delay)


# ---------------------------------------------------------------------------
# Cross-call consecutive-failure budget (run-level circuit breaker)
# ---------------------------------------------------------------------------

# Counts consecutive failed model attempts per endpoint identity. The run7/run8
# post-mortems showed one dead-provider window burning 25 min: every new model
# call re-entered the full per-call retry chain against an endpoint that was
# not answering. The counter lets later calls fail fast instead; any success
# resets it, mirroring pi's retry-counter reset on a successful response.
# Failure records are (timestamp, exception) pairs; only the count matters for
# the budget, but the last exception is kept for the fail-fast message.
#
# Failures older than the recovery window stop counting: a breaker that
# already tripped must not keep failing calls forever once the outage is
# plausibly over (the run may outlive the outage), and stale entries must not
# leak into later runs of a long-lived process. The window is deliberately
# generous — it is a leak guard, not a retry policy; the per-call probe layer
# is what decides whether the endpoint is actually back.
_CONSECUTIVE_FAILURES: dict[str, list[tuple[float, Exception | None]]] = {}
_CONSECUTIVE_FAILURES_LOCK = threading.Lock()
_FAILURE_RECOVERY_WINDOW_SECONDS = 300.0


def _model_endpoint_key(model: str, base_url: str, api_key: str) -> str:
    """Identity of the endpoint a failure is attributed to (secret-free).

    ``base_url`` and ``api_key`` are normalized the same way for every caller
    in the process (explicit argument, else the environment fallback), so a
    caller that passes an empty string and one that passes the env value
    explicitly land on the same counter instead of splitting it.
    """

    resolved_base_url = (base_url or _get_openai_base_url()).strip()
    resolved_api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    return f"{model}|{resolved_base_url.rstrip('/')}|{_structured_output_key_fingerprint(resolved_api_key)}"


def _record_model_failure(endpoint_key: str, *, exc: Exception | None = None) -> None:
    with _CONSECUTIVE_FAILURES_LOCK:
        records = _CONSECUTIVE_FAILURES.setdefault(endpoint_key, [])
        records.append((time.monotonic(), exc))
        _prune_stale_failures_locked(endpoint_key, records)


def _prune_stale_failures_locked(endpoint_key: str, records: list[tuple[float, Exception | None]]) -> None:
    """Drop failures older than the recovery window (caller holds the lock)."""

    cutoff = time.monotonic() - _FAILURE_RECOVERY_WINDOW_SECONDS
    stale_count = next(
        (index for index, (timestamp, _exc) in enumerate(records) if timestamp >= cutoff),
        len(records),
    )
    if stale_count:
        del records[:stale_count]
        if not records:
            _CONSECUTIVE_FAILURES.pop(endpoint_key, None)


def _reset_model_failures(endpoint_key: str) -> None:
    with _CONSECUTIVE_FAILURES_LOCK:
        _CONSECUTIVE_FAILURES.pop(endpoint_key, None)


def _consecutive_failure_count(endpoint_key: str) -> int:
    with _CONSECUTIVE_FAILURES_LOCK:
        records = _CONSECUTIVE_FAILURES.get(endpoint_key)
        if not records:
            return 0
        _prune_stale_failures_locked(endpoint_key, records)
        return len(records)


def reset_consecutive_failure_budget_for_tests() -> None:
    """Drop consecutive-failure state so a test can assert budget behaviour."""

    with _CONSECUTIVE_FAILURES_LOCK:
        _CONSECUTIVE_FAILURES.clear()


def _check_consecutive_failure_budget(
    endpoint_key: str,
    *,
    api_mode: OpenAIAPIMode,
    model: str,
    base_url: str,
    api_key: str,
) -> None:
    """Fail fast when the endpoint already exhausted its failure budget."""

    policy = _resolve_retry_policy()
    if policy.max_consecutive_failures <= 0:
        return
    failures = _consecutive_failure_count(endpoint_key)
    if failures < policy.max_consecutive_failures:
        return
    _raise_endpoint_unreachable(
        api_mode=api_mode,
        model=model,
        base_url=base_url,
        attempts=failures,
        consecutive=True,
    )


def _raise_endpoint_unreachable(
    *,
    api_mode: OpenAIAPIMode,
    model: str,
    base_url: str,
    attempts: int,
    consecutive: bool = False,
) -> NoReturn:
    scope = "consecutive failed model calls" if consecutive else "attempts"
    message = (
        f"Model API endpoint unreachable after {attempts} {scope} using `{api_mode}` mode; "
        f"model={model or '<unknown>'}, base_url={base_url or _get_openai_base_url() or '<default>'}. "
        "The endpoint stopped answering; retrying would only burn more time. "
        "Check network connectivity or the provider status, then rerun with --resume."
    )
    raise ARCModelAPIError(message, api_mode=api_mode, model=model, error_type="EndpointUnreachable")


# ---------------------------------------------------------------------------
# Reachability probe (cheap pre-flight before re-attempting after a failure)
# ---------------------------------------------------------------------------


def _is_connection_failure(exc: Exception) -> bool:
    """Whether the failure suggests the endpoint itself stopped answering."""

    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(getattr(exc, "__cause__", None), httpx.TransportError) or isinstance(
        getattr(exc, "__cause__", None), (APIConnectionError, APITimeoutError)
    )


def _is_client_error(exc: Exception) -> bool:
    """Whether the provider rejected the request itself (HTTP 4xx).

    Used on the streamed path only: a 4xx there means the provider does not
    accept streaming for this request shape, which is a capability gap, not a
    transient failure.
    """

    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and 400 <= status_code < 500


def probe_endpoint_reachable(
    *,
    base_url: str,
    api_key: str = "",
    timeout: float = _REACHABILITY_PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """One cheap GET /models against ``base_url``; True when it answers.

    Any HTTP status (including 4xx auth errors) counts as reachable: the
    endpoint's TCP/TLS stack and routing are alive, so the failure mode the
    probe exists for (silent connection drop, no RST) is ruled out. Only
    transport-level errors (connect refused, timeout, DNS) return False.
    ``transport`` is an injection point for offline tests.
    """

    resolved = (base_url or _get_openai_base_url()).strip()
    if not resolved:
        # No custom endpoint to probe (official OpenAI default); a probe would
        # only duplicate the real attempt. Assume reachable.
        return True
    url = resolved.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            client.get(url, headers=headers)
        return True
    except (httpx.HTTPError, OSError):
        return False


async def _aendpoint_reachable(base_url: str, api_key: str) -> bool:
    # httpx sync client in a short-lived probe; the async loop must not block
    # for the probe duration, so run it in the default executor.
    # asyncio.to_thread never touches the loop while the probe runs: it
    # schedules the sync call on a worker thread and awaits a future, so even
    # a probe that runs its full timeout leaves the loop serving other tasks.
    # ``transport`` is therefore a sync-only injection point (offline tests
    # cover the sync wrapper); the async path probes the real network, which
    # is the production behaviour.
    return await asyncio.to_thread(probe_endpoint_reachable, base_url=base_url, api_key=api_key)


def _endpoint_reachable(base_url: str, api_key: str) -> bool:
    return probe_endpoint_reachable(base_url=base_url, api_key=api_key)


def _log_unreachable_probe(*, probe_rounds: int, policy: _ModelRetryPolicy) -> None:
    logger.warning(
        "Endpoint reachability probe failed (probe round %d of %d); "
        "waiting %.1fs before re-probing without consuming the retry budget.",
        probe_rounds,
        _PROBE_ROUNDS_PER_ATTEMPT,
        policy.retry_delay,
    )


def _parse_retry_after_header(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = str(headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _log_model_retry(
    exc: Exception,
    *,
    failed_attempts: int,
    policy: _ModelRetryPolicy,
    delay: float,
    stream_retry: bool = False,
) -> None:
    transport = "; next attempt switches to streaming transport" if stream_retry else ""
    logger.warning(
        "Transient model API failure (attempt %d of %d); retrying in %.1fs%s: %s",
        failed_attempts,
        1 + policy.max_retries,
        delay,
        transport,
        _short_error_text(exc, limit=300),
    )


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


async def _asleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


def _wrap_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    if isinstance(exc, ARCModelAPIError):
        return exc
    if not _is_model_api_exception(exc):
        return exc
    status_code = getattr(exc, "status_code", None)
    error_type = _extract_error_type(exc)
    message = _format_model_api_error_message(
        exc,
        api_mode=api_mode,
        model=model,
        status_code=status_code,
        error_type=error_type,
    )
    return ARCModelAPIError(
        message,
        api_mode=api_mode,
        model=model,
        status_code=status_code if isinstance(status_code, int) else None,
        error_type=error_type,
        original=exc,
    )


def _is_model_api_exception(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            OpenAIError,
            APIError,
            APIStatusError,
            APIConnectionError,
            APITimeoutError,
            httpx.HTTPError,
        ),
    )


def _format_model_api_error_message(
    exc: Exception,
    *,
    api_mode: OpenAIAPIMode,
    model: str,
    status_code: Any,
    error_type: str,
) -> str:
    parts = [
        f"Model API request failed using `{api_mode}` mode",
        f"model={model or '<unknown>'}",
    ]
    if status_code:
        parts.append(f"status={status_code}")
    if error_type:
        parts.append(f"type={error_type}")
    parts.append(f"error={_short_error_text(exc)}")
    if api_mode == "responses":
        parts.append("If the provider does not support Responses API, set ARC_OPENAI_API_MODE=chat_completions.")
    return "; ".join(parts)


def _extract_error_type(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("type") or error.get("code") or "").strip()
        return str(body.get("type") or body.get("code") or "").strip()
    return type(exc).__name__


def _short_error_text(exc: Exception, limit: int = 800) -> str:
    """Exception text plus its ``__cause__`` chain, on one line.

    The SDK's ``Connection error.`` hides the transport reason (connection
    reset vs read timeout vs EOF vs proxy failure); the httpx exception is
    always attached as ``__cause__``. Without this, an idle-timeout gateway
    drop is indistinguishable from DNS failure in the logs.
    """

    parts = [str(exc).replace("\r", " ").replace("\n", " ").strip()]
    seen = {id(exc)}
    cause = getattr(exc, "__cause__", None)
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        parts.append(f"caused by {type(cause).__name__}: {_one_line(cause)}")
        cause = getattr(cause, "__cause__", None)
    text = "; ".join(part for part in parts if part)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "... [truncated]"


def _one_line(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _should_use_sse_text_compat(base_url: str, api_mode: OpenAIAPIMode) -> bool:
    override = os.getenv("ARC_OPENAI_SSE_TEXT_COMPAT", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return bool(base_url and api_mode == "responses" and not _is_official_openai_base_url(base_url))


def _get_openai_base_url() -> str:
    return os.getenv("OPENAI_API_BASE", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip()


def _is_official_openai_base_url(base_url: str) -> bool:
    try:
        from urllib.parse import urlparse

        host = urlparse(base_url).hostname or ""
    except Exception:
        host = ""
    return host == "api.openai.com" or host.endswith(".openai.com")

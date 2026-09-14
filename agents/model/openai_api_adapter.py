from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAIError
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from agents.model.compatible_openai import CompatibleChatOpenAI
from agents.model.usage_capture import record_chat_result_usage


logger = logging.getLogger(__name__)

OpenAIAPIMode = Literal["responses", "chat_completions"]
_TRUTHY = {"1", "true", "yes", "on", "responses", "response", "responses_api"}
_FALSY = {"0", "false", "no", "off", "chat", "chat_completion", "chat_completions", "chat/completions"}

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_INITIAL_DELAY = 2.0
_DEFAULT_RETRY_MAX_DELAY = 30.0
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})

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
    initial_delay: float
    max_delay: float


def _resolve_retry_policy() -> _ModelRetryPolicy:
    return _ModelRetryPolicy(
        max_retries=_env_int("ARC_MODEL_MAX_RETRIES", _DEFAULT_MAX_RETRIES),
        initial_delay=_env_float("ARC_MODEL_RETRY_INITIAL_DELAY", _DEFAULT_RETRY_INITIAL_DELAY),
        max_delay=_env_float("ARC_MODEL_RETRY_MAX_DELAY", _DEFAULT_RETRY_MAX_DELAY),
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


class ARCChatOpenAI(ChatOpenAI):
    """ChatOpenAI with ARC-level API error normalization and transient-failure retries."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="chat_completions")
    _arc_model_name: str = PrivateAttr(default="")

    def __init__(self, *args: Any, arc_api_mode: OpenAIAPIMode, arc_model_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        result = await _acall_model_with_retries(
            lambda: parent._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
        )
        record_chat_result_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages
        )
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        result = _call_model_with_retries(
            lambda: parent._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
        )
        record_chat_result_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages
        )
        return result


class ARCCompatibleChatOpenAI(CompatibleChatOpenAI):
    """Responses-compatible ChatOpenAI with ARC-level API error normalization and transient-failure retries."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="responses")
    _arc_model_name: str = PrivateAttr(default="")

    def __init__(self, *args: Any, arc_api_mode: OpenAIAPIMode, arc_model_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        result = await _acall_model_with_retries(
            lambda: parent._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
        )
        record_chat_result_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages
        )
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        parent = super()
        result = _call_model_with_retries(
            lambda: parent._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode,
            model=self._arc_model_name,
        )
        record_chat_result_usage(
            result, model=self._arc_model_name, api_mode=self._arc_api_mode, messages=messages
        )
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
        "stream_usage": False,
        "use_responses_api": config.api_mode == "responses",
        "output_version": "responses/v1" if config.api_mode == "responses" else "v0",
        "arc_api_mode": config.api_mode,
        "arc_model_name": config.model_name,
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
_STRUCTURED_OUTPUT_SUPPORT_CACHE: dict[tuple[str, str, str], bool] = {}
_STRUCTURED_OUTPUT_SUPPORT_LOCK = threading.Lock()


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
        if response.status_code in {400, 404, 422} and _TOOL_CALL_ERROR_PATTERN.search(response.text):
            return False
        return None

    try:
        data = response.json()
    except ValueError:
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


def structured_output_supported(model: str | object) -> bool:
    """Decide whether a pydantic ``response_format`` may be passed to agents.

    Resolution order:
    1. ``ARC_STRUCTURED_OUTPUT``=on/off forces the decision without probing.
    2. No custom base URL, an official OpenAI host, or a non-string model
       object: supported.
    3. Otherwise one cached probe per (base URL, model, API mode).

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
    cache_key = (base_url, model_name, api_mode)
    with _STRUCTURED_OUTPUT_SUPPORT_LOCK:
        cached = _STRUCTURED_OUTPUT_SUPPORT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    probe_result = probe_tool_call_support(
        base_url=base_url,
        model=model_name,
        api_mode=api_mode,
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
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


def normalize_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    """Return ARC's normalized API exception when `exc` is model-provider related."""

    return _wrap_model_api_exception(exc, api_mode=api_mode, model=model)


def _raise_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> NoReturn:
    wrapped = _wrap_model_api_exception(exc, api_mode=api_mode, model=model)
    if wrapped is exc:
        raise exc
    raise wrapped from exc


def _call_model_with_retries(call: Callable[[], Any], *, api_mode: OpenAIAPIMode, model: str) -> Any:
    """Invoke a model call, retrying transient failures with exponential backoff."""

    policy = _resolve_retry_policy()
    failed_attempts = 0
    while True:
        try:
            return call()
        except Exception as exc:
            failed_attempts += 1
            if not _should_retry_model_exception(exc, failed_attempts=failed_attempts, policy=policy):
                _raise_model_api_exception(exc, api_mode=api_mode, model=model)
            delay = _compute_retry_delay(policy, failed_attempts - 1, exc)
            _log_model_retry(exc, failed_attempts=failed_attempts, policy=policy, delay=delay)
            _sleep(delay)


async def _acall_model_with_retries(call: Callable[[], Any], *, api_mode: OpenAIAPIMode, model: str) -> Any:
    """Async variant of ``_call_model_with_retries``."""

    policy = _resolve_retry_policy()
    failed_attempts = 0
    while True:
        try:
            return await call()
        except Exception as exc:
            failed_attempts += 1
            if not _should_retry_model_exception(exc, failed_attempts=failed_attempts, policy=policy):
                _raise_model_api_exception(exc, api_mode=api_mode, model=model)
            delay = _compute_retry_delay(policy, failed_attempts - 1, exc)
            _log_model_retry(exc, failed_attempts=failed_attempts, policy=policy, delay=delay)
            await _asleep(delay)


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


def _compute_retry_delay(policy: _ModelRetryPolicy, failed_attempt: int, exc: Exception) -> float:
    retry_after = _parse_retry_after_header(exc)
    if retry_after is not None:
        return min(retry_after, policy.max_delay)
    delay = policy.initial_delay * (2 ** failed_attempt)
    return min(delay, policy.max_delay) * random.uniform(0.75, 1.0)


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
) -> None:
    logger.warning(
        "Transient model API failure (attempt %d of %d); retrying in %.1fs: %s",
        failed_attempts,
        1 + policy.max_retries,
        delay,
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
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "... [truncated]"


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

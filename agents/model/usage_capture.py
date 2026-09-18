"""Token-usage capture for ARC model calls.

Mirrors the ARC-Bench reference (pi) ``Usage`` semantics:

- ``input``   = prompt tokens excluding cache reads/writes (providers report
  cached tokens as a subset of the prompt total, so they are subtracted);
- ``output``  = completion tokens (``reasoning`` is a subset, never subtracted);
- ``cache_read`` / ``cache_write`` = cache hit / cache creation tokens;
- ``total``   = input + output + cache_read + cache_write.

Capture runs in the ``ARCChatOpenAI`` wrapper layer after each successful
model call. Provider-reported usage is preferred; when a provider returns none
(e.g. the SSE-text fallback path), a tiktoken-based estimate is recorded with
``source="estimated"``. Records are dispatched to a process-wide sink;
``core.service.configure_runtime`` registers a sink that persists each call as
an ``llm_usage`` runner event, attributed to the stage's node/phase through
``llm_usage_context``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

from agents.model.costing import compute_model_cost


logger = logging.getLogger(__name__)

PER_MESSAGE_TOKEN_OVERHEAD = 4  # OpenAI-style per-message chat framing estimate
_CHARS_PER_TOKEN = 4

UsageSink = Callable[["LLMUsageRecord"], None]

_sink: UsageSink | None = None
_sink_lock = threading.Lock()
_encoder_cache: dict[str, Any] = {}
_encoder_cache_lock = threading.Lock()

_usage_context: ContextVar[dict[str, str] | None] = ContextVar("arc_llm_usage_context", default=None)


@dataclass(frozen=True)
class LLMUsageRecord:
    """Canonical usage of one model call, ready for ``EventClient.record_llm_usage``."""

    model: str
    api_mode: str
    node_id: str
    phase: str
    source: str  # "reported" | "estimated"
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int
    cost: dict[str, float] | None = field(default=None)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def set_llm_usage_sink(sink: UsageSink | None) -> None:
    """Install the process-wide usage sink (or remove it with ``None``)."""

    global _sink
    with _sink_lock:
        _sink = sink


def get_llm_usage_sink() -> UsageSink | None:
    with _sink_lock:
        return _sink


@contextmanager
def llm_usage_context(node_id: str, phase: str) -> Iterator[None]:
    """Attribute model calls made inside this context to ``node_id``/``phase``."""

    token = _usage_context.set(
        {"node_id": str(node_id or "").strip(), "phase": str(phase or "").strip()}
    )
    try:
        yield
    finally:
        _usage_context.reset(token)


def current_usage_context() -> tuple[str, str]:
    values = _usage_context.get() or {}
    return values.get("node_id", ""), values.get("phase", "")


def record_chat_result_usage(
    result: Any,
    *,
    model: str,
    api_mode: str,
    messages: Sequence[Any] | None = None,
) -> None:
    """Best-effort usage capture for one model call; never raises."""

    try:
        _record_chat_result_usage(result, model=model, api_mode=api_mode, messages=messages)
    except Exception:
        logger.debug("LLM usage capture failed", exc_info=True)


def _record_chat_result_usage(
    result: Any,
    *,
    model: str,
    api_mode: str,
    messages: Sequence[Any] | None,
) -> None:
    sink = get_llm_usage_sink()
    if sink is None:
        return
    usage = extract_usage_from_chat_result(result)
    source = "reported"
    if usage is None:
        if messages is None:
            return
        usage = estimate_usage_from_exchange(messages, result, model=model)
        source = "estimated"
    node_id, phase = current_usage_context()
    sink(
        LLMUsageRecord(
            model=str(model or ""),
            api_mode=str(api_mode or ""),
            node_id=node_id,
            phase=phase,
            source=source,
            input_tokens=int(usage["input"]),
            output_tokens=int(usage["output"]),
            cache_read_tokens=int(usage["cache_read"]),
            cache_write_tokens=int(usage["cache_write"]),
            cache_write_1h_tokens=usage.get("cache_write_1h"),
            reasoning_tokens=usage.get("reasoning"),
            total_tokens=int(usage["total"]),
            cost=compute_model_cost(model, usage),
        )
    )


def extract_usage_from_chat_result(result: Any) -> dict[str, Any] | None:
    """Extract canonical usage from a ``ChatResult``, or ``None`` when absent.

    Prefers the normalized per-message ``usage_metadata``; falls back to the
    raw ``llm_output["token_usage"]`` mapping.
    """
    metadata = _message_usage_metadata(result)
    if metadata is not None:
        return _usage_from_metadata(metadata)
    token_usage = _llm_output_token_usage(result)
    if token_usage is not None:
        return _usage_from_token_usage(token_usage)
    return None


def _message_usage_metadata(result: Any) -> Mapping[str, Any] | None:
    for generation in getattr(result, "generations", None) or []:
        metadata = getattr(getattr(generation, "message", None), "usage_metadata", None)
        if isinstance(metadata, Mapping) and metadata.get("input_tokens") is not None:
            return metadata
    return None


def _llm_output_token_usage(result: Any) -> Mapping[str, Any] | None:
    llm_output = getattr(result, "llm_output", None)
    token_usage = llm_output.get("token_usage") if isinstance(llm_output, Mapping) else None
    return token_usage if isinstance(token_usage, Mapping) else None


def _usage_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    input_details = metadata.get("input_token_details") or {}
    output_details = metadata.get("output_token_details") or {}
    cache_read = _int(input_details.get("cache_read"))
    cache_write = _int(input_details.get("cache_creation"))
    prompt_total = _int(metadata.get("input_tokens"))
    completion_total = _int(metadata.get("output_tokens"))
    return _canonical_usage(
        input=max(0, prompt_total - cache_read - cache_write),
        output=completion_total,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=_optional_int(output_details.get("reasoning")),
    )


def _usage_from_token_usage(token_usage: Mapping[str, Any]) -> dict[str, Any]:
    prompt_details = token_usage.get("prompt_tokens_details") or {}
    completion_details = token_usage.get("completion_tokens_details") or {}
    cache_read = _int(
        prompt_details.get("cached_tokens") or token_usage.get("prompt_cache_hit_tokens")
    )
    cache_write = _int(prompt_details.get("cache_write_tokens"))
    prompt_total = _int(token_usage.get("prompt_tokens"))
    return _canonical_usage(
        input=max(0, prompt_total - cache_read - cache_write),
        output=_int(token_usage.get("completion_tokens")),
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=_optional_int(completion_details.get("reasoning_tokens")),
    )


def _canonical_usage(
    *,
    input: int,
    output: int,
    cache_read: int,
    cache_write: int,
    reasoning: int | None,
    cache_write_1h: int | None = None,
) -> dict[str, Any]:
    return {
        "input": input,
        "output": output,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cache_write_1h": cache_write_1h,
        "reasoning": reasoning,
        "total": input + output + cache_read + cache_write,
    }


def estimate_usage_from_exchange(
    messages: Sequence[Any],
    result: Any,
    *,
    model: str = "",
) -> dict[str, Any]:
    """Estimate input/output tokens when the provider reports no usage.

    Uses tiktoken when available (model encoding, then ``cl100k_base``) and a
    chars/4 heuristic otherwise. Cache and reasoning breakdowns are unknown by
    definition and left at their unknown markers.
    """
    input_tokens = sum(
        _count_tokens(_message_text(message), model) + PER_MESSAGE_TOKEN_OVERHEAD
        for message in messages or []
    )
    output_tokens = sum(
        _count_tokens(_message_text(getattr(generation, "message", None)), model)
        for generation in getattr(result, "generations", None) or []
    )
    return _canonical_usage(
        input=input_tokens,
        output=output_tokens,
        cache_read=0,
        cache_write=0,
        reasoning=None,
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, Mapping):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return str(content.get("text") or "")
    if isinstance(content, Sequence):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content) if content is not None else ""


def _count_tokens(text: str, model: str) -> int:
    if not text:
        return 0
    encoder = _tokenizer_for_model(model)
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            logger.debug("tiktoken encoding failed; falling back to char estimate.", exc_info=True)
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _tokenizer_for_model(model: str) -> Any | None:
    key = str(model or "").strip().lower()
    with _encoder_cache_lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
    encoder = _load_tokenizer(key)
    with _encoder_cache_lock:
        _encoder_cache[key] = encoder
        return encoder


def _load_tokenizer(model: str) -> Any | None:
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        pass
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

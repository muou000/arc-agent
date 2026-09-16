from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _construct_lc_result_from_responses_api


class CompatibleChatOpenAI(ChatOpenAI):
    """ChatOpenAI with a fallback for providers returning SSE text for Responses."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if self._use_responses_api(payload):
            return await self._agenerate_from_sse_text(payload)
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except AttributeError as exc:
            if not _is_responses_sse_attribute_error(exc):
                raise
            return await self._agenerate_from_sse_text(payload)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if self._use_responses_api(payload):
            return self._generate_from_sse_text(payload)
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except AttributeError as exc:
            if not _is_responses_sse_attribute_error(exc):
                raise
            return self._generate_from_sse_text(payload)

    async def _agenerate_from_sse_text(self, payload: dict[str, Any]) -> ChatResult:
        raw_response = await self.root_async_client.responses.with_raw_response.create(**payload)
        parsed = raw_response.parse()
        if not isinstance(parsed, str):
            return _construct_lc_result_from_responses_api(parsed, output_version=self.output_version)
        return _chat_result_from_sse_text(parsed)

    def _generate_from_sse_text(self, payload: dict[str, Any]) -> ChatResult:
        raw_response = self.root_client.responses.with_raw_response.create(**payload)
        parsed = raw_response.parse()
        if not isinstance(parsed, str):
            return _construct_lc_result_from_responses_api(parsed, output_version=self.output_version)
        return _chat_result_from_sse_text(parsed)


def _is_responses_sse_attribute_error(exc: AttributeError) -> bool:
    return "'str' object has no attribute 'error'" in str(exc)


def _chat_result_from_sse_text(payload: str) -> ChatResult:
    parsed = _parse_responses_sse(payload)
    metadata: dict[str, Any] = {"model_provider": "openai", "sse_text_fallback": True}
    # Keep the terminal response status so length truncation stays observable
    # downstream (TruncatedToolCallGuardMiddleware reads status/incomplete_details).
    if parsed["response_status"]:
        metadata["status"] = parsed["response_status"]
    if parsed["incomplete_details"] is not None:
        metadata["incomplete_details"] = parsed["incomplete_details"]
    message = AIMessage(
        content=parsed["content"],
        tool_calls=parsed["tool_calls"],
        invalid_tool_calls=parsed["invalid_tool_calls"],
        response_metadata=metadata,
    )
    if parsed["usage"] is not None:
        # Mirrors langchain's `_create_usage_metadata_responses` so this
        # fallback feeds the same UsageMetadata shape as the parsed-object
        # path; without it every SSE-text call is recorded as estimated usage
        # with a zero cache breakdown in llm_usage events.
        message.usage_metadata = _usage_metadata_from_responses(parsed["usage"])
    return ChatResult(generations=[ChatGeneration(message=message)])


def _usage_metadata_from_responses(token_usage: dict[str, Any]) -> dict[str, Any]:
    """Responses-API ``usage`` payload -> langchain ``UsageMetadata`` mapping.

    Handles both the official field names (``input_tokens_details`` /
    ``output_tokens_details``) and OpenAI-compatible gateway spellings
    (``prompt_tokens_details`` / ``completion_tokens_details``, plus the
    DeepSeek-style top-level ``prompt_cache_hit_tokens``), mirroring the
    aliases ``usage_capture._usage_from_token_usage`` already accepts.

    ``input_tokens`` keeps the provider's prompt total (cache tokens are a
    subset, reported under ``input_token_details``) — the same convention as
    langchain's own ``_create_usage_metadata``/``_create_usage_metadata_responses``.
    This is the single authoritative prompt-total field: consumers must read
    ``input_tokens`` as the full prompt size and subtract the cache details
    themselves if they need the uncached share. The ARC ``llm_usage`` event's
    ``input`` field is exactly that derived share (prompt total minus cache
    read/write, computed once downstream in ``usage_capture``); the
    ``test_sse_usage_to_llm_usage_event_contract`` test pins the end-to-end
    conversion so the two conventions cannot drift apart silently.
    """

    def _int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    input_details = (
        token_usage.get("input_tokens_details")
        if isinstance(token_usage.get("input_tokens_details"), dict)
        else token_usage.get("prompt_tokens_details")
    ) or {}
    output_details = (
        token_usage.get("output_tokens_details")
        if isinstance(token_usage.get("output_tokens_details"), dict)
        else token_usage.get("completion_tokens_details")
    ) or {}
    cache_read = _int(
        input_details.get("cached_tokens")
        # DeepSeek-style gateways report the cache-hit count at the usage top
        # level instead of inside a details object (same alias as
        # usage_capture._usage_from_token_usage).
        or token_usage.get("prompt_cache_hit_tokens")
    )
    cache_write = _int(
        input_details.get("cache_write_tokens")
        or input_details.get("cache_creation_tokens")
    )
    input_tokens = _int(
        token_usage.get("input_tokens")
        if token_usage.get("input_tokens") is not None
        else token_usage.get("prompt_tokens")
    )
    output_tokens = _int(
        token_usage.get("output_tokens")
        if token_usage.get("output_tokens") is not None
        else token_usage.get("completion_tokens")
    )
    # A provider's explicit total wins; when absent (or the contradictory
    # zero-with-nonzero-input shape some gateways emit mid-retry) it falls
    # back to input+output. ARC's only consumer (usage_capture) never reads
    # total_tokens - it recomputes total = input + output + cache itself -
    # so a zero here cannot leak into llm_usage events.
    usage_metadata: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _int(token_usage.get("total_tokens")) or input_tokens + output_tokens,
    }
    input_token_details: dict[str, int] = {}
    if cache_read:
        input_token_details["cache_read"] = cache_read
    if cache_write:
        input_token_details["cache_creation"] = cache_write
    if input_token_details:
        usage_metadata["input_token_details"] = input_token_details
    reasoning = _int(output_details.get("reasoning_tokens") or output_details.get("reasoning"))
    if reasoning:
        usage_metadata["output_token_details"] = {"reasoning": reasoning}
    return usage_metadata


def _parse_responses_sse(payload: str) -> dict[str, Any]:
    text_by_output_index: dict[int, list[str]] = {}
    tool_calls: list[dict[str, Any]] = []
    invalid_tool_calls: list[dict[str, Any]] = []
    response_status = ""
    incomplete_details: dict[str, Any] | None = None
    completed_usage: dict[str, Any] | None = None
    fallback_usage: dict[str, Any] | None = None
    current_event = ""

    for raw_line in str(payload or "").splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = _loads_json_line(line.split(":", 1)[1].strip())
        if not isinstance(data, dict):
            continue
        if current_event == "response.output_text.done":
            text = str(data.get("text", "") or "")
            if text:
                output_index = data.get("output_index")
                if not isinstance(output_index, int):
                    output_index = 0
                text_by_output_index.setdefault(output_index, []).append(text)
            continue
        if current_event in ("response.completed", "response.incomplete"):
            response = data.get("response")
            if isinstance(response, dict):
                response_status = str(response.get("status") or "") or response_status
                # Track the latest terminal event: a stale incomplete_details
                # from an earlier event must not outlive a completed status,
                # or the truncation guard would misfire on a finished response.
                details = response.get("incomplete_details")
                incomplete_details = details if isinstance(details, dict) else None
                usage = response.get("usage")
                if isinstance(usage, dict):
                    if current_event == "response.completed":
                        # A completed event is the authoritative billing record:
                        # it wins regardless of arrival order and never gets
                        # downgraded by a later truncated incomplete replay.
                        completed_usage = usage
                    elif completed_usage is None:
                        # An incomplete event's usage is a fallback for streams
                        # that never produced a completed event.
                        fallback_usage = usage
            continue
        if current_event != "response.output_item.done":
            continue
        item = data.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        _append_tool_call(item, tool_calls, invalid_tool_calls)

    content = ""
    if text_by_output_index:
        content = "\n".join(text_by_output_index[max(text_by_output_index)])
    return {
        "content": content,
        "tool_calls": tool_calls,
        "invalid_tool_calls": invalid_tool_calls,
        "response_status": response_status,
        "incomplete_details": incomplete_details,
        "usage": completed_usage if completed_usage is not None else fallback_usage,
    }


def _append_tool_call(
    item: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    invalid_tool_calls: list[dict[str, Any]],
) -> None:
    name = str(item.get("name", "") or "")
    call_id = str(item.get("call_id") or item.get("id") or "")
    arguments = item.get("arguments", "{}")
    if isinstance(arguments, dict):
        args = arguments
    else:
        args = None
    if args is not None:
        tool_calls.append(
            {
                "type": "tool_call",
                "name": name,
                "args": args,
                "id": call_id,
            }
        )
        return
    try:
        args = json.loads(str(arguments or "{}"), strict=False)
        if not isinstance(args, dict):
            args = {"__arg1": args}
    except JSONDecodeError as exc:
        invalid_tool_calls.append(
            {
                "type": "invalid_tool_call",
                "name": name,
                "args": str(arguments or ""),
                "id": call_id,
                "error": str(exc),
            }
        )
        return
    tool_calls.append(
        {
            "type": "tool_call",
            "name": name,
            "args": args,
            "id": call_id,
        }
    )


def _loads_json_line(value: str) -> Any:
    if not value or value == "[DONE]":
        return None
    try:
        return json.loads(value)
    except JSONDecodeError:
        return None

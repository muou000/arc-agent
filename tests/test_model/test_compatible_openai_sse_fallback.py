"""The Responses-API SSE text fallback must preserve truncation metadata.

``_chat_result_from_sse_text`` builds the AIMessage by hand, so the terminal
``response.completed``/``response.incomplete`` status used to be dropped. ARC's
truncation guard reads ``status``/``incomplete_details`` from
``response_metadata``; the fallback path must surface them or truncated tool
calls would look complete to the agent loop. The same hand-built message also
used to drop the terminal event's ``usage`` block, which made every SSE-text
call record as estimated usage with a zero cache breakdown in ``llm_usage``
events even while the gateway was serving prompt-cache hits.
"""

from __future__ import annotations

import json
from typing import Any

from agents.model.compatible_openai import _chat_result_from_sse_text
from agents.model.usage_capture import extract_usage_from_chat_result
from agents.runtime.factory import _message_hit_output_limit


def _sse(
    final_event: str,
    response: dict[str, Any],
    tool_arguments: str = '{"file_path": "/workspace/a.ts"}',
) -> str:
    item = {
        "type": "function_call",
        "name": "write_file",
        "call_id": "call-1",
        "arguments": tool_arguments,
    }
    events = [
        ("response.output_text.done", {"type": "response.output_text.done", "text": "partial", "output_index": 0}),
        ("response.output_item.done", {"type": "response.output_item.done", "item": item}),
        (final_event, {"type": final_event, "response": response}),
    ]
    lines: list[str] = []
    for event, data in events:
        lines.append(f"event: {event}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return "\n".join(lines)


def test_incomplete_response_keeps_truncation_metadata() -> None:
    payload = _sse(
        "response.incomplete",
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    )

    result = _chat_result_from_sse_text(payload)
    message = result.generations[0].message

    assert message.response_metadata["status"] == "incomplete"
    assert message.response_metadata["incomplete_details"] == {"reason": "max_output_tokens"}
    assert message.response_metadata["sse_text_fallback"] is True
    assert message.tool_calls, "the parsed tool call itself is unchanged"


def test_completed_response_carries_completed_status() -> None:
    payload = _sse("response.completed", {"status": "completed", "incomplete_details": None})

    message = _chat_result_from_sse_text(payload).generations[0].message

    assert message.response_metadata["status"] == "completed"
    assert "incomplete_details" not in message.response_metadata


def test_unparseable_truncated_arguments_still_land_in_invalid_tool_calls() -> None:
    payload = _sse(
        "response.incomplete",
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        tool_arguments='{"file_path": "/workspace/a.ts", "content": "con',
    )

    message = _chat_result_from_sse_text(payload).generations[0].message

    assert message.tool_calls == []
    assert len(message.invalid_tool_calls) == 1
    assert message.invalid_tool_calls[0]["id"] == "call-1"


def test_later_completed_event_clears_stale_incomplete_details() -> None:
    """incomplete_details must follow the latest terminal event.

    A stream that first reports response.incomplete and then closes with
    response.completed (e.g. a proxy replaying events) must not leave stale
    truncation details behind: the guard would otherwise intercept a
    response whose status says completed.
    """

    payload = _sse(
        "response.incomplete",
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    ) + _sse("response.completed", {"status": "completed", "incomplete_details": None})

    message = _chat_result_from_sse_text(payload).generations[0].message

    assert message.response_metadata["status"] == "completed"
    assert "incomplete_details" not in message.response_metadata
    assert not _message_hit_output_limit(message)


def test_fallback_result_feeds_the_truncation_guard() -> None:
    payload = _sse(
        "response.incomplete",
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    )

    message = _chat_result_from_sse_text(payload).generations[0].message

    assert _message_hit_output_limit(message)


def test_responses_style_usage_survives_the_fallback() -> None:
    """Terminal-event usage must land in usage_metadata, not get dropped.

    The gateway reports prompt-cache hits in the ``response.completed`` usage
    block; losing it made every SSE-text call count as estimated usage with
    ``cache_read == 0`` in llm_usage events.
    """

    payload = _sse(
        "response.completed",
        {
            "status": "completed",
            "usage": {
                "input_tokens": 5000,
                "output_tokens": 800,
                "total_tokens": 5800,
                "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 500},
                "output_tokens_details": {"reasoning_tokens": 200},
            },
        },
    )

    message = _chat_result_from_sse_text(payload).generations[0].message

    assert message.usage_metadata == {
        "input_tokens": 5000,
        "output_tokens": 800,
        "total_tokens": 5800,
        "input_token_details": {"cache_read": 4000, "cache_creation": 500},
        "output_token_details": {"reasoning": 200},
    }
    usage = extract_usage_from_chat_result(_chat_result_from_sse_text(payload))
    assert usage is not None
    assert usage["input"] == 500  # 5000 prompt - 4000 cached - 500 written
    assert usage["cache_read"] == 4000
    assert usage["cache_write"] == 500
    assert usage["reasoning"] == 200


def test_gateway_chat_style_usage_aliases_are_accepted() -> None:
    """OpenAI-compatible gateways spell usage with chat-completions field names."""

    payload = _sse(
        "response.completed",
        {
            "status": "completed",
            "usage": {
                "prompt_tokens": 12000,
                "completion_tokens": 300,
                "total_tokens": 12300,
                "prompt_tokens_details": {"cached_tokens": 11000},
            },
        },
    )

    usage = extract_usage_from_chat_result(_chat_result_from_sse_text(payload))

    assert usage is not None
    assert usage["input"] == 1000  # 12000 prompt - 11000 cached
    assert usage["cache_read"] == 11000
    assert usage["output"] == 300


def test_missing_usage_keeps_the_estimate_fallback() -> None:
    payload = _sse("response.completed", {"status": "completed"})

    message = _chat_result_from_sse_text(payload).generations[0].message

    assert message.usage_metadata is None
    assert extract_usage_from_chat_result(_chat_result_from_sse_text(payload)) is None


def test_latest_terminal_event_usage_wins() -> None:
    """A replayed stream must not leave a stale usage block behind."""

    stale = _sse(
        "response.incomplete",
        {
            "status": "incomplete",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )
    final = _sse(
        "response.completed",
        {
            "status": "completed",
            "usage": {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250},
        },
    )

    usage = extract_usage_from_chat_result(_chat_result_from_sse_text(stale + final))

    assert usage is not None
    assert usage["input"] == 200
    assert usage["output"] == 50


def test_incomplete_replay_does_not_downgrade_detailed_usage() -> None:
    """A terminal incomplete after a completed event must not drop cache hits.

    A replaying proxy may emit response.completed (with the authoritative
    cache-detailed usage) and then response.incomplete whose usage block is
    truncated or detail-free. The detailed usage survives; only the status
    follows the latest event.
    """

    completed = _sse(
        "response.completed",
        {
            "status": "completed",
            "usage": {
                "prompt_tokens": 12000,
                "completion_tokens": 300,
                "total_tokens": 12300,
                "prompt_tokens_details": {"cached_tokens": 11000},
            },
        },
    )
    incomplete = _sse(
        "response.incomplete",
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"prompt_tokens": 100, "completion_tokens": 3, "total_tokens": 103},
        },
    )

    result = _chat_result_from_sse_text(completed + incomplete)
    message = result.generations[0].message

    # Status follows the latest terminal event (truncation stays observable)...
    assert message.response_metadata["status"] == "incomplete"
    # ...but the cache-detailed usage of the completed event is kept.
    usage = extract_usage_from_chat_result(result)
    assert usage is not None
    assert usage["cache_read"] == 11000
    assert usage["input"] == 1000  # 12000 prompt - 11000 cached
    assert usage["output"] == 300

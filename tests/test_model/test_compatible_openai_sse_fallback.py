"""The Responses-API SSE text fallback must preserve truncation metadata.

``_chat_result_from_sse_text`` builds the AIMessage by hand, so the terminal
``response.completed``/``response.incomplete`` status used to be dropped. ARC's
truncation guard reads ``status``/``incomplete_details`` from
``response_metadata``; the fallback path must surface them or truncated tool
calls would look complete to the agent loop.
"""

from __future__ import annotations

import json
from typing import Any

from agents.model.compatible_openai import _chat_result_from_sse_text
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


def test_fallback_result_feeds_the_truncation_guard() -> None:
    payload = _sse(
        "response.incomplete",
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    )

    message = _chat_result_from_sse_text(payload).generations[0].message

    assert _message_hit_output_limit(message)

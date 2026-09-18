"""File-tool payloads must be strings, or fail loudly at the tool boundary.

A model that wraps file content in an envelope object (``{"$text": ...}`` was
observed on the 12306 benchmark) used to have that object stringified into the
workspace; repairing the corrupted file then burned dozens of agent turns. The
sanitizer unwraps the unambiguous envelope shape and rejects everything else
before the tool executes.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.runtime.factory import ToolArgumentSanitizerMiddleware, _unwrap_text_envelope


def _request(tool_name: str, args: dict, call_id: str = "call_1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": call_id},
        tool=None,
        state=None,
        runtime=None,
    )


def _handler(recorder: list):
    async def handler(request: ToolCallRequest):
        recorder.append(request.tool_call)
        return "tool-executed"

    return handler


def test_single_key_text_envelope_is_unwrapped() -> None:
    assert _unwrap_text_envelope({"$text": "const x = 1;"}) == "const x = 1;"
    assert _unwrap_text_envelope({"text": "body"}) == "body"
    assert _unwrap_text_envelope({"content": "body"}) == "body"


def test_other_shapes_are_left_for_rejection() -> None:
    assert _unwrap_text_envelope(["1", "2"]) == ["1", "2"]
    assert _unwrap_text_envelope({"$text": 1}) == {"$text": 1}
    assert _unwrap_text_envelope({"a": "1", "b": "2"}) == {"a": "1", "b": "2"}
    assert _unwrap_text_envelope("plain") == "plain"


def test_write_file_envelope_is_unwrapped_before_execution() -> None:
    recorder: list = []
    middleware = ToolArgumentSanitizerMiddleware()
    request = _request("write_file", {"file_path": "a.ts", "content": {"$text": "body"}})

    result = asyncio.run(middleware.awrap_tool_call(request, _handler(recorder)))

    assert result == "tool-executed"
    assert recorder[0]["args"]["content"] == "body"


def test_append_file_envelope_is_unwrapped_before_execution() -> None:
    recorder: list = []
    middleware = ToolArgumentSanitizerMiddleware()
    request = _request("append_file", {"file_path": "a.ts", "content": {"$text": "body"}})

    result = asyncio.run(middleware.awrap_tool_call(request, _handler(recorder)))

    assert result == "tool-executed"
    assert recorder[0]["args"]["content"] == "body"


def test_write_file_non_string_payload_is_rejected_without_executing() -> None:
    recorder: list = []
    middleware = ToolArgumentSanitizerMiddleware()
    request = _request("write_file", {"file_path": "a.ts", "content": ["line1"]})

    result = asyncio.run(middleware.awrap_tool_call(request, _handler(recorder)))

    assert isinstance(result, ToolMessage)
    assert "must be a plain string" in result.content
    assert recorder == [], "the tool must not run with a corrupted payload"


def test_edit_file_anchors_are_sanitized() -> None:
    recorder: list = []
    middleware = ToolArgumentSanitizerMiddleware()
    request = _request(
        "edit_file",
        {"file_path": "a.ts", "old_string": {"text": "old"}, "new_string": "new"},
    )

    asyncio.run(middleware.awrap_tool_call(request, _handler(recorder)))

    assert recorder[0]["args"]["old_string"] == "old"
    assert recorder[0]["args"]["new_string"] == "new"


def test_unrelated_tools_pass_through_untouched() -> None:
    recorder: list = []
    middleware = ToolArgumentSanitizerMiddleware()
    request = _request("grep", {"pattern": {"text": "x"}})

    asyncio.run(middleware.awrap_tool_call(request, _handler(recorder)))

    assert recorder[0]["args"] == {"pattern": {"text": "x"}}


def test_string_args_are_not_touched() -> None:
    recorder: list = []
    middleware = ToolArgumentSanitizerMiddleware()
    request = _request("write_file", {"file_path": "a.ts", "content": "already fine"})

    asyncio.run(middleware.awrap_tool_call(request, _handler(recorder)))

    assert recorder[0]["args"] == {"file_path": "a.ts", "content": "already fine"}


def test_rejection_carries_the_tool_call_id() -> None:
    middleware = ToolArgumentSanitizerMiddleware()
    request = _request("write_file", {"file_path": "a.ts", "content": {"oops": 1}}, call_id="call_9")

    result = asyncio.run(middleware.awrap_tool_call(request, _handler([])))

    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call_9"

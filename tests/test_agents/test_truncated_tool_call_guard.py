"""Tool calls from output-truncated responses must fail, not execute.

pi lesson (``packages/agent/src/agent-loop.ts``, ``failToolCallsFromTruncatedMessage``):
a response cut off by the output token limit can end mid-argument, and the
partial tool-call payload may still parse and validate — so none of the calls
in a truncated message are safe to run. The guard rewrites the model response
so every unanswered call gets an explicit error tool result and the loop hands
control back to the model to re-issue them with complete arguments.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from agents.runtime.factory import TruncatedToolCallGuardMiddleware, _message_hit_output_limit


def _tool_call_message(*call_ids: str, metadata: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "write_file", "args": {"file_path": "/workspace/a.ts", "content": "const x"}, "id": call_id, "type": "tool_call"}
            for call_id in call_ids
        ],
        response_metadata=metadata,
    )


def _model_response(*messages: Any, structured_response: Any = None) -> ModelResponse:
    return ModelResponse(result=list(messages), structured_response=structured_response)


def _handler(response: ModelResponse, recorder: list):
    async def handler(request) -> ModelResponse:
        recorder.append(request)
        return response

    return handler


def test_length_truncated_tool_calls_get_error_results() -> None:
    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = _tool_call_message("call-truncated", metadata={"finish_reason": "length"})
    response = _model_response(message)

    result = asyncio.run(
        middleware.awrap_model_call(None, _handler(response, recorder))
    )

    assert recorder, "handler must still have been invoked"
    assert result is not response
    assert result.result[0] is message, "the original message must be kept so its tool_calls stay answerable"
    rejection = result.result[1]
    assert isinstance(rejection, ToolMessage)
    assert rejection.tool_call_id == "call-truncated"
    assert rejection.status == "error"
    assert "output token limit" in rejection.content
    assert "Re-issue" in rejection.content


def test_incomplete_details_without_status_is_intercepted() -> None:
    """Proxies may pass through incomplete_details only; that still marks a cut."""

    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = _tool_call_message(
        "call-truncated",
        metadata={"incomplete_details": {"reason": "max_output_tokens"}},
    )

    result = asyncio.run(
        middleware.awrap_model_call(None, _handler(_model_response(message), recorder))
    )

    rejections = [m for m in result.result if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in rejections] == ["call-truncated"]


def test_responses_api_incomplete_status_is_intercepted() -> None:
    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = _tool_call_message(
        "call-truncated",
        metadata={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    )

    result = asyncio.run(
        middleware.awrap_model_call(None, _handler(_model_response(message), recorder))
    )

    rejections = [m for m in result.result if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in rejections] == ["call-truncated"]


def test_completed_response_passes_through_unchanged() -> None:
    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = _tool_call_message("call-ok", metadata={"finish_reason": "tool_calls"})
    response = _model_response(message)

    result = asyncio.run(
        middleware.awrap_model_call(None, _handler(response, recorder))
    )

    assert result is response, "no rewrites without a truncation signal"
    assert not any(isinstance(m, ToolMessage) for m in result.result)


def test_chat_completions_tool_calls_finish_reason_is_not_intercepted() -> None:
    middleware = TruncatedToolCallGuardMiddleware()
    message = AIMessage(content="", tool_calls=[], response_metadata={"finish_reason": "tool_calls"})

    result = asyncio.run(
        middleware.awrap_model_call(None, _handler(_model_response(message), []))
    )

    assert not any(isinstance(m, ToolMessage) for m in result.result)


def test_already_answered_tool_calls_are_not_duplicated() -> None:
    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = _tool_call_message("call-a", "call-b", metadata={"finish_reason": "length"})
    answered = ToolMessage(content="structured output", tool_call_id="call-b")
    response = _model_response(message, answered)

    result = asyncio.run(
        middleware.awrap_model_call(None, _handler(response, recorder))
    )

    answered_ids = [m.tool_call_id for m in result.result if isinstance(m, ToolMessage)]
    assert answered_ids.count("call-b") == 1, "the pre-existing result must not be duplicated"
    assert "call-a" in answered_ids, "the unanswered call must get an error result"


def test_invalid_tool_calls_also_get_error_results() -> None:
    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = AIMessage(
        content="",
        tool_calls=[],
        invalid_tool_calls=[
            {"name": "write_file", "args": '{"file_path": "/workspace/a.ts", "content": "con', "id": "call-bad", "error": "Expecting value", "type": "invalid_tool_call"}
        ],
        response_metadata={"finish_reason": "length"},
    )

    result = asyncio.run(
        middleware.awrap_model_call(None, _handler(_model_response(message), recorder))
    )

    rejections = [m for m in result.result if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in rejections] == ["call-bad"]
    assert "could not be parsed" in rejections[0].content


def test_structured_response_is_preserved() -> None:
    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = _tool_call_message("call-truncated", metadata={"finish_reason": "length"})
    structured = {"summary": "partial"}

    result = asyncio.run(
        middleware.awrap_model_call(
            None, _handler(_model_response(message, structured_response=structured), recorder)
        )
    )

    assert result.structured_response is structured


def test_sync_wrap_model_call_intercepts_too() -> None:
    recorder: list = []
    middleware = TruncatedToolCallGuardMiddleware()
    message = _tool_call_message("call-truncated", metadata={"finish_reason": "length"})

    def sync_handler(request) -> ModelResponse:
        recorder.append(request)
        return _model_response(message)

    result = middleware.wrap_model_call(None, sync_handler)

    assert isinstance(result.result[1], ToolMessage)
    assert result.result[1].tool_call_id == "call-truncated"


def test_response_without_result_attribute_passes_through() -> None:
    middleware = TruncatedToolCallGuardMiddleware()
    passthrough = object()

    assert middleware._fail_truncated_tool_calls(passthrough) is passthrough


def test_message_hit_output_limit_metadata_variants() -> None:
    assert _message_hit_output_limit(AIMessage(content="", response_metadata={"finish_reason": "length"}))
    assert _message_hit_output_limit(AIMessage(content="", response_metadata={"status": "incomplete"}))
    assert _message_hit_output_limit(AIMessage(content="", response_metadata={"incomplete_details": {"reason": "max_output_tokens"}}))
    assert _message_hit_output_limit(AIMessage(content="", response_metadata={"incomplete_details": {"reason": "content_filter"}}))
    assert not _message_hit_output_limit(AIMessage(content="", response_metadata={"finish_reason": "stop"}))
    assert not _message_hit_output_limit(AIMessage(content="", response_metadata={"finish_reason": "tool_calls"}))
    assert not _message_hit_output_limit(AIMessage(content="", response_metadata={"status": "completed"}))
    assert not _message_hit_output_limit(AIMessage(content="", response_metadata={"incomplete_details": None}))
    assert not _message_hit_output_limit(AIMessage(content="", response_metadata={}))
    assert not _message_hit_output_limit(AIMessage(content=""))
    assert not _message_hit_output_limit(ToolMessage(content="x", tool_call_id="call-1"))

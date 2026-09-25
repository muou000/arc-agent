"""Unit tests for ``ToolUsageMiddleware`` (agents/runtime/tool_usage.py).

The middleware observes every stage-agent tool round-trip (including calls
blocked by ``StageDisciplineMiddleware``, which runs one layer inward) and
dispatches one ``ToolUsageRecord`` per call to the process-wide sink. These
tests wire a capturing sink directly, so no agent runtime is needed.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.model.usage_capture import llm_usage_context
from agents.runtime.stage_discipline import StageDisciplineMiddleware
from agents.runtime.tool_usage import (
    ToolUsageMiddleware,
    get_tool_usage_sink,
    record_tool_usage,
    set_tool_usage_sink,
)
from tests.helpers.tool_result_texts import ALL_ZERO_BUILD_RESULT, MIXED_BUILD_RESULT


def make_request(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    call_id: str = "call-1",
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id},
        tool=None,
        state={},
        runtime=None,
    )


def ok_tool(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="line one\nline two", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


def error_tool(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(
        content="Error: file not found",
        name=request.tool_call["name"],
        tool_call_id=request.tool_call["id"],
        status="error",
    )


def setup_function() -> None:
    set_tool_usage_sink(None)


def teardown_function() -> None:
    set_tool_usage_sink(None)


def test_ok_call_is_recorded_with_context_attribution() -> None:
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    with llm_usage_context("REQ-1", "IMPLEMENT"):
        result = middleware.wrap_tool_call(make_request("edit_file", {"file_path": "/workspace/src/app.ts"}), ok_tool)

    assert isinstance(result, ToolMessage) and result.content == "line one\nline two"
    assert len(records) == 1
    record = records[0]
    assert record.tool == "edit_file"
    assert record.node_id == "REQ-1"
    assert record.phase == "IMPLEMENT"
    assert record.status == "ok"
    assert record.path == "/workspace/src/app.ts"
    assert record.result_chars == len("line one\nline two")
    assert record.offset is None and record.limit is None


def test_read_file_records_pagination_and_unpaged_reads_stay_none() -> None:
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("read_file", {"file_path": "/workspace/src/big.ts", "offset": 200, "limit": 100}), ok_tool
    )
    # No explicit limit: the whole-file-read signal (limit stays None).
    middleware.wrap_tool_call(make_request("read_file", {"file_path": "/workspace/src/big.ts"}), ok_tool)

    assert [record.limit for record in records] == [100, None]
    assert [record.offset for record in records] == [200, None]
    assert records[1].status == "ok"


def test_blocked_call_is_recorded_when_outermost() -> None:
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    usage = ToolUsageMiddleware()
    discipline = StageDisciplineMiddleware(stage="interface_design")
    path = "/workspace/src/dup.ts"

    with llm_usage_context("REQ-2", "DESIGN"):
        first = usage.wrap_tool_call(
            make_request("write_file", {"file_path": path, "content": "a\n"}, call_id="c1"),
            lambda req: discipline.wrap_tool_call(req, ok_tool),
        )
        second = usage.wrap_tool_call(
            make_request("write_file", {"file_path": path, "content": "b\n"}, call_id="c2"),
            lambda req: discipline.wrap_tool_call(req, ok_tool),
        )

    assert isinstance(first, ToolMessage) and first.status != "error"
    assert isinstance(second, ToolMessage) and "Repeated write blocked" in second.content
    statuses = [(record.tool, record.status) for record in records]
    assert statuses == [("write_file", "ok"), ("write_file", "blocked")]
    assert records[1].node_id == "REQ-2"


def test_error_result_is_recorded_as_error() -> None:
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(make_request("grep", {"query": "missing"}), error_tool)

    assert records[0].status == "error"
    assert records[0].result_chars == len("Error: file not found")


def test_string_content_with_mixed_exit_code_is_recorded_as_error() -> None:
    # run_build's ToolMessage carries the two concatenated build results with
    # no error status; the observation must apply the same failure predicate
    # as the write-lock discipline instead of reading "ok" off the absence
    # of a message status.
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("run_build"),
        lambda request: ToolMessage(content=MIXED_BUILD_RESULT, name="run_build", tool_call_id=request.tool_call["id"]),
    )

    assert records[0].tool == "run_build"
    assert records[0].status == "error"
    assert records[0].result_chars == len(MIXED_BUILD_RESULT)


def test_string_content_with_all_zero_exit_codes_is_recorded_as_ok() -> None:
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("run_build"),
        lambda request: ToolMessage(
            content=ALL_ZERO_BUILD_RESULT, name="run_build", tool_call_id=request.tool_call["id"]
        ),
    )

    assert records[0].status == "ok"


def test_read_of_log_with_failure_markers_is_recorded_as_ok() -> None:
    # arc-output-serial-4 REQ-1: the 21:41:19 read_file of Integration-008.log
    # returned its 6419 chars fine but was recorded status=error because the
    # log text holds build-failure markers. A read's result text is the file's
    # content, not a verdict the tool computed — the same transcription
    # run_build produces (MIXED_BUILD_RESULT) reads as ok when it is what a
    # read or a grep fetched.
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("read_file", {"file_path": "/workspace/Integration-008.log"}),
        lambda request: ToolMessage(
            content=MIXED_BUILD_RESULT, name="read_file", tool_call_id=request.tool_call["id"]
        ),
    )
    middleware.wrap_tool_call(
        make_request("grep", {"query": "Exit Code"}),
        lambda request: ToolMessage(
            content="/workspace/Integration-008.log:2: Exit Code: 1", name="grep", tool_call_id=request.tool_call["id"]
        ),
    )

    assert [(record.tool, record.status) for record in records] == [("read_file", "ok"), ("grep", "ok")]


def test_read_whose_content_starts_with_an_error_line_is_recorded_as_ok() -> None:
    # Full content-fetch exemption: the result text is the file's body
    # (line-numbered or verbatim, depending on the renderer), so even a file
    # that opens with an error line cannot make the read itself a failure —
    # deepagents marks read failures with ToolMessage status instead.
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("read_file", {"file_path": "/workspace/build.log"}),
        lambda request: ToolMessage(
            content="Error: build failed\nExit Code: 1\nSTDERR:\n...", name="read_file", tool_call_id=request.tool_call["id"]
        ),
    )

    assert records[0].status == "ok"


def test_failed_read_is_still_recorded_as_error() -> None:
    # The narrowing only lifts content-inherited markers: a read that itself
    # failed keeps its ToolMessage error status.
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("read_file", {"file_path": "/workspace/missing.log"}),
        lambda request: ToolMessage(
            content="Error: file not found",
            name="read_file",
            tool_call_id=request.tool_call["id"],
            status="error",
        ),
    )

    assert records[0].status == "error"


def test_run_tests_failure_output_is_recorded_as_error() -> None:
    # run_tests carries the execution verdict: its failure transcript must
    # stay status=error under the aggregate reading (#196), unchanged by the
    # read-class narrowing (#220).
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("run_tests"),
        lambda request: ToolMessage(
            content="Exit Code: 1\nSTDERR:\n2 failed, 3 passed in 1.2s", name="run_tests", tool_call_id=request.tool_call["id"]
        ),
    )

    assert records[0].tool == "run_tests"
    assert records[0].status == "error"


def test_string_content_without_exit_code_is_recorded_as_ok() -> None:
    # Fail-open: text with no parseable exit-code segment keeps the old
    # ok-by-default reading (only Error-prefixed text reads as failed).
    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    middleware.wrap_tool_call(
        make_request("run_build"),
        lambda request: ToolMessage(
            content="Command timed out after 120.0 seconds.", name="run_build", tool_call_id=request.tool_call["id"]
        ),
    )

    assert records[0].status == "ok"


def test_missing_sink_is_a_noop() -> None:
    middleware = ToolUsageMiddleware()
    result = middleware.wrap_tool_call(make_request("read_file", {"file_path": "/workspace/src/a.ts"}), ok_tool)
    assert isinstance(result, ToolMessage)
    assert get_tool_usage_sink() is None


def test_broken_sink_never_breaks_the_tool_call() -> None:
    def broken_sink(record: Any) -> None:
        raise RuntimeError("sink exploded")

    set_tool_usage_sink(broken_sink)
    middleware = ToolUsageMiddleware()

    result = middleware.wrap_tool_call(make_request("edit_file", {"file_path": "/workspace/src/a.ts"}), ok_tool)

    assert isinstance(result, ToolMessage) and result.content == "line one\nline two"


def test_record_tool_usage_outside_context_has_empty_attribution() -> None:
    records: list[Any] = []
    set_tool_usage_sink(records.append)

    record_tool_usage(tool="grep", status="ok", result_chars=0)

    assert records[0].node_id == ""
    assert records[0].phase == ""


def test_async_wrap_records_usage() -> None:
    import asyncio

    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()

    async def async_ok(request: ToolCallRequest) -> ToolMessage:
        return ok_tool(request)

    async def run() -> Any:
        return await middleware.awrap_tool_call(
            make_request("read_file", {"file_path": "/workspace/src/a.ts", "limit": 50}), async_ok
        )

    result = asyncio.run(run())
    assert isinstance(result, ToolMessage)
    assert len(records) == 1
    assert records[0].limit == 50


def test_glob_sentinel_receipt_carries_result_text_for_telemetry() -> None:
    """The raw receipt text travels with the record so the SDK's
    ``result_empty`` classification can recognize the upstream empty-result
    sentinel texts (a bare ``result_chars`` count cannot)."""

    records: list[Any] = []
    set_tool_usage_sink(records.append)
    middleware = ToolUsageMiddleware()
    receipt = ToolMessage(
        content="No files found\n\nNote: 1 match withheld by read-deny (node_modules).",
        name="glob",
        tool_call_id="call-glob-1",
    )

    def glob_tool(request: ToolCallRequest) -> ToolMessage:
        return receipt

    middleware.wrap_tool_call(make_request("glob", {"pattern": "**/cli*", "path": "/workspace"}), glob_tool)

    assert len(records) == 1
    record = records[0]
    assert record.result_chars == len(receipt.content)
    assert record.result_text == receipt.content

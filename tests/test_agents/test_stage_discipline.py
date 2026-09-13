"""Unit tests for ``StageDisciplineMiddleware`` (agents/runtime/stage_discipline.py).

The middleware guards every tool call of the three stage agents:

- ``test_generation`` may only write test assets and must not run validation.
- ``interface_design`` may materialize at most 8 small skeleton files.
- every stage blocks repeated writes/re-reads until a file-operation or
  validation failure unlocks the path again.

These tests call the middleware directly with synthetic ``ToolCallRequest``
objects, so no agent runtime is needed.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.runtime.stage_discipline import StageDisciplineMiddleware


def make_request(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    call_id: str = "call-1",
    state: dict[str, Any] | None = None,
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id},
        tool=None,
        state=state if state is not None else {},
        runtime=None,
    )


def ok_tool(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="ok", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


def run(middleware: StageDisciplineMiddleware, request: ToolCallRequest, handler=ok_tool) -> Any:
    return middleware.wrap_tool_call(request, handler)


def make(stage: str) -> StageDisciplineMiddleware:
    return StageDisciplineMiddleware(stage=stage)


# ---------------------------------------------------------------------------
# Hard-disabled tools
# ---------------------------------------------------------------------------


def test_execute_and_delete_are_always_blocked() -> None:
    for stage in ("interface_design", "test_generation", "implementation"):
        middleware = make(stage)
        for name in ("execute", "delete"):
            result = run(middleware, make_request(name))
            assert isinstance(result, ToolMessage)
            assert result.status == "error"
            assert "disabled in ARC's staged file workflow" in result.content


# ---------------------------------------------------------------------------
# test_generation stage
# ---------------------------------------------------------------------------


def test_test_generation_cannot_run_validation() -> None:
    middleware = make("test_generation")
    for name in ("run_tests", "run_build"):
        result = run(middleware, make_request(name))
        assert isinstance(result, ToolMessage) and result.status == "error"
        assert "must not run validation" in result.content


def test_test_generation_write_to_non_test_asset_is_blocked() -> None:
    middleware = make("test_generation")
    result = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/src/calc.py", "content": "x = 1\n"}),
    )
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "not a test asset" in result.content


def test_test_generation_write_to_test_asset_passes() -> None:
    middleware = make("test_generation")
    for path in (
        "/workspace/tests/unit/test_calc.py",
        "/workspace/src/app.spec.ts",
        "/workspace/vitest.config.ts",
    ):
        result = run(middleware, make_request("write_file", {"file_path": path, "content": "test\n"}))
        assert isinstance(result, ToolMessage)
        assert result.content == "ok", f"expected {path} to be a permitted test asset"


def test_test_generation_repeated_test_write_is_blocked() -> None:
    middleware = make("test_generation")
    path = "/workspace/tests/unit/test_calc.py"
    first = run(middleware, make_request("write_file", {"file_path": path, "content": "a\n"}, call_id="c1"))
    assert isinstance(first, ToolMessage) and first.content == "ok"
    second = run(middleware, make_request("write_file", {"file_path": path, "content": "b\n"}, call_id="c2"))
    assert isinstance(second, ToolMessage) and second.status == "error"
    assert "Repeated write blocked" in second.content


# ---------------------------------------------------------------------------
# implementation stage: write lock and unlock semantics
# ---------------------------------------------------------------------------


def test_implementation_repeated_write_blocked_until_failure_unlocks_path() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    args = {"file_path": path}

    assert run(middleware, make_request("write_file", {**args, "content": "v1\n"}, call_id="c1")).content == "ok"
    blocked = run(middleware, make_request("write_file", {**args, "content": "v2\n"}, call_id="c2"))
    assert blocked.status == "error" and "Repeated write blocked" in blocked.content

    # A failing validation run unlocks every written path for fixes.
    middleware._record_result(
        make_request("run_tests"),
        ToolMessage(content="Exit Code: 1\nfailed", name="run_tests", tool_call_id="t1"),
    )
    assert run(middleware, make_request("write_file", {**args, "content": "fix\n"}, call_id="c3")).content == "ok"


def test_implementation_repeated_write_blocked_until_file_error_unlocks_path() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="c1"))

    # A failing non-validation tool result on the same path unlocks only it.
    middleware._record_result(
        make_request("edit_file", {"file_path": path}),
        ToolMessage(content="Error: anchor not found", name="edit_file", tool_call_id="e1", status="error"),
    )
    assert run(middleware, make_request("write_file", {"file_path": path, "content": "v2\n"}, call_id="c2")).content == "ok"
    other = run(
        middleware, make_request("write_file", {"file_path": "/workspace/src/other.py", "content": "x\n"}, call_id="c3")
    )
    assert other.content == "ok"  # untouched path was never locked


def test_run_tests_exit_code_zero_does_not_unlock() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="c1"))
    middleware._record_result(
        make_request("run_tests"),
        ToolMessage(content="Exit Code: 0\nall good", name="run_tests", tool_call_id="t1"),
    )
    blocked = run(middleware, make_request("write_file", {"file_path": path, "content": "v2\n"}, call_id="c2"))
    assert blocked.status == "error" and "Repeated write blocked" in blocked.content


# ---------------------------------------------------------------------------
# interface_design stage: skeleton budget
# ---------------------------------------------------------------------------


def test_interface_design_blocks_more_than_eight_skeleton_writes() -> None:
    middleware = make("interface_design")
    for index in range(8):
        result = run(
            middleware,
            make_request("write_file", {"file_path": f"/workspace/src/mod_{index}.py", "content": "class A:\n"}, call_id=f"c{index}"),
        )
        assert result.content == "ok", f"write {index} should pass"
    ninth = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/src/mod_new.py", "content": "class B:\n"}, call_id="c9"),
    )
    assert ninth.status == "error" and "at most 8 small skeleton files" in ninth.content
    # Rewriting an already-written path is governed by the repeated-write rule,
    # not the skeleton budget.
    rewrite = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/src/mod_0.py", "content": "class A2:\n"}, call_id="c10"),
    )
    assert rewrite.status == "error" and "Repeated write blocked" in rewrite.content


def test_interface_design_blocks_large_files() -> None:
    middleware = make("interface_design")
    result = run(
        middleware,
        make_request(
            "write_file",
            {"file_path": "/workspace/src/big.py", "content": "\n".join(f"line {i}" for i in range(161))},
        ),
    )
    assert result.status == "error" and "small skeletons (at most 160 lines" in result.content


# ---------------------------------------------------------------------------
# read_file discipline
# ---------------------------------------------------------------------------


def test_repeated_overlapping_read_blocked_but_new_range_allowed() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    first = run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    assert first.content == "ok"
    repeated = run(middleware, make_request("read_file", {"file_path": path, "offset": 10, "limit": 50}, call_id="r2"))
    assert repeated.status == "error" and "Repeated read blocked" in repeated.content
    next_page = run(middleware, make_request("read_file", {"file_path": path, "offset": 100, "limit": 100}, call_id="r3"))
    assert next_page.content == "ok"


def test_re_reading_written_path_blocked_until_failure() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="c1"))
    blocked = run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    assert blocked.status == "error" and "Read blocked" in blocked.content
    middleware._record_result(
        make_request("run_tests"),
        ToolMessage(content="Exit Code: 1\nfailed", name="run_tests", tool_call_id="t1"),
    )
    assert run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r2")).content == "ok"


def test_bounded_read_clamps_limit_and_forces_offset_for_workspace_reads() -> None:
    middleware = make("implementation")
    captured: dict[str, Any] = {}

    def capture(request: ToolCallRequest) -> ToolMessage:
        captured.update(request.tool_call["args"])
        return ok_tool(request)

    run(
        middleware,
        make_request("read_file", {"file_path": "/workspace/src/calc.py", "offset": -5, "limit": 99999}),
        capture,
    )
    assert captured["offset"] == 0
    assert captured["limit"] == 200


def test_bounded_read_skips_non_workspace_paths() -> None:
    middleware = make("implementation")
    captured: dict[str, Any] = {}

    def capture(request: ToolCallRequest) -> ToolMessage:
        captured.update(request.tool_call["args"])
        return ok_tool(request)

    run(middleware, make_request("read_file", {"file_path": "/skills/foo/SKILL.md", "offset": 3, "limit": 7}), capture)
    assert captured["offset"] == 3
    assert captured["limit"] == 7


# ---------------------------------------------------------------------------
# state + blocked message shape
# ---------------------------------------------------------------------------


def test_state_records_written_paths_and_read_summaries() -> None:
    middleware = make("implementation")
    state: dict[str, Any] = {}
    run(middleware, make_request("write_file", {"file_path": "/workspace/src/a.py"}, call_id="c1", state=state))
    run(
        middleware,
        make_request("read_file", {"file_path": "/workspace/src/b.py", "offset": 0, "limit": 20}, call_id="r1", state=state),
    )
    assert state["arc_written_paths"] == ["/workspace/src/a.py"]
    assert "/workspace/src/b.py" in state["arc_read_summaries"]
    assert "lines 0-19" in state["arc_read_summaries"]["/workspace/src/b.py"]


def test_blocked_result_echoes_tool_call_id_and_name() -> None:
    middleware = make("implementation")
    blocked = run(middleware, make_request("execute", {"command": "rm -rf /"}, call_id="call-42"))
    assert isinstance(blocked, ToolMessage)
    assert blocked.tool_call_id == "call-42"
    assert blocked.name == "execute"
    assert blocked.content.startswith("Error: ARC stage discipline:")


def test_paths_without_file_path_are_not_validated() -> None:
    middleware = make("test_generation")
    # A tool without file_path args (e.g. traceability queries) must pass.
    result = run(middleware, make_request("get_interface", {"interface_id": "IF-1"}))
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# design-stage shared glue denylist (registration contract)
# ---------------------------------------------------------------------------


def make_design_with_glue_denylist(paths: list[str]) -> StageDisciplineMiddleware:
    return StageDisciplineMiddleware(stage="interface_design", denied_write_paths=paths)


def test_design_write_to_denied_glue_path_is_blocked() -> None:
    middleware = make_design_with_glue_denylist(["frontend/src/App.tsx", "backend/src/app.js"])
    for name in ("write_file", "edit_file"):
        args = (
            {"file_path": "/workspace/frontend/src/App.tsx", "content": "export {}"}
            if name == "write_file"
            else {"file_path": "/workspace/frontend/src/App.tsx", "old_string": "a", "new_string": "b"}
        )
        result = run(middleware, make_request(name, args))
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "must not edit shared runtime glue" in result.content
        assert "Registration Contract" in result.content


def test_denied_glue_paths_normalize_workspace_relative_entries() -> None:
    middleware = make_design_with_glue_denylist(["backend\\src\\app.js"])
    result = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/backend/src/app.js", "content": "x"}),
    )
    assert isinstance(result, ToolMessage) and result.status == "error"


def test_denied_glue_paths_do_not_block_other_stages() -> None:
    for stage in ("test_generation", "implementation"):
        middleware = StageDisciplineMiddleware(
            stage=stage, denied_write_paths=["frontend/src/App.tsx"]  # type: ignore[arg-type]
        )
        args = (
            {"file_path": "/workspace/frontend/src/App.tsx", "content": "x"}
            if stage == "implementation"
            else {"file_path": "/workspace/frontend/tests/app.test.tsx", "content": "x"}
        )
        result = run(middleware, make_request("write_file", args))
        assert result.content == "ok"


def test_design_writes_outside_the_denylist_are_still_allowed() -> None:
    middleware = make_design_with_glue_denylist(["frontend/src/App.tsx"])
    result = run(
        middleware,
        make_request(
            "write_file",
            {"file_path": "/workspace/frontend/src/sections/home/HeroSection.tsx", "content": "x"},
        ),
    )
    assert result.content == "ok"


def test_design_without_denylist_keeps_previous_behavior() -> None:
    middleware = make("interface_design")
    result = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/frontend/src/App.tsx", "content": "x"}),
    )
    assert result.content == "ok"

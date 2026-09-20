"""Unit tests for ``StageDisciplineMiddleware`` (agents/runtime/stage_discipline.py).

The middleware guards every tool call of the three stage agents:

- ``test_generation`` may only write test assets and must not run validation.
- ``interface_design`` may touch at most 12 distinct files in a pass (leaf
  default; the middleware accepts a higher tiered ceiling for non-leaf shell
  passes), counted per path across write_file/edit_file/append_file and
  reserved at validation time so parallel batches respect the cap.
- every stage blocks repeated writes/re-reads until a file-operation or
  validation failure unlocks the path again; in ``test_generation`` a
  successful delete also releases the path, so delete-then-rewrite works
  without waiting for an accidental failure to unlock it.

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


def test_execute_is_always_blocked() -> None:
    for stage in ("interface_design", "test_generation", "implementation"):
        middleware = make(stage)
        result = run(middleware, make_request("execute"))
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "disabled in ARC's staged file workflow" in result.content


def test_delete_is_blocked_outside_test_generation_and_for_non_test_assets() -> None:
    # interface_design / implementation: fully blocked.
    for stage in ("interface_design", "implementation"):
        middleware = make(stage)
        result = run(middleware, make_request("delete", {"file_path": "/workspace/tests/unit/test_calc.py"}))
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "disabled in ARC's staged file workflow" in result.content
    # test_generation: blocked for non-test assets...
    middleware = make("test_generation")
    result = run(middleware, make_request("delete", {"file_path": "/workspace/src/calc.py"}))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "disabled in ARC's staged file workflow" in result.content
    # ...and allowed for test assets (green-baseline rejection may remove a
    # tautological test file).
    result = run(middleware, make_request("delete", {"file_path": "/workspace/tests/unit/test_calc.py"}))
    assert not isinstance(result, ToolMessage) or result.status != "error"


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


def test_test_generation_repeated_write_block_lists_actionable_exits() -> None:
    # The generic "wait for an error" exit was unreachable in test_generation
    # (validation tools are disabled), so the run7 loop burned 52 blocked
    # writes; the message must name the real ways out. Since the manifest
    # lock, "write the revision to a new path" is deliberately NOT an exit
    # for test files (rename churn is the failure mode the lock exists for);
    # the ways out are delete-then-rewrite the same declared path or return
    # the manifest.
    for stage, expected in (
        ("test_generation", ("delete", "manifest")),
        ("implementation", ("run the tests", "new path")),
        ("interface_design", ("response", "skeleton")),
    ):
        middleware = make(stage)
        path = "/workspace/tests/unit/test_calc.py" if stage == "test_generation" else "/workspace/src/calc.py"
        first = run(middleware, make_request("write_file", {"file_path": path, "content": "a\n"}, call_id="c1"))
        assert first.content == "ok"
        blocked = run(middleware, make_request("write_file", {"file_path": path, "content": "b\n"}, call_id="c2"))
        assert blocked.status == "error"
        assert blocked.content.startswith("Error: ARC stage discipline:")
        assert "Repeated write blocked" in blocked.content
        for keyword in expected:
            assert keyword in blocked.content, f"{stage} write block should mention {keyword!r}"
        if stage == "test_generation":
            assert "new path" not in blocked.content


def test_interface_design_first_repeated_write_explains_unlock_condition() -> None:
    middleware = make("interface_design")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="c1"))

    first_block = run(
        middleware,
        make_request("write_file", {"file_path": path, "content": "v2\n"}, call_id="c2"),
    )
    assert first_block.status == "error"
    assert "Unlock condition" in first_block.content
    assert "failed file operation" in first_block.content
    assert "run_build" in first_block.content and "run_tests" in first_block.content
    assert "delete" in first_block.content
    assert "do not invoke validation merely to unlock it" in first_block.content
    assert "Do not retry this path" in first_block.content
    assert "final response's `interfaces` array" in first_block.content

    # The detailed hint is only needed on the first blocked rewrite for a path;
    # later blocks use the existing compact exit to avoid context churn.
    second_block = run(
        middleware,
        make_request("write_file", {"file_path": path, "content": "v3\n"}, call_id="c3"),
    )
    assert second_block.status == "error"
    assert "Unlock condition" not in second_block.content
    assert "response" in second_block.content and "skeleton" in second_block.content


def test_non_interface_stages_do_not_allocate_design_write_block_counter() -> None:
    for stage, path in (
        ("test_generation", "/workspace/tests/unit/test_calc.py"),
        ("implementation", "/workspace/src/calc.py"),
    ):
        middleware = make(stage)
        assert middleware._write_block_counts is None
        run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="c1"))
        blocked = run(
            middleware,
            make_request("write_file", {"file_path": path, "content": "v2\n"}, call_id="c2"),
        )
        assert blocked.status == "error"
        assert "Unlock condition" not in blocked.content


def test_repeated_read_blocked_only_after_the_fresh_reread_budget() -> None:
    # run7 evidence: 50 consecutive offset=0..49 probe reads, each accepted as
    # a "non-overlapping range" by the old message that suggested paginated
    # re-reads as a justification. The message must not invite that again.
    # The fbd4a73b TDD run showed the other edge: a hard block on the first
    # re-read pushed the agent to rebuild file content with 10 consecutive
    # greps. The budget serves the legitimate fresh re-reads first and only
    # then blocks the loop.
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    first = run(middleware, make_request("read_file", {"file_path": path, "offset": 10, "limit": 50}, call_id="r2"))
    assert first.content == "ok"
    second = run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r3"))
    assert second.content == "ok"
    blocked = run(middleware, make_request("read_file", {"file_path": path, "offset": 10, "limit": 50}, call_id="r4"))
    assert blocked.status == "error"
    assert "Repeated read blocked" in blocked.content
    assert "non-overlapping" not in blocked.content
    assert "offset" in blocked.content
    # arc-output4 REQ-2: after this block the TestGenerator fired four narrow
    # `limit: 5` probes at freshly written test files. The message must state
    # up front that a narrower limit or shifted offset is the same blocked
    # read, so the retry never looks like an unexplored exit.
    assert "A narrower limit or shifted offset is the same blocked read." in blocked.content


def test_failed_read_unlocks_the_path_beyond_the_read_budget() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    read = {"file_path": path, "offset": 0, "limit": 100}
    run(middleware, make_request("read_file", read, call_id="r1"))
    run(middleware, make_request("read_file", read, call_id="r2"))
    run(middleware, make_request("read_file", read, call_id="r3"))
    blocked = run(middleware, make_request("read_file", read, call_id="r4"))
    assert blocked.status == "error"

    # A failed file operation is the generic unlock: the very next re-read
    # must pass regardless of the budget (only successful reads consume it).
    middleware._record_result(
        make_request("read_file", read),
        ToolMessage(content="Error: transient read failure", name="read_file", tool_call_id="t0", status="error"),
    )
    assert run(middleware, make_request("read_file", read, call_id="r5")).content == "ok"


def test_validation_failure_unlocks_re_reads_beyond_the_budget() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    read = {"file_path": path, "offset": 0, "limit": 100}
    run(middleware, make_request("read_file", read, call_id="r1"))
    run(middleware, make_request("read_file", read, call_id="r2"))
    run(middleware, make_request("read_file", read, call_id="r3"))
    blocked = run(middleware, make_request("read_file", read, call_id="r4"))
    assert blocked.status == "error"

    middleware._record_result(
        make_request("run_tests"),
        ToolMessage(content="Exit Code: 1\nfailed", name="run_tests", tool_call_id="t1"),
    )
    # A failing validation unlocks the paths: the TDD fail->fix loop must be
    # able to re-read its own edits without any budget limit.
    assert run(middleware, make_request("read_file", read, call_id="r5")).content == "ok"
    assert run(middleware, make_request("read_file", read, call_id="r6")).content == "ok"


def test_read_of_written_file_block_points_to_next_action() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="c1"))
    blocked = run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    assert blocked.status == "error"
    assert "Read blocked" in blocked.content
    assert "Continue with the next action" in blocked.content
    # arc-output4 REQ-1: four seconds after this exact block the agent retried
    # the same path with limit=5 and was blocked again. The message must name
    # the retry shapes so the narrow probe does not look like an escape.
    assert "Any retry shape (smaller limit, shifted offset) is blocked too." in blocked.content


# ---------------------------------------------------------------------------
# test_generation stage: delete releases the write lock (run8 regression)
# ---------------------------------------------------------------------------


def test_test_generator_delete_then_rewrite_releases_write_lock() -> None:
    """Exact run8 sequence: the rewrite after a successful delete must pass.

    Empirical trace (REQ-1 TestGenerator, passengerRepository.test.js):
    write ok -> write blocked -> delete ok -> write blocked x6 -> delete
    error (not found) -> write ok. The rewrite was only unlocked by the
    *failed* second delete; with the lock released on successful delete the
    rewrite passes immediately after the first one.
    """

    middleware = make("test_generation")
    path = "/workspace/backend/tests/passengerRepository.test.js"
    state: dict[str, Any] = {}
    write_args = {"file_path": path, "content": "test v1\n"}

    first = run(middleware, make_request("write_file", write_args, call_id="c1", state=state))
    assert isinstance(first, ToolMessage) and first.content == "ok"
    assert state["arc_written_paths"] == [path]

    blocked = run(middleware, make_request("write_file", write_args, call_id="c2", state=state))
    assert blocked.status == "error" and "Repeated write blocked" in blocked.content

    deleted = run(middleware, make_request("delete", {"file_path": path}, call_id="c3", state=state))
    assert not isinstance(deleted, ToolMessage) or deleted.status != "error"
    # The state mirror must drop the path together with the internal lock.
    assert state["arc_written_paths"] == []

    rewritten = run(middleware, make_request("write_file", write_args, call_id="c4", state=state))
    assert isinstance(rewritten, ToolMessage) and rewritten.content == "ok"
    assert state["arc_written_paths"] == [path]


def test_test_generator_delete_releases_read_cache_for_rewritten_file() -> None:
    """A delete must clear the read cache of the deleted path.

    The cached ranges describe content that no longer exists; keeping them
    would turn the next read of the same range into a zombie
    "Repeated read blocked" lock. Reading the rewritten file afterwards is
    governed by the written-path rule, so the release is observed directly on
    the deleted path.
    """

    middleware = make("test_generation")
    path = "/workspace/backend/tests/passengerRepository.test.js"
    read_args = {"file_path": path, "offset": 0, "limit": 100}

    assert run(middleware, make_request("read_file", read_args, call_id="r1")).content == "ok"
    # The fresh re-read budget serves two overlapping re-reads first...
    assert run(middleware, make_request("read_file", read_args, call_id="r2")).content == "ok"
    assert run(middleware, make_request("read_file", read_args, call_id="r3")).content == "ok"
    # ...then the loop cap kicks in.
    blocked = run(middleware, make_request("read_file", read_args, call_id="r4"))
    assert blocked.status == "error" and "Repeated read blocked" in blocked.content

    assert run(middleware, make_request("delete", {"file_path": path}, call_id="d1")).content == "ok"
    # Same range as before the delete: the cache entry died with the file.
    assert run(middleware, make_request("read_file", read_args, call_id="r5")).content == "ok"

    # After the delete-then-rewrite cycle, reads of the new content are
    # governed by the written-path rule — never by the stale pre-delete range.
    assert run(middleware, make_request("write_file", {"file_path": path, "content": "test v2\n"}, call_id="c1")).content == "ok"
    rewrite_read = run(middleware, make_request("read_file", read_args, call_id="r6"))
    assert rewrite_read.status == "error" and "Read blocked" in rewrite_read.content


def test_test_generator_failed_delete_still_unlocks_via_failure_recording() -> None:
    """run8's accidental escape stays valid: a *failed* delete records the
    path in ``_failed_paths`` (the generic file-operation failure unlock),
    so the rewrite passes without the delete-release semantics."""
    middleware = make("test_generation")
    path = "/workspace/backend/tests/passengerRepository.test.js"
    write_args = {"file_path": path, "content": "test v1\n"}

    assert run(middleware, make_request("write_file", write_args, call_id="c1")).content == "ok"
    failed_delete = run(
        middleware,
        make_request("delete", {"file_path": path}, call_id="c2"),
        handler=lambda req: ToolMessage(
            content="Error: path not found", name="delete", tool_call_id="c2", status="error"
        ),
    )
    assert failed_delete.status == "error"
    # The failed file operation unlocks the path for the rewrite.
    assert run(middleware, make_request("write_file", write_args, call_id="c3")).content == "ok"


def test_materialized_paths_excludes_deleted_paths() -> None:
    # materialized_paths() is only consumed in interface_design (where delete
    # is disabled), so this documents the intended semantics rather than a
    # live path: a deleted file is no longer materialized.
    middleware = make("test_generation")
    kept = "/workspace/backend/tests/kept.test.js"
    removed = "/workspace/backend/tests/removed.test.js"

    run(middleware, make_request("write_file", {"file_path": kept, "content": "test\n"}, call_id="c1"))
    run(middleware, make_request("write_file", {"file_path": removed, "content": "test\n"}, call_id="c2"))
    run(middleware, make_request("delete", {"file_path": removed}, call_id="c3"))

    assert middleware.materialized_paths() == [kept]


# ---------------------------------------------------------------------------
# test_generation stage: delete-rewrite cycle budget (arc-output3 regression)
# ---------------------------------------------------------------------------


def test_test_generator_delete_rewrite_budget_blocks_third_cycle() -> None:
    """arc-output3 (2026-09-19) evidence: the model ran the delete+write
    escape 5-7 times per file until the step budget crashed the whole DESIGN
    task. Two full cycles stay legal (the legitimate fix path); the third
    delete is refused, so the last written version stands on disk."""

    middleware = make("test_generation")
    path = "/workspace/tests/unit/test_calc.py"
    for cycle in range(2):
        written = run(
            middleware,
            make_request("write_file", {"file_path": path, "content": f"v{cycle}\n"}, call_id=f"w{cycle}"),
        )
        assert written.content == "ok"
        deleted = run(middleware, make_request("delete", {"file_path": path}, call_id=f"d{cycle}"))
        assert not isinstance(deleted, ToolMessage) or deleted.status != "error"
    final = run(
        middleware,
        make_request("write_file", {"file_path": path, "content": "final\n"}, call_id="w-final"),
    )
    assert final.content == "ok"

    blocked = run(middleware, make_request("delete", {"file_path": path}, call_id="d-3rd"))
    assert blocked.status == "error"
    assert "Rewrite budget blocked" in blocked.content
    assert "2 delete-rewrite cycles" in blocked.content


def test_delete_rewrite_budget_message_points_to_final_response_when_manifest_complete() -> None:
    """When every declared file is already materialized, the block must name
    the real exit (return the manifest) — the model in the arc-output3 run
    kept polishing because nothing told it the work was done."""

    middleware = make_locked(declared=["tests/unit/a.test.py", "tests/unit/b.test.py"])
    for path in ("a", "b"):
        full = f"/workspace/tests/unit/{path}.test.py"
        result = run(middleware, make_request("write_file", {"file_path": full, "content": "v1\n"}, call_id=f"w1{path}"))
        assert result.content == "ok"

    # One full cycle on a.test.py (legal), then the third delete refuses.
    run(middleware, make_request("delete", {"file_path": "/workspace/tests/unit/a.test.py"}, call_id="d1"))
    run(middleware, make_request("write_file", {"file_path": "/workspace/tests/unit/a.test.py", "content": "v2\n"}, call_id="w2"))
    run(middleware, make_request("delete", {"file_path": "/workspace/tests/unit/a.test.py"}, call_id="d2"))
    run(middleware, make_request("write_file", {"file_path": "/workspace/tests/unit/a.test.py", "content": "v3\n"}, call_id="w3"))

    blocked = run(middleware, make_request("delete", {"file_path": "/workspace/tests/unit/a.test.py"}, call_id="d3"))
    assert blocked.status == "error" and "Rewrite budget blocked" in blocked.content
    assert "every declared manifest file is written" in blocked.content
    assert "return your manifest response now" in blocked.content
    assert "tests/unit/b.test.py" in blocked.content


def test_failed_write_keeps_the_delete_rewrite_exit_open() -> None:
    """The budget limits self-review churn, not repair after an error: a
    failed file operation unlocks the path and the delete passes again."""

    middleware = make("test_generation")
    path = "/workspace/tests/unit/test_calc.py"
    assert run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="w1")).content == "ok"

    def failing_write(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="Error: disk full",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    failed = run(
        middleware,
        make_request("write_file", {"file_path": path, "content": "v2\n"}, call_id="w2"),
        failing_write,
    )
    assert failed.status == "error"
    unlocked_delete = run(middleware, make_request("delete", {"file_path": path}, call_id="d1"))
    assert not isinstance(unlocked_delete, ToolMessage) or unlocked_delete.status != "error"


def test_failed_delete_does_not_consume_the_rewrite_budget() -> None:
    """Only a *successful* delete starts a rewrite cycle: a failed delete
    (e.g. the file was already gone) leaves the budget untouched."""

    middleware = make("test_generation")
    path = "/workspace/tests/unit/test_calc.py"

    def failing_delete(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="Error: path not found",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    assert run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="w1")).content == "ok"
    failed = run(middleware, make_request("delete", {"file_path": path}, call_id="d0"), failing_delete)
    assert failed.status == "error"
    # The path was unlocked by the failure, and the cycle count is still 0:
    # two more full delete-rewrite cycles remain legal.
    for cycle in range(2):
        written = run(
            middleware,
            make_request("write_file", {"file_path": path, "content": f"v{cycle}\n"}, call_id=f"w{cycle}"),
        )
        assert written.content == "ok"
        deleted = run(middleware, make_request("delete", {"file_path": path}, call_id=f"d{cycle}"))
        assert not isinstance(deleted, ToolMessage) or deleted.status != "error"


def test_delete_rewrite_budget_is_per_path() -> None:
    """The cap limits polishing one file; other declared files keep their
    own full budget."""

    middleware = make("test_generation")
    hot = "/workspace/tests/unit/hot.test.py"
    other = "/workspace/tests/unit/other.test.py"
    for cycle in range(2):
        assert run(middleware, make_request("write_file", {"file_path": hot, "content": f"v{cycle}\n"}, call_id=f"wh{cycle}")).content == "ok"
        assert run(middleware, make_request("delete", {"file_path": hot}, call_id=f"dh{cycle}")).content == "ok"
    assert run(middleware, make_request("write_file", {"file_path": hot, "content": "final\n"}, call_id="wh2")).content == "ok"

    assert run(middleware, make_request("write_file", {"file_path": other, "content": "v1\n"}, call_id="wo1")).content == "ok"
    fresh = run(middleware, make_request("delete", {"file_path": other}, call_id="do1"))
    assert not isinstance(fresh, ToolMessage) or fresh.status != "error"


def test_read_block_after_write_warns_against_rewrite_verification() -> None:
    """The arc-output3 model could not re-read its written test file, drew
    the wrong conclusion ("can't re-read") and started rewriting instead.
    The block message must close that door explicitly."""

    middleware = make("test_generation")
    path = "/workspace/tests/unit/test_calc.py"
    assert run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="w1")).content == "ok"
    blocked = run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    assert blocked.status == "error"
    assert "never rewrite the file to verify it" in blocked.content


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


def test_interface_design_blocks_beyond_the_leaf_skeleton_write_budget() -> None:
    middleware = make("interface_design")  # default = leaf ceiling (12)
    for index in range(12):
        result = run(
            middleware,
            make_request("write_file", {"file_path": f"/workspace/src/mod_{index}.py", "content": "class A:\n"}, call_id=f"c{index}"),
        )
        assert result.content == "ok", f"write {index} should pass"
    thirteenth = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/src/mod_new.py", "content": "class B:\n"}, call_id="c13"),
    )
    assert thirteenth.status == "error" and "at most 12" in thirteenth.content and "distinct files" in thirteenth.content
    thirteenth_append = run(
        middleware,
        make_request("append_file", {"file_path": "/workspace/src/mod_new.py", "content": "class B:\n"}, call_id="a13"),
    )
    assert thirteenth_append.status == "error" and "at most 12" in thirteenth_append.content
    # Rewriting an already-written path is governed by the repeated-write rule,
    # not the skeleton budget.
    rewrite = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/src/mod_0.py", "content": "class A2:\n"}, call_id="c14"),
    )
    assert rewrite.status == "error" and "Repeated write blocked" in rewrite.content


def test_interface_design_write_budget_reserves_across_a_parallel_batch() -> None:
    """A parallel tool-call burst must not overshoot the cap (arc-output4 ROOT).

    Twelve write_file calls arrive before any of them completes; counting
    only on success let every validation read the same stale count and the
    whole burst through. Reservation at validation time makes the batch
    respect the cap exactly.
    """

    middleware = StageDisciplineMiddleware(stage="interface_design", max_design_writes=8)
    batch = [
        make_request("write_file", {"file_path": f"/workspace/src/part_{index}.py", "content": "x = 1\n"}, call_id=f"b{index}")
        for index in range(12)
    ]
    # Simulate the runtime interleaving: every call is validated before any
    # result is recorded (wrap_tool_call on a batch of concurrent handlers).
    for request in batch[:8]:
        assert middleware._validate_tool_call(request) is None
    for request in batch[:8]:
        run(middleware, request)
    blocked_validation = middleware._validate_tool_call(batch[8])
    assert blocked_validation is not None and "at most 8" in blocked_validation
    ninth = run(middleware, batch[8])
    assert ninth.status == "error" and "at most 8" in ninth.content


def test_interface_design_write_budget_counts_edit_and_write_paths_the_same() -> None:
    """The budget counts distinct paths; edit_file's first touch costs one unit."""

    middleware = StageDisciplineMiddleware(stage="interface_design", max_design_writes=2)
    first = run(middleware, make_request("write_file", {"file_path": "/workspace/src/a.py", "content": "a\n"}, call_id="c1"))
    assert first.content == "ok"
    # First touch of a second path via edit_file consumes the remaining unit.
    second = run(middleware, make_request("edit_file", {"file_path": "/workspace/src/b.py", "old_string": "x", "new_string": "y"}, call_id="c2"))
    assert second.content == "ok"
    third = run(middleware, make_request("write_file", {"file_path": "/workspace/src/c.py", "content": "c\n"}, call_id="c3"))
    assert third.status == "error" and "at most 2" in third.content
    # Re-touching a path already touched costs nothing: appending to a.py is
    # budget-free (the per-file append cap is a separate rule).
    append_same = run(middleware, make_request("append_file", {"file_path": "/workspace/src/a.py", "content": "more\n"}, call_id="c4"))
    assert append_same.content == "ok"


def test_interface_design_write_budget_reserves_every_write_tool_in_one_batch() -> None:
    """Invariant pin: a mixed parallel batch of write/edit/append first-touches
    each reserves one unit, whatever tool carries it (see the call-site
    invariant in ``_reserve_design_write``'s docstring)."""

    middleware = StageDisciplineMiddleware(stage="interface_design", max_design_writes=2)
    batch = [
        make_request("write_file", {"file_path": "/workspace/src/one.py", "content": "1\n"}, call_id="m1"),
        make_request("edit_file", {"file_path": "/workspace/src/two.py", "old_string": "x", "new_string": "y"}, call_id="m2"),
        make_request("append_file", {"file_path": "/workspace/src/three.py", "content": "3\n"}, call_id="m3"),
    ]
    # Validate all three before recording any result (parallel-batch shape).
    validations = [middleware._validate_tool_call(request) for request in batch]
    assert validations[0] is None and validations[1] is None
    assert validations[2] is not None and "at most 2" in validations[2]


def test_interface_design_write_budget_failure_releases_the_reservation() -> None:
    """An errored write refunds its unit; a failed retry of a materialized
    path does not (its slot is already occupied)."""

    def failing_tool(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="Error: no such file", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])

    middleware = StageDisciplineMiddleware(stage="interface_design", max_design_writes=1)
    blocked_first = run(middleware, make_request("write_file", {"file_path": "/workspace/src/gone.py", "content": "x\n"}, call_id="c1"), handler=failing_tool)
    assert "Error: no such file" in blocked_first.content
    # The failed write freed the only budget unit, so a different path may use it.
    retry = run(middleware, make_request("write_file", {"file_path": "/workspace/src/other.py", "content": "y\n"}, call_id="c2"))
    assert retry.content == "ok"

    # A path that already materialized keeps its reservation even when a later
    # unlocked retry of it fails: the unlock (via the first failure) lets the
    # retry through to the handler, but the slot it occupies is not re-freed.
    middleware2 = StageDisciplineMiddleware(stage="interface_design", max_design_writes=1)
    ok_write = run(middleware2, make_request("write_file", {"file_path": "/workspace/src/ok.py", "content": "v1\n"}, call_id="c1"))
    assert ok_write.content == "ok"
    middleware2._failed_paths.add("/workspace/src/ok.py")  # a file-op failure unlocks a rewrite
    failed_rewrite = run(middleware2, make_request("write_file", {"file_path": "/workspace/src/ok.py", "content": "v2\n"}, call_id="c2"), handler=failing_tool)
    assert "Error: no such file" in failed_rewrite.content
    other = run(middleware2, make_request("write_file", {"file_path": "/workspace/src/zz.py", "content": "z\n"}, call_id="c3"))
    assert other.status == "error" and "at most 1" in other.content


def test_interface_design_non_leaf_budget_can_be_raised() -> None:
    """A non-leaf shell pass (16-file ceiling) admits the arc-output4 ROOT shape."""

    middleware = StageDisciplineMiddleware(stage="interface_design", max_design_writes=16)
    for index in range(16):
        result = run(
            middleware,
            make_request("write_file", {"file_path": f"/workspace/src/shell_{index}.tsx", "content": "export {}\n"}, call_id=f"s{index}"),
        )
        assert result.content == "ok", f"write {index} should pass"
    seventeenth = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/src/extra.tsx", "content": "export {}\n"}, call_id="s16"),
    )
    assert seventeenth.status == "error" and "at most 16" in seventeenth.content


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


def test_interface_design_blocks_obvious_business_mutations() -> None:
    middleware = make("interface_design")
    result = run(
        middleware,
        make_request(
            "write_file",
            {
                "file_path": "/workspace/backend/src/services/notes.js",
                "content": "export function createNote(db, note) {\n  return db.insert(note);\n}\n",
            },
        ),
    )
    assert result.status == "error"
    assert "contract skeletons" in result.content


def test_interface_design_blocks_repository_upsert_mutation() -> None:
    middleware = make("interface_design")
    result = run(
        middleware,
        make_request(
            "write_file",
            {
                "file_path": "/workspace/backend/src/services/notes.js",
                "content": (
                    "export function createNote(repo, note) {\n"
                    "  return repo.upsert({ table: 'notes', values: note });\n"
                    "}\n"
                ),
            },
        ),
    )
    assert result.status == "error"
    assert "contract skeletons" in result.content


def test_interface_design_allows_contract_only_content() -> None:
    middleware = make("interface_design")
    result = run(
        middleware,
        make_request(
            "write_file",
            {
                "file_path": "/workspace/backend/src/services/notes.js",
                "content": "export function createNote(note) {\n  throw new Error('NOT_IMPLEMENTED');\n}\n",
            },
        ),
    )
    assert result.content == "ok"


def test_interface_design_allows_sql_documented_in_comments() -> None:
    """Row shapes and queries documented in comments are not mutations."""
    middleware = make("interface_design")
    result = run(
        middleware,
        make_request(
            "write_file",
            {
                "file_path": "/workspace/backend/src/repositories/notes.js",
                "content": (
                    "/**\n"
                    " * Note persistence contract.\n"
                    " * Row shape: {id, body}. Backing store performs\n"
                    " * INSERT INTO notes (body) VALUES (?) on create.\n"
                    " */\n"
                    "// Purge removes rows with DELETE FROM notes WHERE trashed = 1.\n"
                    "// Provider adapter owns UPDATE notes SET body = ?.\n"
                    "export function createNote(note) {\n"
                    "  throw new Error('NOT_IMPLEMENTED');\n"
                    "}\n"
                ),
            },
        ),
    )
    assert result.content == "ok"


def test_interface_design_still_blocks_mutation_after_comment_lines() -> None:
    """Comment stripping must not open a hole: real calls still block."""
    middleware = make("interface_design")
    result = run(
        middleware,
        make_request(
            "write_file",
            {
                "file_path": "/workspace/backend/src/repositories/notes.js",
                "content": (
                    "// marks the insert path below\n"
                    "export function createNote(db, note) {\n"
                    "  return db.insert(note);\n"
                    "}\n"
                ),
            },
        ),
    )
    assert result.status == "error"
    assert "contract skeletons" in result.content


def test_interface_design_allows_bounded_append_continuations() -> None:
    middleware = make("interface_design")
    path = "/workspace/src/page.tsx"
    assert run(
        middleware,
        make_request("write_file", {"file_path": path, "content": "export function Page() {\n"}, call_id="w1"),
    ).content == "ok"

    for index in range(3):
        result = run(
            middleware,
            make_request("append_file", {"file_path": path, "content": f"  // section {index}\n"}, call_id=f"a{index}"),
        )
        assert result.content == "ok"

    fourth = run(
        middleware,
        make_request("append_file", {"file_path": path, "content": "  // too many\n"}, call_id="a3"),
    )
    assert fourth.status == "error" and "at most 3 times" in fourth.content

    oversized = run(
        make("interface_design"),
        make_request("append_file", {"file_path": "/workspace/src/other.ts", "content": "x\n" * 81}),
    )
    assert oversized.status == "error" and "at most 80 lines" in oversized.content

    blocked = run(
        make("implementation"),
        make_request("append_file", {"file_path": path, "content": "x\n"}),
    )
    assert blocked.status == "error" and "only available during the interface_design stage" in blocked.content


def test_failed_append_attempts_consume_the_per_file_budget() -> None:
    middleware = make("interface_design")
    path = "/workspace/src/page.tsx"
    assert run(
        middleware,
        make_request("write_file", {"file_path": path, "content": "export function Page() {\n"}, call_id="w1"),
    ).content == "ok"

    def failed_append(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="Error: transient append failure",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    for index in range(3):
        result = run(
            middleware,
            make_request("append_file", {"file_path": path, "content": "  // retry\n"}, call_id=f"f{index}"),
            failed_append,
        )
        assert result.status == "error" and "transient append failure" in result.content

    blocked = run(
        middleware,
        make_request("append_file", {"file_path": path, "content": "  // retry again\n"}, call_id="f3"),
    )
    assert blocked.status == "error" and "at most 3 times" in blocked.content


# ---------------------------------------------------------------------------
# read_file discipline
# ---------------------------------------------------------------------------


def test_repeated_overlapping_read_blocked_but_new_range_allowed() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    first = run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    assert first.content == "ok"
    # The overlapping re-reads are served fresh while the budget lasts...
    repeated = run(middleware, make_request("read_file", {"file_path": path, "offset": 10, "limit": 50}, call_id="r2"))
    assert repeated.content == "ok"
    second = run(middleware, make_request("read_file", {"file_path": path, "offset": 10, "limit": 50}, call_id="r3"))
    assert second.content == "ok"
    capped = run(middleware, make_request("read_file", {"file_path": path, "offset": 10, "limit": 50}, call_id="r4"))
    assert capped.status == "error" and "Repeated read blocked" in capped.content
    # ...and pagination into a new range is never part of the budget.
    next_page = run(middleware, make_request("read_file", {"file_path": path, "offset": 100, "limit": 100}, call_id="r5"))
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
# manifest-first gate (test_generation only)
# ---------------------------------------------------------------------------


def make_locked(stage: str = "test_generation", declared: list[str] | None = None) -> StageDisciplineMiddleware:
    from agents.tools.test_manifest import DeclaredTestFile, TestManifestLock

    lock = TestManifestLock(
        declared_files={
            path: DeclaredTestFile(file_path=path, test_type="Unit", interface_ids=[])
            for path in declared or []
        }
    )
    return StageDisciplineMiddleware(stage=stage, test_manifest_lock=lock)


def test_test_file_write_blocked_before_declaration() -> None:
    middleware = make_locked(declared=[])
    blocked = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/tests/unit/test_a.py", "content": "x\n"}),
    )
    assert blocked.status == "error" and "Manifest-first blocked" in blocked.content
    assert "declare_test_manifest" in blocked.content


def test_test_file_write_allowed_on_declared_path_after_lock() -> None:
    middleware = make_locked(declared=["tests/unit/test_a.py"])
    result = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/tests/unit/test_a.py", "content": "x\n"}),
    )
    assert result.content == "ok"


def test_test_file_write_blocked_outside_declared_manifest() -> None:
    middleware = make_locked(declared=["tests/unit/test_a.py"])
    blocked = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/tests/unit/test_b.py", "content": "x\n"}),
    )
    assert blocked.status == "error" and "Manifest lock blocked" in blocked.content
    assert "tests/unit/test_a.py" in blocked.content


def test_declared_path_matches_despite_prefix_forms() -> None:
    middleware = make_locked(declared=["tests/unit/a.test.py"])
    for path in ("/workspace/tests/unit/a.test.py", "tests/unit/a.test.py", "./tests/unit/a.test.py"):
        result = run(
            middleware,
            make_request("edit_file", {"file_path": path, "old_string": "a", "new_string": "b"}),
        )
        assert result.content == "ok", f"declared path in form {path!r} must pass"


def test_edit_file_is_gated_like_write_file() -> None:
    """edit_file must not be a bypass: the gate lives in _validate_write,
    which covers both file-write tools, and the undeclared case is blocked
    with the same manifest message."""
    middleware = make_locked(declared=["tests/unit/a.test.py"])
    blocked = run(
        middleware,
        make_request("edit_file", {"file_path": "/workspace/tests/unit/b.test.py", "old_string": "a", "new_string": "b"}),
    )
    assert blocked.status == "error" and "Manifest lock blocked" in blocked.content

    undeclared_stage = make_locked(declared=[])
    blocked = run(
        undeclared_stage,
        make_request("edit_file", {"file_path": "/workspace/tests/unit/a.test.py", "old_string": "a", "new_string": "b"}),
    )
    assert blocked.status == "error" and "Manifest-first blocked" in blocked.content


def test_manifest_lock_blocks_delete_of_undeclared_test_file() -> None:
    middleware = make_locked(declared=["tests/unit/test_a.py"])
    blocked = run(middleware, make_request("delete", {"file_path": "/workspace/tests/unit/test_b.py"}))
    assert blocked.status == "error" and "Manifest lock blocked" in blocked.content
    # A declared test asset stays deletable (green-baseline repair).
    result = run(middleware, make_request("delete", {"file_path": "/workspace/tests/unit/test_a.py"}))
    assert not isinstance(result, ToolMessage) or result.status != "error"


def test_manifest_lock_ignores_helpers_and_configs() -> None:
    middleware = make_locked(declared=["tests/unit/a.test.py"])
    for path in ("/workspace/tests/setup-tests.ts", "/workspace/backend/vitest.config.js"):
        result = run(middleware, make_request("write_file", {"file_path": path, "content": "x\n"}))
        assert result.content == "ok", f"helper/config {path} must stay writable"


def test_test_e2e_directory_files_are_test_assets_and_gated() -> None:
    """A web E2E file may carry a plain JS name under `test-e2e/`; it must be
    writable when declared (test-asset check) and blocked when not declared
    (manifest gate) — a declared-but-unwritable path would be a dead end."""
    middleware = make_locked(declared=["backend/test-e2e/login.js"])
    declared = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/backend/test-e2e/login.js", "content": "// e2e\n"}),
    )
    assert declared.content == "ok"

    undeclared = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/backend/test-e2e/logout.js", "content": "// e2e\n"}),
    )
    assert undeclared.status == "error" and "Manifest lock blocked" in undeclared.content


def test_manifest_lock_inactive_for_other_stages() -> None:
    from agents.tools.test_manifest import TestManifestLock

    for stage in ("interface_design", "implementation"):
        middleware = StageDisciplineMiddleware(
            stage=stage,
            test_manifest_lock=TestManifestLock(declared_files={}),
        )
        result = run(
            middleware,
            make_request("write_file", {"file_path": "/workspace/src/mod.py", "content": "x\n"}),
        )
        assert result.content == "ok", f"{stage} must ignore the manifest lock"


# ---------------------------------------------------------------------------
# pending contract registration (interface_design write-time notice)
# ---------------------------------------------------------------------------


_ROUTER_SKELETON = (
    "const express = require('express');\n"
    "const router = express.Router();\n"
    "router.post('/login', (req, res) => res.json({}));\n"
    "module.exports = router;\n"
)


def _design_middleware_with_registry(root, stage: str = "interface_design") -> StageDisciplineMiddleware:
    from agents.design.contract_skeleton import PendingContractRegistry

    return StageDisciplineMiddleware(
        stage=stage,
        pending_contract_registry=PendingContractRegistry(node_id="REQ-2", workspace_root=str(root)),
    )


def test_design_write_appends_the_pending_contract_notice(tmp_path) -> None:
    (tmp_path / "backend" / "src" / "routes").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "routes" / "auth_routes.js").write_text(_ROUTER_SKELETON, encoding="utf-8")
    middleware = _design_middleware_with_registry(tmp_path)

    result = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/backend/src/routes/auth_routes.js", "content": "x\n"}),
    )

    assert result.content.startswith("ok")
    assert "[ARC pending contract: REQ-2-API-AuthRoutes" in result.content
    assert "interfaces` array" in result.content


def test_pending_contract_notice_only_for_contract_embodied_design_writes(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.txt").write_text("plain text\n", encoding="utf-8")
    middleware = _design_middleware_with_registry(tmp_path)

    plain = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/docs/notes.txt", "content": "plain text\n"}),
    )
    assert plain.content == "ok", "a file without a derivable contract gets no notice"

    implementation = _design_middleware_with_registry(tmp_path, stage="implementation")
    other_stage = run(
        implementation,
        make_request("write_file", {"file_path": "/workspace/src/other.py", "content": "y = 2\n"}),
    )
    assert other_stage.content == "ok", "non-design stages never annotate"

    no_registry = StageDisciplineMiddleware(stage="interface_design")
    unwired = run(
        no_registry,
        make_request("write_file", {"file_path": "/workspace/src/more.py", "content": "z = 3\n"}),
    )
    assert unwired.content == "ok", "no registry wired, no annotation"


def test_failed_design_write_gets_no_pending_contract_notice(tmp_path) -> None:
    (tmp_path / "backend" / "src" / "routes").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "routes" / "auth_routes.js").write_text(_ROUTER_SKELETON, encoding="utf-8")
    middleware = _design_middleware_with_registry(tmp_path)

    def failing_write(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="Error: disk full",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    result = run(
        middleware,
        make_request("write_file", {"file_path": "/workspace/backend/src/routes/auth_routes.js", "content": "x\n"}),
        failing_write,
    )
    assert "ARC pending contract" not in str(result.content)


def test_append_notice_covers_only_newly_registered_ids(tmp_path) -> None:
    (tmp_path / "backend" / "src" / "db").mkdir(parents=True)
    path = "/workspace/backend/src/db/init_db.js"
    middleware = _design_middleware_with_registry(tmp_path)

    def disk_handler(request: ToolCallRequest) -> ToolMessage:
        # Mirror the real filesystem tools so the write-time derivation sees
        # the accumulated file content.
        args = request.tool_call["args"]
        rel = str(args["file_path"]).replace("/workspace/", "", 1)
        target = tmp_path / rel
        if request.tool_call["name"] == "append_file":
            with target.open("a", encoding="utf-8") as handle:
                handle.write(str(args["content"]))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(args["content"]), encoding="utf-8")
        return ok_tool(request)

    first = run(
        middleware,
        make_request(
            "write_file",
            {
                "file_path": path,
                "content": "const users = `\n      CREATE TABLE IF NOT EXISTS users (\n        id INTEGER PRIMARY KEY\n      )\n    `;\n",
            },
            call_id="c1",
        ),
        disk_handler,
    )
    assert "REQ-2-DB-UsersTable" in first.content

    again = run(
        middleware,
        make_request("append_file", {"file_path": path, "content": "// trailing comment\n"}, call_id="a1"),
        disk_handler,
    )
    assert again.content == "ok", "re-registering unchanged rows must not repeat the notice"

    grown = run(
        middleware,
        make_request(
            "append_file",
            {
                "file_path": path,
                "content": "const sessions = `\n      CREATE TABLE IF NOT EXISTS sessions (\n        token TEXT PRIMARY KEY\n      )\n    `;\n",
            },
            call_id="a2",
        ),
        disk_handler,
    )
    assert "REQ-2-DB-SessionsTable" in grown.content
    assert "REQ-2-DB-UsersTable" not in grown.content.split("ok\n", 1)[1], "only the new id is announced"

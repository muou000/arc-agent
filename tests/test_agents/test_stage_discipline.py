"""Unit tests for ``StageDisciplineMiddleware`` (agents/runtime/stage_discipline.py).

The middleware guards every tool call of the three stage agents:

- ``test_generation`` may only write test assets and must not run validation.
- ``interface_design`` may materialize at most 8 small skeleton files.
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


def test_repeated_read_block_does_not_offer_offset_probing() -> None:
    # run7 evidence: 50 consecutive offset=0..49 probe reads, each accepted as
    # a "non-overlapping range" by the old message that suggested paginated
    # re-reads as a justification. The message must not invite that again.
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    blocked = run(middleware, make_request("read_file", {"file_path": path, "offset": 10, "limit": 50}, call_id="r2"))
    assert blocked.status == "error"
    assert "Repeated read blocked" in blocked.content
    assert "non-overlapping" not in blocked.content
    assert "offset" in blocked.content


def test_read_of_written_file_block_points_to_next_action() -> None:
    middleware = make("implementation")
    path = "/workspace/src/calc.py"
    run(middleware, make_request("write_file", {"file_path": path, "content": "v1\n"}, call_id="c1"))
    blocked = run(middleware, make_request("read_file", {"file_path": path, "offset": 0, "limit": 100}, call_id="r1"))
    assert blocked.status == "error"
    assert "Read blocked" in blocked.content
    assert "Continue with the next action" in blocked.content


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
    blocked = run(middleware, make_request("read_file", read_args, call_id="r2"))
    assert blocked.status == "error" and "Repeated read blocked" in blocked.content

    assert run(middleware, make_request("delete", {"file_path": path}, call_id="d1")).content == "ok"
    # Same range as before the delete: the cache entry died with the file.
    assert run(middleware, make_request("read_file", read_args, call_id="r3")).content == "ok"

    # After the delete-then-rewrite cycle, reads of the new content are
    # governed by the written-path rule — never by the stale pre-delete range.
    assert run(middleware, make_request("write_file", {"file_path": path, "content": "test v2\n"}, call_id="c1")).content == "ok"
    rewrite_read = run(middleware, make_request("read_file", read_args, call_id="r4"))
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

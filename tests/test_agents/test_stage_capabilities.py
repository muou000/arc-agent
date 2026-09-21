"""Capability-table tests (``agents/runtime/capabilities.py``, issue #100).

"May stage X call tool Y on path P?" must have one authoritative answer. These
tests assert that answer directly through the public ``capability_for`` query
— never through middleware privates — and pin the middleware's public
blocking behavior and the prompts' tool-availability restatements to the same
table, so the three consumers cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.context.prompts import common as common_prompts
from agents.context.prompts import interface_designer as interface_designer_prompts
from agents.context.prompts import test_generator as test_generator_prompts
from agents.runtime.capabilities import (
    DISABLED_BUILTIN_TOOLS,
    STAGES,
    capability_for,
    is_test_asset,
    is_test_file_path,
)
from agents.runtime.stage_discipline import StageDisciplineMiddleware


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
    return ToolMessage(content="ok", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


# ---------------------------------------------------------------------------
# (stage, tool, path) verdicts — asserted through the public query only
# ---------------------------------------------------------------------------


def test_disabled_builtins_are_denied_in_every_stage() -> None:
    for stage in STAGES:
        for tool in ("execute", "write_todos"):
            verdict = capability_for(stage, tool)
            assert not verdict.allowed, (stage, tool)
            assert "disabled in ARC's staged file workflow" in verdict.message


def test_derived_disabled_builtin_set_matches_the_table() -> None:
    # DISABLED_BUILTIN_TOOLS is derived from the table (every stage denies);
    # this pin keeps the derivation honest if the builtin list changes.
    for tool in ("execute", "write_todos"):
        assert tool in DISABLED_BUILTIN_TOOLS
        assert all(not capability_for(stage, tool).allowed for stage in STAGES)


def test_delete_is_denied_in_interface_design_regardless_of_path() -> None:
    for path in ("/workspace/src/app.js", "/workspace/tests/unit/test_calc.py", ""):
        verdict = capability_for("interface_design", "delete", path)
        assert not verdict.allowed
        assert "`delete` is disabled in ARC's staged file workflow." == verdict.message


def test_delete_in_test_generation_allows_only_test_assets() -> None:
    for path in (
        "/workspace/tests/unit/test_calc.py",
        "/workspace/src/app.spec.ts",
        "/workspace/vitest.config.ts",
        "/workspace/backend/test-e2e/login.js",
    ):
        assert capability_for("test_generation", "delete", path).allowed, path
    for path in ("/workspace/src/calc.py", "/workspace/frontend/src/App.jsx", ""):
        verdict = capability_for("test_generation", "delete", path)
        assert not verdict.allowed, path
        assert "disabled in ARC's staged file workflow" in verdict.message


def test_delete_in_implementation_static_channel_is_test_asset_scoped() -> None:
    # The table's static verdict allows test-asset paths; the middleware then
    # narrows the channel to files the session wrote itself (issue #89).
    assert capability_for("implementation", "delete", "/workspace/frontend/tests/diag.test.tsx").allowed
    verdict = capability_for("implementation", "delete", "/workspace/frontend/src/App.jsx")
    assert not verdict.allowed
    assert "disabled in ARC's staged file workflow" in verdict.message


def test_append_file_is_only_allowed_in_interface_design() -> None:
    assert capability_for("interface_design", "append_file", "/workspace/src/page.tsx").allowed
    for stage in ("test_generation", "implementation"):
        verdict = capability_for(stage, "append_file", "/workspace/src/page.tsx")
        assert not verdict.allowed
        assert verdict.message == "append_file is only available during the interface_design stage."


def test_validation_tools_are_denied_in_test_generation_only() -> None:
    for tool in ("run_build", "run_tests"):
        verdict = capability_for("test_generation", tool)
        assert not verdict.allowed
        assert verdict.message == (
            "TestGenerator only creates tests and its manifest; it must not run validation."
        )
        for stage in ("interface_design", "implementation"):
            assert capability_for(stage, tool).allowed, (stage, tool)


def test_test_generation_writes_and_edits_require_test_assets() -> None:
    for tool in ("write_file", "edit_file"):
        product = "/workspace/src/calc.py"
        verdict = capability_for("test_generation", tool, product)
        assert not verdict.allowed, (tool, product)
        assert product in verdict.message
        assert "not a test asset" in verdict.message
        for path in (
            "/workspace/tests/unit/test_calc.py",
            "/workspace/src/app.spec.ts",
            "/workspace/tests/setup-tests.ts",
            "/workspace/backend/vitest.config.js",
        ):
            assert capability_for("test_generation", tool, path).allowed, (tool, path)
    # The other stages have no test-asset restriction.
    for stage in ("interface_design", "implementation"):
        assert capability_for(stage, "write_file", "/workspace/src/calc.py").allowed


def test_uncategorized_tools_default_to_allowed() -> None:
    # Default-open matches the historical middleware semantics: containment
    # for these is the filesystem permission layer's job.
    for stage in STAGES:
        for tool in ("read_file", "ls", "glob", "grep", "get_interface", "declare_test_manifest"):
            assert capability_for(stage, tool, "/workspace/x.py").allowed, (stage, tool)


def test_denial_message_path_placeholder_is_substituted() -> None:
    path = "/workspace/src/calc.py"
    verdict = capability_for("test_generation", "write_file", path)
    assert not verdict.allowed
    assert "{path}" not in verdict.message
    assert verdict.message.endswith(f"; {path} is not a test asset.")


def test_path_predicates_keep_their_distinct_scopes() -> None:
    # The write-permission predicate includes helpers/configs; the manifest
    # predicate deliberately does not (helpers carry no manifest entry).
    helper = "/workspace/tests/setup-tests.ts"
    assert is_test_asset(helper)
    assert not is_test_file_path(helper)
    # The manifest predicate accepts the Python unittest naming the asset
    # predicate only catches via a test directory segment.
    assert is_test_file_path("backend/tests/unit/test_calc.py")
    assert is_test_asset("backend/tests/unit/test_calc.py")


# ---------------------------------------------------------------------------
# Middleware agreement: the public blocking behavior follows the table
# ---------------------------------------------------------------------------


def _blocked_message(middleware: StageDisciplineMiddleware, name: str, args: dict[str, Any]) -> str:
    result = middleware.wrap_tool_call(make_request(name, args), ok_tool)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    return str(result.content)


def test_middleware_blocks_exactly_where_the_table_denies() -> None:
    # Denied by the table → the middleware returns the table's message.
    assert "`execute` is disabled" in _blocked_message(
        StageDisciplineMiddleware(stage="implementation"), "execute", {}
    )
    assert "must not run validation" in _blocked_message(
        StageDisciplineMiddleware(stage="test_generation"), "run_tests", {}
    )
    assert "append_file is only available during the interface_design stage." in _blocked_message(
        StageDisciplineMiddleware(stage="test_generation"), "append_file", {"file_path": "/workspace/src/x.py"}
    )
    assert "is not a test asset." in _blocked_message(
        StageDisciplineMiddleware(stage="test_generation"),
        "write_file",
        {"file_path": "/workspace/src/calc.py", "content": "x = 1\n"},
    )

    # Allowed by the table → the middleware does not categorically block (the
    # call reaches the handler; dynamic guards are separate behavior covered
    # by test_stage_discipline.py).
    middleware = StageDisciplineMiddleware(stage="test_generation")
    result = middleware.wrap_tool_call(
        make_request("write_file", {"file_path": "/workspace/tests/unit/test_calc.py", "content": "x = 1\n"}),
        ok_tool,
    )
    assert not isinstance(result, ToolMessage) or result.status != "error"


def test_middleware_delete_channel_narrows_the_implementation_static_verdict() -> None:
    # The table allows test-asset deletes in implementation; the session-
    # ownership gate keeps pre-existing files blocked with the same message.
    middleware = StageDisciplineMiddleware(stage="implementation")
    message = _blocked_message(middleware, "delete", {"file_path": "/workspace/frontend/tests/auth.test.ts"})
    assert "disabled in ARC's staged file workflow" in message


# ---------------------------------------------------------------------------
# Prompt consistency pins: hand-written restatements ↔ table verdicts
# ---------------------------------------------------------------------------


def test_common_prompt_execute_statement_matches_table() -> None:
    policy = common_prompts.workspace_tool_policy()
    assert "The generic `execute` tool is disabled" in policy
    assert all(not capability_for(stage, "execute").allowed for stage in STAGES)


def test_common_prompt_delete_statement_matches_table() -> None:
    policy = common_prompts.workspace_tool_policy()
    assert "`delete` is stage-scoped: TestGenerator may delete a declared test file" in policy
    assert "TestDrivenDeveloper may delete a test-named file it wrote itself" in policy
    # The two declared channels are exactly the stages whose tables admit
    # test-asset deletes; DESIGN has none.
    assert not capability_for("interface_design", "delete", "/workspace/tests/x.test.ts").allowed
    assert capability_for("test_generation", "delete", "/workspace/tests/x.test.ts").allowed
    assert capability_for("implementation", "delete", "/workspace/tests/x.test.ts").allowed


def test_test_generator_prompt_boundary_matches_table() -> None:
    prompt = test_generator_prompts.get_system_prompt()
    assert "Do not implement or edit product code, run tests/builds" in prompt
    assert not capability_for("test_generation", "run_tests").allowed
    assert not capability_for("test_generation", "run_build").allowed
    assert not capability_for("test_generation", "write_file", "/workspace/src/calc.py").allowed


def test_interface_designer_prompt_append_file_statement_matches_table() -> None:
    prompt = interface_designer_prompts.get_system_prompt()
    assert "then use append_file for each cohesive continuation" in prompt
    assert capability_for("interface_design", "append_file").allowed
    assert all(
        not capability_for(stage, "append_file").allowed
        for stage in STAGES
        if stage != "interface_design"
    )

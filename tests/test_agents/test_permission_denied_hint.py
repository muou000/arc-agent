"""Permission-denied tool results must teach the valid virtual roots.

The online run showed 7 ``permission denied for read`` events per node: a
model that addressed a file as ``/frontend/src/...`` (host-style) or
``backend/x.js`` (relative) hit the catch-all deny rule, and the raw upstream
error names only the denied path — no hint that ``/workspace/`` is the valid
root — so the model had to re-derive the correction from the system prompt,
costing retries each time.

``PermissionDeniedHintMiddleware`` (wired by ``build_stage_agent``) rewrites
permission-denied tool results with a one-line remediation. These tests drive
a real ``build_stage_agent`` deep agent with a scripted model, so what is
asserted is exactly what the model receives — the tests apply no patches and
call no installation functions themselves.

The same harness pins the read_file format contract (the third build-path
behavior): the source body must reach the model verbatim, with no
line-number gutter rows it could mistake for indentation.
"""

from __future__ import annotations

import re
from pathlib import Path

from agents.runtime.filesystem_adapters import PermissionDeniedHintMiddleware
from tests.helpers.faux import drive_scripted_tool_call


def test_permission_denied_read_gains_the_workspace_root_hint(
    tmp_project_dir: Path,
) -> None:
    """A host-style path like /frontend/src/x.tsx is denied by the catch-all
    rule; the result must name /workspace (and /skills) as the valid roots."""

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": "/frontend/src/App.tsx"},
    )

    assert content.startswith("Error: permission denied for read on /frontend/src/App.tsx")
    assert "/workspace/<path>" in content
    assert "/skills/<name>/SKILL.md" in content


def test_permission_denied_hint_applies_to_relative_paths_too(
    tmp_project_dir: Path,
) -> None:
    """Relative paths are normalized to /-anchored virtual paths and denied
    the same way; they get the same remediation."""

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": "backend/src/database/seed_db.js"},
    )

    assert "permission denied for read on /backend/src/database/seed_db.js" in content
    assert "/workspace/<path>" in content


def test_successful_reads_are_untouched(tmp_project_dir: Path) -> None:
    """The hint must only touch permission-denied results: a normal read of
    a workspace file returns its content without the hint suffix."""

    (tmp_project_dir / "notes.txt").write_text("hello", encoding="utf-8")
    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": "/workspace/notes.txt"},
    )

    assert "hello" in content
    assert "permission denied" not in content
    assert "/workspace/<path>" not in content


def test_other_error_messages_are_untouched(tmp_project_dir: Path) -> None:
    """Non-permission errors (e.g. reading a missing file) keep their original
    text — the hint matches the permission-denied prefix only."""

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": "/workspace/definitely-missing.txt"},
    )

    assert "not found" in content.lower()
    assert "/workspace/<path>" not in content


def test_read_file_body_is_verbatim_without_line_numbers(tmp_project_dir: Path) -> None:
    """read_file must deliver the source body verbatim.

    deepagents < 0.7 rendered each line with a line-number gutter and models
    mistook the separator for indentation, submitting edit_file anchors that
    could not match. deepagents >= 0.7 emits the body unchanged (the old
    process-level formatter patch became dead code); this build-path probe
    pins the contract: the body follows the status header exactly, with no
    gutter rows anywhere.
    """

    (tmp_project_dir / "multi.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "read_file",
        {"file_path": "/workspace/multi.txt"},
    )

    assert content.endswith("alpha\nbeta\ngamma")
    assert not re.search(r"(?m)^\s*\d+\s\s", content), content


def test_hint_middleware_rewrites_denied_sync_results() -> None:
    """The middleware's sync wrap path mirrors the async one: a denied result
    gains the hint, and the upstream prefix is preserved verbatim."""

    from langgraph.prebuilt.tool_node import ToolCallRequest

    request = ToolCallRequest(
        tool_call={"name": "read_file", "args": {"file_path": "/x"}, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )

    def handler(request: ToolCallRequest):
        from langchain_core.messages import ToolMessage

        return ToolMessage(
            content="Error: permission denied for read on /frontend/src/App.tsx",
            tool_call_id="call-1",
            status="error",
        )

    result = PermissionDeniedHintMiddleware().wrap_tool_call(request, handler)

    assert result.content.startswith("Error: permission denied for read on /frontend/src/App.tsx")
    assert "/workspace/<path>" in result.content

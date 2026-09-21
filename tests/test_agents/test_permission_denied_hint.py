"""Permission-denied tool results must teach the valid virtual roots.

The online run showed 7 ``permission denied for read`` events per node: a
model that addressed a file as ``/frontend/src/...`` (host-style) or
``backend/x.js`` (relative) hit the catch-all deny rule, and the raw upstream
error names only the denied path — no hint that ``/workspace/`` is the valid
root — so the model had to re-derive the correction from the system prompt,
costing retries each time.

``_apply_permission_denied_hint`` (applied by ``build_stage_agent``) wraps
``FilesystemMiddleware.awrap_tool_call`` and appends a one-line remediation to
permission-denied tool results. These tests drive the real middleware through
``awrap_tool_call`` with ARC's production permission rules, so the patched
chain runs exactly as it does for stage agents.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolRuntime

from agents.runtime.factory import (
    _apply_permission_denied_hint,
    _apply_unambiguous_read_file_format,
    _build_filesystem_permissions,
)


@pytest.fixture(autouse=True)
def _factory_patches_applied() -> None:
    """Apply the same runtime patches ``build_stage_agent`` installs, so the
    read output (no line-number padding) matches what stage agents see."""

    _apply_unambiguous_read_file_format()
    _apply_permission_denied_hint()


def _make_middleware(tmp_project_dir: Path) -> FilesystemMiddleware:
    """Build the filesystem middleware over ARC's real routes and permissions."""

    root = tmp_project_dir.resolve()
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/workspace/": FilesystemBackend(root_dir=str(root), virtual_mode=True),
        },
    )
    permissions = _build_filesystem_permissions(
        root,
        [str(root)],
        skill_instruction_paths=[],
    )
    return FilesystemMiddleware(backend=backend, _permissions=permissions)


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda *_: None,
        tool_call_id="call-read-1",
        store=None,
        tools=[],
    )


def _run_read(middleware: FilesystemMiddleware, file_path: str) -> str:
    """Invoke the async read_file tool through the patched awrap_tool_call."""

    tools = {tool.name: tool for tool in middleware.tools}
    read_tool = tools["read_file"]
    request = ToolCallRequest(
        tool_call={"name": "read_file", "args": {"file_path": file_path}, "id": "call-read-1"},
        tool=read_tool,
        state={},
        runtime=_runtime(),
    )

    async def handler(request: ToolCallRequest) -> Any:
        return await read_tool.coroutine(
            runtime=request.runtime, **request.tool_call["args"]
        )

    message = asyncio.run(middleware.awrap_tool_call(request, handler))
    return str(message.content)


def test_permission_denied_read_gains_the_workspace_root_hint(
    tmp_project_dir: Path,
) -> None:
    """A host-style path like /frontend/src/x.tsx is denied by the catch-all
    rule; the result must name /workspace (and /skills) as the valid roots."""

    middleware = _make_middleware(tmp_project_dir)
    content = _run_read(middleware, "/frontend/src/App.tsx")

    assert content.startswith("Error: permission denied for read on /frontend/src/App.tsx")
    assert "/workspace/<path>" in content
    assert "/skills/<name>/SKILL.md" in content


def test_permission_denied_hint_applies_to_relative_paths_too(
    tmp_project_dir: Path,
) -> None:
    """Relative paths are normalized to /-anchored virtual paths and denied
    the same way; they get the same remediation."""

    middleware = _make_middleware(tmp_project_dir)
    content = _run_read(middleware, "backend/src/database/seed_db.js")

    assert "permission denied for read on /backend/src/database/seed_db.js" in content
    assert "/workspace/<path>" in content


def test_successful_reads_are_untouched(tmp_project_dir: Path) -> None:
    """The patch must only touch permission-denied results: a normal read of
    a workspace file returns its content without the hint suffix."""

    (tmp_project_dir / "notes.txt").write_text("hello", encoding="utf-8")
    middleware = _make_middleware(tmp_project_dir)
    content = _run_read(middleware, "/workspace/notes.txt")

    # Upstream deepagents may prefix read output with an `@@ lines X-Y of Z @@`
    # header; the patch's contract is only that the file content survives and
    # no permission-denied hint is appended to a successful read.
    assert "hello" in content
    assert "permission denied" not in content


def test_other_error_messages_are_untouched(tmp_project_dir: Path) -> None:
    """Non-permission errors (e.g. reading a missing file) keep their original
    text — the patch matches the permission-denied prefix only."""

    middleware = _make_middleware(tmp_project_dir)
    content = _run_read(middleware, "/workspace/definitely-missing.txt")

    assert "not found" in content.lower()
    assert "/workspace/<path>" not in content


def test_patch_double_apply_is_idempotent() -> None:
    """Re-running the patch (concurrent first call, explicit re-apply) must
    not stack a second wrapper: the wrapper carries a sentinel, and the
    second application detects it and leaves the method alone."""

    from deepagents.middleware.filesystem import FilesystemMiddleware

    _apply_permission_denied_hint()
    wrapped = FilesystemMiddleware.awrap_tool_call
    assert getattr(wrapped, "_arc_permission_hint", False) is True

    # A second apply sees the sentinel and keeps the existing wrapper.
    _apply_permission_denied_hint()
    assert FilesystemMiddleware.awrap_tool_call is wrapped

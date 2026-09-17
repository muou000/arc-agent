"""Delete tool must report `not found` for missing paths, not deny-rule spam.

Upstream deepagents' delete tool ran its conservative "may have descendants"
permission scan before the permission check; a missing path (``ls`` ->
``path_not_found``) was not on the leaf whitelist, so every ``**`` deny rule
matched and the model saw "permission denied" plus the full deny-rule list
for a file that did not exist — leading to 3-4 retries against the same path
(observed on the 12306 benchmark during green-baseline rejections, where
TestGenerator legitimately deletes renamed-away test files).

``_apply_delete_not_found_precedence`` (applied by ``build_stage_agent``)
recognizes the backend's explicit ``path_not_found`` as "nothing to protect":
a missing path inside a writable root now reaches the backend and gets its
honest ``not found``, while denied paths stay refused before the backend runs.

These tests exercise the real ``FilesystemMiddleware`` delete tool with ARC's
production permission rules (``_build_filesystem_permissions``), so the
patched decision chain runs exactly as it does for stage agents.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langgraph.prebuilt.tool_node import ToolRuntime

from agents.runtime.factory import (
    _apply_delete_not_found_precedence,
    _build_filesystem_permissions,
)


def _make_delete_tool(tmp_project_dir: Path) -> Any:
    """Build the delete tool over ARC's real backend routes and permissions."""

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
    middleware = FilesystemMiddleware(backend=backend, _permissions=permissions)
    return {tool.name: tool for tool in middleware.tools}["delete"]


def _run_delete(tool: Any, file_path: str) -> str:
    """Invoke the sync delete handler with a minimal injected runtime."""

    runtime = ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda *_: None,
        tool_call_id="call-delete-1",
        store=None,
        tools=[],
    )
    message = tool.func(file_path=file_path, runtime=runtime)
    return str(message.content)


def _run_delete_async(tool: Any, file_path: str) -> str:
    """Invoke the async delete handler with a minimal injected runtime."""

    runtime = ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda *_: None,
        tool_call_id="call-delete-1",
        store=None,
        tools=[],
    )
    message = asyncio.run(tool.coroutine(file_path=file_path, runtime=runtime))
    return str(message.content)


@pytest.fixture(autouse=True)
def _delete_patch_applied() -> None:
    # build_stage_agent applies this idempotently; apply it here so the tests
    # do not depend on an agent having been built earlier in the process.
    _apply_delete_not_found_precedence()


def test_delete_missing_path_in_writable_root_reports_not_found(tmp_project_dir: Path) -> None:
    tool = _make_delete_tool(tmp_project_dir)
    content = _run_delete(tool, "/workspace/src/does-not-exist.ts")

    assert "not found" in content
    assert "permission denied" not in content
    # The deny-rule list (the "spam" that triggered retries) must be absent.
    assert "deny rule" not in content


def test_delete_missing_path_async_matches_sync(tmp_project_dir: Path) -> None:
    tool = _make_delete_tool(tmp_project_dir)
    content = _run_delete_async(tool, "/workspace/src/does-not-exist.ts")

    assert "not found" in content
    assert "permission denied" not in content


def test_delete_existing_file_still_succeeds(tmp_project_dir: Path) -> None:
    target = tmp_project_dir / "src" / "removable.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export {}\n", encoding="utf-8")

    tool = _make_delete_tool(tmp_project_dir)
    content = _run_delete(tool, "/workspace/src/removable.ts")

    assert "Deleted /workspace/src/removable.ts" in content
    assert not target.exists()


def test_delete_denied_path_still_reports_permission_denied(tmp_project_dir: Path) -> None:
    """Existing protected paths keep their permission refusal."""

    protected = tmp_project_dir / ".git" / "HEAD"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("ref: refs/heads/main\n", encoding="utf-8")

    tool = _make_delete_tool(tmp_project_dir)
    content = _run_delete(tool, "/workspace/.git/HEAD")

    assert "permission denied" in content


def test_delete_denied_missing_path_stays_fail_closed(tmp_project_dir: Path) -> None:
    """A missing path under a deny rule is refused before the backend runs.

    Otherwise delete could be used to probe which protected files exist:
    "not found" vs "permission denied" would leak existence information.
    """

    tool = _make_delete_tool(tmp_project_dir)
    content = _run_delete(tool, "/workspace/.git/never-existed")

    assert "permission denied" in content


def test_delete_existing_directory_still_scans_subtree(tmp_project_dir: Path) -> None:
    """A real directory keeps the recursive deny scan (node_modules stays denied)."""

    denied_dir = tmp_project_dir / "node_modules" / "some-pkg"
    denied_dir.mkdir(parents=True, exist_ok=True)
    (denied_dir / "index.js").write_text("module.exports = 1\n", encoding="utf-8")

    tool = _make_delete_tool(tmp_project_dir)
    content = _run_delete(tool, "/workspace/node_modules/some-pkg")

    assert "permission denied" in content


def test_patch_is_idempotent() -> None:
    # Applying twice must not double-wrap (the second call is a no-op that
    # keeps the first wrapper's identity).
    import deepagents.middleware.filesystem as filesystem_middleware

    _apply_delete_not_found_precedence()
    first = filesystem_middleware._delete_target_may_have_descendants
    _apply_delete_not_found_precedence()
    assert filesystem_middleware._delete_target_may_have_descendants is first

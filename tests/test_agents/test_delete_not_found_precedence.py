"""Delete tool must report `not found` for missing paths, not deny-rule spam.

Upstream deepagents' delete tool ran its conservative "may have descendants"
permission scan before the permission check; a missing path (``ls`` ->
``path_not_found``) was not on the leaf whitelist, so every ``**`` deny rule
matched and the model saw "permission denied" plus the full deny-rule list
for a file that did not exist — leading to 3-4 retries against the same path
(observed on the 12306 benchmark during green-baseline rejections, where
TestGenerator legitimately deletes renamed-away test files).

``ARCFilesystemMiddleware`` (wired by ``build_stage_agent``; it replaces the
stock middleware by name) recognizes the backend's explicit
``path_not_found`` as "nothing to protect": a missing path inside a writable
root reaches the backend and gets its honest ``not found``, while denied
paths stay refused before the backend runs.

Two assertion layers:

- Build-path probes drive a real ``build_stage_agent`` deep agent with a
  scripted model, asserting the results the model actually receives.
- Adapter probes drive ``ARCFilesystemMiddleware`` directly (the same class
  and construction shape the factory uses) for the permission boundaries
  stage discipline deliberately shields from the model: protected paths are
  blocked by stage discipline before the filesystem layer, so the deny-rule
  semantics can only be observed on the adapter itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agents.runtime.filesystem_adapters import (
    ARCFilesystemMiddleware,
    _confirmed_missing,
    _missing_by_ls_error,
    workspace_filesystem_backend,
)
from tests.helpers.faux import drive_scripted_tool_call


# ---------------------------------------------------------------------------
# Build-path probes: what the model receives from a built stage agent
# ---------------------------------------------------------------------------


def test_delete_missing_path_in_writable_root_reports_not_found(tmp_project_dir: Path) -> None:
    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "delete",
        {"file_path": "/workspace/tests/does-not-exist.spec.ts"},
        stage="test_generation",
    )

    assert "not found" in content
    assert "permission denied" not in content
    # The deny-rule list (the "spam" that triggered retries) must be absent.
    assert "deny rule" not in content


def test_delete_existing_file_still_succeeds(tmp_project_dir: Path) -> None:
    target = tmp_project_dir / "tests" / "removable.spec.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export {}\n", encoding="utf-8")

    (content,) = drive_scripted_tool_call(
        tmp_project_dir,
        "delete",
        {"file_path": "/workspace/tests/removable.spec.ts"},
        stage="test_generation",
    )

    assert "Deleted" in content
    assert not target.exists()


# ---------------------------------------------------------------------------
# Adapter probes: permission boundaries stage discipline shields from the model
# ---------------------------------------------------------------------------


def _make_middleware(tmp_project_dir: Path) -> ARCFilesystemMiddleware:
    """Build the production adapter over ARC's real backend routes and permissions.

    Mirrors the factory's construction (same backend helper, same permission
    rules) without going through create_deep_agent: protected-path deletes
    are refused by stage discipline long before the filesystem layer, so the
    permission semantics are only observable here.
    """

    from agents.runtime.factory import _build_filesystem_permissions
    from deepagents.backends import CompositeBackend, StateBackend

    root = tmp_project_dir.resolve()
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/workspace/": workspace_filesystem_backend(str(root)),
        },
    )
    permissions = _build_filesystem_permissions(
        root,
        [str(root)],
        skill_instruction_paths=[],
    )
    return ARCFilesystemMiddleware(backend=backend, _permissions=permissions)


def _delete_tools(tmp_project_dir: Path) -> Any:
    middleware = _make_middleware(tmp_project_dir)
    return {tool.name: tool for tool in middleware.tools}["delete"]


def _runtime() -> Any:
    from langgraph.prebuilt.tool_node import ToolRuntime

    return ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda *_: None,
        tool_call_id="call-delete-1",
        store=None,
        tools=[],
    )


def _run_delete_sync(tool: Any, file_path: str) -> str:
    message = tool.func(file_path=file_path, runtime=_runtime())
    return str(message.content)


def _run_delete_async(tool: Any, file_path: str) -> str:
    message = asyncio.run(tool.coroutine(file_path=file_path, runtime=_runtime()))
    return str(message.content)


def test_delete_missing_path_async_matches_sync(tmp_project_dir: Path) -> None:
    tool = _delete_tools(tmp_project_dir)

    assert _run_delete_sync(tool, "/workspace/tests/missing-a.spec.ts") == _run_delete_async(
        tool, "/workspace/tests/missing-b.spec.ts"
    ).replace("missing-b", "missing-a")


def test_delete_denied_path_still_reports_permission_denied(tmp_project_dir: Path) -> None:
    """Existing protected paths keep their permission refusal."""

    protected = tmp_project_dir / ".git" / "HEAD"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("ref: refs/heads/main\n", encoding="utf-8")

    tool = _delete_tools(tmp_project_dir)

    assert "permission denied" in _run_delete_sync(tool, "/workspace/.git/HEAD")


def test_delete_denied_missing_path_stays_fail_closed(tmp_project_dir: Path) -> None:
    """A missing path under a deny rule is refused before the backend runs.

    Otherwise delete could be used to probe which protected files exist:
    "not found" vs "permission denied" would leak existence information.
    """

    tool = _delete_tools(tmp_project_dir)

    assert "permission denied" in _run_delete_sync(tool, "/workspace/.git/never-existed")


def test_delete_existing_directory_still_scans_subtree(tmp_project_dir: Path) -> None:
    """A real directory keeps the recursive deny scan (node_modules stays denied)."""

    denied_dir = tmp_project_dir / "node_modules" / "some-pkg"
    denied_dir.mkdir(parents=True, exist_ok=True)
    (denied_dir / "index.js").write_text("module.exports = 1\n", encoding="utf-8")

    tool = _delete_tools(tmp_project_dir)

    assert "permission denied" in _run_delete_sync(tool, "/workspace/node_modules/some-pkg")


# ---------------------------------------------------------------------------
# Adapter units: fail-closed behavior under uncertain or broken backends
# ---------------------------------------------------------------------------


def test_probe_failure_keeps_conservative_answer() -> None:
    """A crashing probe `ls` must not confirm a target missing.

    When the confirmation probe raises a transient error, the adapter treats
    the target as NOT confirmed-missing and keeps upstream's conservative
    behavior (fail closed) instead of the exception escaping or relaxing the
    delete check.
    """

    class _FlakyLsBackend:
        def ls(self, path: str) -> Any:
            raise OSError("transient backend failure")

    assert _confirmed_missing(_FlakyLsBackend(), "/workspace/tests/x.spec.ts") is False


def test_missing_sentinel_match_is_anchored() -> None:
    """Only the backends' exact `Path '...': path_not_found` suffix counts.

    A directory literally named ``path_not_found`` whose ls fails some other
    way must not be mistaken for a confirmed-missing target: the error string
    contains the token only as part of the *path*, and relaxing the descendant
    check there would skip the recursive deny scan.
    """

    from deepagents.backends.protocol import LsResult

    assert _missing_by_ls_error(
        LsResult(error="Path '/workspace/path_not_found': path_not_found")
    ) is True
    assert _missing_by_ls_error(
        LsResult(error="Cannot list '/workspace/path_not_found': transient backend failure")
    ) is False


def test_delete_tool_degrades_to_upstream_when_deny_helper_disappears(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_project_dir: Path,
) -> None:
    """A deepagents upgrade that renames the deny-pattern helper must not
    crash agent builds: the adapter keeps the upstream delete tool and warns.

    Fails closed either way — upstream's conservative scan is the fallback.
    """

    import logging

    import deepagents.middleware.filesystem as filesystem_middleware

    monkeypatch.setattr(filesystem_middleware, "_find_delete_deny_patterns", None, raising=False)

    with caplog.at_level(logging.WARNING, logger="agents.runtime.filesystem_adapters"):
        middleware = _make_middleware(tmp_project_dir)

    assert any("delete deny-pattern helper" in record.message for record in caplog.records)

    # The delete tool is still functional: a traversal attempt is rejected by
    # the shared validate_path head before any permission or backend access.
    tool = {t.name: t for t in middleware.tools}["delete"]
    content = _run_delete_sync(tool, "/workspace/../escape")
    assert content.startswith("Error:")

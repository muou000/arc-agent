"""Glob must disclose matches withheld by the read-deny filter, not lie empty.

The easy-ticketbooking 2026-09-25 run died in a glob loop: 117 globs, 109 of
them answered ``No files found`` with ``status=ok`` while the files were
actually on disk behind read-deny rules (``/workspace/**/node_modules``,
``**/dist``, ...). Upstream deepagents' glob filters backend matches by read
permission at the middleware layer and renders the filtered-empty list as the
same bare ``No files found`` a genuinely empty search produces, so the model
saw "successful empty result", kept mutating the pattern (the
``node_modules/vitest/dist/**/cli*`` family alone fired 48 times) and burned
the last 15 minutes of the run.

``ARCFilesystemMiddleware`` rebuilds the glob tool on the upstream
``_create_glob_tool`` factory seam (the write/edit/delete rebuild pattern):
when the receipt would be the bare sentinel but the search actually matched
read-denied files, the receipt gains a disclosure note — the withheld count
plus the denied subtree segment names (``node_modules``, ``dist``, ...),
never the full matching paths, matching the delete tool's rule that receipts
must not enable probing which protected files exist. Genuinely empty
searches keep the bare sentinel; denied ``path`` arguments and traversal keep
their explicit errors.

Telemetry: ``arcbench_agent_runtime`` counts the sentinel text as an empty
result (``tests/test_python_sdk/test_events.py``); this file pins the
receipt the model sees.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agents.runtime.filesystem_adapters import (
    ARCFilesystemMiddleware,
    workspace_filesystem_backend,
)

# The receipt the model must never receive again: the bare upstream sentinel
# for a search whose only matches sit behind read-deny rules.
_GLOB_NO_FILES_SENTINEL = "No files found"


def _make_middleware(tmp_project_dir: Path, *, backend: Any = None) -> ARCFilesystemMiddleware:
    """Build the production adapter over ARC's real backend routes and permissions.

    Mirrors the factory's construction (same backend helper, same permission
    rules) without going through create_deep_agent, so the permission-filter
    boundary is observable directly. ``backend=`` swaps in a stub for
    fail-open probes.
    """

    from agents.runtime.factory import _build_filesystem_permissions
    from deepagents.backends import CompositeBackend, StateBackend

    root = tmp_project_dir.resolve()
    if backend is None:
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


def _glob_tool(tmp_project_dir: Path, **kwargs: Any) -> Any:
    middleware = _make_middleware(tmp_project_dir, **kwargs)
    return {tool.name: tool for tool in middleware.tools}["glob"]


def _runtime() -> Any:
    from langgraph.prebuilt.tool_node import ToolRuntime

    return ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda *_: None,
        tool_call_id="call-glob-1",
        store=None,
        tools=[],
    )


def _run_sync(tool: Any, **args: Any) -> str:
    message = tool.func(runtime=_runtime(), **args)
    return str(message.content)


def _run_async(tool: Any, **args: Any) -> str:
    message = asyncio.run(tool.coroutine(runtime=_runtime(), **args))
    return str(message.content)


def _seed_node_modules_cli(tmp_project_dir: Path) -> None:
    """The run's death family: a real CLI script inside a denied node_modules."""

    cli = tmp_project_dir / "backend" / "node_modules" / "vitest" / "dist" / "cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Disclosure on the bare-sentinel path
# ---------------------------------------------------------------------------


def test_deny_subtree_pattern_discloses_withheld_matches(tmp_project_dir: Path) -> None:
    """A pattern whose only hits sit behind read-deny says so, with a count."""

    _seed_node_modules_cli(tmp_project_dir)
    tool = _glob_tool(tmp_project_dir)

    content = _run_sync(tool, pattern="**/vitest/dist/**/cli*", path="/workspace")

    assert content.startswith(_GLOB_NO_FILES_SENTINEL)
    assert "1 match withheld by read-deny" in content
    assert "node_modules" in content  # the denied subtree segment name


def test_disclosure_never_echoes_full_withheld_paths(tmp_project_dir: Path) -> None:
    """Count + subtree segment names only — receipts must not enable probing
    which protected files exist (the delete-tool precedent)."""

    _seed_node_modules_cli(tmp_project_dir)
    tool = _glob_tool(tmp_project_dir)

    content = _run_sync(tool, pattern="**/node_modules/**/cli*", path="/workspace")

    assert "cli.js" not in content
    assert "vitest" not in content
    assert "/workspace/backend" not in content


def test_disclosure_names_each_denied_subtree_with_counts(tmp_project_dir: Path) -> None:
    """Matches spread over two denied subtrees disclose the total and both names."""

    node_modules_file = tmp_project_dir / "backend" / "node_modules" / "left-pad" / "index.js"
    node_modules_file.parent.mkdir(parents=True, exist_ok=True)
    node_modules_file.write_text("module.exports = 1\n", encoding="utf-8")
    dist_file = tmp_project_dir / "frontend" / "dist" / "bundle.js"
    dist_file.parent.mkdir(parents=True, exist_ok=True)
    dist_file.write_text("// bundle\n", encoding="utf-8")
    tool = _glob_tool(tmp_project_dir)

    content = _run_sync(tool, pattern="**/*.js", path="/workspace")

    assert content.startswith(_GLOB_NO_FILES_SENTINEL)
    assert "2 matches withheld by read-deny" in content
    assert "node_modules" in content
    assert "dist" in content


def test_genuinely_empty_pattern_keeps_bare_sentinel(tmp_project_dir: Path) -> None:
    """A search with no matches at all keeps upstream's exact empty receipt."""

    tool = _glob_tool(tmp_project_dir)

    content = _run_sync(tool, pattern="**/no-such-file-xyz-*", path="/workspace")

    assert content == _GLOB_NO_FILES_SENTINEL


def test_partial_visibility_lists_visible_matches_without_fabricating(tmp_project_dir: Path) -> None:
    """A pattern matching a visible file and a denied file still lists the visible one.

    The disclosure obligation is the bare sentinel (the shape that read as
    "successful empty"); a partially visible result keeps upstream behavior.
    """

    _seed_node_modules_cli(tmp_project_dir)
    visible = tmp_project_dir / "backend" / "scripts" / "cli.js"
    visible.parent.mkdir(parents=True, exist_ok=True)
    visible.write_text("// app cli\n", encoding="utf-8")
    tool = _glob_tool(tmp_project_dir)

    content = _run_sync(tool, pattern="**/cli.js", path="/workspace")

    assert _GLOB_NO_FILES_SENTINEL not in content
    assert "/workspace/backend/scripts/cli.js" in content


# ---------------------------------------------------------------------------
# Unchanged upstream behavior: explicit refusals
# ---------------------------------------------------------------------------


def test_denied_path_param_still_reports_permission_denied(tmp_project_dir: Path) -> None:
    """A path argument landing in a deny rule keeps the explicit error."""

    tool = _glob_tool(tmp_project_dir)

    content = _run_sync(tool, pattern="**/cli*", path="/workspace/backend/node_modules")

    assert "permission denied" in content


def test_traversal_path_still_rejected(tmp_project_dir: Path) -> None:
    tool = _glob_tool(tmp_project_dir)

    content = _run_sync(tool, pattern="**/*.js", path="/workspace/../escape")

    assert content.startswith("Error:")


def test_async_disclosure_matches_sync(tmp_project_dir: Path) -> None:
    _seed_node_modules_cli(tmp_project_dir)
    tool = _glob_tool(tmp_project_dir)

    sync_content = _run_sync(tool, pattern="**/node_modules/**/cli*", path="/workspace")
    async_content = _run_async(tool, pattern="**/node_modules/**/cli*", path="/workspace")

    assert async_content.startswith(_GLOB_NO_FILES_SENTINEL)
    assert "withheld by read-deny" in async_content
    assert "node_modules" in async_content
    assert async_content == sync_content


# ---------------------------------------------------------------------------
# Fail-open: a broken probe or missing upstream helper keeps upstream receipts
# ---------------------------------------------------------------------------


class _FirstSearchThenProbeBackend:
    """Backend that answers the real search, then shapes only the probe.

    The upstream glob tool runs the first search; the disclosure wrapper
    re-runs it at the backend level as the probe. ``probe_result`` lets a
    test make the probe fail or truncate without disturbing the receipt
    path upstream owns.
    """

    def __init__(self, probe_result: Any) -> None:
        self._probe_result = probe_result
        self.calls = 0

    def glob(self, pattern: str, path: str | None = None) -> Any:
        from deepagents.backends.protocol import GlobResult

        self.calls += 1
        if self.calls == 1:
            # The real search: every match sits behind read-deny, so
            # upstream renders the bare sentinel.
            return GlobResult(
                matches=[{"path": "/workspace/backend/node_modules/vitest/dist/cli.js"}]
            )
        return self._probe_result


def test_probe_failure_keeps_bare_sentinel(tmp_project_dir: Path) -> None:
    """A failing disclosure probe must not corrupt or crash the receipt."""

    tool = _glob_tool(
        tmp_project_dir, backend=_FirstSearchThenProbeBackend(OSError("transient backend failure"))
    )

    content = _run_sync(tool, pattern="**/node_modules/**/cli*", path="/workspace")

    assert content == _GLOB_NO_FILES_SENTINEL


def test_truncated_probe_reports_lower_bound(tmp_project_dir: Path) -> None:
    """A probe truncated mid-walk discloses 'at least N', not a false exact count."""

    from deepagents.backends.protocol import GlobResult

    truncated = GlobResult(
        matches=[{"path": "/workspace/backend/node_modules/vitest/dist/cli.js"}],
        truncated=True,
    )
    tool = _glob_tool(tmp_project_dir, backend=_FirstSearchThenProbeBackend(truncated))

    content = _run_sync(tool, pattern="**/node_modules/**/cli*", path="/workspace")

    assert "at least 1 match withheld by read-deny" in content


def test_degrades_to_upstream_when_filter_helper_disappears(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_project_dir: Path,
) -> None:
    """A deepagents upgrade renaming the glob filter must not crash builds:
    the adapter keeps the upstream glob tool (bare sentinel) and warns."""

    import logging

    import deepagents.middleware.filesystem as filesystem_middleware

    monkeypatch.setattr(
        filesystem_middleware, "_apply_permissions_to_glob_results", None, raising=False
    )

    with caplog.at_level(logging.WARNING, logger="agents.runtime.filesystem_adapters"):
        tool = _glob_tool(tmp_project_dir)

    assert any("glob permission filter" in record.message for record in caplog.records)

    # The upstream glob tool still answers: traversal is rejected by the
    # shared validate_path head before any permission or backend access.
    content = _run_sync(tool, pattern="**/*.js", path="/workspace/../escape")
    assert content.startswith("Error:")

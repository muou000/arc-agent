"""Contract tests for the per-app-type scaffold context file list.

The context pipeline injects these workspace-relative files as a static
context layer shared by every node. The declared set must stay relative,
boundary-safe, and limited to template-owned files — node integration glue
(route registration, entry files) must never be listed because agents edit
it during the run and a static snapshot would go stale.
"""

from __future__ import annotations

from app_type_handler.base import AppTypeHandler
from app_type_handler.web import WebAppType


def test_web_declares_the_database_scaffold_and_test_configs() -> None:
    files = WebAppType.scaffold_context_files()

    assert "backend/src/database/test_harness.js" in files
    assert "backend/src/database/init_db.js" in files
    assert "backend/src/database/db_runtime.js" in files
    assert "backend/vitest.config.js" in files
    assert "backend/playwright.config.js" in files
    assert "frontend/vite.config.js" in files
    assert "frontend/test/setup.ts" in files


def test_scaffold_context_files_are_workspace_relative_and_boundary_safe() -> None:
    for files in (AppTypeHandler.scaffold_context_files(), WebAppType.scaffold_context_files()):
        for path in files:
            assert not path.startswith("/"), f"absolute scaffold path: {path}"
            assert "\\" not in path, f"non-normalized scaffold path: {path}"
            assert ".." not in path.split("/"), f"scaffold path escapes the workspace: {path}"


def test_node_integration_glue_is_not_declared_as_scaffold() -> None:
    files = set(WebAppType.scaffold_context_files())

    # These files are edited by nodes during compilation (route registration,
    # server entry, styling) — a static snapshot of them would go stale.
    assert "backend/src/app.js" not in files
    assert "backend/src/index.js" not in files
    assert "frontend/src/App.tsx" not in files
    assert "frontend/src/main.tsx" not in files

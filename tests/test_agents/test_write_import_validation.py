"""Tests for the write-time test-import validation (issue #156).

The 2026-09-22 serial run lost a full IMPLEMENT retry budget to two
mechanically detectable import defects in a DESIGN-produced test file: a
relative import one ``../`` short and an ESM import without ``.js``. The
validation (``agents/runtime/import_checks.py`` + the
``StageDisciplineMiddleware`` write hooks) must:

- pass imports that resolve against the workspace exactly as written;
- reject wrong depth / missing extension / missing target with a message
  that names the exact correction;
- fail open on everything it cannot be sure about (opaque dynamic imports,
  bare specifiers, no workspace root, non-JS test files).

The middleware-level tests build a small template-shaped workspace in
``tmp_path`` and drive ``wrap_tool_call`` with synthetic requests, the same
way ``test_stage_discipline.py`` does; no agent runtime is involved.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.runtime.import_checks import (
    build_import_block_message,
    classify_import,
    extract_relative_esm_imports,
)
from agents.runtime.stage_discipline import StageDisciplineMiddleware
from agents.tools.test_manifest import DeclaredTestFile, TestManifestLock

WORKSPACE = "/workspace"

# A template-shaped backend: the files the web template ships before any
# stage writes anything. Depths mirror the serial-run defect report.
TEMPLATE_FILES: dict[str, str] = {
    "backend/src/app.js": "export default {};\n",
    "backend/src/database/test_harness.js": "export function createTestDatabaseHarness() {}\n",
    "backend/src/database/index.js": "export {};\n",
    "backend/src/repositories/domainRepository.js": "export {};\n",
    "backend/src/services/authService.js": "export {};\n",
    "frontend/src/pages/LoginPage.jsx": "export default () => null;\n",
}


@pytest.fixture()
def workspace(tmp_path):
    for relative, content in TEMPLATE_FILES.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


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


def run(middleware: StageDisciplineMiddleware, request: ToolCallRequest) -> Any:
    return middleware.wrap_tool_call(request, ok_tool)


def make(stage: str, workspace_root: str | None) -> StageDisciplineMiddleware:
    return StageDisciplineMiddleware(stage=stage, workspace_root=workspace_root)


def declared_lock(paths: list[str]) -> TestManifestLock:
    lock = TestManifestLock()
    lock.declare(
        [
            DeclaredTestFile(
                file_path=path,
                test_type="Integration",
                interface_ids=["IF-X"],
                coverage_scope="owned",
            )
            for path in paths
        ]
    )
    return lock


def build_testgen(workspace_root: str | None, manifest_paths: list[str]) -> StageDisciplineMiddleware:
    return StageDisciplineMiddleware(
        stage="test_generation",
        workspace_root=workspace_root,
        test_manifest_lock=declared_lock(manifest_paths),
    )


TEST_PATH = f"{WORKSPACE}/backend/tests/integration/auth_register_api.test.js"
DECLARED = ["backend/tests/integration/auth_register_api.test.js"]


def write_args(content: str, path: str = TEST_PATH) -> dict[str, Any]:
    return {"file_path": path, "content": content}


# ---------------------------------------------------------------------------
# Pass-through: imports that resolve exactly as written
# ---------------------------------------------------------------------------


def test_correct_import_passes(workspace) -> None:
    content = (
        "import { describe, it, expect } from 'vitest';\n"
        "import request from 'supertest';\n"
        "import { createTestDatabaseHarness } from '../../src/database/test_harness.js';\n"
        "let app;\n"
        "beforeAll(async () => {\n"
        "  app = (await import('../../src/app.js')).default;\n"
        "});\n"
    )
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert not isinstance(result, ToolMessage) or result.status != "error"
    assert result.content == "ok"


def test_bare_specifiers_and_frontend_alias_pass(workspace) -> None:
    content = (
        "import { describe, expect } from 'vitest';\n"
        "import { render } from '@testing-library/react';\n"
        "import { fetchDomain } from '@/api/domain';\n"
    )
    path = f"{WORKSPACE}/frontend/tests/pages/LoginPage.test.jsx"
    middleware = build_testgen(str(workspace), ["frontend/tests/pages/LoginPage.test.jsx"])
    result = run(middleware, make_request("write_file", {"file_path": path, "content": content}))
    assert result.content == "ok"


def test_commented_out_import_is_ignored(workspace) -> None:
    content = (
        "// import { gone } from '../../src/services/missing.js';\n"
        "/* import { alsoGone } from '../../src/services/also-missing.js'; */\n"
        "import { createTestDatabaseHarness } from '../../src/database/test_harness.js';\n"
    )
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert result.content == "ok"


def test_import_from_comment_trailing_line_is_ignored(workspace) -> None:
    content = (
        "import { createTestDatabaseHarness } from '../../src/database/test_harness.js'; // from '../../src/app'\n"
    )
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert result.content == "ok"


def test_multiline_import_is_parsed(workspace) -> None:
    content = (
        "import {\n"
        "  createTestDatabaseHarness,\n"
        "} from '../../src/database/test_harness';\n"
    )
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "test_harness.js" in result.content


def test_python_test_file_is_out_of_scope(workspace) -> None:
    content = "from ..src.app import create_app\nimport utils\n"
    path = f"{WORKSPACE}/tests/cli/test_main.py"
    middleware = StageDisciplineMiddleware(stage="implementation", workspace_root=str(workspace))
    result = run(middleware, make_request("write_file", {"file_path": path, "content": content}))
    assert result.content == "ok"


def test_without_workspace_root_validation_is_off() -> None:
    content = "import app from '../src/app';\n"
    middleware = build_testgen(None, DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert result.content == "ok"


def test_path_outside_manifest_fails_open(workspace) -> None:
    content = "import ghost from '../../src/services/ghost.js';\n"
    path = f"{WORKSPACE}/backend/tests/integration/diagnostic.test.js"
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        workspace_root=str(workspace),
        test_manifest_lock=declared_lock(DECLARED),
    )
    result = run(middleware, make_request("write_file", write_args(content, path=path)))
    assert result.content == "ok"


def test_without_manifest_lock_validation_is_off(workspace) -> None:
    content = "import ghost from '../../src/services/ghost.js';\n"
    middleware = make("implementation", str(workspace))
    result = run(middleware, make_request("write_file", write_args(content)))
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# The three rejections, each with the exact correction in the message
# ---------------------------------------------------------------------------


def test_wrong_depth_is_blocked_with_correction(workspace) -> None:
    content = "let app;\nbeforeAll(async () => {\n  app = (await import('../src/app')).default;\n});\n"
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "Test import blocked" in result.content
    assert "did you mean '../../src/app.js'" in result.content


def test_missing_extension_is_blocked_with_correction(workspace) -> None:
    content = "import { createTestDatabaseHarness } from '../../src/database/test_harness';\n"
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "explicit extension" in result.content
    assert "'../../src/database/test_harness.js'" in result.content


def test_missing_target_is_blocked_without_invented_correction(workspace) -> None:
    content = "import { authService } from '../../src/services/tokenService.js';\n"
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "no file at 'backend/src/services/tokenService.js'" in result.content
    # No correction may be suggested: nothing near the specifier exists.
    assert "did you mean" not in result.content


def test_multiple_violations_land_in_one_message(workspace) -> None:
    content = (
        "import { createTestDatabaseHarness } from '../../src/database/test_harness';\n"
        "app = (await import('../src/app')).default;\n"
    )
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(result, ToolMessage)
    assert result.content.count("- '") == 2


def test_index_resolution_suggests_explicit_index_path(workspace) -> None:
    content = "import { resetDatabase } from '../../src/database';\n"
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "'../../src/database/index.js'" in result.content


def test_frontend_jsx_extension_correction(workspace) -> None:
    content = "import LoginPage from '../../src/pages/LoginPage';\n"
    path = f"{WORKSPACE}/frontend/tests/pages/LoginPage.test.jsx"
    middleware = build_testgen(str(workspace), ["frontend/tests/pages/LoginPage.test.jsx"])
    result = run(middleware, make_request("write_file", {"file_path": path, "content": content}))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "'../../src/pages/LoginPage.jsx'" in result.content


# ---------------------------------------------------------------------------
# Fail-open: shapes the check cannot be sure about pass with a count
# ---------------------------------------------------------------------------


def test_opaque_dynamic_import_fails_open_with_count(workspace) -> None:
    content = (
        "const modulePath = '../src/app';\n"
        "app = (await import(modulePath)).default;\n"
    )
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert result.content == "ok"
    assert middleware.import_check_fail_opens() == 1


def test_escaping_relative_import_is_blocked(workspace) -> None:
    """A literal import that climbs above the workspace is statically invalid.

    Unlike opaque dynamic imports, this specifier is fully resolved and
    provably names no workspace file, so it blocks instead of failing open.
    """

    content = "import thing from '../../../../../outside.js';\n"
    middleware = build_testgen(str(workspace), DECLARED)
    result = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "outside the workspace" in result.content or "<outside workspace>" in result.content


# ---------------------------------------------------------------------------
# Stage coverage: DESIGN produces test assets, TDD repairs them
# ---------------------------------------------------------------------------


def test_interface_design_write_of_broken_test_file_is_blocked_and_unlocks_path(workspace) -> None:
    content = "app = (await import('../src/app')).default;\n"
    middleware = StageDisciplineMiddleware(
        stage="interface_design",
        workspace_root=str(workspace),
        test_manifest_lock=declared_lock(DECLARED),
    )
    blocked = run(middleware, make_request("write_file", write_args(content)))
    assert isinstance(blocked, ToolMessage)
    assert blocked.status == "error"
    # The rejected write consumed no budget...
    assert middleware._design_write_reservations == set()
    # ...and left the path unlocked for the corrected rewrite.
    fixed = run(middleware, make_request("write_file", write_args(
        "app = (await import('../../src/app.js')).default;\n"
    ),))
    assert fixed.content == "ok"


def test_interface_design_reservation_satisfies_same_batch_import(workspace) -> None:
    """A skeleton validated earlier in the same parallel batch counts as existing."""

    middleware = StageDisciplineMiddleware(
        stage="interface_design",
        workspace_root=str(workspace),
        test_manifest_lock=declared_lock(DECLARED),
    )
    skeleton = make_request(
        "write_file",
        {"file_path": f"{WORKSPACE}/backend/src/services/tokenService.js", "content": "export {};\n"},
    )
    assert middleware._validate_tool_call(skeleton) is None  # reserved, not yet materialized
    content = "import { tokenService } from '../../src/services/tokenService.js';\n"
    result = run(middleware, make_request("write_file", write_args(content)))
    assert result.content == "ok"


def test_interface_design_append_of_broken_import_is_blocked(workspace) -> None:
    middleware = StageDisciplineMiddleware(
        stage="interface_design",
        workspace_root=str(workspace),
        test_manifest_lock=declared_lock(DECLARED),
    )
    result = run(middleware, make_request(
        "append_file",
        {
            "file_path": f"{WORKSPACE}/backend/tests/integration/auth_register_api.test.js",
            "content": "import { harness } from '../src/database/test_harness';\n",
        },
    ))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "Test import blocked" in result.content


def test_implementation_write_of_broken_test_file_is_blocked(workspace) -> None:
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        workspace_root=str(workspace),
        test_manifest_lock=declared_lock(DECLARED),
    )
    result = run(middleware, make_request("write_file", write_args(
        "import { createTestDatabaseHarness } from '../src/database/test_harness.js';\n"
    )))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


# ---------------------------------------------------------------------------
# edit_file: surviving imports are validated through the merged content
# ---------------------------------------------------------------------------

ON_DISK_TEST = (
    "import { createTestDatabaseHarness } from '../src/database/test_harness';\n"
    "\n"
    "describe('auth', () => {\n"
    "  it('works', () => {});\n"
    "});\n"
)


@pytest.fixture()
def workspace_with_broken_test(workspace):
    target = workspace / "backend/tests/integration/auth_register_api.test.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ON_DISK_TEST, encoding="utf-8")
    return workspace


def test_edit_elsewhere_still_flags_surviving_broken_import(workspace_with_broken_test) -> None:
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        workspace_root=str(workspace_with_broken_test),
        test_manifest_lock=declared_lock(DECLARED),
    )
    result = run(middleware, make_request(
        "edit_file",
        {
            "file_path": TEST_PATH,
            "old_string": "it('works', () => {});",
            "new_string": "it('works again', () => {});",
        },
    ))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "did you mean '../../src/database/test_harness.js'" in result.content


def test_fixing_edit_resolves_and_passes(workspace_with_broken_test) -> None:
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        workspace_root=str(workspace_with_broken_test),
        test_manifest_lock=declared_lock(DECLARED),
    )
    result = run(middleware, make_request(
        "edit_file",
        {
            "file_path": TEST_PATH,
            "old_string": "from '../src/database/test_harness';",
            "new_string": "from '../../src/database/test_harness.js';",
        },
    ))
    assert result.content == "ok"


def test_edit_with_stale_old_string_falls_back_to_new_string_scan(workspace_with_broken_test) -> None:
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        workspace_root=str(workspace_with_broken_test),
        test_manifest_lock=declared_lock(DECLARED),
    )
    result = run(middleware, make_request(
        "edit_file",
        {
            "file_path": TEST_PATH,
            "old_string": "NOT ON DISK",
            "new_string": "import { x } from '../../src/services/tokenService.js';\n",
        },
    ))
    # tokenService.js does not exist in this fixture: flagged via fallback.
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "tokenService.js" in result.content


def test_edit_of_clean_test_file_passes(workspace_with_broken_test) -> None:
    clean = ON_DISK_TEST.replace("'../src/database/test_harness';", "'../../src/database/test_harness.js';")
    (workspace_with_broken_test / "backend/tests/integration/auth_register_api.test.js").write_text(
        clean, encoding="utf-8"
    )
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        workspace_root=str(workspace_with_broken_test),
        test_manifest_lock=declared_lock(DECLARED),
    )
    result = run(middleware, make_request(
        "edit_file",
        {
            "file_path": TEST_PATH,
            "old_string": "it('works', () => {});",
            "new_string": "it('works again', () => {});",
        },
    ))
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# TDD adapter: the implementation stage builds its lock from the node manifest
# ---------------------------------------------------------------------------


def test_tdd_builds_import_lock_from_node_tests() -> None:
    from agents.test_driven_developer import TestDrivenDeveloper

    developer = TestDrivenDeveloper()
    lock = developer._build_import_manifest_lock(
        [
            {
                "file_path": "/workspace/backend/tests/integration/auth.test.js",
                "type": "Integration",
                "interface_ids": ["IF-AUTH"],
            },
            # Helpers/configs never carry manifest rows: filtered out.
            {"file_path": "backend/tests/helpers/util.js", "type": "Unit"},
            # Duplicates collapse to one row.
            {
                "file_path": "backend/tests/integration/auth.test.js",
                "type": "Integration",
            },
            # Rows without a file_path are skipped.
            {"test_id": "T-1", "type": "Unit"},
        ]
    )
    assert lock is not None
    assert list(lock.declared_files) == ["backend/tests/integration/auth.test.js"]
    row = lock.declared_files["backend/tests/integration/auth.test.js"]
    assert row.test_type == "Integration"
    assert row.interface_ids == ["IF-AUTH"]


def test_tdd_import_lock_is_none_without_manifest_rows() -> None:
    from agents.test_driven_developer import TestDrivenDeveloper

    developer = TestDrivenDeveloper()
    assert developer._build_import_manifest_lock([]) is None
    assert developer._build_import_manifest_lock([{"summary": "no rows"}]) is None


# ---------------------------------------------------------------------------
# Pure classification unit tests (no middleware)
# ---------------------------------------------------------------------------


def _exists_factory(present: set[str]):
    return lambda rel: rel in present


PRESENT = {
    "backend/src/app.js",
    "backend/src/database/test_harness.js",
    "backend/src/database/index.js",
}


def test_classify_exact_hit_returns_none() -> None:
    assert classify_import("../../src/app.js", "backend/tests/integration", _exists_factory(PRESENT)) is None


def test_classify_depth_suggestion_carries_extension() -> None:
    violation = classify_import("../src/app", "backend/tests/integration", _exists_factory(PRESENT))
    assert violation is not None
    assert violation.kind == "depth"
    assert violation.suggestion == "../../src/app.js"
    assert violation.resolved == "backend/src/app.js"


def test_classify_extension_kind() -> None:
    violation = classify_import(
        "../../src/database/test_harness", "backend/tests/integration", _exists_factory(PRESENT)
    )
    assert violation is not None
    assert violation.kind == "extension"
    assert violation.suggestion == "../../src/database/test_harness.js"


def test_classify_missing_kind_has_no_suggestion() -> None:
    violation = classify_import("../../src/services/ghost.js", "backend/tests/integration", _exists_factory(PRESENT))
    assert violation is not None
    assert violation.kind == "missing"
    assert violation.suggestion == ""


def test_classify_escaping_import_is_blocked() -> None:
    violation = classify_import(
        "../../../../etc/x.js", "backend/tests/integration", _exists_factory(PRESENT)
    )
    assert violation is not None
    assert violation.kind == "missing"
    assert violation.suggestion == ""


def test_extract_reports_relative_only_and_counts_opaque() -> None:
    content = (
        "import { describe } from 'vitest';\n"
        "import x from './relative.js';\n"
        "import '../side-effect.js';\n"
        "export { helper } from '../exporter.js';\n"
        "const lazy = () => import(dynamicPath);\n"
        "const lazy2 = () => import(`../${name}.js`);\n"
        "// import { commented } from '../commented.js';\n"
    )
    specifiers, opaque = extract_relative_esm_imports(content)
    assert specifiers == ["./relative.js", "../side-effect.js", "../exporter.js"]
    assert opaque == 2


def test_message_builder_lists_target_and_corrections() -> None:
    violation = classify_import("../src/app", "backend/tests/integration", _exists_factory(PRESENT))
    message = build_import_block_message(
        "/workspace/backend/tests/integration/auth_register_api.test.js", [violation]
    )
    assert "backend/tests/integration/auth_register_api.test.js" in message
    assert "did you mean '../../src/app.js'" in message

"""DESIGN-phase static satisfiability check and its TDD handoff.

``run_design_phase`` extracts the observable hooks of the generated
E2E/Integration tests, classifies them against the requirement + interface
specs, and stores the un-grounded remainder as ``test_contract_hooks`` in the
node session. The context pipeline then surfaces that block to
TestDrivenDeveloper — closing the dual-blind gap where the tests drove
labels/selectors the implementer never saw (the REQ-1 failure loop).

These tests drive the real ``WorkflowPhaseRunner.run_design_phase`` with stub
adapters (same harness as ``test_design_baseline_red_gate``) and assert on
the node session plus the pipeline output.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from agents.context import pipeline as context_pipeline_module
from agents.context.pipeline import context_pipeline
from core import sessions
from core.phases import WorkflowPhaseRunner
from tests.helpers.faux import FakeAppHandler, failing_test_output
from tests.test_agents.conftest import arc_runtime  # noqa: F401


E2E_TEST_FILE = "backend/test-e2e/register.e2e.spec.js"

E2E_SPEC = """\
const { test, expect } = require('@playwright/test');
test('register flow', async ({ page }) => {
  await page.goto('/register');
  await page.getByLabel('用户名').fill('tb-user-abc');
  await page.getByTestId('submit-btn').click();
  await expect(page.getByText('Sign out')).toBeVisible();
});
"""

REQ_DATA = {
    "name": "注册旅客账号",
    "description": "注册页提供 `用户名` 输入框；提交后导航到 /register。",
}


class _StubDesigner:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def run(self, node_id: str, requirement_data: dict) -> dict:
        return self.payload


class _StubGenerator:
    def __init__(self, manifest: list[dict[str, Any]]) -> None:
        self.app_handler = None
        self._manifest = manifest

    async def run(self, node_id: str, requirement_data: dict, **kwargs: Any) -> tuple:
        return (list(self._manifest), "{}")


class _StubTDD:
    def __init__(self) -> None:
        self.app_handler = None


def _make_runner(tmp_project_dir: Path) -> tuple[WorkflowPhaseRunner, list[tuple]]:
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    requirements_dir = tmp_project_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=_StubDesigner(
            {
                "summary": "Register contract.",
                "interfaces": [
                    {
                        "interface_id": "IF-REG",
                        "type": "UI",
                        "name": "register-page",
                        "responsibility": "Register form",
                        "specification": "Renders the register form at /register.",
                        "file_path": "frontend/src/pages/Register.jsx",
                        "first_line": "export function Register()",
                    }
                ],
                "files_written": [],
            }
        ),
        test_generator=_StubGenerator(
            [
                {
                    "test_id": "T-E2E",
                    "req_id": "REQ-HOOK-1",
                    "interface_ids": [],
                    "type": "E2E",
                    "file_path": E2E_TEST_FILE,
                    "first_line": "const { test, expect }",
                }
            ]
        ),
        test_driven_developer=_StubTDD(),
        log_cb=log_cb,
    )
    runner.app_handler = FakeAppHandler([failing_test_output()])
    return runner, logs


def _write_e2e_spec(tmp_project_dir: Path) -> None:
    path = tmp_project_dir / E2E_TEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(E2E_SPEC, encoding="utf-8")


def _run_design(runner: WorkflowPhaseRunner, node_id: str) -> bool:
    return asyncio.run(runner.run_design_phase(node_id, dict(REQ_DATA)))


def test_design_phase_stores_test_contract_hooks(tmp_project_dir: Path, arc_runtime) -> None:
    """Un-grounded hooks land in the node session; grounded ones do not.

    The requirement names `用户名` and `/register`, so those hooks are grounded;
    `submit-btn` (testid) and `Sign out` (text) are test-defined and must be
    declared to the implementer.
    """

    node_id = "REQ-HOOK-1"
    arc_runtime.traceability.store_requirement_tree({"id": node_id, **REQ_DATA})
    _write_e2e_spec(tmp_project_dir)
    runner, logs = _make_runner(tmp_project_dir)

    assert _run_design(runner, node_id) is True

    session = sessions.load_node_session(node_id)
    hooks = session.get("test_contract_hooks") or []
    values = {(h["kind"], h["value"]) for h in hooks}
    assert ("testid", "submit-btn") in values
    assert ("text", "Sign out") in values
    # Grounded hooks are not re-declared (the implementer's requirement and
    # interface texts already carry them).
    assert ("label", "用户名") not in values
    assert ("url", "/register") not in values
    # The run log names the handed-over hooks.
    joined = "\n".join(message for _, message, _, _ in logs)
    assert "Static satisfiability check" in joined
    assert "submit-btn" in joined


def test_design_phase_check_fails_open(tmp_project_dir: Path, arc_runtime) -> None:
    """A checker crash must not fail DESIGN — the gate logs and continues."""

    node_id = "REQ-HOOK-2"
    arc_runtime.traceability.store_requirement_tree({"id": node_id, **REQ_DATA})
    _write_e2e_spec(tmp_project_dir)
    runner, logs = _make_runner(tmp_project_dir)

    def exploding_check(**kwargs: Any) -> list:
        raise RuntimeError("boom")

    original = runner._check_test_contract_satisfiability
    runner._check_test_contract_satisfiability = exploding_check
    try:
        assert _run_design(runner, node_id) is True
    finally:
        runner._check_test_contract_satisfiability = original
    joined = "\n".join(message for _, message, _, _ in logs)
    assert "Static satisfiability check failed (boom)" in joined
    # Fail-open leaves no declared hooks (empty list, not a crash artifact).
    assert sessions.load_node_session(node_id).get("test_contract_hooks") == []


def test_tdd_context_surfaces_test_contract_hooks(tmp_project_dir: Path, arc_runtime) -> None:
    """The pipeline renders the stored hooks for the implementation stages."""

    node_id = "REQ-HOOK-3"
    arc_runtime.traceability.store_requirement_tree({"id": node_id, **REQ_DATA})
    sessions.merge_node_session(
        node_id,
        {
            "test_contract_hooks": [
                {"kind": "testid", "value": "submit-btn", "file_path": E2E_TEST_FILE},
                {"kind": "text", "value": "Sign out", "file_path": E2E_TEST_FILE},
            ]
        },
    )
    context_pipeline.configure(workspace_dir=str(tmp_project_dir), app_type="web")
    context = context_pipeline_module.context_pipeline.build_agent_context(
        node_id=node_id,
        agent_type="TestDrivenDeveloper",
    )
    assert "<test_contract_hooks>" in context
    assert "`submit-btn`" in context
    assert "`Sign out`" in context
    assert "used by backend/test-e2e/register.e2e.spec.js" in context
    # Non-implementation agents do not get the block.
    generator_context = context_pipeline_module.context_pipeline.build_agent_context(
        node_id=node_id,
        agent_type="TestGenerator",
    )
    assert "<test_contract_hooks>" not in generator_context


def test_tdd_context_without_hooks_has_no_block(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-HOOK-4"
    arc_runtime.traceability.store_requirement_tree({"id": node_id, **REQ_DATA})
    context_pipeline.configure(workspace_dir=str(tmp_project_dir), app_type="web")
    context = context_pipeline_module.context_pipeline.build_agent_context(
        node_id=node_id,
        agent_type="TestDrivenDeveloper",
    )
    assert "<test_contract_hooks>" not in context


def test_tdd_context_hooks_refresh_after_session_rewrite(tmp_project_dir: Path, arc_runtime) -> None:
    """A same-process hook rewrite must not serve the cached old block.

    The DESIGN phase rewrites ``test_contract_hooks`` in the node session on
    every (re)design pass; ``_update_node_session`` invalidates the db layers,
    so the next context build for the same node must read the new list. This
    pins the invalidation wiring (``test_contract_hooks`` belongs to the
    db-layer invalidation set).
    """

    node_id = "REQ-HOOK-5"
    arc_runtime.traceability.store_requirement_tree({"id": node_id, **REQ_DATA})
    context_pipeline.configure(workspace_dir=str(tmp_project_dir), app_type="web")

    sessions.merge_node_session(
        node_id,
        {"test_contract_hooks": [{"kind": "testid", "value": "old-hook", "file_path": E2E_TEST_FILE}]},
    )
    first = context_pipeline.build_agent_context(node_id=node_id, agent_type="TestDrivenDeveloper")
    assert "`old-hook`" in first

    # The workflow's session write path: merge + invalidate_db_layers.
    sessions.merge_node_session(
        node_id,
        {"test_contract_hooks": [{"kind": "testid", "value": "new-hook", "file_path": E2E_TEST_FILE}]},
    )
    context_pipeline.cache.invalidate_db_layers(node_id)

    second = context_pipeline.build_agent_context(node_id=node_id, agent_type="TestDrivenDeveloper")
    assert "`new-hook`" in second
    assert "`old-hook`" not in second


def _make_status_runner(
    tmp_project_dir: Path,
    status_code: int,
    *,
    interface_specification: str = "POST /api/notes returns 201.",
) -> tuple[WorkflowPhaseRunner, list[tuple]]:
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    requirements_dir = tmp_project_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=_StubDesigner(
            {
                "summary": "Notes API contract.",
                "interfaces": [
                    {
                        "interface_id": "IF-NOTES",
                        "type": "API",
                        "name": "notes-route",
                        "responsibility": "Creates notes.",
                        "specification": interface_specification,
                        "file_path": "backend/src/routes/notes.js",
                        "first_line": "router.post('/notes'",
                    }
                ],
                "files_written": [],
            }
        ),
        test_generator=_StubGenerator(
            [
                {
                    "test_id": "T-NOTES-E2E",
                    "req_id": "REQ-HOOK-STATUS",
                    "interface_ids": ["IF-NOTES"],
                    "coverage_scope": "owned",
                    "type": "E2E",
                    "file_path": E2E_TEST_FILE,
                    "first_line": "const response = await page.request.post",
                }
            ]
        ),
        test_driven_developer=_StubTDD(),
        log_cb=log_cb,
    )
    runner.app_handler = FakeAppHandler([failing_test_output()])
    return runner, logs


def _write_status_fixture(tmp_project_dir: Path, status_code: int) -> None:
    route = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(
        "const router = express.Router();\n"
        f"router.post('/notes', (req, res) => res.status({status_code}).json({{}}));\n",
        encoding="utf-8",
    )
    test = tmp_project_dir / E2E_TEST_FILE
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(
        "const response = await page.request.post('/api/notes');\n"
        f"expect(response.status()).toBe({status_code});\n",
        encoding="utf-8",
    )


def test_design_phase_blocks_conflicting_http_status_before_baseline(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-HOOK-STATUS"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Create note", "description": "Create a note."}
    )
    runner, logs = _make_status_runner(tmp_project_dir, 200)
    _write_status_fixture(tmp_project_dir, 200)

    assert asyncio.run(runner.run_design_phase(node_id, {"name": "Create note"})) is False
    diagnostics = sessions.load_node_session(node_id).get("test_contract_diagnostics") or []
    assert diagnostics[0]["code"] == "status_code_conflict"
    assert diagnostics[0]["assertion"].endswith("toBe(200)")
    assert any("HTTP status contract validation rejected" in message for _, message, _, _ in logs)
    assert runner.app_handler.calls == []


def test_design_phase_accepts_explicit_non_default_route_status(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-HOOK-STATUS"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Create note", "description": "Create a note."}
    )
    runner, _logs = _make_status_runner(tmp_project_dir, 201)
    _write_status_fixture(tmp_project_dir, 201)

    assert asyncio.run(runner.run_design_phase(node_id, {"name": "Create note"})) is True
    assert sessions.load_node_session(node_id).get("test_contract_diagnostics") == []
    assert runner.app_handler.calls == [("E2E", [E2E_TEST_FILE])]


def test_design_phase_reports_needs_info_when_status_contract_is_missing(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-HOOK-STATUS"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Create note", "description": "Create a note."}
    )
    runner, logs = _make_status_runner(
        tmp_project_dir,
        200,
        interface_specification="POST /api/notes creates a note.",
    )
    _write_status_fixture(tmp_project_dir, 200)
    route = tmp_project_dir / "backend" / "src" / "routes" / "notes.js"
    route.write_text(
        "const router = express.Router();\n"
        "router.post('/notes', (req, res) => res.json({}));\n",
        encoding="utf-8",
    )

    assert asyncio.run(runner.run_design_phase(node_id, {"name": "Create note"})) is False
    diagnostics = sessions.load_node_session(node_id).get("test_contract_diagnostics") or []
    assert diagnostics[0]["code"] == "status_code_needs_info"
    assert "needs-info" in diagnostics[0]["message"]
    assert any("not enough HTTP status" in message for _, message, _, _ in logs)
    assert runner.app_handler.calls == []


def test_import_route_requires_api_card_before_test_generation_and_reaches_tdd_after_fix(
    tmp_project_dir: Path, arc_runtime,
) -> None:
    node_id = "REQ-HOOK-STATUS"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Import workbook", "description": "Import CSV."}
    )
    route = tmp_project_dir / "backend/src/routes/workbooks.js"
    route.parent.mkdir(parents=True)
    route.write_text(
        "const router = require('express').Router();\n"
        "// POST /api/workbooks/import -> 201 {workbook}, 400 {errors}\n"
        "router.post('/import', (req, res) => {\n"
        "  res.status(501).json({code: 'NOT_IMPLEMENTED'});\n"
        "});\n", encoding="utf-8",
    )
    test_file = tmp_project_dir / E2E_TEST_FILE
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "const res = await request.post('/api/workbooks/import');\n"
        "expect(res.status).toBe(201);\n"
        "expect(res.status).toBe(400);\n", encoding="utf-8",
    )
    runner, logs = _make_status_runner(tmp_project_dir, 201)
    runner.interface_designer.payload = {
        "interfaces": [{"interface_id": "IF-IMPORT-UI", "type": "UI",
                        "file_path": "frontend/src/components/ImportCsvButton.jsx",
                        "specification": "CSV upload button."}],
        "files_written": ["backend/src/routes/workbooks.js"],
        "materialized_paths": ["/workspace/backend/src/routes/workbooks.js"],
    }
    assert asyncio.run(runner.run_interface_design_stage(node_id, {})) is False
    diagnostics = sessions.load_node_session(node_id)["interface_design_diagnostics"]
    assert diagnostics[0]["code"] == "api_contract_missing"
    assert diagnostics[0]["path"] == "/api/workbooks/import"
    assert any("before TEST_GENERATION" in message for _, message, _, _ in logs)
    assert runner.app_handler.calls == []

    runner.interface_designer.payload["interfaces"].append({
        "interface_id": "IF-IMPORT-API", "type": "API",
        "file_path": "backend/src/routes/workbooks.js",
        "specification": "POST /api/workbooks/import -> 201 {workbook}, 400 {errors}.",
    })
    runner.test_generator._manifest = [{
        "test_id": "T-IMPORT", "req_id": node_id,
        "interface_ids": ["IF-IMPORT-API", "IF-IMPORT-UI"],
        "coverage_scope": "owned", "type": "E2E",
        "file_path": E2E_TEST_FILE, "first_line": "const res = await request.post",
    }]
    assert asyncio.run(runner.run_interface_design_stage(node_id, {})) is True
    assert asyncio.run(runner.run_test_generation_stage(node_id, {})) is True
    assert sessions.load_node_session(node_id)["interface_design_diagnostics"] == []
    assert runner.app_handler.calls == [("E2E", [E2E_TEST_FILE])]


def test_design_route_gate_ignores_template_health_from_git_baseline(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-HOOK-STATUS"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "List workbooks", "description": "List workbooks."}
    )

    app_path = tmp_project_dir / "backend/src/app.js"
    app_path.parent.mkdir(parents=True, exist_ok=True)
    baseline = (
        "const app = express();\n"
        "app.get('/api/health', (req, res) => {\n"
        "  res.json({ code: 200, message: 'Backend Ready' });\n"
        "});\n"
    )
    app_path.write_text(baseline, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_project_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "backend/src/app.js"], cwd=tmp_project_dir, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "template baseline",
        ],
        cwd=tmp_project_dir,
        check=True,
        capture_output=True,
    )
    app_path.write_text(
        baseline
        + "app.get('/api/workbooks', (req, res) => {\n"
        + "  res.json({ workbooks: [] });\n"
        + "});\n",
        encoding="utf-8",
    )

    runner, logs = _make_status_runner(tmp_project_dir, 200)
    runner.interface_designer.payload = {
        "summary": "List workbooks API.",
        "interfaces": [
            {
                "interface_id": "IF-WORKBOOKS",
                "type": "API",
                "name": "workbooks-route",
                "responsibility": "Lists workbooks.",
                "specification": "GET /api/workbooks -> 200 { workbooks }.",
                "file_path": "backend/src/app.js",
                "first_line": "app.get('/api/workbooks'",
            }
        ],
        "files_written": ["backend/src/app.js"],
        "materialized_paths": ["/workspace/backend/src/app.js"],
    }

    assert asyncio.run(runner.run_interface_design_stage(node_id, {})) is True
    assert not any("GET /api/health" in message for _, message, _, _ in logs)


def test_dependency_status_check_resolves_named_foreign_api_card(tmp_project_dir: Path, arc_runtime) -> None:
    arc_runtime.traceability.upsert_interface(
        interface_id="IF-WORKBOOKS", req_ids=["REQ-PREREQ"], type="API",
        file_path="backend/src/routes/workbooks.js",
        content='{"specification": "GET /api/workbooks returns 200 {workbooks}."}',
    )
    test = tmp_project_dir / E2E_TEST_FILE
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(
        "const res = await request.get('/api/workbooks');\n"
        "expect(res.status).toBe(200);\n", encoding="utf-8",
    )
    runner, _ = _make_status_runner(tmp_project_dir, 201)
    sessions.merge_node_session("REQ-HOOK-STATUS", {"interfaces": []})
    assert asyncio.run(runner._validate_http_status_contracts(
        node_id="REQ-HOOK-STATUS", requirement_data={},
        tests=[{"type": "E2E", "file_path": E2E_TEST_FILE,
                "interface_ids": ["IF-WORKBOOKS"], "coverage_scope": "dependency"}],
    )) is True

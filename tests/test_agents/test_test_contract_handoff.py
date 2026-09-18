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

"""DESIGN must hard-fail when the pass materialized files but recorded no
interface contracts.

The 2026-09-14 arc-bench run deadlocked TestGenerator on an empty interface
registry: InterfaceDesigner serialized the design into ``summary`` prose and
returned ``"interfaces": []`` while having written real skeleton files. The
registry stayed empty for the whole run. This gate turns that silent passage
into an explicit DESIGN failure while keeping the legitimate "nothing to
record" case on the historical warning-only path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.phases import WorkflowPhaseRunner

# Reuse the process-wide runtime fixture so WorkflowPhaseRunner.traceability,
# core.sessions and context_pipeline all resolve inside tmp_project_dir.
from tests.test_agents.conftest import arc_runtime  # noqa: F401


class _StubDesigner:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def run(self, node_id: str, requirement_data: dict) -> dict:
        return self.payload


class _StubGenerator:
    def __init__(self) -> None:
        self.app_handler = None

    async def run(self, node_id: str, requirement_data: dict) -> tuple:
        return (None, "")


class _StubTDD:
    def __init__(self) -> None:
        self.app_handler = None


def _make_runner(tmp_project_dir: Path, payload: dict):
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    requirements_dir = tmp_project_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=_StubDesigner(payload),
        test_generator=_StubGenerator(),
        test_driven_developer=_StubTDD(),
        log_cb=log_cb,
    )
    return runner, logs


def _seed_leaf_requirement(runtime, node_id: str) -> None:
    runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )


def test_design_phase_fails_when_files_materialized_but_interfaces_empty(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-GATE-1"
    _seed_leaf_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Contracts fully specified below in prose.",
            "interfaces": [],
            "files_written": [],
            "materialized_paths": ["/workspace/frontend/src/features/auth/authApi.ts"],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Calculator", "description": "Add two numbers"})
    )

    assert ok is False
    errors = [entry for entry in logs if entry[2] == "error"]
    assert any("DESIGN failed" in entry[1] for entry in errors)


def test_design_phase_warns_but_does_not_gate_when_nothing_materialized(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-GATE-2"
    _seed_leaf_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Nothing to record.",
            "interfaces": [],
            "files_written": [],
            "materialized_paths": [],
        },
    )

    asyncio.run(
        runner.run_design_phase(node_id, {"name": "Calculator", "description": "Add two numbers"})
    )

    warnings = [entry for entry in logs if entry[2] == "warning"]
    assert any("no current-node owned interface definitions" in entry[1] for entry in warnings)
    assert not any("DESIGN failed" in entry[1] for entry in logs)

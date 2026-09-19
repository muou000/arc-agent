"""DESIGN must hard-fail when a pass produces no usable interface contracts.

Two gates cover the empty-contract shapes, both observed live on arc-bench:

- 2026-09-14: the pass materialized skeleton files but serialized the design
  into ``summary`` prose and returned ``"interfaces": []``. The registry
  stayed empty for the whole run and TestGenerator deadlocked. The
  materialized-files gate turns that silent passage into an explicit DESIGN
  failure (leaf and non-leaf alike).
- 2026-09-19 (submission 77bdef8ce610): a leaf pass claimed it reused the
  parent-designed UI shell and returned an empty ``interfaces`` array with no
  files at all. That used to pass with only a warning; the contradiction
  surfaced one stage later as a confusing TestGenerator ownership failure
  after the whole tree had waited on the node. The leaf gate fails DESIGN
  immediately; the historical warning-only path now only covers a non-leaf
  node that legitimately records nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import sessions
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
        self.run_calls: list[str] = []

    async def run(self, node_id: str, requirement_data: dict) -> tuple:
        self.run_calls.append(node_id)
        return (None, "")


class _StubTDD:
    def __init__(self) -> None:
        self.app_handler = None


def _make_runner(tmp_project_dir: Path, payload: dict, test_generator: _StubGenerator | None = None):
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
        test_generator=test_generator or _StubGenerator(),
        test_driven_developer=_StubTDD(),
        log_cb=log_cb,
    )
    return runner, logs


def _seed_leaf_requirement(runtime, node_id: str) -> None:
    runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )


def _seed_non_leaf_requirement(runtime, node_id: str) -> None:
    """A non-leaf WITH visual references, so DESIGN runs instead of skipping."""
    runtime.traceability.store_requirement_tree(
        {
            "id": node_id,
            "name": "Home Shell",
            "description": "Composition node owning the visual shell",
            "visual_reference": ["screens/home.png"],
            "children": [
                {"id": f"{node_id}-CHILD", "name": "Child feature", "description": "Leaf"}
            ],
        }
    )


# ---------------------------------------------------------------------------
# Materialized-files gate: skeleton files written, no contracts recorded.
# ---------------------------------------------------------------------------


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


def test_design_phase_materialized_gate_also_covers_non_leaf(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-GATE-1B"
    _seed_non_leaf_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Shell described in prose.",
            "interfaces": [],
            "files_written": [],
            "materialized_paths": ["/workspace/frontend/src/pages/Home.tsx"],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(
            node_id,
            # children_ids must ride on the passed-in requirement data: the
            # leaf/non-leaf split reads it before the traceability re-read,
            # the same way core.workflow._run_task feeds this method.
            {"name": "Home Shell", "description": "Shell", "children_ids": ["REQ-GATE-1B-CHILD"]},
        )
    )

    assert ok is False
    errors = [entry for entry in logs if entry[2] == "error"]
    assert any("skeleton file(s) were materialized" in entry[1] for entry in errors)


# ---------------------------------------------------------------------------
# Leaf gate: no contracts and no files at all (the reuse-in-prose shortcut).
# ---------------------------------------------------------------------------


def test_design_phase_fails_fast_for_leaf_with_no_interfaces_and_no_files(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-GATE-3"
    _seed_leaf_requirement(arc_runtime, node_id)
    generator = _StubGenerator()
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": (
                "REQ-1.1 is a leaf; the parent-designed UI shell is reused with its "
                "stable interface ids, this pass records the owned contracts against "
                "the existing files."
            ),
            "interfaces": [],
            "files_written": [],
            "materialized_paths": [],
        },
        test_generator=generator,
    )

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Calculator", "description": "Add two numbers"})
    )

    assert ok is False
    errors = [entry for entry in logs if entry[2] == "error"]
    assert any("DESIGN failed" in entry[1] for entry in errors)
    assert any("original interface_id" in entry[1] for entry in errors)
    # Fail fast: the contradiction must not surface one stage later through
    # TestGenerator's ownership gate.
    assert generator.run_calls == []
    assert (
        sessions.load_node_session(node_id).get("phase_status", {}).get("design")
        != "completed"
    )


def test_design_phase_fails_fast_for_leaf_claiming_files_it_never_wrote(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-GATE-4"
    _seed_leaf_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Contracts recorded in the files.",
            "interfaces": [],
            "files_written": ["frontend/src/pages/RegisterPage.tsx"],
            "materialized_paths": [],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Calculator", "description": "Add two numbers"})
    )

    assert ok is False
    errors = [entry for entry in logs if entry[2] == "error"]
    assert any("DESIGN failed" in entry[1] for entry in errors)


# ---------------------------------------------------------------------------
# Non-leaf regression: "nothing to record" stays on the warning-only path.
# ---------------------------------------------------------------------------


def test_design_phase_warns_but_does_not_gate_when_non_leaf_records_nothing(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-GATE-2"
    _seed_non_leaf_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Nothing to record.",
            "interfaces": [],
            "files_written": [],
            "materialized_paths": [],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(
            node_id,
            {"name": "Home Shell", "description": "Shell", "children_ids": ["REQ-GATE-2-CHILD"]},
        )
    )

    assert ok is True
    warnings = [entry for entry in logs if entry[2] == "warning"]
    assert any("no current-node owned interface definitions" in entry[1] for entry in warnings)
    assert not any("DESIGN failed" in entry[1] for entry in logs)

"""Independent InterfaceDesigner, TestGenerator, and TDD stage contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core import sessions
from core.phases import WorkflowPhaseRunner
from core.queue_state import (
    PHASE_DESIGN,
    STAGE_IMPLEMENTATION,
    STAGE_INTERFACE_DESIGN,
    STAGE_PUBLISHED,
    STAGE_TEST_GENERATION,
    STAGE_VISUAL_ANALYSIS,
    TASK_RUNNING,
    load_or_create_queue,
    stage_status_of,
    task_status,
)
from core.workflow import ARCWorkflowManager
from tests.helpers.faux import FakeAppHandler, failing_test_output
from tests.helpers.jsonl import read_jsonl
from tests.test_agents.conftest import arc_runtime  # noqa: F401


NODE_ID = "REQ-STAGE-SPLIT"
REQ_DATA = {"name": "Calculator", "description": "Add two numbers"}
TEST_FILE = "tests/unit/test_calc.py"


class _Designer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, node_id: str, requirement_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(node_id)
        return {
            "summary": "Calculator contract.",
            "interfaces": [
                {
                    "interface_id": "IF-CALC",
                    "type": "FUNC",
                    "name": "add",
                    "responsibility": "Add two integers.",
                    "file_path": "src/calc.py",
                    "first_line": "def add(a, b):",
                }
            ],
            "files_written": [],
        }


class _Generator:
    def __init__(self) -> None:
        self.app_handler = None
        self.calls: list[str] = []

    async def run(
        self, node_id: str, requirement_data: dict[str, Any], **kwargs: Any
    ) -> tuple[list[dict[str, Any]], str]:
        self.calls.append(node_id)
        return (
            [
                {
                    "test_id": "T-CALC",
                    "req_id": node_id,
                    "interface_ids": ["IF-CALC"],
                    "coverage_scope": "owned",
                    "type": "Unit",
                    "file_path": TEST_FILE,
                    "first_line": "def test_add():",
                }
            ],
            "generated calculator test",
        )


class _TDD:
    def __init__(self) -> None:
        self.app_handler = None
        self.calls: list[str] = []


def _make_runner(tmp_project_dir: Path) -> tuple[WorkflowPhaseRunner, _Designer, _Generator, FakeAppHandler]:
    requirements_dir = tmp_project_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    designer = _Designer()
    generator = _Generator()
    fake_handler = FakeAppHandler([failing_test_output()])
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=designer,
        test_generator=generator,
        test_driven_developer=_TDD(),
        log_cb=lambda *_args, **_kwargs: None,
    )
    runner.app_handler = fake_handler
    return runner, designer, generator, fake_handler


def test_interface_and_test_generation_stages_publish_independently(
    tmp_project_dir: Path, arc_runtime
) -> None:
    arc_runtime.traceability.store_requirement_tree({"id": NODE_ID, **REQ_DATA, "children": []})
    test_path = tmp_project_dir / TEST_FILE
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("def test_add():\n    assert False\n", encoding="utf-8")
    runner, designer, generator, fake_handler = _make_runner(tmp_project_dir)

    assert asyncio.run(runner.run_interface_design_stage(NODE_ID, dict(REQ_DATA))) is True
    assert designer.calls == [NODE_ID]
    assert generator.calls == []
    assert fake_handler.calls == []
    assert [row["interface_id"] for row in arc_runtime.traceability.list_interfaces(req_id=NODE_ID)] == [
        "IF-CALC"
    ]
    assert arc_runtime.traceability.list_tests(req_id=NODE_ID) == []
    assert sessions.load_node_session(NODE_ID)["phase_status"] == {
        "design": "prepared",
        "test": "pending",
        "implement": "pending",
    }

    assert asyncio.run(runner.run_test_generation_stage(NODE_ID, dict(REQ_DATA))) is True
    assert generator.calls == [NODE_ID]
    assert fake_handler.calls == [("Unit", [TEST_FILE])]
    assert [row["test_id"] for row in arc_runtime.traceability.list_tests(req_id=NODE_ID)] == [
        "T-CALC"
    ]
    session = sessions.load_node_session(NODE_ID)
    assert session["phase_status"]["test"] == "completed"
    assert session["phase_status"]["design"] == "completed"
    assert session["design_baseline"] == {TEST_FILE: "red"}
    # The test publication must not re-write the contracts its interface
    # publication already stored: exactly one upsert row event per fact.
    events = read_jsonl(Path(arc_runtime.paths.runner_events_path))
    assert [
        event["interface_id"] for event in events if event.get("type") == "interface_upsert"
    ] == ["IF-CALC"]
    assert [event["test_id"] for event in events if event.get("type") == "test_upsert"] == ["T-CALC"]


def test_stage_drain_runs_test_generation_before_tdd_and_skips_non_leaf_tests(
    tmp_project_dir: Path, runtime, monkeypatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    tree = {
        "id": "ROOT",
        "name": "Root",
        "description": "Root",
        "children": [{"id": "LEAF", "name": "Leaf", "description": "Leaf", "children": []}],
    }
    runtime.traceability.store_requirement_tree(tree)
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_project_dir),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime
    manager._save_processing_queue = lambda _queue: None
    # The workspace is not a Git repository; the aggregate-completion
    # checkpoint is the shared-workspace rail's business, not the stage
    # boundary under test here.
    manager._commit_phase_checkpoint = lambda *_args, **_kwargs: asyncio.sleep(0)
    calls: list[tuple[str, str]] = []
    queue = load_or_create_queue(
        str(tmp_project_dir / ".arc" / "processing_queue.json"),
        tree,
    )
    for task in queue["stage_tasks"]:
        if task["stage"] == STAGE_VISUAL_ANALYSIS:
            task["status"] = STAGE_PUBLISHED
    design_task_status: list[str] = []

    class _Runner:
        async def run_interface_design_stage(
            self, node_id: str, _requirement: dict[str, Any]
        ) -> bool:
            calls.append((node_id, "interface"))
            return True

        async def run_test_generation_stage(
            self, node_id: str, _requirement: dict[str, Any]
        ) -> bool:
            calls.append((node_id, "tests"))
            design_task_status.append(
                task_status(
                    queue,
                    next(
                        item
                        for item in queue["tasks"]
                        if item["node_id"] == node_id and item["phase"] == PHASE_DESIGN
                    ),
                )
            )
            return True

        async def run_implement_phase(
            self, node_id: str, _requirement: dict[str, Any]
        ) -> bool:
            calls.append((node_id, "tdd"))
            return True

    manager.phase_runner = _Runner()

    asyncio.run(
        manager._drain_stage_tasks(
            queue,
            lambda stage_task: manager._execute_stage_task(stage_task, queue),
        )
    )

    assert calls == [
        ("ROOT", "interface"),
        ("LEAF", "interface"),
        ("LEAF", "tests"),
        ("LEAF", "tdd"),
        ("ROOT", "tdd"),
    ]
    # The interface stage's begin already carries the node across the test
    # publication: the aggregate DESIGN task reads RUNNING (not PENDING)
    # while TEST_GENERATION executes.
    assert design_task_status == [TASK_RUNNING]
    assert stage_status_of(queue, "ROOT", STAGE_TEST_GENERATION) == "SKIPPED"
    assert stage_status_of(queue, "LEAF", STAGE_INTERFACE_DESIGN) == STAGE_PUBLISHED
    assert stage_status_of(queue, "LEAF", STAGE_TEST_GENERATION) == STAGE_PUBLISHED
    assert stage_status_of(queue, "LEAF", STAGE_IMPLEMENTATION) == STAGE_PUBLISHED
    assert queue["node_states"] == {"ROOT": "PASSED", "LEAF": "PASSED"}

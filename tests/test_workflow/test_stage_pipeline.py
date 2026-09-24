"""Stage-pipeline readiness, capacity, backpressure, and overlap rules."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.queue_state import (
    STAGE_INTERFACE_DESIGN,
    STAGE_IMPLEMENTATION,
    STAGE_PENDING,
    STAGE_BLOCKED,
    STAGE_FAILED,
    STAGE_PUBLISHED,
    STAGE_READY_TO_MERGE,
    STAGE_TEST_GENERATION,
    STAGE_VISUAL_ANALYSIS,
    recover_interrupted,
)
from core.scheduling import (
    next_runnable_stage_task,
    stage_overlap_allowed,
    stage_task_dependencies_met,
    stage_write_sets_disjoint,
)
from core.workflow import ARCWorkflowManager


def _stage(
    node_id: str,
    stage: str,
    order: int,
    status: str = STAGE_PENDING,
    *,
    writes: list[str] | None = None,
    node_order: int | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "stage_task_id": f"{node_id}:{stage}",
        "node_id": node_id,
        "stage": stage,
        "order": order,
        "status": status,
        "applicable": True,
        "attempt_count": 0,
        "retry_at": None,
        "publication": None,
        "error": None,
    }
    if writes is not None:
        task["declared_write_set"] = writes
    if node_order is not None:
        task["node_order"] = node_order
    return task


def _queue(*stage_tasks: dict[str, Any], dependencies: dict[str, list[str]] | None = None) -> dict[str, Any]:
    node_ids = list(dict.fromkeys(str(task["node_id"]) for task in stage_tasks))
    return {
        "tasks": [],
        "stage_tasks": list(stage_tasks),
        "node_states": {node_id: "UNSEEN" for node_id in node_ids},
        "node_design_done": {node_id: False for node_id in node_ids},
        "parents": {},
        "descendants": {},
        "dependencies": dependencies or {},
    }


def _set_stage(queue: dict[str, Any], node_id: str, stage: str, status: str) -> None:
    task = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == f"{node_id}:{stage}")
    task["status"] = status


def test_stage_design_keeps_parent_and_declared_dependency_gates() -> None:
    queue = _queue(
        _stage("P", STAGE_VISUAL_ANALYSIS, 0, STAGE_PUBLISHED),
        _stage("P", STAGE_INTERFACE_DESIGN, 1, STAGE_PUBLISHED),
        _stage("P", STAGE_TEST_GENERATION, 2, STAGE_PUBLISHED),
        _stage("P", STAGE_IMPLEMENTATION, 3, STAGE_PUBLISHED),
        _stage("C", STAGE_VISUAL_ANALYSIS, 4, STAGE_PUBLISHED),
        _stage("C", STAGE_INTERFACE_DESIGN, 5),
        _stage("D", STAGE_VISUAL_ANALYSIS, 6, STAGE_PUBLISHED),
        _stage("D", STAGE_INTERFACE_DESIGN, 7, STAGE_PUBLISHED),
        _stage("D", STAGE_TEST_GENERATION, 8, STAGE_PUBLISHED),
        _stage("D", STAGE_IMPLEMENTATION, 9),
        dependencies={"C": ["D"]},
    )
    queue["parents"] = {"C": "P"}

    child_design = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "C:INTERFACE_DESIGN")

    assert not stage_task_dependencies_met(queue, child_design), (
        "the child must wait for the declared dependency's IMPLEMENT even "
        "when the parent DESIGN is already complete"
    )

    _set_stage(queue, "D", STAGE_IMPLEMENTATION, STAGE_PUBLISHED)
    assert stage_task_dependencies_met(queue, child_design)


def test_failed_parent_stage_releases_child_design() -> None:
    queue = _queue(
        _stage("P", STAGE_VISUAL_ANALYSIS, 0, STAGE_FAILED),
        _stage("P", STAGE_INTERFACE_DESIGN, 1, STAGE_BLOCKED),
        _stage("P", STAGE_TEST_GENERATION, 2, STAGE_BLOCKED),
        _stage("P", STAGE_IMPLEMENTATION, 3, STAGE_BLOCKED),
        _stage("C", STAGE_VISUAL_ANALYSIS, 4, STAGE_PUBLISHED),
        _stage("C", STAGE_INTERFACE_DESIGN, 5),
    )
    queue["parents"] = {"C": "P"}
    queue["node_states"]["P"] = "FAILED"

    child_design = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == "C:INTERFACE_DESIGN")

    assert stage_task_dependencies_met(queue, child_design)


def test_stage_implementation_keeps_descendant_gate() -> None:
    queue = _queue(
        _stage("P", STAGE_VISUAL_ANALYSIS, 0, STAGE_PUBLISHED),
        _stage("P", STAGE_INTERFACE_DESIGN, 1, STAGE_PUBLISHED),
        _stage("P", STAGE_TEST_GENERATION, 2, STAGE_PUBLISHED),
        _stage("P", STAGE_IMPLEMENTATION, 3),
        _stage("C", STAGE_VISUAL_ANALYSIS, 4, STAGE_PUBLISHED),
        _stage("C", STAGE_INTERFACE_DESIGN, 5, STAGE_PUBLISHED),
        _stage("C", STAGE_TEST_GENERATION, 6, STAGE_PUBLISHED),
        _stage("C", STAGE_IMPLEMENTATION, 7),
    )
    queue["descendants"] = {"P": ["C"]}

    parent_implementation = next(
        item for item in queue["stage_tasks"] if item["stage_task_id"] == "P:IMPLEMENTATION"
    )
    assert not stage_task_dependencies_met(queue, parent_implementation)

    _set_stage(queue, "C", STAGE_IMPLEMENTATION, STAGE_PUBLISHED)
    assert stage_task_dependencies_met(queue, parent_implementation)


def test_only_approved_disjoint_stage_windows_can_overlap() -> None:
    test_generation = _stage("N", STAGE_TEST_GENERATION, 0, writes=["tests/n.test.ts"])
    interface_design = _stage("N+1", STAGE_INTERFACE_DESIGN, 1, writes=["src/n1.ts"])
    implementation = _stage("N", STAGE_IMPLEMENTATION, 2, writes=["src/n.ts"])
    next_test_generation = _stage(
        "N+1", STAGE_TEST_GENERATION, 3, writes=["tests/n1.test.ts"]
    )
    other_stage = _stage("N+1", STAGE_IMPLEMENTATION, 4, writes=["src/n1.ts"])
    conflicting_design = _stage("N+1", STAGE_INTERFACE_DESIGN, 5, writes=["tests/n.test.ts"])

    assert stage_write_sets_disjoint(test_generation, interface_design)
    assert stage_overlap_allowed(test_generation, interface_design)
    assert stage_overlap_allowed(implementation, next_test_generation)
    assert not stage_overlap_allowed(interface_design, other_stage)
    assert not stage_overlap_allowed(test_generation, conflicting_design)


def test_missing_write_set_fails_closed_before_overlap() -> None:
    test_generation = _stage("N", STAGE_TEST_GENERATION, 0, writes=["tests/n.test.ts"])
    interface_design = _stage("N+1", STAGE_INTERFACE_DESIGN, 1)

    assert not stage_write_sets_disjoint(test_generation, interface_design)
    assert not stage_overlap_allowed(test_generation, interface_design)


def test_published_write_set_is_used_when_the_task_field_is_not_registered_yet() -> None:
    left = _stage("N", STAGE_TEST_GENERATION, 0, writes=[])
    right = _stage("N+1", STAGE_INTERFACE_DESIGN, 1, writes=["src/n1.ts"])
    left["declared_write_set"] = None
    left["publication"] = {"declared_write_set": ["tests/n.test.ts"]}

    assert stage_write_sets_disjoint(left, right)


def test_stage_selector_skips_conflicting_earlier_work_and_keeps_later_work_fair() -> None:
    active_design = _stage(
        "A", STAGE_INTERFACE_DESIGN, 1, STAGE_READY_TO_MERGE,
        writes=["src/shared.ts"], node_order=2,
    )
    blocked_implementation = _stage(
        "B", STAGE_IMPLEMENTATION, 2, writes=["src/shared.ts"], node_order=1
    )
    ready_test_generation = _stage(
        "C", STAGE_TEST_GENERATION, 3, writes=["tests/c.test.ts"], node_order=1
    )
    queue = _queue(
            _stage("B", STAGE_VISUAL_ANALYSIS, 0, STAGE_PUBLISHED, node_order=1),
            _stage("B", STAGE_INTERFACE_DESIGN, 4, STAGE_PUBLISHED, node_order=1),
            _stage("B", STAGE_TEST_GENERATION, 5, STAGE_PUBLISHED, node_order=1),
        blocked_implementation,
            _stage("C", STAGE_VISUAL_ANALYSIS, 6, STAGE_PUBLISHED, node_order=1),
            _stage("C", STAGE_INTERFACE_DESIGN, 7, STAGE_PUBLISHED, node_order=1),
        ready_test_generation,
    )

    pick = next_runnable_stage_task(
        queue,
        [active_design],
        max_in_flight=2,
        stage_capacities={STAGE_IMPLEMENTATION: 1, STAGE_TEST_GENERATION: 1},
    )

    assert pick is ready_test_generation


def test_stage_selector_rejects_non_adjacent_overlap_even_when_writes_are_disjoint() -> None:
    active_design = _stage(
        "A", STAGE_INTERFACE_DESIGN, 1, STAGE_READY_TO_MERGE,
        writes=["src/a.ts"], node_order=0,
    )
    candidate = _stage(
        "C", STAGE_TEST_GENERATION, 2, writes=["tests/c.test.ts"], node_order=2,
    )
    queue = _queue(
        _stage("C", STAGE_VISUAL_ANALYSIS, 3, STAGE_PUBLISHED, node_order=2),
        _stage("C", STAGE_INTERFACE_DESIGN, 4, STAGE_PUBLISHED, node_order=2),
        candidate,
    )

    assert next_runnable_stage_task(queue, [active_design], max_in_flight=2) is None


def test_stage_backpressure_stops_new_work_when_publications_are_queued() -> None:
    ready_to_merge = _stage(
        "A", STAGE_INTERFACE_DESIGN, 0, STAGE_READY_TO_MERGE,
        writes=["src/a.ts"], node_order=1,
    )
    candidate = _stage("B", STAGE_TEST_GENERATION, 1, writes=["tests/b.test.ts"], node_order=0)
    queue = _queue(
        ready_to_merge,
        _stage("B", STAGE_VISUAL_ANALYSIS, 2, STAGE_PUBLISHED, node_order=0),
        _stage("B", STAGE_INTERFACE_DESIGN, 3, STAGE_PUBLISHED, node_order=0),
        candidate,
    )

    assert next_runnable_stage_task(queue, [], max_in_flight=2, max_ready_to_merge=1) is None
    assert next_runnable_stage_task(queue, [], max_in_flight=2, max_ready_to_merge=2) is candidate


def test_resume_requeues_orphaned_running_stage_tasks() -> None:
    running = _stage("A", STAGE_INTERFACE_DESIGN, 0, "RUNNING")
    queue = _queue(running)

    assert recover_interrupted(queue, git_status_lines=[]) == []
    assert running["status"] == STAGE_PENDING
    assert queue["recovered_interrupted_stage_tasks"] == ["A:INTERFACE_DESIGN"]


def test_stage_drain_uses_bounded_slots_and_the_approved_overlap_window(
    tmp_project_dir: Path, runtime, monkeypatch
) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_project_dir),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime
    manager._save_processing_queue = lambda _queue: None
    queue = _queue(
        _stage("A", STAGE_VISUAL_ANALYSIS, 0, STAGE_PUBLISHED, node_order=0),
        _stage("A", STAGE_INTERFACE_DESIGN, 1, STAGE_PUBLISHED, node_order=0),
        _stage("A", STAGE_TEST_GENERATION, 2, writes=["tests/a.test.ts"], node_order=0),
        _stage("B", STAGE_VISUAL_ANALYSIS, 3, STAGE_PUBLISHED, node_order=1),
        _stage("B", STAGE_INTERFACE_DESIGN, 4, writes=["src/b.ts"], node_order=1),
        _stage("B", STAGE_TEST_GENERATION, 5, node_order=1),
    )
    active: set[str] = set()
    peak = 0

    async def execute(stage_task: dict[str, Any]) -> dict[str, Any]:
        nonlocal peak
        active.add(stage_task["stage_task_id"])
        peak = max(peak, len(active))
        await asyncio.sleep(0)
        active.remove(stage_task["stage_task_id"])
        return {"status": STAGE_PUBLISHED}

    asyncio.run(manager._drain_stage_tasks(queue, execute))

    assert peak == 2
    assert all(task["status"] == STAGE_PUBLISHED for task in queue["stage_tasks"])


def test_serial_stage_pipeline_compile_uses_the_stage_drain(
    tmp_project_dir: Path, runtime, monkeypatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.delenv("ARC_NODE_WORKTREES", raising=False)
    tree = {"id": "A", "name": "A", "description": "A", "children": []}
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_project_dir),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *_args, **_kwargs: None,
    )
    manager.runtime = runtime

    class _Runner:
        async def run_design_phase(self, _node_id: str, _requirement: dict[str, Any]) -> bool:
            return True

        async def run_implement_phase(self, _node_id: str, _requirement: dict[str, Any]) -> bool:
            return True

    manager.phase_runner = _Runner()
    manager._commit_phase_checkpoint = lambda *_args, **_kwargs: asyncio.sleep(0)
    manager._reconcile_call_edges = lambda: asyncio.sleep(0)
    calls: list[str] = []
    original_drain = manager._drain_stage_tasks

    async def recording_drain(queue_state: dict[str, Any], execute_stage_task: Any) -> None:
        calls.append("stage")
        await original_drain(queue_state, execute_stage_task)

    manager._drain_stage_tasks = recording_drain

    result = asyncio.run(manager.compile_requirement_tree(tree))

    assert calls == ["stage"]
    assert result["ok"] is True

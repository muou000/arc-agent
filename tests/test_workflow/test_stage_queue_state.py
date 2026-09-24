"""Stage-task persistence and aggregate DESIGN/IMPLEMENT projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.config as core_config
from core import sessions
from core.queue_state import (
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    STAGE_BLOCKED,
    STAGE_FAILED,
    STAGE_INTERFACE_DESIGN,
    STAGE_IMPLEMENTATION,
    STAGE_PENDING,
    STAGE_PUBLISHED,
    STAGE_READY,
    STAGE_READY_TO_MERGE,
    STAGE_RUNNING,
    STAGE_SKIPPED,
    STAGE_TEST_GENERATION,
    STAGE_VISUAL_ANALYSIS,
    TASK_COMPLETED,
    TASK_FAILED,
    aggregate_phase_status,
    begin_task,
    build_processing_tasks,
    build_stage_tasks,
    complete_task,
    fail_stage_task,
    fail_task,
    load_or_create_queue,
    recover_interrupted,
    reset_node_for_retry,
    save_queue,
    stage_status_of,
    transition_stage_task,
)


def _tree() -> dict[str, object]:
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [
            {
                "id": "L",
                "name": "leaf",
                "description": "leaf",
                "children": [],
            }
        ],
    }


def _queue(tmp_path: Path) -> dict[str, object]:
    return load_or_create_queue(
        str(tmp_path / ".arc" / "processing_queue.json"),
        _tree(),
    )


def test_build_stage_tasks_preserves_node_order_and_skips_non_leaf_tests() -> None:
    tasks = build_stage_tasks(_tree())

    assert [task["stage_task_id"] for task in tasks] == [
        "R:VISUAL_ANALYSIS",
        "R:INTERFACE_DESIGN",
        "R:TEST_GENERATION",
        "R:IMPLEMENTATION",
        "L:VISUAL_ANALYSIS",
        "L:INTERFACE_DESIGN",
        "L:TEST_GENERATION",
        "L:IMPLEMENTATION",
    ]
    assert tasks[2]["status"] == STAGE_SKIPPED
    assert tasks[6]["status"] == STAGE_PENDING


def test_fresh_queue_persists_stage_schema_without_changing_aggregate_tasks(tmp_path: Path) -> None:
    queue = _queue(tmp_path)

    assert queue["stage_task_schema_version"] == 1
    assert len(queue["stage_tasks"]) == 8
    assert len(queue["tasks"]) == len(build_processing_tasks(_tree()))
    assert aggregate_phase_status(queue, "R", PHASE_DESIGN) == STAGE_PENDING
    assert aggregate_phase_status(queue, "R", PHASE_IMPLEMENT) == STAGE_PENDING


def test_stage_publication_projects_to_completed_design_and_implement(tmp_path: Path) -> None:
    queue = _queue(tmp_path)

    for stage in (STAGE_VISUAL_ANALYSIS, STAGE_INTERFACE_DESIGN):
        transition_stage_task(queue, "L", stage, STAGE_READY)
        transition_stage_task(queue, "L", stage, STAGE_READY_TO_MERGE)
        transition_stage_task(queue, "L", stage, STAGE_PUBLISHED)
    transition_stage_task(queue, "L", STAGE_TEST_GENERATION, STAGE_RUNNING)
    transition_stage_task(queue, "L", STAGE_TEST_GENERATION, STAGE_READY_TO_MERGE)
    transition_stage_task(
        queue,
        "L",
        STAGE_TEST_GENERATION,
        STAGE_PUBLISHED,
        publication={"artifact_commit": "abc123"},
    )

    assert aggregate_phase_status(queue, "L", PHASE_DESIGN) == TASK_COMPLETED
    assert stage_status_of(queue, "L", STAGE_TEST_GENERATION) == STAGE_PUBLISHED
    assert queue["stage_tasks"][6]["attempt_count"] == 1

    transition_stage_task(queue, "L", STAGE_IMPLEMENTATION, STAGE_RUNNING)
    transition_stage_task(queue, "L", STAGE_IMPLEMENTATION, STAGE_READY_TO_MERGE)
    transition_stage_task(queue, "L", STAGE_IMPLEMENTATION, STAGE_PUBLISHED)

    assert aggregate_phase_status(queue, "L", PHASE_IMPLEMENT) == TASK_COMPLETED


def test_published_stage_cannot_reenter_running_without_explicit_retry(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    transition_stage_task(queue, "L", STAGE_INTERFACE_DESIGN, STAGE_READY)
    transition_stage_task(queue, "L", STAGE_INTERFACE_DESIGN, STAGE_PUBLISHED)

    with pytest.raises(ValueError, match="Invalid stage transition"):
        transition_stage_task(queue, "L", STAGE_INTERFACE_DESIGN, STAGE_RUNNING)


def test_repeating_running_transition_does_not_consume_an_attempt(tmp_path: Path) -> None:
    queue = _queue(tmp_path)

    transition_stage_task(queue, "L", STAGE_INTERFACE_DESIGN, STAGE_RUNNING)
    transition_stage_task(queue, "L", STAGE_INTERFACE_DESIGN, STAGE_RUNNING)

    task = next(
        item for item in queue["stage_tasks"] if item["stage_task_id"] == "L:INTERFACE_DESIGN"
    )
    assert task["attempt_count"] == 1


def test_failing_visual_stage_blocks_only_that_nodes_downstream_stages(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    changes: list[tuple[str, str]] = []

    fail_stage_task(
        queue,
        "L",
        STAGE_VISUAL_ANALYSIS,
        error="invalid image",
        on_state_change=lambda node_id, state: changes.append((node_id, state)),
    )

    assert stage_status_of(queue, "L", STAGE_VISUAL_ANALYSIS) == STAGE_FAILED
    assert stage_status_of(queue, "L", STAGE_INTERFACE_DESIGN) == STAGE_BLOCKED
    assert stage_status_of(queue, "L", STAGE_TEST_GENERATION) == STAGE_BLOCKED
    assert stage_status_of(queue, "L", STAGE_IMPLEMENTATION) == STAGE_BLOCKED
    assert queue["node_states"]["L"] == "FAILED"
    assert changes == [("L", "FAILED")]


def test_legacy_design_transitions_keep_stage_and_aggregate_views_coherent(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    design_task = next(task for task in queue["tasks"] if task["task_id"] == "L:DESIGN")

    begin_task(queue, design_task)

    assert stage_status_of(queue, "L", STAGE_INTERFACE_DESIGN) == STAGE_RUNNING
    assert aggregate_phase_status(queue, "L", PHASE_DESIGN) == STAGE_RUNNING

    complete_task(queue, "L", PHASE_DESIGN)

    assert stage_status_of(queue, "L", STAGE_VISUAL_ANALYSIS) == STAGE_PUBLISHED
    assert stage_status_of(queue, "L", STAGE_INTERFACE_DESIGN) == STAGE_PUBLISHED
    assert stage_status_of(queue, "L", STAGE_TEST_GENERATION) == STAGE_PUBLISHED
    assert aggregate_phase_status(queue, "L", PHASE_DESIGN) == TASK_COMPLETED


def test_legacy_failure_and_interruption_update_stage_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_config, "_workspace_root", tmp_path.resolve())
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(tmp_path))
    queue = _queue(tmp_path)
    design_task = next(task for task in queue["tasks"] if task["task_id"] == "L:DESIGN")

    begin_task(queue, design_task)
    fail_task(queue, "L")

    assert stage_status_of(queue, "L", STAGE_INTERFACE_DESIGN) == STAGE_FAILED
    assert aggregate_phase_status(queue, "L", PHASE_DESIGN) == TASK_FAILED

    queue = _queue(tmp_path / "recovery")
    design_task = next(task for task in queue["tasks"] if task["task_id"] == "L:DESIGN")
    begin_task(queue, design_task)

    recovered = recover_interrupted(queue, git_status_lines=[])

    assert recovered == [{"node_id": "L", "phase": PHASE_DESIGN, "task_id": "L:DESIGN"}]
    assert stage_status_of(queue, "L", STAGE_INTERFACE_DESIGN) == STAGE_PENDING
    assert queue["node_states"]["L"] == "UNSEEN"


def test_legacy_queue_migration_seeds_stage_tasks_from_aggregate_state(tmp_path: Path) -> None:
    path = tmp_path / ".arc" / "processing_queue.json"
    path.parent.mkdir(parents=True)
    legacy_tasks = [
        {**task, "status": "COMPLETED" if task["task_id"] == "R:DESIGN" else "PENDING"}
        for task in build_processing_tasks(_tree())
    ]
    path.write_text(
        json.dumps(
            {
                "root_id": "R",
                "tasks": legacy_tasks,
                "node_states": {"R": "DESIGNED", "L": "UNSEEN"},
                "node_design_done": {"R": True, "L": False},
            }
        ),
        encoding="utf-8",
    )

    queue = load_or_create_queue(str(path), _tree())

    assert stage_status_of(queue, "R", STAGE_INTERFACE_DESIGN) == STAGE_PUBLISHED
    assert stage_status_of(queue, "R", STAGE_TEST_GENERATION) == STAGE_SKIPPED
    assert stage_status_of(queue, "R", STAGE_IMPLEMENTATION) == STAGE_PENDING
    assert stage_status_of(queue, "L", STAGE_INTERFACE_DESIGN) == STAGE_PENDING
    assert stage_status_of(queue, "L", STAGE_TEST_GENERATION) == STAGE_PENDING


def test_migration_normalizes_a_newly_non_leaf_test_stage_to_skipped(tmp_path: Path) -> None:
    path = tmp_path / ".arc" / "processing_queue.json"
    path.parent.mkdir(parents=True)
    queue = _queue(tmp_path)
    queue["stage_tasks"][2]["status"] = STAGE_PUBLISHED
    save_queue(queue, str(path))

    migrated = load_or_create_queue(str(path), _tree())

    assert stage_status_of(migrated, "R", STAGE_TEST_GENERATION) == STAGE_SKIPPED


def test_save_queue_persists_stage_publication_metadata(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    transition_stage_task(queue, "L", STAGE_INTERFACE_DESIGN, STAGE_RUNNING)
    transition_stage_task(
        queue,
        "L",
        STAGE_INTERFACE_DESIGN,
        STAGE_READY_TO_MERGE,
        publication={
            "base_commit": "base",
            "artifact_commit": "artifact",
            "declared_write_set": ["frontend/App.tsx"],
            "contract_hash": "contract",
        },
    )

    path = tmp_path / ".arc" / "processing_queue.json"
    save_queue(queue, str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    published = next(item for item in saved["stage_tasks"] if item["stage_task_id"] == "L:INTERFACE_DESIGN")

    assert published["status"] == STAGE_READY_TO_MERGE
    assert published["attempt_count"] == 1
    assert published["publication"]["artifact_commit"] == "artifact"


def test_implement_retry_preserves_design_publications_and_resets_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_config, "_workspace_root", tmp_path.resolve())
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(tmp_path))
    queue = _queue(tmp_path)
    queue["node_states"]["L"] = "FAILED"
    queue["node_design_done"]["L"] = True
    for stage in (STAGE_VISUAL_ANALYSIS, STAGE_INTERFACE_DESIGN, STAGE_TEST_GENERATION):
        task = next(item for item in queue["stage_tasks"] if item["stage_task_id"] == f"L:{stage}")
        task["status"] = STAGE_PUBLISHED
    implementation = next(
        item for item in queue["stage_tasks"] if item["stage_task_id"] == "L:IMPLEMENTATION"
    )
    implementation["status"] = STAGE_FAILED
    sessions.merge_node_session("L", {"coverage_reuse": {"status": "reused"}})

    plan = reset_node_for_retry(queue, "L")

    assert plan.kind == "implement"
    assert stage_status_of(queue, "L", STAGE_INTERFACE_DESIGN) == STAGE_PUBLISHED
    assert stage_status_of(queue, "L", STAGE_TEST_GENERATION) == STAGE_PUBLISHED
    assert stage_status_of(queue, "L", STAGE_IMPLEMENTATION) == STAGE_PENDING
    assert sessions.load_node_session("L")["coverage_reuse"] == {"status": "reused"}


def test_design_retry_clears_stale_coverage_reuse_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_config, "_workspace_root", tmp_path.resolve())
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(tmp_path))
    queue = _queue(tmp_path)
    queue["node_states"]["L"] = "FAILED"
    design = next(item for item in queue["tasks"] if item["task_id"] == "L:DESIGN")
    design["status"] = TASK_FAILED
    sessions.merge_node_session("L", {"coverage_reuse": {"status": "reused"}})

    plan = reset_node_for_retry(queue, "L")

    assert plan.kind == "design"
    assert sessions.load_node_session("L")["coverage_reuse"] is None

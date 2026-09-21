"""State persistence tests: the derived task-status projection and the
per-generation migration of old ``processing_queue.json`` shapes.

The typed queue (``core.queue_state``) keeps ``node_states`` +
``node_design_done`` as the persisted truth; a task's status is a pure
projection derived at every read and re-derived on load, so a saved
task-status field is never trusted. These tests pin:

- the projection (a restored state implies the right task statuses);
- one fixture per saved-file generation (the migrations live in
  ``load_or_create_queue``, one branch per missing map).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import set_workspace_root
from core.queue_state import (
    NODE_CONVERGED,
    NODE_CONVERGED_WITH_FAILED_CHILDREN,
    NODE_DESIGNED,
    NODE_FAILED,
    NODE_PASSED,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_BLOCKED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    load_or_create_queue,
    recover_interrupted,
    task_status,
)


def _tasks() -> list[dict[str, object]]:
    return [
        {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": TASK_PENDING},
        {"task_id": "R:IMPLEMENT", "node_id": "R", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
    ]


def _queue_state(node_state: str, *, design_done: bool | None = None) -> dict[str, object]:
    state: dict[str, object] = {
        "tasks": _tasks(),
        "node_states": {"R": node_state},
    }
    if design_done is not None:
        state["node_design_done"] = {"R": design_done}
    return state


@pytest.mark.parametrize("node_state", [NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN])
def test_terminal_saved_state_completes_both_node_phases(node_state: str) -> None:
    queue_state = _queue_state(node_state)

    assert [task_status(queue_state, task) for task in queue_state["tasks"]] == [
        TASK_COMPLETED,
        TASK_COMPLETED,
    ]


def test_designed_saved_state_only_completes_design_phase() -> None:
    queue_state = _queue_state(NODE_DESIGNED)

    assert [task_status(queue_state, task) for task in queue_state["tasks"]] == [
        TASK_COMPLETED,
        TASK_PENDING,
    ]


def test_failed_state_with_completed_design_keeps_the_design_landed() -> None:
    """A node that failed during IMPLEMENT keeps its DESIGN artifacts: the
    design phase reads COMPLETED (an implement-only retry must not re-run
    it), the implement phase reads FAILED."""
    queue_state = _queue_state(NODE_FAILED, design_done=True)

    assert [task_status(queue_state, task) for task in queue_state["tasks"]] == [
        TASK_COMPLETED,
        TASK_FAILED,
    ]


def test_failed_state_without_design_progress_fails_both_phases() -> None:
    queue_state = _queue_state(NODE_FAILED, design_done=False)

    assert [task_status(queue_state, task) for task in queue_state["tasks"]] == [
        TASK_FAILED,
        TASK_FAILED,
    ]


def test_blocked_state_derives_blocked_tasks() -> None:
    queue_state = _queue_state("BLOCKED_BY_DEPENDENCY", design_done=False)

    assert [task_status(queue_state, task) for task in queue_state["tasks"]] == [
        TASK_BLOCKED,
        TASK_BLOCKED,
    ]


# ---------------------------------------------------------------------------
# per-generation migration fixtures (old saved shapes stay loadable)
# ---------------------------------------------------------------------------


def _tree() -> dict[str, object]:
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [
            {
                "id": "RA",
                "name": "leaf",
                "description": "leaf",
                "children": [],
                "dependencies": ["RB"],
            },
            {
                "id": "RB",
                "name": "dependency",
                "description": "dependency",
                "children": [],
            },
        ],
    }


# The flat task order build_processing_tasks produces for _tree():
# R:DESIGN, RA:DESIGN, RA:IMPLEMENT, RB:DESIGN, RB:IMPLEMENT, R:IMPLEMENT.
def _expected_tasks(statuses: dict[str, str] | None = None) -> list[dict[str, object]]:
    statuses = statuses or {}
    order = [
        ("R", PHASE_DESIGN),
        ("RA", PHASE_DESIGN),
        ("RA", PHASE_IMPLEMENT),
        ("RB", PHASE_DESIGN),
        ("RB", PHASE_IMPLEMENT),
        ("R", PHASE_IMPLEMENT),
    ]
    return [
        {
            "task_id": f"{node_id}:{phase}",
            "node_id": node_id,
            "phase": phase,
            "status": statuses.get(f"{node_id}:{phase}", TASK_PENDING),
        }
        for node_id, phase in order
    ]


def _write_queue(tmp_path: Path, payload: dict[str, object]) -> None:
    arc_dir = tmp_path / ".arc"
    arc_dir.mkdir(parents=True, exist_ok=True)
    (arc_dir / "processing_queue.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_migration_from_minimal_shape_root_and_tasks_only(tmp_path: Path) -> None:
    """Generation 0: only ``root_id`` + flat tasks (before node states were
    persisted). Every map is rebuilt; all nodes start UNSEEN and schedulable."""
    _write_queue(tmp_path, {"root_id": "R", "tasks": _expected_tasks()})

    queue = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())

    assert queue["node_states"] == {"R": "UNSEEN", "RA": "UNSEEN", "RB": "UNSEEN"}
    assert queue["node_design_done"] == {"R": False, "RA": False, "RB": False}
    assert queue["parents"] == {"RA": "R", "RB": "R"}
    assert queue["descendants"] == {"R": ["RA", "RB"]}
    assert queue["dependencies"] == {"RA": ["RB"]}
    assert queue["dropped_dependency_edges"] == []
    assert [task["status"] for task in queue["tasks"]] == [TASK_PENDING] * 6


def test_migration_from_pre_worktree_shape_missing_descendants_and_affinity(tmp_path: Path) -> None:
    """Generation 1: node_states exist, but the maps per-node worktree
    parallelism, parent-serial DESIGN and affinity-depth split added later
    are absent; each is rebuilt without touching the saved states."""
    _write_queue(
        tmp_path,
        {
            "root_id": "R",
            "tasks": _expected_tasks({"R:DESIGN": TASK_COMPLETED}),
            "node_states": {"R": "DESIGNED", "RA": "UNSEEN", "RB": "UNSEEN"},
        },
    )

    queue = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())

    assert queue["node_states"] == {"R": "DESIGNED", "RA": "UNSEEN", "RB": "UNSEEN"}
    # The restored state drives the projection: R's design is completed.
    assert queue["node_design_done"] == {"R": True, "RA": False, "RB": False}
    assert [task_status(queue, task) for task in queue["tasks"]] == [
        TASK_COMPLETED,
        TASK_PENDING,
        TASK_PENDING,
        TASK_PENDING,
        TASK_PENDING,
        TASK_PENDING,
    ]
    assert queue["affinity"] == {"R": "R", "RA": "RA", "RB": "RB"}
    assert queue["parents"] == {"RA": "R", "RB": "R"}


def test_migration_from_pre_dependency_shape_fails_open(tmp_path: Path) -> None:
    """Generation 2: affinity exists, dependencies gating does not; the
    declared map is built from the tree and nothing is dropped."""
    _write_queue(
        tmp_path,
        {
            "root_id": "R",
            "tasks": _expected_tasks(),
            "node_states": {"R": "UNSEEN", "RA": "UNSEEN", "RB": "UNSEEN"},
            "descendants": {"R": ["RA", "RB"]},
            "parents": {"RA": "R", "RB": "R"},
            "affinity": {"R": "R", "RA": "RA", "RB": "RB"},
        },
    )

    queue = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())

    assert queue["dependencies"] == {"RA": ["RB"]}
    assert queue["dropped_dependency_edges"] == []


def test_migration_of_mixed_shape_and_unschedulable_edges(tmp_path: Path) -> None:
    """A hybrid generation (some maps present, some absent) migrates per map,
    and dependency edges the queue cannot schedule are dropped with reasons
    instead of stalling the drain."""
    _write_queue(
        tmp_path,
        {
            "root_id": "R",
            "tasks": _expected_tasks(),
            "node_states": {"R": "UNSEEN", "RA": "UNSEEN", "RB": "UNSEEN"},
            "dependencies": {"RA": ["RB", "GHOST"], "GHOST2": ["RA"]},
        },
    )

    queue = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())

    assert queue["dependencies"] == {"RA": ["RB"]}
    assert sorted(queue["dropped_dependency_edges"]) == sorted(
        [("RA", "GHOST", "no-implement-task"), ("GHOST2", "", "no-implement-task")]
    )


def test_migration_seeds_design_progress_for_failed_nodes_from_legacy_statuses(
    tmp_path: Path,
) -> None:
    """The last read of the legacy representation: a FAILED node whose saved
    task statuses show a completed DESIGN gets ``node_design_done=True``, so
    an implement-only retry stays possible after the upgrade."""
    _write_queue(
        tmp_path,
        {
            "root_id": "R",
            "tasks": _expected_tasks(
                {"RA:DESIGN": TASK_COMPLETED, "RA:IMPLEMENT": TASK_FAILED}
            ),
            "node_states": {"R": "UNSEEN", "RA": "FAILED", "RB": "UNSEEN"},
            "descendants": {"R": ["RA", "RB"]},
            "parents": {"RA": "R", "RB": "R"},
            "affinity": {"R": "R", "RA": "RA", "RB": "RB"},
            "dependencies": {"RA": ["RB"]},
        },
    )

    queue = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())

    assert queue["node_design_done"] == {"R": False, "RA": True, "RB": False}
    assert [task_status(queue, task) for task in queue["tasks"]] == [
        TASK_PENDING,
        TASK_COMPLETED,
        TASK_FAILED,
        TASK_PENDING,
        TASK_PENDING,
        TASK_PENDING,
    ]


def test_migration_lifts_legacy_running_task_into_an_interrupted_state(tmp_path: Path) -> None:
    """A generation-0 file interrupted mid-run recorded the in-flight task
    only as a RUNNING task status. The migration lifts it into the phase's
    in-flight node state, so interrupted-run recovery fires exactly like the
    pre-typing flow (which keyed on ``task["status"] == RUNNING``)."""
    _write_queue(
        tmp_path,
        {
            "root_id": "R",
            "tasks": _expected_tasks({"RA:IMPLEMENT": TASK_RUNNING}),
        },
    )

    queue = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())

    assert queue["node_states"]["RA"] == "IMPLEMENTING"
    assert queue["node_design_done"]["RA"] is True

    # Recovery writes the resume context into the node session, which is
    # rooted at the process workspace root: point it at this test's tmp.
    set_workspace_root(str(tmp_path))
    recovered = recover_interrupted(queue, git_status_lines=[])

    assert [record["node_id"] for record in recovered] == ["RA"]
    assert recovered[0]["phase"] == PHASE_IMPLEMENT
    assert queue["node_states"]["RA"] == NODE_DESIGNED


def test_migration_of_legacy_states_is_idempotent_on_a_second_load(tmp_path: Path) -> None:
    """A migrated file reloaded through the same path must not drift."""
    _write_queue(
        tmp_path,
        {
            "root_id": "R",
            "tasks": _expected_tasks({"R:DESIGN": TASK_COMPLETED}),
            "node_states": {"R": "DESIGNED", "RA": "UNSEEN", "RB": "UNSEEN"},
        },
    )

    first = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())
    second = load_or_create_queue(str(tmp_path / ".arc" / "processing_queue.json"), _tree())

    assert first["node_design_done"] == second["node_design_done"]
    assert first["dependencies"] == second["dependencies"]
    assert [task_status(second, task) for task in second["tasks"]] == [
        task_status(first, task) for task in first["tasks"]
    ]

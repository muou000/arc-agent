"""Scheduling rules for the parallel worktree drain.

The flat queue order encodes DESIGN-before-IMPLEMENT and children-before-
parent. Parallel draining may overlap only tasks the ordering does not
constrain: sibling subtrees. These tests pin the dependency guard and the
port-slot allocator; the port threading through the web test path is covered
by asserting the per-task port reaches the execution plan and runtime env.
"""

from __future__ import annotations

from core.config import build_web_runtime_env, get_web_base_url
from core.workflow import (
    ARCWorkflowManager,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
)
from app_type_handler.web import _build_e2e_runtime_env, _build_web_group_execution


def _task(node_id: str, phase: str, status: str = TASK_PENDING, order: int = 0) -> dict:
    return {"task_id": f"{node_id}:{phase}", "node_id": node_id, "phase": phase, "order": order, "status": status}


def _queue(tasks: list[dict], descendants: dict[str, list[str]]) -> dict:
    return {"tasks": tasks, "descendants": descendants}


def _tree() -> dict:
    return {
        "id": "R",
        "children": [
            {
                "id": "RA",
                "children": [{"id": "RA1", "children": []}],
            },
            {"id": "RB", "children": []},
        ],
    }


def test_descendants_map_covers_transitive_children() -> None:
    assert ARCWorkflowManager._build_descendants_map(_tree()) == {
        "R": ["RA", "RA1", "RB"],
        "RA": ["RA1"],
    }


def test_design_tasks_have_no_dependencies() -> None:
    queue = _queue([_task("RA", PHASE_DESIGN, TASK_RUNNING)], {"R": ["RA"], "RA": ["RA1"]})
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][0]) is True


def test_implement_requires_its_own_design_completed() -> None:
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_RUNNING, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_PENDING, 1),
        ],
        {},
    )
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][1]) is False


def test_implement_waits_for_pending_descendant_implement() -> None:
    queue = _queue(
        [
            _task("R", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 1),
            _task("R", PHASE_IMPLEMENT, TASK_PENDING, 2),
        ],
        {"R": ["RA"]},
    )
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_implement_unblocked_when_descendants_finished_or_failed() -> None:
    queue = _queue(
        [
            _task("R", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_COMPLETED, 1),
            _task("RB", PHASE_IMPLEMENT, TASK_FAILED, 2),
            _task("R", PHASE_IMPLEMENT, TASK_PENDING, 3),
        ],
        {"R": ["RA", "RB"]},
    )
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][3]) is True


def test_sibling_implements_do_not_block_each_other() -> None:
    queue = _queue(
        [
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 0),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 1),
        ],
        {"R": ["RA", "RB"]},
    )
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][1]) is True


def test_non_descendant_earlier_implement_does_not_block() -> None:
    """The old order-based rule blocked on ALL earlier IMPLEMENTs; siblings
    under other subtrees are independent and must not serialize the drain."""
    queue = _queue(
        [
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 0),
            _task("RZ", PHASE_IMPLEMENT, TASK_PENDING, 1),
        ],
        {"RZ": []},
    )
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][1]) is True


def test_next_runnable_task_skips_busy_and_blocked_tasks() -> None:
    queue = _queue(
        [
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 0),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("R", PHASE_IMPLEMENT, TASK_PENDING, 2),
        ],
        {"R": ["RA", "RB"]},
    )
    in_flight = [queue["tasks"][0]]
    pick = ARCWorkflowManager._next_runnable_task(queue, in_flight)
    assert pick["task_id"] == "RB:IMPLEMENT"


# ----------------------------------------------------------------------
# port slots
# ----------------------------------------------------------------------


def test_slot_ports_are_base_plus_offset(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *a, **k: None,
    )
    manager._port_slot_count = 2
    assert manager._acquire_port_slot("RA") == 0
    assert manager._acquire_port_slot("RB") == 1
    assert manager._slot_port(0) == 4001
    assert manager._slot_port(1) == 4002

    manager._release_port_slot(0)
    assert manager._acquire_port_slot("RC") == 0, "released slots are reusable"
    assert manager._port_slots == {0: "RC", 1: "RB"}


def test_max_concurrent_tasks_clamps_and_defaults(monkeypatch) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "999")
    manager = ARCWorkflowManager(
        workspace_path=".",
        requirement_path="",
        web_port=4000,
        log_cb=lambda *a, **k: None,
    )
    assert manager._max_concurrent_tasks() == 8

    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "0")
    assert manager._max_concurrent_tasks() == 1

    monkeypatch.delenv("ARC_MAX_CONCURRENT_TASKS")
    assert manager._max_concurrent_tasks() == 1, "parallel mode without a level stays at 1"

    monkeypatch.setenv("ARC_NODE_WORKTREES", "0")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "4")
    # _parallel_mode is captured at construction, matching one CLI process.
    serial = ARCWorkflowManager(
        workspace_path=".",
        requirement_path="",
        web_port=4000,
        log_cb=lambda *a, **k: None,
    )
    assert serial._max_concurrent_tasks() == 1, "no worktrees, no concurrency"


def test_port_slot_exhaustion_fails_loudly(tmp_path, monkeypatch) -> None:
    """A leaked slot is a scheduler bug: it must raise, not widen the port
    range into unrelated services (PR #8 review)."""
    import pytest

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *a, **k: None,
    )
    manager._port_slot_count = 1
    assert manager._acquire_port_slot("RA") == 0

    with pytest.raises(RuntimeError, match="No free port slot"):
        manager._acquire_port_slot("RB")
    # The failed acquisition must not mutate the slot table.
    assert manager._port_slots == {0: "RA"}


# ----------------------------------------------------------------------
# per-task port threading through the web test path
# ----------------------------------------------------------------------


def test_web_runtime_env_honours_port_override() -> None:
    env = build_web_runtime_env(web_port=4321)
    assert env["PORT"] == "4321"
    assert env["ARC_WEB_PORT"] == "4321"
    assert env["BASE_URL"] == "http://localhost:4321"
    assert get_web_base_url(4321) == "http://localhost:4321"


def test_group_execution_and_e2e_env_carry_the_task_port(tmp_path) -> None:
    execution = _build_web_group_execution(
        "e2e",
        ["backend/test-e2e/a.spec.ts"],
        str(tmp_path),
        web_port=4321,
    )
    assert execution["web_port"] == "4321"
    assert execution["base_url"] == "http://localhost:4321"

    env = _build_e2e_runtime_env(str(tmp_path), ["a.spec.ts"], web_port=4321)
    assert env["PLAYWRIGHT_BASE_URL"] == "http://127.0.0.1:4321"
    assert env["PORT"] == "4321"
    # The E2E database path stays derived from the workspace, which in
    # worktree mode is the per-node worktree (isolation by construction).
    assert str(tmp_path) in env["ARC_E2E_DB_PATH"]

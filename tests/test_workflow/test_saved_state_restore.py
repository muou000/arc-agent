from __future__ import annotations

import pytest

from core.workflow import (
    ARCWorkflowManager,
    NODE_CONVERGED,
    NODE_CONVERGED_WITH_FAILED_CHILDREN,
    NODE_DESIGNED,
    NODE_PASSED,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_PENDING,
)


def _queue_state(node_state: str) -> dict[str, object]:
    return {
        "tasks": [
            {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": TASK_PENDING},
            {"task_id": "R:IMPLEMENT", "node_id": "R", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
        ],
        "node_states": {"R": node_state},
    }


@pytest.mark.parametrize("node_state", [NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN])
def test_terminal_saved_state_completes_both_node_phases(node_state: str) -> None:
    queue_state = _queue_state(node_state)

    ARCWorkflowManager._apply_saved_states_to_tasks(queue_state)

    assert [task["status"] for task in queue_state["tasks"]] == [TASK_COMPLETED, TASK_COMPLETED]


def test_designed_saved_state_only_completes_design_phase() -> None:
    queue_state = _queue_state(NODE_DESIGNED)

    ARCWorkflowManager._apply_saved_states_to_tasks(queue_state)

    assert [task["status"] for task in queue_state["tasks"]] == [TASK_COMPLETED, TASK_PENDING]

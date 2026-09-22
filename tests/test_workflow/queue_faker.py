"""Test helper: derive queue node states from the task statuses a test sets.

The typed queue (``core.queue_state``) derives every task status from its
node's state, so a scenario that wants a task to read COMPLETED or FAILED
is expressed through the node's state. Tests here keep describing scenarios
with task statuses (the scheduling language they already speak) and use
this helper to get the equivalent ``node_states`` +
``node_design_done`` maps: the inverse of ``queue_state.task_status``.
"""

from __future__ import annotations

from typing import Any

from core.queue_state import (
    NODE_BLOCKED_BY_DEPENDENCY,
    NODE_DESIGNED,
    NODE_DESIGNING,
    NODE_FAILED,
    NODE_IMPLEMENTING,
    NODE_PASSED,
    NODE_UNSEEN,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_BLOCKED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    task_status,
)


def node_maps_from_tasks(tasks: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, bool]]:
    """Invert the task-status projection for the nodes a task list covers."""

    per_node: dict[str, dict[str, str]] = {}
    for task in tasks:
        per_node.setdefault(str(task["node_id"]), {})[str(task["phase"])] = str(
            task.get("status", TASK_PENDING)
        )

    states: dict[str, str] = {}
    design_done: dict[str, bool] = {}
    for node_id, statuses in per_node.items():
        design = statuses.get(PHASE_DESIGN)
        implement = statuses.get(PHASE_IMPLEMENT)
        if design == TASK_COMPLETED or (
            design is None and implement in {TASK_COMPLETED, TASK_RUNNING, TASK_FAILED}
        ):
            # The node's DESIGN finished; the implement status separates the
            # mid-flight and terminal states.
            design_done[node_id] = True
            if implement == TASK_PENDING:
                states[node_id] = NODE_DESIGNED
            elif implement == TASK_RUNNING:
                states[node_id] = NODE_IMPLEMENTING
            elif implement == TASK_BLOCKED:
                states[node_id] = NODE_BLOCKED_BY_DEPENDENCY
            elif implement == TASK_FAILED:
                states[node_id] = NODE_FAILED
            else:
                states[node_id] = NODE_PASSED
        elif design == TASK_RUNNING:
            states[node_id] = NODE_DESIGNING
            design_done[node_id] = False
        elif design == TASK_FAILED:
            states[node_id] = NODE_FAILED
            design_done[node_id] = False
        elif design == TASK_BLOCKED:
            states[node_id] = NODE_BLOCKED_BY_DEPENDENCY
            design_done[node_id] = False
        else:
            states[node_id] = NODE_UNSEEN
            design_done[node_id] = False
    return states, design_done


def queue_with_states(
    tasks: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    """Build a queue dict whose node states match the task statuses set."""

    states, design_done = node_maps_from_tasks(tasks)
    queue: dict[str, Any] = {
        "tasks": tasks,
        "node_states": states,
        "node_design_done": design_done,
    }
    queue.update(extra)
    return queue


def settle(
    queue: dict[str, Any],
    node_id: str,
    state: str,
    *,
    design_done: bool | None = None,
) -> None:
    """Move a node to a state (the replacement for mutating a task status)."""

    queue.setdefault("node_states", {})[node_id] = state
    if design_done is not None:
        queue.setdefault("node_design_done", {})[node_id] = design_done
    for task in queue.get("tasks", []):
        if str(task.get("node_id", "")) == node_id:
            task["status"] = task_status(queue, task)

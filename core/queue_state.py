"""Typed owner of the compile queue's state.

``processing_queue.json`` used to be a shapeless dict mutated from a dozen
call sites, with the same node state recorded twice (``node_states`` and
per-task ``status``) and back-filled in both directions. This module is the
single owner of that state:

- ``node_states`` is the persisted truth. ``node_design_done`` records how
  far a node got (whether its DESIGN completed), which a bare ``FAILED`` or
  ``BLOCKED_BY_DEPENDENCY`` state cannot express.
- A task's ``status`` is never authoritative: it is a pure projection of
  the node's state, derived by :func:`task_status` at every read and
  written back only as the file projection for observers such as
  ``core.evals``.
- Every state transition - begin/complete/fail, dependency blocks and
  their release, retry resets, interrupted-run recovery - has exactly one
  implementation here (issue #106: was three near-identical retry payloads
  plus two requeue payloads).
- Loading an old queue file migrates it in one place, one branch per
  saved-file generation.

The module owns queue state only. Runtime traceability writes and the
context cache stay with the caller: transitions report what they changed
(and reset plans carry the side-effect flags) so ``core.workflow`` can
apply them; node sessions are written here because they are the queue's
sibling ``.arc`` state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Literal

from core import sessions
from core.files import read_json_file, write_json_file
from core.provider_outage import (
    OUTAGE_OPEN,
    RUN_STATUS_PROVIDER_OUTAGE,
    RUN_STATUS_RUNNING,
    build_provider_outage_health_state,
    build_provider_outage_state,
    current_open_provider_outage,
    provider_outage_threshold,
    provider_outage_window_seconds,
)

QUEUE_FILENAME = "processing_queue.json"

PHASE_DESIGN = "DESIGN"
PHASE_IMPLEMENT = "IMPLEMENT"

STAGE_VISUAL_ANALYSIS = "VISUAL_ANALYSIS"
STAGE_INTERFACE_DESIGN = "INTERFACE_DESIGN"
STAGE_TEST_GENERATION = "TEST_GENERATION"
STAGE_IMPLEMENTATION = "IMPLEMENTATION"
STAGE_PIPELINE = (
    STAGE_VISUAL_ANALYSIS,
    STAGE_INTERFACE_DESIGN,
    STAGE_TEST_GENERATION,
    STAGE_IMPLEMENTATION,
)
STAGE_TASK_SCHEMA_VERSION = 1

TaskStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"]
TASK_PENDING: TaskStatus = "PENDING"
TASK_RUNNING: TaskStatus = "RUNNING"
TASK_COMPLETED: TaskStatus = "COMPLETED"
TASK_FAILED: TaskStatus = "FAILED"
TASK_BLOCKED: TaskStatus = "BLOCKED"

StageTaskStatus = Literal[
    "PENDING",
    "RUNNING",
    "RETRY_WAIT",
    "READY",
    "READY_TO_MERGE",
    "PUBLISHED",
    "SKIPPED",
    "FAILED",
    "BLOCKED",
]
STAGE_PENDING: StageTaskStatus = "PENDING"
STAGE_RUNNING: StageTaskStatus = "RUNNING"
STAGE_RETRY_WAIT: StageTaskStatus = "RETRY_WAIT"
STAGE_READY: StageTaskStatus = "READY"
STAGE_READY_TO_MERGE: StageTaskStatus = "READY_TO_MERGE"
STAGE_PUBLISHED: StageTaskStatus = "PUBLISHED"
STAGE_SKIPPED: StageTaskStatus = "SKIPPED"
STAGE_FAILED: StageTaskStatus = "FAILED"
STAGE_BLOCKED: StageTaskStatus = "BLOCKED"
_STAGE_STATUS_VALUES = frozenset(
    {
        STAGE_PENDING,
        STAGE_RUNNING,
        STAGE_RETRY_WAIT,
        STAGE_READY,
        STAGE_READY_TO_MERGE,
        STAGE_PUBLISHED,
        STAGE_SKIPPED,
        STAGE_FAILED,
        STAGE_BLOCKED,
    }
)
_STAGE_TERMINAL_SUCCESS = frozenset({STAGE_PUBLISHED, STAGE_SKIPPED})
_STAGE_ACTIVE = frozenset({STAGE_RUNNING, STAGE_READY, STAGE_READY_TO_MERGE})
_STAGE_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STAGE_PENDING: frozenset(
        {
            STAGE_RUNNING,
            STAGE_RETRY_WAIT,
            STAGE_READY,
            STAGE_PUBLISHED,
            STAGE_SKIPPED,
            STAGE_FAILED,
            STAGE_BLOCKED,
        }
    ),
    STAGE_RUNNING: frozenset(
        {
            STAGE_READY,
            STAGE_READY_TO_MERGE,
            STAGE_PUBLISHED,
            STAGE_RETRY_WAIT,
            STAGE_FAILED,
            STAGE_BLOCKED,
        }
    ),
    STAGE_RETRY_WAIT: frozenset({STAGE_PENDING, STAGE_RUNNING, STAGE_FAILED, STAGE_BLOCKED}),
    STAGE_READY: frozenset({STAGE_READY_TO_MERGE, STAGE_PUBLISHED, STAGE_FAILED}),
    STAGE_READY_TO_MERGE: frozenset({STAGE_PUBLISHED, STAGE_FAILED}),
    STAGE_PUBLISHED: frozenset(),
    STAGE_SKIPPED: frozenset(),
    STAGE_FAILED: frozenset({STAGE_PENDING, STAGE_RETRY_WAIT, STAGE_BLOCKED}),
    STAGE_BLOCKED: frozenset({STAGE_PENDING, STAGE_RETRY_WAIT, STAGE_FAILED}),
}

NodeState = Literal[
    "UNSEEN",
    "DESIGNING",
    "DESIGNED",
    "IMPLEMENTING",
    "PASSED",
    "CONVERGED",
    "CONVERGED_WITH_FAILED_CHILDREN",
    "FAILED",
    "BLOCKED_BY_DEPENDENCY",
]
NODE_UNSEEN: NodeState = "UNSEEN"
NODE_DESIGNING: NodeState = "DESIGNING"
NODE_DESIGNED: NodeState = "DESIGNED"
NODE_IMPLEMENTING: NodeState = "IMPLEMENTING"
NODE_PASSED: NodeState = "PASSED"
NODE_CONVERGED: NodeState = "CONVERGED"
NODE_CONVERGED_WITH_FAILED_CHILDREN: NodeState = "CONVERGED_WITH_FAILED_CHILDREN"
NODE_FAILED: NodeState = "FAILED"
NODE_BLOCKED_BY_DEPENDENCY: NodeState = "BLOCKED_BY_DEPENDENCY"

# States that imply the node's DESIGN finished (terminal success, or mid-
# flight IMPLEMENT). FAILED/BLOCKED nodes instead read node_design_done.
_DESIGN_FINISHED_STATES = frozenset(
    {NODE_DESIGNED, NODE_IMPLEMENTING, NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN}
)

# Called after every node-state write so the traceability ``node_states``
# table stays current; the queue module itself stays runtime-agnostic.
#
# Callback contract: the module invokes it only AFTER it has written the
# node state and re-synced that node's task-status projection, so the
# callback observes a fully consistent queue. The callback must not
# mutate ``queue_state``: the transition walkers (block propagation,
# release, recovery) iterate over it to a fixpoint and re-derive every
# decision from it, so a re-entrant write would corrupt the walk. It is
# for runtime-side effects only (traceability writes, event marks).
StateChangeCallback = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# queries (the derived task-status projection)
# ---------------------------------------------------------------------------


def node_state(queue_state: dict[str, Any], node_id: str) -> str:
    """The node's state, normalized; unknown ids read as UNSEEN."""

    raw = (queue_state.get("node_states") or {}).get(node_id)
    return str(raw or "").strip().upper() or NODE_UNSEEN


def outage_is_open(queue_state: dict[str, Any]) -> bool:
    """Whether the queue currently requires provider recovery before resume."""

    outage = queue_state.get("provider_outage")
    return isinstance(outage, dict) and str(outage.get("status", "")).upper() == OUTAGE_OPEN


def record_provider_outage(
    queue_state: dict[str, Any],
    details: dict[str, Any],
    *,
    threshold: int | None = None,
    window_seconds: int | None = None,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Apply one provider outage transition to the owned queue state."""

    opened, state, fingerprints, new_window = build_provider_outage_state(
        queue_state.get("provider_outage_fingerprints"),
        details,
        threshold=provider_outage_threshold() if threshold is None else threshold,
        window_seconds=provider_outage_window_seconds() if window_seconds is None else window_seconds,
        now=now,
    )
    queue_state["provider_outage_fingerprints"] = fingerprints
    open_state = current_open_provider_outage(fingerprints)
    queue_state["provider_outage"] = open_state if open_state is not None else state
    if new_window:
        queue_state["provider_outage_deferred_task_ids"] = []
    if open_state is not None or opened:
        queue_state["run_status"] = RUN_STATUS_PROVIDER_OUTAGE
    else:
        queue_state.setdefault("run_status", RUN_STATUS_RUNNING)
    return opened, state


def mark_provider_outage_health_check(
    queue_state: dict[str, Any],
    *,
    healthy: bool,
    now: datetime | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Apply a resume-time provider health-check result to the queue."""

    state = build_provider_outage_health_state(
        queue_state.get("provider_outage"),
        healthy=healthy,
        now=now,
        message=message,
    )
    queue_state["provider_outage"] = state
    fingerprints = queue_state.get("provider_outage_fingerprints")
    if not isinstance(fingerprints, dict):
        fingerprints = {}
    if state.get("fingerprint"):
        fingerprints[str(state["fingerprint"])] = state
    queue_state["provider_outage_fingerprints"] = fingerprints
    if healthy:
        queue_state["run_status"] = RUN_STATUS_RUNNING
        clear_provider_outage_deferred_tasks(queue_state)
    else:
        queue_state["run_status"] = RUN_STATUS_PROVIDER_OUTAGE
    return state


def defer_provider_outage_task(queue_state: dict[str, Any], task_id: str) -> None:
    """Keep one outage-interrupted task out of the current observation pass."""

    normalized = str(task_id or "").strip()
    if not normalized:
        return
    deferred = queue_state.setdefault("provider_outage_deferred_task_ids", [])
    if normalized not in deferred:
        deferred.append(normalized)


def clear_provider_outage_deferred_tasks(queue_state: dict[str, Any]) -> None:
    """Allow deferred outage tasks to participate in a fresh resume pass."""

    queue_state["provider_outage_deferred_task_ids"] = []


def design_done(queue_state: dict[str, Any], node_id: str) -> bool:
    """Whether the node's DESIGN completed, even if the node later failed."""

    if node_state(queue_state, node_id) in _DESIGN_FINISHED_STATES:
        return True
    return bool((queue_state.get("node_design_done") or {}).get(node_id))


def task_status(queue_state: dict[str, Any], task: dict[str, Any]) -> str:
    """A task's status: a pure projection of its node's state.

    The transition table of the queue. Every status a task can carry is
    derived here and only here; nothing may write ``tasks[*].status``
    outside the save-time projection. A node's DESIGN always precedes its
    IMPLEMENT, so DESIGNING/IMPLEMENTING identify exactly which phase is
    in flight. For a FAILED or BLOCKED node, ``node_design_done`` keeps
    the completed DESIGN readable (its artifacts stay landed in the
    workspace and an implement-only retry must remain possible).
    """

    state = node_state(queue_state, str(task.get("node_id", "")))
    phase = str(task.get("phase", ""))
    if state in {NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN}:
        return TASK_COMPLETED
    if state == NODE_DESIGNED:
        return TASK_COMPLETED if phase == PHASE_DESIGN else TASK_PENDING
    if state == NODE_DESIGNING:
        return TASK_RUNNING if phase == PHASE_DESIGN else TASK_PENDING
    if state == NODE_IMPLEMENTING:
        return TASK_COMPLETED if phase == PHASE_DESIGN else TASK_RUNNING
    if state == NODE_FAILED:
        if phase == PHASE_DESIGN and design_done(queue_state, str(task.get("node_id", ""))):
            return TASK_COMPLETED
        return TASK_FAILED
    if state == NODE_BLOCKED_BY_DEPENDENCY:
        if phase == PHASE_DESIGN and design_done(queue_state, str(task.get("node_id", ""))):
            return TASK_COMPLETED
        return TASK_BLOCKED
    return TASK_PENDING


def stage_task_status(queue_state: dict[str, Any], task: dict[str, Any]) -> str:
    """Return a normalized persisted stage-task status.

    Stage task status is intentionally independent from the legacy aggregate
    task projection. The aggregate DESIGN/IMPLEMENT tasks remain the
    compatibility surface until the stage scheduler adopts this list.
    Unknown values from a hand-edited or future queue are treated as pending
    so the queue cannot silently claim a stage completed.
    """

    raw = str(task.get("status", "") or "").strip().upper()
    return raw if raw in _STAGE_STATUS_VALUES else STAGE_PENDING


def stage_task_of(
    queue_state: dict[str, Any], node_id: str, stage: str
) -> dict[str, Any] | None:
    """Return one node stage task, or ``None`` for legacy queue shapes."""

    for task in queue_state.get("stage_tasks", []):
        if (
            str(task.get("node_id", "")) == node_id
            and str(task.get("stage", "")) == stage
        ):
            return task
    return None


def stage_status_of(queue_state: dict[str, Any], node_id: str, stage: str) -> str | None:
    """Return a node stage status, or ``None`` when stage tasks are absent."""

    task = stage_task_of(queue_state, node_id, stage)
    return None if task is None else stage_task_status(queue_state, task)


def aggregate_phase_status(
    queue_state: dict[str, Any], node_id: str, phase: str
) -> str | None:
    """Project stage tasks back to the legacy DESIGN/IMPLEMENT vocabulary.

    This projection is additive: queues without ``stage_tasks`` fall back to
    the existing task projection, and the existing ``design_status_of`` /
    ``implement_status_of`` functions remain unchanged until a later stage
    scheduler issue explicitly switches their callers over.
    """

    if phase == PHASE_DESIGN:
        stages = (STAGE_INTERFACE_DESIGN, STAGE_TEST_GENERATION)
        fallback = design_status_of(queue_state, node_id)
    elif phase == PHASE_IMPLEMENT:
        stages = (STAGE_IMPLEMENTATION,)
        fallback = implement_status_of(queue_state, node_id)
    else:
        raise ValueError(f"Unknown aggregate phase: {phase}")

    statuses = [stage_status_of(queue_state, node_id, stage) for stage in stages]
    if any(status is None for status in statuses):
        return fallback
    normalized = [str(status) for status in statuses]
    if any(status == STAGE_FAILED for status in normalized):
        return TASK_FAILED
    if any(status == STAGE_BLOCKED for status in normalized):
        return TASK_BLOCKED
    if all(status in _STAGE_TERMINAL_SUCCESS for status in normalized):
        return TASK_COMPLETED
    if any(status in _STAGE_ACTIVE for status in normalized):
        return TASK_RUNNING
    return TASK_PENDING


def transition_stage_task(
    queue_state: dict[str, Any],
    node_id: str,
    stage: str,
    status: str,
    *,
    publication: dict[str, Any] | None = None,
    error: str | None = None,
    retry_at: str | None = None,
    error_category: str | None = None,
) -> dict[str, Any]:
    """Apply one validated stage transition and return the task payload.

    Retry resets are intentionally handled by :func:`reset_node_for_retry`,
    not by allowing arbitrary transitions from published stages. This keeps a
    landed publication immutable until an explicit node retry is requested.
    """

    task = stage_task_of(queue_state, node_id, stage)
    if task is None:
        raise ValueError(f"Stage task {node_id}:{stage} is missing")
    next_status = str(status or "").strip().upper()
    if next_status not in _STAGE_STATUS_VALUES:
        raise ValueError(f"Unknown stage status: {status}")
    current = stage_task_status(queue_state, task)
    if next_status != current and next_status not in _STAGE_ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid stage transition {current} -> {next_status} for {node_id}:{stage}")

    task["status"] = next_status
    if next_status == STAGE_RUNNING and next_status != current:
        task["attempt_count"] = int(task.get("attempt_count", 0) or 0) + 1
    if retry_at is not None or (next_status != STAGE_RETRY_WAIT and next_status != current):
        task["retry_at"] = retry_at
    if publication is not None:
        task["publication"] = dict(publication)
    if error is not None:
        task["error"] = error
    elif next_status not in {STAGE_FAILED, STAGE_RETRY_WAIT}:
        task["error"] = None
    if error_category is not None:
        task["error_category"] = error_category
    elif next_status not in {STAGE_FAILED, STAGE_RETRY_WAIT}:
        task["error_category"] = None
    return task


def fail_stage_task(
    queue_state: dict[str, Any],
    node_id: str,
    stage: str,
    *,
    error: str,
    error_category: str | None = None,
    on_state_change: StateChangeCallback | None = None,
) -> dict[str, Any]:
    """Fail one stage and project the node into the legacy FAILED state.

    Stage workers use this helper when a stage-specific gate fails before the
    aggregate DESIGN/IMPLEMENT task starts. Keeping the node projection here
    makes a visual-stage failure visible to the existing scheduler and result
    builder without duplicating queue invariants in the coordinator.
    """

    task = transition_stage_task(
        queue_state,
        node_id,
        stage,
        STAGE_FAILED,
        error=error,
        error_category=error_category,
    )
    try:
        failed_index = STAGE_PIPELINE.index(stage)
    except ValueError:
        failed_index = -1
    if failed_index >= 0:
        for later_stage in STAGE_PIPELINE[failed_index + 1 :]:
            later_task = stage_task_of(queue_state, node_id, later_stage)
            if later_task is None:
                continue
            later_status = stage_task_status(queue_state, later_task)
            if later_status in {STAGE_PENDING, STAGE_RETRY_WAIT}:
                later_task["status"] = STAGE_BLOCKED
                later_task["error"] = f"blocked by failed {stage} stage"
                later_task["error_category"] = "blocked_by_stage"
                later_task["retry_at"] = None
            elif later_status in {STAGE_RUNNING, STAGE_READY, STAGE_READY_TO_MERGE}:
                transition_stage_task(
                    queue_state,
                    node_id,
                    later_stage,
                    STAGE_FAILED,
                    error=f"blocked by failed {stage} stage",
                    error_category="blocked_by_stage",
                )
    _set_node_state(queue_state, node_id, NODE_FAILED, on_state_change)
    return task


def design_status_of(queue_state: dict[str, Any], node_id: str) -> str | None:
    """The node's DESIGN task status (derived), or None without a DESIGN task."""

    task = _node_task(queue_state, node_id, PHASE_DESIGN)
    return None if task is None else task_status(queue_state, task)


def implement_status_of(queue_state: dict[str, Any], node_id: str) -> str | None:
    """The node's IMPLEMENT task status (derived), or None without one."""

    task = _node_task(queue_state, node_id, PHASE_IMPLEMENT)
    return None if task is None else task_status(queue_state, task)


def _node_task(queue_state: dict[str, Any], node_id: str, phase: str) -> dict[str, Any] | None:
    for task in queue_state.get("tasks", []):
        if str(task.get("node_id", "")) == node_id and str(task.get("phase", "")) == phase:
            return task
    return None


def has_phase_tasks(queue_state: dict[str, Any], node_id: str) -> bool:
    """Whether the node has both a DESIGN and an IMPLEMENT task in the queue."""

    return (
        _node_task(queue_state, node_id, PHASE_DESIGN) is not None
        and _node_task(queue_state, node_id, PHASE_IMPLEMENT) is not None
    )


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------


def _set_node_state(
    queue_state: dict[str, Any],
    node_id: str,
    state: str,
    on_state_change: StateChangeCallback | None,
) -> None:
    queue_state.setdefault("node_states", {})[node_id] = state
    _sync_task_statuses(queue_state, node_id)
    if on_state_change is not None:
        on_state_change(node_id, state)


def _sync_task_statuses(queue_state: dict[str, Any], node_id: str) -> None:
    """Refresh the node's task-status fields to the derived projection.

    The persisted/in-memory ``tasks[*].status`` values are never
    authoritative - every decision reads :func:`task_status` - but they are
    kept in sync on each transition so observers (tests, the file
    projection) see coherent values without re-deriving.
    """

    for task in queue_state.get("tasks", []):
        if str(task.get("node_id", "")) == node_id:
            task["status"] = task_status(queue_state, task)


def _sync_all_task_statuses(queue_state: dict[str, Any]) -> None:
    for node_id in list(queue_state.get("node_states", {})):
        _sync_task_statuses(queue_state, node_id)


def _set_stage_status_if_present(
    queue_state: dict[str, Any],
    node_id: str,
    stage: str,
    status: str,
) -> None:
    """Best-effort projection for the legacy aggregate transitions."""

    task = stage_task_of(queue_state, node_id, stage)
    if task is None:
        return
    current = stage_task_status(queue_state, task)
    if current == status or current in _STAGE_TERMINAL_SUCCESS:
        return
    if status not in _STAGE_ALLOWED_TRANSITIONS[current]:
        return
    transition_stage_task(queue_state, node_id, stage, status)


def _reset_stage_tasks_for_retry(
    queue_state: dict[str, Any],
    node_id: str,
    *,
    reset_design: bool,
    reset_implementation: bool,
    preserve_published: bool = False,
) -> None:
    """Reset stage task state while preserving attempt history."""

    for task in queue_state.get("stage_tasks", []):
        if str(task.get("node_id", "")) != node_id:
            continue
        stage = str(task.get("stage", ""))
        if stage in {STAGE_VISUAL_ANALYSIS, STAGE_INTERFACE_DESIGN, STAGE_TEST_GENERATION}:
            if not reset_design:
                continue
        elif stage == STAGE_IMPLEMENTATION:
            if not reset_implementation:
                continue
        else:
            continue
        if preserve_published and stage_task_status(queue_state, task) in _STAGE_TERMINAL_SUCCESS:
            continue
        if not bool(task.get("applicable", True)):
            task["status"] = STAGE_SKIPPED
        else:
            task["status"] = STAGE_PENDING
        task["retry_at"] = None
        task["publication"] = None
        task["error"] = None
        task["error_category"] = None


def _block_pending_stage_tasks(queue_state: dict[str, Any], node_id: str) -> None:
    for task in queue_state.get("stage_tasks", []):
        if str(task.get("node_id", "")) != node_id:
            continue
        status = stage_task_status(queue_state, task)
        if status in {STAGE_PENDING, STAGE_RETRY_WAIT}:
            task["status"] = STAGE_BLOCKED


def _release_stage_task_blocks(queue_state: dict[str, Any], node_id: str) -> None:
    for task in queue_state.get("stage_tasks", []):
        if str(task.get("node_id", "")) != node_id:
            continue
        if stage_task_status(queue_state, task) != STAGE_BLOCKED:
            continue
        task["status"] = STAGE_SKIPPED if not bool(task.get("applicable", True)) else STAGE_PENDING
        task["error"] = None
        task["error_category"] = None
        task["retry_at"] = None


def begin_task(
    queue_state: dict[str, Any],
    task: dict[str, Any],
    on_state_change: StateChangeCallback | None = None,
) -> None:
    """A task starts: its node enters the phase's in-flight state."""

    phase = str(task.get("phase", ""))
    node_id = str(task.get("node_id", ""))
    if phase == PHASE_DESIGN:
        _set_stage_status_if_present(queue_state, node_id, STAGE_INTERFACE_DESIGN, STAGE_RUNNING)
    else:
        _set_stage_status_if_present(queue_state, node_id, STAGE_IMPLEMENTATION, STAGE_RUNNING)
    state = NODE_DESIGNING if phase == PHASE_DESIGN else NODE_IMPLEMENTING
    _set_node_state(queue_state, node_id, state, on_state_change)
    queue_state["last_task_id"] = task.get("task_id")


def complete_task(
    queue_state: dict[str, Any],
    node_id: str,
    phase: str,
    on_state_change: StateChangeCallback | None = None,
) -> str:
    """A phase finished successfully; returns the node's new state.

    An IMPLEMENT completion resolves the terminal state through the node
    session's ``result_state`` (a parent may converge over failed
    children), reusing ``core.sessions`` for the path - never a hand-built
    one that skips the node-id cleanup.
    """

    if phase == PHASE_DESIGN:
        new_state = NODE_DESIGNED
        queue_state.setdefault("node_design_done", {})[node_id] = True
        _set_stage_status_if_present(queue_state, node_id, STAGE_VISUAL_ANALYSIS, STAGE_PUBLISHED)
        _set_stage_status_if_present(queue_state, node_id, STAGE_INTERFACE_DESIGN, STAGE_PUBLISHED)
        test_task = stage_task_of(queue_state, node_id, STAGE_TEST_GENERATION)
        if test_task is not None:
            target = STAGE_SKIPPED if not bool(test_task.get("applicable", True)) else STAGE_PUBLISHED
            _set_stage_status_if_present(queue_state, node_id, STAGE_TEST_GENERATION, target)
    else:
        session = sessions.load_node_session(node_id)
        result_state = str(session.get("result_state", "") or "").strip().upper()
        if result_state == NODE_CONVERGED_WITH_FAILED_CHILDREN:
            new_state = NODE_CONVERGED_WITH_FAILED_CHILDREN
        elif result_state == NODE_CONVERGED:
            new_state = NODE_CONVERGED
        else:
            new_state = NODE_PASSED
        _set_stage_status_if_present(queue_state, node_id, STAGE_IMPLEMENTATION, STAGE_PUBLISHED)
    _set_node_state(queue_state, node_id, new_state, on_state_change)
    return new_state


def fail_task(
    queue_state: dict[str, Any],
    node_id: str,
    on_state_change: StateChangeCallback | None = None,
) -> None:
    """A phase failed: the node fails; remaining tasks read FAILED."""

    previous_state = node_state(queue_state, node_id)
    if previous_state == NODE_DESIGNING:
        _set_stage_status_if_present(queue_state, node_id, STAGE_INTERFACE_DESIGN, STAGE_FAILED)
    elif previous_state == NODE_IMPLEMENTING:
        _set_stage_status_if_present(queue_state, node_id, STAGE_IMPLEMENTATION, STAGE_FAILED)
    _set_node_state(queue_state, node_id, NODE_FAILED, on_state_change)


def failed_prerequisite_ids(queue_state: dict[str, Any], node_id: str) -> list[str]:
    """Failed declared dependencies and failed child work for the node."""

    failed: list[str] = []
    for dependency_id in (queue_state.get("dependencies") or {}).get(node_id, []):
        if implement_status_of(queue_state, str(dependency_id)) in {TASK_FAILED, TASK_BLOCKED}:
            failed.append(str(dependency_id))
    for descendant_id in (queue_state.get("descendants") or {}).get(node_id, []):
        if implement_status_of(queue_state, str(descendant_id)) in {TASK_FAILED, TASK_BLOCKED}:
            failed.append(str(descendant_id))
    return failed


def propagate_dependency_blocks(
    queue_state: dict[str, Any],
    on_state_change: StateChangeCallback | None = None,
) -> list[tuple[str, list[str]]]:
    """Mark pending dependents blocked when a prerequisite has failed.

    Only never-started work is marked: a node whose state is DESIGNING or
    IMPLEMENTING is executing and must not be rewritten. Returns the
    (node_id, blocked_by) changes for the caller to log/persist.
    """

    changed: list[tuple[str, list[str]]] = []
    while True:
        progress = False
        for node_id in list(queue_state.get("node_states", {})):
            state = node_state(queue_state, node_id)
            if state in {NODE_DESIGNING, NODE_IMPLEMENTING}:
                continue
            node_tasks = [
                task
                for task in queue_state.get("tasks", [])
                if str(task.get("node_id", "")) == node_id
            ]
            if not any(task_status(queue_state, task) == TASK_PENDING for task in node_tasks):
                continue
            blocked_by = failed_prerequisite_ids(queue_state, node_id)
            if not blocked_by:
                continue
            _block_pending_stage_tasks(queue_state, node_id)
            _set_node_state(queue_state, node_id, NODE_BLOCKED_BY_DEPENDENCY, on_state_change)
            changed.append((node_id, blocked_by))
            progress = True
        if not progress:
            break
    return changed


def release_dependency_blocks(
    queue_state: dict[str, Any],
    on_state_change: StateChangeCallback | None = None,
) -> list[str]:
    """Return BLOCKED nodes to schedulable state after a retry reset.

    The mirror of :func:`propagate_dependency_blocks`: a node whose
    failed prerequisites were all reset goes back to schedulable state -
    DESIGNED when its DESIGN had completed (an implement-only retry), else
    UNSEEN. Iterates to a fixpoint because BLOCKED is transitive.
    """

    released: list[str] = []
    while True:
        progress = False
        for node_id in list(queue_state.get("node_states", {})):
            if node_state(queue_state, node_id) != NODE_BLOCKED_BY_DEPENDENCY:
                continue
            if failed_prerequisite_ids(queue_state, node_id):
                continue
            _release_stage_task_blocks(queue_state, node_id)
            _set_node_state(
                queue_state,
                node_id,
                NODE_DESIGNED if design_done(queue_state, node_id) else NODE_UNSEEN,
                on_state_change,
            )
            released.append(node_id)
            progress = True
        if not progress:
            break
    return released


def recover_interrupted(
    queue_state: dict[str, Any],
    git_status_lines: list[str],
    on_state_change: StateChangeCallback | None = None,
) -> list[dict[str, str]]:
    """Reset nodes whose in-flight state marks an interrupted run.

    A persisted DESIGNING/IMPLEMENTING state means a task was executing
    when the process died: the node falls back to its pre-task state, and
    the resume context is recorded in the node session. Returns records
    for the caller to log (the persisted ``recovered_interrupted_tasks``
    shape).
    """

    recovered: list[dict[str, str]] = []
    recovered_stage_tasks: list[str] = []
    for task in queue_state.get("stage_tasks", []) or []:
        if stage_task_status(queue_state, task) != STAGE_RUNNING:
            continue
        stage_task_id = str(task.get("stage_task_id", "") or "")
        task["status"] = STAGE_SKIPPED if not bool(task.get("applicable", True)) else STAGE_PENDING
        task["retry_at"] = None
        task["error"] = "stage task was interrupted before publication"
        recovered_stage_tasks.append(stage_task_id)
    queue_state["recovered_interrupted_stage_tasks"] = recovered_stage_tasks
    for node_id in list(queue_state.get("node_states", {})):
        record = recover_interrupted_task(
            queue_state,
            node_id,
            git_status_lines,
            on_state_change=on_state_change,
        )
        if record is not None:
            recovered.append(record)
    queue_state["recovered_interrupted_tasks"] = recovered
    return recovered


def recover_interrupted_task(
    queue_state: dict[str, Any],
    node_id: str,
    git_status_lines: list[str],
    on_state_change: StateChangeCallback | None = None,
) -> dict[str, str] | None:
    """Recover one in-flight node without touching other active tasks."""

    state = node_state(queue_state, node_id)
    if state == NODE_DESIGNING:
        phase, fallback = PHASE_DESIGN, NODE_UNSEEN
    elif state == NODE_IMPLEMENTING:
        phase, fallback = PHASE_IMPLEMENT, NODE_DESIGNED
    else:
        return None

    task = _node_task(queue_state, node_id, phase)
    task_id = str((task or {}).get("task_id", "") or "")
    sessions.merge_node_session(
        node_id,
        {
            "resume_context": {
                "interrupted": True,
                "task_id": task_id,
                "phase": phase,
                "previous_node_state": state,
                "recovered_node_state": fallback,
                "git_status": list(git_status_lines[:80]),
                "instruction": (
                    "This node is resuming after an interrupted agent stage. "
                    "Preserve useful existing source, test, and traceability artifacts; inspect the listed dirty files "
                    "and current-node records before regenerating or overwriting work."
                ),
            },
            "phase_status": {phase.lower(): "interrupted"},
        },
    )
    if phase == PHASE_DESIGN:
        _reset_stage_tasks_for_retry(
            queue_state,
            node_id,
            reset_design=True,
            reset_implementation=False,
            preserve_published=True,
        )
    else:
        _reset_stage_tasks_for_retry(
            queue_state,
            node_id,
            reset_design=False,
            reset_implementation=True,
            preserve_published=True,
        )
    _set_node_state(queue_state, node_id, fallback, on_state_change)
    return {"node_id": node_id, "phase": phase, "task_id": task_id}


# ---------------------------------------------------------------------------
# retry resets: one spec table, one executor (was three retry payloads
# plus two merge-conflict requeue payloads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResetPlan:
    """What a reset changed and which side effects the caller must apply.

    ``kind``: "design" (both phases re-run), "implement" (DESIGN artifacts
    preserved), or "full" (a passed node re-runs everything). The traceability
    and context-cache effects cannot run inside this module (runtime is not
    reachable here), so the plan carries them as flags for the workflow's
    single executor.
    """

    kind: str
    clear_design_artifacts: bool
    reset_test_pass: bool
    invalidate_file_layers: bool
    invalidate_db_layers: bool


def reset_node_for_retry(
    queue_state: dict[str, Any],
    node_id: str,
    *,
    phase: str | None = None,
    conflict_paths: list[str] | None = None,
    on_state_change: StateChangeCallback | None = None,
) -> ResetPlan:
    """Reset a node for retry or requeue; the one reset implementation.

    ``phase`` forces that phase's reset (the merge-conflict requeue path,
    which knows the conflicting phase); without it the kind is chosen from
    how far the node got: a failed/incomplete DESIGN resets both phases,
    a failed IMPLEMENT resets IMPLEMENT only, a completed node re-runs
    everything. ``conflict_paths`` switches to the requeue payload, which
    records the conflicting paths for the prompt and spends the phase's
    one-shot conflict budget.

    The session patches below are the payloads the workflow wrote before
    the typed module existed, made explicit as table rows - including the
    asymmetries (a design requeue clears neither the failure summary nor
    the design baseline, unlike a design retry; a full retry does not
    clear design artifacts). They are preserved deliberately: this is a
    refactor, not a semantics change.
    """

    design_task = _node_task(queue_state, node_id, PHASE_DESIGN)
    implement_task = _node_task(queue_state, node_id, PHASE_IMPLEMENT)
    if design_task is None or implement_task is None:
        raise ValueError(f"Retry requested for node {node_id}, but its queue tasks are incomplete.")

    design_status = task_status(queue_state, design_task)
    implement_status = task_status(queue_state, implement_task)
    requeue = conflict_paths is not None

    if phase is not None:
        kind = "design" if phase == PHASE_DESIGN else "implement"
    elif design_status == TASK_FAILED or design_status != TASK_COMPLETED:
        kind = "design"
    elif implement_status in {TASK_FAILED, TASK_BLOCKED}:
        kind = "implement"
    else:
        kind = "full"

    if kind == "implement":
        new_state = NODE_DESIGNED
        base_patch: dict[str, Any] = {
            "phase_status": {"implement": "pending"},
            "resume_context": {},
            "result_state": "",
            # The workspace now contains the node's own landed
            # implementation (or the winning sibling's files); the DESIGN
            # baseline states are stale for this pass, so IMPLEMENT must
            # re-baseline from scratch.
            "design_baseline": {},
        }
        if requeue:
            base_patch["recent_failure_summary"] = ""
        plan = ResetPlan(
            kind=kind,
            clear_design_artifacts=False,
            reset_test_pass=True,
            invalidate_file_layers=False,
            invalidate_db_layers=True,
        )
    elif kind == "full":
        new_state = NODE_UNSEEN
        base_patch = {
            "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
            "resume_context": {},
            "result_state": "",
            "coverage_reuse": None,
            "recent_failure_summary": "",
            # Fresh DESIGN pass: the baseline gate will rebuild the
            # per-file states from the new manifest.
            "design_baseline": {},
        }
        plan = ResetPlan(
            kind=kind,
            clear_design_artifacts=False,
            reset_test_pass=False,
            invalidate_file_layers=False,
            invalidate_db_layers=True,
        )
    else:
        new_state = NODE_UNSEEN
        base_patch = {
            "interfaces": [],
            "materialized_files": [],
            "test_artifacts": [],
            "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
            "resume_context": {},
            "result_state": "",
            "coverage_reuse": None,
        }
        if not requeue:
            # Today's design-retry payload also wipes the failure summary
            # and the design baseline (the baseline gate rebuilds the
            # per-file states from the new manifest); the design requeue
            # payload never carried those keys - the conflict paths replace
            # the summary as guidance, and the landed design artifacts the
            # branch reset keeps stay the baseline source.
            base_patch["recent_failure_summary"] = ""
            base_patch["design_baseline"] = {}
        plan = ResetPlan(
            kind=kind,
            clear_design_artifacts=True,
            reset_test_pass=True,
            invalidate_file_layers=True,
            invalidate_db_layers=True,
        )

    if requeue:
        base_patch["merge_conflict_context"] = {"paths": list(conflict_paths or []), "phase": kind}
        base_patch["merge_conflict_retry_used"] = True
    else:
        # A manual retry is a fresh pass: restore the node's one-shot
        # conflict retry budget and drop stale conflict paths so the
        # prompt is not misdirected (None replaces the dict wholesale;
        # deep-merge would keep a {} patch intact).
        base_patch["merge_conflict_context"] = None
        base_patch["merge_conflict_retry_used"] = False

    if kind in {"design", "full"}:
        # A fresh DESIGN pass retracts the recorded design progress (a
        # full retry of a passed node is the same retraction: without it a
        # later FAILED state would still derive its DESIGN as COMPLETED).
        queue_state.setdefault("node_design_done", {})[node_id] = False
    _reset_stage_tasks_for_retry(
        queue_state,
        node_id,
        reset_design=kind in {"design", "full"},
        reset_implementation=kind in {"implement", "full"},
    )
    _set_node_state(queue_state, node_id, new_state, on_state_change)
    sessions.merge_node_session(node_id, base_patch)
    return plan


def apply_retry_plan(
    queue_state: dict[str, Any],
    *,
    retry_failed: bool = False,
    retry_node_ids: list[str] | None = None,
    on_state_change: StateChangeCallback | None = None,
) -> list[tuple[str, ResetPlan]]:
    """Reset the requested nodes; returns (node_id, plan) for each retry.

    The plans carry the side effects (traceability clears, cache
    invalidation) the caller must apply per node.
    """

    requested_ids: list[str] = []
    if retry_failed:
        requested_ids = [
            node_id
            for node_id in queue_state.get("node_states", {})
            if node_state(queue_state, node_id) in {NODE_FAILED, NODE_BLOCKED_BY_DEPENDENCY}
        ]
    elif retry_node_ids:
        requested_ids = [str(node_id).strip() for node_id in retry_node_ids if str(node_id).strip()]

    requested_ids = list(dict.fromkeys(requested_ids))
    if not requested_ids:
        return []

    known_ids = set(queue_state.get("node_states", {}).keys())
    unknown_ids = [node_id for node_id in requested_ids if node_id not in known_ids]
    if unknown_ids:
        raise ValueError(f"Retry requested for unknown node id(s): {', '.join(unknown_ids)}")

    resets: list[tuple[str, ResetPlan]] = []
    for node_id in requested_ids:
        plan = reset_node_for_retry(queue_state, node_id, on_state_change=on_state_change)
        resets.append((node_id, plan))

    queue_state["last_task_id"] = None
    queue_state["retry_plan"] = {"requested_node_ids": requested_ids, "retry_failed": bool(retry_failed)}
    return resets


# ---------------------------------------------------------------------------
# persistence and migration (one branch per saved-file generation)
# ---------------------------------------------------------------------------


def save_queue(queue_state: dict[str, Any], path: str) -> None:
    """Persist the queue; task statuses are written as the derived projection.

    ``tasks[*].status`` in the file is an observer-facing projection
    (``core.evals`` counts it), never trusted on load: a task's status is
    always re-derived from the node states.
    """

    projection = dict(queue_state)
    projection["tasks"] = [
        {**task, "status": task_status(queue_state, task)}
        for task in queue_state.get("tasks", [])
    ]
    projection["stage_tasks"] = [
        {**task, "status": stage_task_status(queue_state, task)}
        for task in queue_state.get("stage_tasks", [])
    ]
    write_json_file(path, projection)


def load_or_create_queue(
    path: str,
    requirement_tree: dict[str, Any],
    *,
    affinity_depth: int = 1,
    require_compatible_existing_queue: bool = False,
) -> dict[str, Any]:
    """Load and migrate an existing queue, or create a fresh one.

    The migrations below are ordered oldest-file-first, one branch per
    generation of the saved shape. A restored map is the durable contract
    (in-flight work was grouped under it), but a dependencies map is
    re-validated: edges this queue cannot schedule are dropped and
    reported instead of stalling the drain.
    """

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    root_id = str(requirement_tree.get("id", ""))
    expected_tasks = build_processing_tasks(requirement_tree)
    expected_task_ids = [task["task_id"] for task in expected_tasks]
    expected_stage_tasks = build_stage_tasks(requirement_tree)
    node_ids = collect_node_ids(expected_tasks)
    descendants = build_descendants_map(requirement_tree)
    parents = build_parents_map(requirement_tree)
    affinity = build_affinity_map(requirement_tree, affinity_depth)
    declared = build_dependencies_map(requirement_tree)
    dependencies, ancestor_dropped = drop_ancestor_dependency_edges(declared, parents)
    dependencies, cycle_dropped = break_dependency_cycles(
        dependencies,
        structural_precedence_edges(parents, node_ids),
    )
    dropped_dependency_edges = [
        (dependent_id, dependency_id, "ancestor-descendant")
        for dependent_id, dependency_id in ancestor_dropped
    ] + [
        (dependent_id, dependency_id, "cycle")
        for dependent_id, dependency_id in cycle_dropped
    ]
    existing_queue = read_json_file(path)
    if _is_compatible_queue(existing_queue, root_id, expected_task_ids):
        queue_state = existing_queue
        queue_state.setdefault("run_status", RUN_STATUS_RUNNING)
        queue_state.setdefault("provider_outage", None)
        queue_state.setdefault("provider_outage_fingerprints", {})
        # Queues saved before node states existed lack the map.
        had_node_states = "node_states" in queue_state
        queue_state.setdefault("node_states", {})
        for node_id in node_ids:
            queue_state["node_states"].setdefault(node_id, NODE_UNSEEN)
        if not had_node_states:
            # A generation-0 file recorded an in-flight task only in the task
            # status; lift RUNNING into the phase's in-flight state so the
            # interrupted-run recovery (keyed on DESIGNING/IMPLEMENTING)
            # still fires for an interrupted pre-typing run.
            for task in queue_state.get("tasks", []):
                if str(task.get("status", "") or "").strip().upper() != TASK_RUNNING:
                    continue
                node_id = str(task.get("node_id", "") or "").strip()
                if node_id:
                    queue_state["node_states"][node_id] = (
                        NODE_DESIGNING
                        if str(task.get("phase", "")) == PHASE_DESIGN
                        else NODE_IMPLEMENTING
                    )
        # Queues saved before per-node worktree parallelism lack the map.
        queue_state.setdefault("descendants", descendants)
        # Queues saved before parent-serial DESIGN lack the map.
        queue_state.setdefault("parents", parents)
        # Queues saved before affinity-depth split lack a finer map; like
        # the dependencies map, a restored map is the durable contract -
        # in-flight work was grouped under it, and regrouping mid-run
        # would put one subtree's tasks in two groups' worktree files.
        queue_state.setdefault("affinity", affinity)
        # Queues saved before the typed state module lack the design
        # progress map. For most nodes the state already says how far
        # they got; FAILED/BLOCKED nodes recorded it only in the task
        # statuses, so convert those (the last read of the legacy
        # representation).
        if "node_design_done" not in queue_state:
            queue_state["node_design_done"] = _migrate_design_done(queue_state)
        queue_state["stage_tasks"] = _migrate_stage_tasks(queue_state, expected_stage_tasks)
        queue_state["stage_task_schema_version"] = STAGE_TASK_SCHEMA_VERSION
        # Queues saved before dependency gating lack the map. A restored map
        # is the durable contract, but it is not trusted blindly: an edge
        # that references a node this queue cannot schedule (a hand-edited
        # or foreign queue file) would block its dependent's IMPLEMENT
        # forever, and a foreign cycle would stall the drain; both are
        # dropped and reported instead.
        if "dependencies" in queue_state:
            restored, unschedulable = drop_unschedulable_dependencies(
                queue_state["dependencies"], queue_state
            )
            structural = structural_precedence_edges(parents, node_ids)
            restored, restored_ancestors = drop_ancestor_dependency_edges(restored, parents)
            restored, restored_cycles = break_dependency_cycles(restored, structural)
            queue_state["dependencies"] = restored
            queue_state["dropped_dependency_edges"] = list(unschedulable) + [
                (dependent_id, dependency_id, "ancestor-descendant")
                for dependent_id, dependency_id in restored_ancestors
            ] + [
                (dependent_id, dependency_id, "cycle")
                for dependent_id, dependency_id in restored_cycles
            ]
        else:
            queue_state.setdefault("dependencies", dependencies)
            queue_state["dropped_dependency_edges"] = dropped_dependency_edges
        _sync_all_task_statuses(queue_state)
        return queue_state
    if require_compatible_existing_queue:
        raise ValueError(
            "Resume or retry requested, but the existing processing queue is missing or "
            "incompatible with the current requirement tree."
        )
    return {
        "root_id": root_id,
        "tasks": expected_tasks,
        "stage_tasks": expected_stage_tasks,
        "stage_task_schema_version": STAGE_TASK_SCHEMA_VERSION,
        "node_states": {node_id: NODE_UNSEEN for node_id in node_ids},
        "node_design_done": {node_id: False for node_id in node_ids},
        "descendants": descendants,
        "parents": parents,
        "affinity": affinity,
        "dependencies": dependencies,
        "dropped_dependency_edges": dropped_dependency_edges,
        "last_task_id": None,
        "run_status": RUN_STATUS_RUNNING,
        "provider_outage": None,
        "provider_outage_fingerprints": {},
    }


def _migrate_design_done(queue_state: dict[str, Any]) -> dict[str, bool]:
    """Seed the design-progress map when loading a pre-typing queue file."""

    legacy_design: dict[str, str] = {}
    for task in queue_state.get("tasks", []):
        if str(task.get("phase", "")) == PHASE_DESIGN:
            legacy_design[str(task.get("node_id", ""))] = str(task.get("status", "") or "").strip().upper()

    migrated: dict[str, bool] = {}
    for node_id in queue_state.get("node_states", {}):
        if node_state(queue_state, node_id) in _DESIGN_FINISHED_STATES:
            migrated[node_id] = True
        elif legacy_design.get(node_id) == TASK_COMPLETED:
            # A FAILED/BLOCKED node whose saved DESIGN task reads COMPLETED:
            # the legacy status carried the progress the state cannot. This
            # is the last read of the legacy representation - the derived
            # projection would answer from the state itself and lose it.
            migrated[node_id] = True
        else:
            migrated[node_id] = False
    return migrated


def _legacy_stage_status(
    queue_state: dict[str, Any], node_id: str, stage: str, *, applicable: bool
) -> str:
    """Seed stage status from the aggregate state of a pre-stage queue."""

    if not applicable:
        return STAGE_SKIPPED
    state = node_state(queue_state, node_id)
    design_finished = design_done(queue_state, node_id)
    if state in {NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN}:
        return STAGE_PUBLISHED
    if stage != STAGE_IMPLEMENTATION and design_finished:
        return STAGE_PUBLISHED
    if stage == STAGE_IMPLEMENTATION:
        legacy_status = implement_status_of(queue_state, node_id)
        if legacy_status == TASK_COMPLETED:
            return STAGE_PUBLISHED
        if legacy_status == TASK_FAILED:
            return STAGE_FAILED
        if legacy_status == TASK_BLOCKED:
            return STAGE_BLOCKED
        if state == NODE_IMPLEMENTING:
            return STAGE_RUNNING
        return STAGE_PENDING
    if state == NODE_DESIGNING:
        return STAGE_RUNNING if stage == STAGE_INTERFACE_DESIGN else STAGE_PENDING
    if state == NODE_FAILED:
        return STAGE_FAILED if stage == STAGE_INTERFACE_DESIGN else STAGE_PENDING
    if state == NODE_BLOCKED_BY_DEPENDENCY:
        return STAGE_BLOCKED
    return STAGE_PENDING


def _migrate_stage_tasks(
    queue_state: dict[str, Any], expected_stage_tasks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add or normalize stage tasks without discarding existing publications."""

    existing = {
        str(task.get("stage_task_id", "")): task
        for task in queue_state.get("stage_tasks", [])
        if isinstance(task, dict) and str(task.get("stage_task_id", ""))
    }
    migrated: list[dict[str, Any]] = []
    for template in expected_stage_tasks:
        task_id = str(template["stage_task_id"])
        current = existing.get(task_id)
        if current is None:
            current = {
                **template,
                "status": _legacy_stage_status(
                    queue_state,
                    str(template["node_id"]),
                    str(template["stage"]),
                    applicable=bool(template.get("applicable", True)),
                ),
            }
        else:
            current = {**template, **current}
            current["applicable"] = bool(template.get("applicable", True))
            raw_status = str(current.get("status", "") or "").strip().upper()
            if not current["applicable"]:
                current["status"] = STAGE_SKIPPED
            elif raw_status not in _STAGE_STATUS_VALUES:
                current["status"] = STAGE_PENDING
        current.setdefault("attempt_count", 0)
        current.setdefault("retry_at", None)
        current.setdefault("declared_write_set", None)
        current.setdefault("publication", None)
        current.setdefault("error", None)
        current.setdefault("error_category", None)
        migrated.append(current)
    return migrated


def _is_compatible_queue(
    queue_state: dict[str, Any] | None,
    root_id: str,
    expected_task_ids: list[str],
) -> bool:
    if not queue_state or queue_state.get("root_id") != root_id:
        return False
    return [task.get("task_id") for task in queue_state.get("tasks", [])] == expected_task_ids


# ---------------------------------------------------------------------------
# queue-shape builders (moved from core.workflow; the module owns the shape)
# ---------------------------------------------------------------------------


def build_processing_tasks(root_node: dict[str, Any]) -> list[dict[str, Any]]:
    """The flat task order: a node's DESIGN precedes its IMPLEMENT, and
    children IMPLEMENT before their parent."""

    tasks: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            return
        tasks.append(_make_task(node_id, PHASE_DESIGN, len(tasks)))
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child)
        tasks.append(_make_task(node_id, PHASE_IMPLEMENT, len(tasks)))

    walk(root_node)
    return tasks


def build_stage_tasks(root_node: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the persisted per-node stage task list.

    The list is deliberately separate from ``tasks``: existing DESIGN and
    IMPLEMENT consumers keep their aggregate task order while the stage
    scheduler can later select individual pipeline stages.
    """

    tasks: list[dict[str, Any]] = []

    next_node_order = 0

    def walk(node: dict[str, Any]) -> None:
        nonlocal next_node_order
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            return
        node_order = next_node_order
        next_node_order += 1
        is_leaf = not bool(node.get("children"))
        for stage in STAGE_PIPELINE:
            tasks.append(
                _make_stage_task(
                    node_id,
                    stage,
                    len(tasks),
                    node_order=node_order,
                    applicable=stage != STAGE_TEST_GENERATION or is_leaf,
                )
            )
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child)

    walk(root_node)
    return tasks


def _make_task(node_id: str, phase: str, order: int) -> dict[str, Any]:
    return {
        "task_id": f"{node_id}:{phase}",
        "node_id": node_id,
        "phase": phase,
        "order": order,
        "status": TASK_PENDING,
    }


def _make_stage_task(
    node_id: str,
    stage: str,
    order: int,
    *,
    node_order: int | None = None,
    applicable: bool = True,
) -> dict[str, Any]:
    return {
        "stage_task_id": f"{node_id}:{stage}",
        "node_id": node_id,
        "stage": stage,
        "order": order,
        "node_order": node_order,
        "status": STAGE_PENDING if applicable else STAGE_SKIPPED,
        "applicable": applicable,
        "attempt_count": 0,
        "retry_at": None,
        "declared_write_set": None,
        "publication": None,
        "error": None,
        "error_category": None,
    }


def collect_node_ids(tasks: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for task in tasks:
        node_id = task["node_id"]
        if node_id not in seen:
            seen.append(node_id)
    return seen


def build_descendants_map(root_node: dict[str, Any]) -> dict[str, list[str]]:
    descendants: dict[str, list[str]] = {}

    def walk(node: dict[str, Any], ancestors: list[str]) -> None:
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            return
        for ancestor_id in ancestors:
            descendants.setdefault(ancestor_id, []).append(node_id)
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child, ancestors + [node_id])

    walk(root_node, [])
    return descendants


def build_parents_map(root_node: dict[str, Any]) -> dict[str, str]:
    """Map every node id to its immediate parent id (the root has none)."""

    parents: dict[str, str] = {}

    def walk(node: dict[str, Any], parent_id: str) -> None:
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            return
        if parent_id:
            parents[node_id] = parent_id
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child, node_id)

    walk(root_node, "")
    return parents


def build_affinity_map(root_node: dict[str, Any], split_depth: int = 1) -> dict[str, str]:
    """Map every node id to the subtree group it shares a worktree with.

    Tasks of one group run sequentially in the group's reusable worktree,
    so a parent's and its children's design phases never race on shared
    skeleton files; different groups drain in parallel. The root itself
    forms its own group.

    ``split_depth`` bounds how deep a top-level subtree stays one group:
    depth 1 is the historical top-level-subtree grouping; a deeper split
    gives each descendant subtree at that depth (e.g. feature subtrees
    under a wide parent) its own group so siblings can drain in parallel.
    Nodes deeper than ``split_depth`` inherit their ancestor's group, so a
    group boundary is always a whole subtree, never a node subset.
    """

    affinity: dict[str, str] = {}
    root_id = str(root_node.get("id", "")).strip()
    if root_id:
        affinity[root_id] = root_id

    def walk(node: dict[str, Any], group: str, depth: int) -> None:
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            return
        # A node at depth <= split_depth heads its own group; deeper nodes
        # inherit the boundary ancestor's group, so a group is always a
        # whole subtree, never a node subset.
        node_group = node_id if depth <= split_depth else group
        affinity[node_id] = node_group
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child, node_group, depth + 1)

    for child in root_node.get("children", []) or []:
        if isinstance(child, dict):
            child_id = str(child.get("id", "")).strip()
            if child_id:
                walk(child, child_id, 1)
    return affinity


def build_dependencies_map(root_node: dict[str, Any]) -> dict[str, list[str]]:
    """Map every node id to the requirements it declares as dependencies.

    The declared ``dependencies`` list is authored data (ARC-Bench trees
    use it to model runtime prerequisites: a login node depends on the
    registration node that creates the account its scenarios use). Only
    edges that can participate in scheduling survive: ids must exist in
    this tree and self-references are dropped, so a malformed edge can
    never stall the drain on a node that is not in the queue.
    """

    declared_by_node: dict[str, list[str]] = {}

    def collect(node: dict[str, Any]) -> None:
        node_id = str(node.get("id") or "").strip()
        if node_id:
            declared = node.get("dependencies")
            values = [
                str(item or "").strip()
                for item in (declared if isinstance(declared, list) else [])
            ]
            declared_by_node[node_id] = [value for value in values if value]
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                collect(child)

    collect(root_node)
    dependencies: dict[str, list[str]] = {}
    for node_id, declared in declared_by_node.items():
        kept = [
            dependency_id
            for dependency_id in declared
            if dependency_id != node_id and dependency_id in declared_by_node
        ]
        kept = list(dict.fromkeys(kept))
        if kept:
            dependencies[node_id] = kept
    return dependencies


def drop_unschedulable_dependencies(
    dependencies: Any,
    queue_state: dict[str, Any],
) -> tuple[dict[str, list[str]], list[tuple[str, str, str]]]:
    """Drop dependency edges this queue cannot schedule, with a reason.

    The IMPLEMENT gate blocks on an edge whose dependency has no IMPLEMENT
    task at all (matching the unknown-parent rule), so such an edge would
    leave the dependent PENDING forever. Edges are therefore validated
    against the queue's own task set: both endpoints must have an IMPLEMENT
    task here. The map is rebuilt rather than reused so a malformed value
    (null, wrong types, unknown ids) can only ever degrade to "no
    dependencies", never to a stalled drain.
    """

    implement_nodes = {
        str(task.get("node_id", ""))
        for task in queue_state.get("tasks", [])
        if task.get("phase") == PHASE_IMPLEMENT
    }
    if not isinstance(dependencies, dict):
        return {}, []
    kept: dict[str, list[str]] = {}
    dropped: list[tuple[str, str, str]] = []
    for dependent_id, dependency_ids in dependencies.items():
        dependent_id = str(dependent_id)
        if dependent_id not in implement_nodes:
            dropped.append((dependent_id, "", "no-implement-task"))
            continue
        if not isinstance(dependency_ids, list):
            dropped.append((dependent_id, "", "malformed-edges"))
            continue
        for dependency_id in dependency_ids:
            dependency_id = str(dependency_id)
            if dependency_id not in implement_nodes:
                dropped.append((dependent_id, dependency_id, "no-implement-task"))
                continue
            kept.setdefault(dependent_id, []).append(dependency_id)
    return kept, dropped


def structural_precedence_edges(
    parents: dict[str, str],
    node_ids: list[str],
) -> dict[str, set[str]]:
    """The ordering the queue already enforces, as phase-vertex edges.

    Vertices are ``D:<node>`` and ``I:<node>`` (a node's DESIGN and
    IMPLEMENT tasks). The queue always runs a node's DESIGN before its
    IMPLEMENT, a child's DESIGN after its parent's DESIGN, and a parent's
    IMPLEMENT after its descendants' IMPLEMENTs; those rules are edges
    here so declared dependencies can be checked against them.
    """

    edges: dict[str, set[str]] = {}
    for node_id in node_ids:
        edges.setdefault(f"D:{node_id}", set()).add(f"I:{node_id}")
    for child_id, parent_id in parents.items():
        edges.setdefault(f"D:{parent_id}", set()).add(f"D:{child_id}")
        edges.setdefault(f"I:{child_id}", set()).add(f"I:{parent_id}")
    return edges


def drop_ancestor_dependency_edges(
    dependencies: dict[str, list[str]],
    parents: dict[str, str],
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Drop declared edges between an ancestor and its own descendant.

    The parent-child rules already sequence such a pair (the child's
    DESIGN waits for the ancestor's DESIGN, the ancestor's IMPLEMENT
    waits for the descendant's), and the dependency gate adds the
    reverse wait, so either direction of the edge makes the pair wait on
    itself and deadlocks the drain. The edge schedules nothing beyond
    those rules, so it is dropped here with its own reason instead of
    surfacing as an anonymous cycle later.

    The ancestry is derived here from ``parents`` (immediate parent per
    node, the shape build_parents_map guarantees) rather than accepted
    as a precomputed descendants map: walking the parent chain per node
    cannot misclassify a grandparent<->grandchild edge even if a future
    map shape changes, so the classification is structural instead of a
    convention callers must uphold.
    """

    def has_ancestor(node_id: str, candidate_id: str) -> bool:
        parent_id = str((parents or {}).get(node_id, "") or "")
        while parent_id:
            if parent_id == candidate_id:
                return True
            parent_id = str((parents or {}).get(parent_id, "") or "")
        return False

    kept: dict[str, list[str]] = {}
    dropped: list[tuple[str, str]] = []
    for dependent_id, dependency_ids in dependencies.items():
        for dependency_id in dependency_ids:
            if has_ancestor(dependent_id, dependency_id) or has_ancestor(dependency_id, dependent_id):
                dropped.append((dependent_id, dependency_id))
                continue
            kept.setdefault(dependent_id, []).append(dependency_id)
    return kept, dropped


def break_dependency_cycles(
    dependencies: dict[str, list[str]],
    structural_edges: dict[str, set[str]] | None = None,
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Drop dependency edges that close a cycle, keeping an acyclic graph.

    The gate blocks a node's DESIGN and IMPLEMENT until its dependencies'
    IMPLEMENTs end, so a cycle would leave every node in it permanently
    unrunnable and the drain would end with PENDING tasks instead of a
    reported failure. A declared edge means "the dependency's IMPLEMENT
    precedes the dependent's DESIGN" (``I:<dependency>`` before
    ``D:<dependent>``); ``structural_edges`` (from
    structural_precedence_edges) adds the precedence the queue enforces
    on its own, so an edge that closes a cycle *through those rules* -
    e.g. a node depending on a sibling that depends on one of its
    children - is caught too, not just pure declared cycles. Without
    structural edges the check degenerates to the historical node-level
    graph over the declared edges alone.

    Edges are visited one at a time in map order (which follows the tree
    walk) and an edge is dropped when its target can already reach its
    source through the edges accepted so far. Both properties that matter
    hold at every visit, including forward edges whose cycle is only
    completed by later edges: (1) a dropped edge always closes a cycle in
    the original graph, because the accepted edges are a subset of it, and
    (2) every cycle loses an edge, because its last edge in visit order
    finds all its other edges accepted. The structural edges are acyclic
    by construction (design vertices precede implement vertices, ancestors
    design first, descendants implement first), so every cycle contains a
    declared edge and the pass above is enough. Without ``structural_edges``
    the pass still seeds each map node's inherent ``D:N -> I:N`` edge, which
    makes the phase graph equivalent to the historical node-level graph: a
    phase cycle must alternate declared ``I:dep -> D:dependent`` edges with
    ``D:N -> I:N`` edges, so the two cycle notions coincide. Which edge of
    a cycle is dropped follows the tree order and is reported to the
    caller; the result is therefore deterministic for a given tree, never
    partially applied.
    """

    adjacency: dict[str, set[str]] = {}
    for source, targets in (structural_edges or {}).items():
        adjacency.setdefault(source, set()).update(targets)
    if structural_edges is None:
        # Degenerate mode (no tree context): seed only the inherent
        # design-before-implement edges so declared cycles still close.
        for node_id in {str(key) for key in dependencies} | {
            str(value)
            for values in dependencies.values()
            for value in values
        }:
            adjacency.setdefault(f"D:{node_id}", set()).add(f"I:{node_id}")

    def reaches(start: str, goal: str, seen: set[str]) -> bool:
        if start == goal:
            return True
        if start in seen:
            return False
        seen.add(start)
        for next_id in adjacency.get(start, ()):
            if reaches(next_id, goal, seen):
                return True
        return False

    kept: dict[str, list[str]] = {}
    dropped: list[tuple[str, str]] = []
    for dependent_id, dependency_ids in dependencies.items():
        for dependency_id in dependency_ids:
            source, target = f"I:{dependency_id}", f"D:{dependent_id}"
            if reaches(target, source, set()):
                dropped.append((dependent_id, dependency_id))
                continue
            kept.setdefault(dependent_id, []).append(dependency_id)
            adjacency.setdefault(source, set()).add(target)
    return kept, dropped

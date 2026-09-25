"""Pure scheduling decisions over the compile queue's state (issue #166).

Every "which task may run next" rule lives here as a function of
``queue_state`` alone: aggregate dependency eligibility (parent-serial DESIGN,
declared-dependency gating in both the default and the
``ARC_DESIGN_GATE_PIPELINE`` modes, descendants-before-parent IMPLEMENT),
the affinity-group weighted pick for the parallel drain, the flat-order
serial pick, and stage readiness/capacity/overlap decisions. ``ARCWorkflowManager``
keeps the side-effecting half - the drain loop, worktrees, port slots, queue
persistence, logging - and asks this module what to start; the module performs
no I/O and mutates nothing (the queue's state math - begin/complete/fail,
blocks, retries - stays in ``core.queue_state``).

Import safety: this module must never import ``core.workflow`` (whose
import runs ``load_project_env()``); it reads the pipelining switch through
``os.environ`` directly, with the name from ``core.scheduling_switches``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Iterable

from core.queue_state import (
    NODE_FAILED,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    STAGE_INTERFACE_DESIGN,
    STAGE_IMPLEMENTATION,
    STAGE_PIPELINE,
    STAGE_PUBLISHED,
    STAGE_READY,
    STAGE_READY_TO_MERGE,
    STAGE_SKIPPED,
    STAGE_TEST_GENERATION,
    STAGE_VISUAL_ANALYSIS,
    STAGE_PENDING,
    TASK_BLOCKED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    aggregate_phase_status,
    design_status_of,
    implement_status_of,
    node_state,
    stage_task_of,
    stage_task_status,
    task_status,
)
from core.scheduling_switches import ARC_DESIGN_GATE_PIPELINE


def design_pipelining_enabled() -> bool:
    """Whether a dependent's DESIGN waits only for its dependencies' DESIGNs.

    Default off: the dependent's DESIGN waits for every declared dependency's
    IMPLEMENT (the run8 serial semantics PR #38 installed, which eliminates
    run7's parallel duplicate implementations). With the gate open the wait
    relaxes to the dependency's DESIGN completing and merging - its
    registered interface cards, which carry the ``implemented`` flag, are
    then readable and the dependent designs against them incrementally. The
    semantic conflicts this re-exposes are owned by the merge rails
    (additive resolution + health gate + arbitration, #81) and by the
    contract drift check; the dependent's IMPLEMENT still waits for the
    dependency's IMPLEMENT (its scenarios read runtime state only the landed
    implementation creates).
    """

    return os.environ.get(ARC_DESIGN_GATE_PIPELINE, "").strip().lower() in {"1", "true", "yes", "on"}


def task_dependencies_met(queue_state: dict[str, Any], task: dict[str, Any]) -> bool:
    """Guard the ordering the queue relies on but never encoded as edges.

    A node's DESIGN waits for its parent's DESIGN (children design against
    the parent's merged shell, so a parent's rewrite of shared surfaces can
    never conflict with a child's additive edits in flight; a failed parent
    does not block its children, matching the failed-descendant rule
    below) and for the declared dependencies: by default the dependency's
    IMPLEMENT (an IMPLEMENT task completes only after its work is merged,
    so the dependent designs against the dependency's real surfaces
    (routes, session helpers) and reuses them instead of designing a
    duplicate - run7's parallel run had the login node write its own auth
    routes precisely because its design ran before the registration node's
    routes existed); with ``ARC_DESIGN_GATE_PIPELINE`` on, only the
    dependency's DESIGN (merged, so its registered interface cards are
    readable and the dependent designs incrementally against them; the
    drift this exposes is owned by the merge rails plus the contract
    drift check). A node's IMPLEMENT waits for its own DESIGN, for every
    descendant node's IMPLEMENT (children before their parent; a failed
    descendant keeps the parent IMPLEMENT from claiming completion over an
    incomplete subtree), and for the IMPLEMENT of every declared
    dependency: the node's scenarios routinely read runtime state
    (accounts, routes, orders) that only those nodes create, so
    implementing earlier turns a missing prerequisite into a false test
    failure. A failed dependency blocks both phases and is propagated as
    ``BLOCKED_BY_DEPENDENCY``; only a completed dependency exposes a
    verified reusable surface. Independent subtrees still drain in
    parallel. Sibling subtrees otherwise impose no order on each other,
    which is what makes parallel draining sound.
    """

    node_id = task["node_id"]
    phase = task["phase"]
    if phase == PHASE_DESIGN:
        parent_id = str((queue_state.get("parents") or {}).get(node_id, "") or "")
        if parent_id:
            parent_design_status = design_status_of(queue_state, parent_id)
            # A parents entry without a matching DESIGN task means the
            # queue is inconsistent with its own map (tasks are built
            # from the same tree, so this should be unreachable): block
            # instead of designing against an unknown baseline.
            if parent_design_status not in {TASK_COMPLETED, TASK_FAILED}:
                return False
        return _declared_dependencies_satisfied(
            queue_state, node_id, phase=PHASE_DESIGN
        )
    if phase == PHASE_IMPLEMENT:
        own_design_status = design_status_of(queue_state, node_id)
        if own_design_status is not None and own_design_status != TASK_COMPLETED:
            return False
        if not _declared_dependencies_satisfied(queue_state, node_id):
            return False
        descendants = set(queue_state.get("descendants", {}).get(node_id, []))
        if not descendants:
            return True
        descendant_tasks = [
            other
            for other in queue_state["tasks"]
            if other["phase"] == PHASE_IMPLEMENT and other["node_id"] in descendants
        ]
        return all(task_status(queue_state, other) == TASK_COMPLETED for other in descendant_tasks)
    return True


def _declared_dependencies_satisfied(
    queue_state: dict[str, Any],
    node_id: str,
    *,
    phase: str = PHASE_IMPLEMENT,
) -> bool:
    """True when every declared dependency has passed the required phase.

    Default (gate closed, and for IMPLEMENT tasks in every mode): the
    dependency's IMPLEMENT must have passed. An IMPLEMENT completes only
    after its work is merged, so satisfied dependencies mean the
    dependency's surfaces are already on the integration HEAD the task
    starts from. A failed dependency is not satisfied; the scheduler
    propagates an explicit blocked state to the dependent instead of
    designing or implementing against an unverified surface. A dependency
    with no IMPLEMENT task means the queue is inconsistent with the tree
    it was built from (restored maps are validated against this): block,
    matching the unknown-parent rule.

    Pipeline mode (``ARC_DESIGN_GATE_PIPELINE``, DESIGN tasks only): the
    dependency's DESIGN must have passed - it merged, so the registered
    interface cards are readable and the dependent designs against them.
    The same failed/unknown rules apply (a failed DESIGN never exposed a
    usable contract baseline; a dependency without a DESIGN task is an
    inconsistent queue). IMPLEMENT tasks keep the default rule.
    """

    pipelined = phase == PHASE_DESIGN and design_pipelining_enabled()
    for dependency_id in (queue_state.get("dependencies") or {}).get(node_id, []):
        if pipelined:
            dependency_status = design_status_of(queue_state, dependency_id)
        else:
            dependency_status = implement_status_of(queue_state, dependency_id)
        if dependency_status != TASK_COMPLETED:
            return False
    return True


# ---------------------------------------------------------------------------
# stage-pipeline scheduling
# ---------------------------------------------------------------------------


_STAGE_TERMINAL_SUCCESS = frozenset({STAGE_PUBLISHED, STAGE_SKIPPED})
_STAGE_ACTIVE = frozenset({"RUNNING", STAGE_READY, STAGE_READY_TO_MERGE})
_STAGE_PRODUCT_WORK = frozenset(
    {STAGE_INTERFACE_DESIGN, STAGE_TEST_GENERATION, STAGE_IMPLEMENTATION}
)
_APPROVED_STAGE_OVERLAPS = frozenset(
    {
        frozenset({STAGE_INTERFACE_DESIGN, STAGE_TEST_GENERATION}),
        frozenset({STAGE_TEST_GENERATION, STAGE_IMPLEMENTATION}),
    }
)


def _stage_task_id(task: Mapping[str, Any]) -> str:
    return str(
        task.get("stage_task_id")
        or f"{task.get('node_id', '')}:{task.get('stage', '')}"
    )


def _stage_name(task: Mapping[str, Any]) -> str:
    return str(task.get("stage", "") or "").strip().upper()


def _stage_node_id(task: Mapping[str, Any]) -> str:
    return str(task.get("node_id", "") or "").strip()


def _stage_from_in_flight(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Unwrap a direct stage task or a coordinator execution wrapper."""

    if "stage" in item and "node_id" in item:
        return item
    for key in ("stage_task", "task"):
        nested = item.get(key)
        if isinstance(nested, Mapping) and "stage" in nested:
            return nested
    return None


def _stage_status(queue_state: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    """Read a stage status from the queue when the task is persisted there."""

    stored = stage_task_of(
        dict(queue_state),
        _stage_node_id(task),
        _stage_name(task),
    )
    if stored is not None:
        return stage_task_status(dict(queue_state), stored)
    return stage_task_status(dict(queue_state), dict(task))


def _stage_success(
    queue_state: Mapping[str, Any],
    node_id: str,
    stage: str,
    *,
    allow_ready: bool = False,
) -> bool:
    task = stage_task_of(dict(queue_state), node_id, stage)
    if task is None:
        # Legacy queues have no persisted VISUAL_ANALYSIS task. Their formal
        # stages already use the old synchronous visual path, so preserve that
        # compatibility while failing closed for missing formal predecessors.
        return stage == STAGE_VISUAL_ANALYSIS
    status = stage_task_status(dict(queue_state), dict(task))
    if allow_ready:
        return status in _STAGE_TERMINAL_SUCCESS or status == STAGE_READY
    return status in _STAGE_TERMINAL_SUCCESS


def _stage_aggregate_status(
    queue_state: dict[str, Any], node_id: str, phase: str
) -> str | None:
    """Project a stage-backed node into the legacy aggregate vocabulary."""

    if queue_state.get("stage_tasks"):
        return aggregate_phase_status(queue_state, node_id, phase)
    if phase == PHASE_DESIGN:
        return design_status_of(queue_state, node_id)
    return implement_status_of(queue_state, node_id)


def _stage_design_dependencies_met(queue_state: dict[str, Any], node_id: str) -> bool:
    """Apply the existing parent and declared-dependency DESIGN gates."""

    parent_id = str((queue_state.get("parents") or {}).get(node_id, "") or "")
    if parent_id:
        parent_status = _stage_aggregate_status(queue_state, parent_id, PHASE_DESIGN)
        # This intentionally mirrors task_dependencies_met: a failed parent
        # releases its children, but an unfinished or unknown parent does not.
        if parent_status == TASK_BLOCKED and node_state(queue_state, parent_id) == NODE_FAILED:
            # A failed stage projects later stage tasks as BLOCKED, while the
            # legacy parent DESIGN gate intentionally releases children after
            # a failed parent. Keep that established behavior.
            parent_status = TASK_FAILED
        if parent_status not in {TASK_COMPLETED, TASK_FAILED}:
            return False

    pipelined = design_pipelining_enabled()
    for dependency_id in (queue_state.get("dependencies") or {}).get(node_id, []):
        dependency_id = str(dependency_id or "")
        phase = PHASE_DESIGN if pipelined else PHASE_IMPLEMENT
        if _stage_aggregate_status(queue_state, dependency_id, phase) != TASK_COMPLETED:
            return False
    return True


def _stage_implementation_dependencies_met(queue_state: dict[str, Any], node_id: str) -> bool:
    """Apply the existing DESIGN, descendant, and dependency IMPLEMENT gates."""

    if _stage_aggregate_status(queue_state, node_id, PHASE_DESIGN) != TASK_COMPLETED:
        return False
    for dependency_id in (queue_state.get("dependencies") or {}).get(node_id, []):
        if _stage_aggregate_status(queue_state, str(dependency_id), PHASE_IMPLEMENT) != TASK_COMPLETED:
            return False
    for descendant_id in (queue_state.get("descendants") or {}).get(node_id, []):
        if _stage_aggregate_status(queue_state, str(descendant_id), PHASE_IMPLEMENT) != TASK_COMPLETED:
            return False
    return True


def stage_task_dependencies_met(
    queue_state: dict[str, Any], stage_task: dict[str, Any]
) -> bool:
    """Return whether one stage task has reached its deterministic ready gate.

    The stage graph is additive to the legacy aggregate graph. Formal stages
    still pass through the same parent and declared-dependency checks as
    DESIGN/IMPLEMENT, but their own predecessor is read from ``stage_tasks``
    so a future stage runner does not need to forge aggregate node states.
    Visual analysis is preparatory work outside product worktrees; it has no
    parent/dependency gate and never releases a formal stage by itself.
    """

    stage = _stage_name(stage_task)
    node_id = _stage_node_id(stage_task)
    if not node_id or stage not in STAGE_PIPELINE:
        return False
    if not bool(stage_task.get("applicable", True)):
        return False
    if _stage_status(queue_state, stage_task) != STAGE_PENDING:
        return False

    return _stage_prerequisites_met(queue_state, stage_task)


def stage_publication_dependencies_met(
    queue_state: dict[str, Any], stage_task: dict[str, Any]
) -> bool:
    """Return whether a ready publication may enter integration.

    A stage can finish before an earlier publication lands.  The merge queue
    uses this query instead of treating ``READY_TO_MERGE`` as permission to
    bypass the same-node predecessor or the existing parent/dependency gates.
    """

    stage = _stage_name(stage_task)
    node_id = _stage_node_id(stage_task)
    if not node_id or stage not in STAGE_PIPELINE:
        return False
    if not bool(stage_task.get("applicable", True)):
        return False
    if _stage_status(queue_state, stage_task) != STAGE_READY_TO_MERGE:
        return False
    return _stage_prerequisites_met(queue_state, stage_task)


def _stage_prerequisites_met(
    queue_state: dict[str, Any], stage_task: Mapping[str, Any]
) -> bool:
    """Shared predecessor logic for starting and publishing formal stages."""

    stage = _stage_name(stage_task)
    node_id = _stage_node_id(stage_task)

    if stage == STAGE_VISUAL_ANALYSIS:
        return True

    if not _stage_success(
        queue_state,
        node_id,
        STAGE_VISUAL_ANALYSIS,
        allow_ready=True,
    ):
        return False

    if stage in {STAGE_INTERFACE_DESIGN, STAGE_TEST_GENERATION}:
        if stage == STAGE_TEST_GENERATION and not _stage_success(
            queue_state, node_id, STAGE_INTERFACE_DESIGN
        ):
            return False
        return _stage_design_dependencies_met(queue_state, node_id)

    if stage == STAGE_IMPLEMENTATION:
        if not _stage_success(queue_state, node_id, STAGE_INTERFACE_DESIGN):
            return False
        test_task = stage_task_of(dict(queue_state), node_id, STAGE_TEST_GENERATION)
        if test_task is not None and bool(test_task.get("applicable", True)):
            if not _stage_success(queue_state, node_id, STAGE_TEST_GENERATION):
                return False
        return _stage_implementation_dependencies_met(queue_state, node_id)

    return False


def _declared_stage_write_set(task: Mapping[str, Any]) -> set[str] | None:
    """Return a normalized declared write set, or ``None`` when absent.

    An explicit empty list is a valid declaration. Missing metadata is not:
    the scheduler cannot prove that two worktrees are independent and must
    therefore refuse the overlap before either stage starts.
    """

    value = task.get("declared_write_set")
    if value is None and isinstance(task.get("publication"), Mapping):
        value = task["publication"].get("declared_write_set")
    if value is None or not isinstance(value, (list, tuple)):
        return None
    normalized: set[str] = set()
    for path in value:
        if not isinstance(path, str):
            return None
        text = path.strip().replace("\\", "/")
        parts = [part for part in text.split("/") if part not in {"", "."}]
        if not parts or ".." in parts or text.startswith("/") or ":" in parts[0]:
            return None
        normalized.add("/".join(parts).casefold())
    return normalized


def stage_write_sets_disjoint(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two declared stage write sets are provably disjoint."""

    left_set = _declared_stage_write_set(left)
    right_set = _declared_stage_write_set(right)
    if left_set is None or right_set is None:
        return False
    return left_set.isdisjoint(right_set)


def stage_overlap_allowed(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two stage worktrees may execute at the same time.

    Visual analysis does not write a product worktree, so it is independent of
    every formal stage. All other overlap is limited to the two ADR-approved
    adjacent windows and requires explicit, disjoint write sets.
    """

    left_stage = _stage_name(left)
    right_stage = _stage_name(right)
    if left_stage == STAGE_VISUAL_ANALYSIS or right_stage == STAGE_VISUAL_ANALYSIS:
        return True
    if not left_stage or not right_stage or left_stage == right_stage:
        return False
    if _stage_node_id(left) == _stage_node_id(right):
        return False
    if frozenset({left_stage, right_stage}) not in _APPROVED_STAGE_OVERLAPS:
        return False
    return stage_write_sets_disjoint(left, right)


def _stage_node_order(task: Mapping[str, Any]) -> int | None:
    if "node_order" not in task or task.get("node_order") is None:
        return None
    try:
        return int(task["node_order"])
    except (TypeError, ValueError):
        return None


def _approved_stage_overlap_for_queue(
    candidate: Mapping[str, Any], blocker: Mapping[str, Any]
) -> bool:
    """Apply the n/n+1 adjacency rule when queue metadata can prove it."""

    if not stage_overlap_allowed(candidate, blocker):
        return False
    candidate_order = _stage_node_order(candidate)
    blocker_order = _stage_node_order(blocker)
    if candidate_order is None or blocker_order is None:
        # An incomplete persisted queue must fail closed rather than guessing
        # its adjacency relationship.
        return False
    first, second = (
        (candidate, blocker)
        if candidate_order < blocker_order
        else (blocker, candidate)
    )
    return abs(candidate_order - blocker_order) == 1 and (
        (_stage_name(first), _stage_name(second))
        in {
            (STAGE_TEST_GENERATION, STAGE_INTERFACE_DESIGN),
            (STAGE_IMPLEMENTATION, STAGE_TEST_GENERATION),
        }
    )


def _configured_stage_capacity(
    queue_state: dict[str, Any],
    stage: str,
    stage_capacities: Mapping[str, int] | int | None,
    default: int,
) -> int:
    configured: Any = None
    if isinstance(stage_capacities, Mapping):
        configured = stage_capacities.get(stage)
    elif isinstance(stage_capacities, int):
        configured = stage_capacities
    if configured is None:
        queue_config = queue_state.get("stage_capacities")
        if isinstance(queue_config, Mapping):
            configured = queue_config.get(stage)
        elif isinstance(queue_config, int):
            configured = queue_config
    if configured is None:
        configured = queue_state.get("stage_capacity", default)
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        return max(1, int(default))


def _stage_worktree_tasks(queue_state: dict[str, Any]) -> list[Mapping[str, Any]]:
    return [
        task
        for task in queue_state.get("stage_tasks", []) or []
        if isinstance(task, Mapping)
        and _stage_name(task) in _STAGE_PRODUCT_WORK
        and _stage_status(queue_state, task) in _STAGE_ACTIVE
    ]


def _stage_in_flight_tasks(in_flight: Iterable[dict[str, Any]]) -> list[Mapping[str, Any]]:
    tasks: list[Mapping[str, Any]] = []
    for item in in_flight:
        if not isinstance(item, Mapping):
            continue
        task = _stage_from_in_flight(item)
        if task is not None:
            tasks.append(task)
    return tasks


def _stage_ready_to_merge_count(queue_state: dict[str, Any]) -> int:
    return sum(
        1
        for task in queue_state.get("stage_tasks", []) or []
        if isinstance(task, Mapping)
        and _stage_status(queue_state, task) == STAGE_READY_TO_MERGE
    )


def stage_backpressure_state(
    queue_state: dict[str, Any],
    max_ready_to_merge: int,
) -> dict[str, int] | None:
    """Return an auditable backpressure snapshot when publication slots are full."""

    try:
        limit = max(1, int(max_ready_to_merge))
    except (TypeError, ValueError):
        limit = 1
    ready_to_merge = _stage_ready_to_merge_count(queue_state)
    pending = sum(
        1
        for task in queue_state.get("stage_tasks", []) or []
        if isinstance(task, Mapping)
        and _stage_status(queue_state, task) == STAGE_PENDING
    )
    if pending and ready_to_merge >= limit:
        return {
            "ready_to_merge": ready_to_merge,
            "limit": limit,
            "pending": pending,
        }
    return None


def next_runnable_stage_task(
    queue_state: dict[str, Any],
    in_flight: Iterable[dict[str, Any]] = (),
    *,
    max_in_flight: int | None = None,
    stage_capacities: Mapping[str, int] | int | None = None,
    max_ready_to_merge: int | None = None,
) -> dict[str, Any] | None:
    """Select the first fair, ready stage task that fits the available slots.

    The queue's stable ``order`` is the topological tie-break. A candidate
    that is blocked by a full stage, backpressure, or a declared write-set
    conflict is skipped so an independent later candidate can use the free
    slot. This makes the selector deterministic while avoiding starvation of
    ready work behind one incompatible stage.
    """

    active = [
        task
        for task in _stage_in_flight_tasks(in_flight)
        if _stage_name(task) in _STAGE_PRODUCT_WORK
    ]
    persisted_active = _stage_worktree_tasks(queue_state)
    blockers: list[Mapping[str, Any]] = list(persisted_active)
    blocker_ids = {_stage_task_id(item) for item in blockers}
    blockers.extend(task for task in active if _stage_task_id(task) not in blocker_ids)
    busy_nodes = {
        _stage_node_id(task)
        for task in blockers
        if _stage_node_id(task)
    }

    if max_in_flight is None:
        raw_limit = queue_state.get("stage_max_in_flight", queue_state.get("stage_capacity", 1))
        try:
            max_in_flight = max(1, int(raw_limit))
        except (TypeError, ValueError):
            max_in_flight = 1
    else:
        max_in_flight = max(1, int(max_in_flight))

    if len(blockers) >= max_in_flight:
        return None

    if max_ready_to_merge is None:
        configured_limit = queue_state.get("stage_max_ready_to_merge")
        if configured_limit is None:
            configured_limit = max_in_flight
        try:
            max_ready_to_merge = max(1, int(configured_limit))
        except (TypeError, ValueError):
            max_ready_to_merge = max_in_flight
    if _stage_ready_to_merge_count(queue_state) >= max_ready_to_merge:
        return None

    stage_counts: dict[str, int] = {}
    for task in blockers:
        stage = _stage_name(task)
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    candidates = sorted(
        (
            task
            for task in queue_state.get("stage_tasks", []) or []
            if isinstance(task, dict)
            and _stage_status(queue_state, task) == STAGE_PENDING
        ),
        key=lambda task: (
            int(task.get("order", 0) or 0),
            _stage_task_id(task),
        ),
    )
    for candidate in candidates:
        node_id = _stage_node_id(candidate)
        stage = _stage_name(candidate)
        # #252 owns visual analysis as bounded background work, not as a
        # product worktree competing for a formal stage slot.
        if stage == STAGE_VISUAL_ANALYSIS:
            continue
        if not node_id or node_id in busy_nodes:
            continue
        if not stage_task_dependencies_met(queue_state, candidate):
            continue
        capacity = _configured_stage_capacity(
            queue_state,
            stage,
            stage_capacities,
            max_in_flight,
        )
        if stage_counts.get(stage, 0) >= capacity:
            continue
        if any(
            not _approved_stage_overlap_for_queue(candidate, blocker)
            for blocker in blockers
        ):
            continue
        return candidate
    return None


def next_runnable_task(
    queue_state: dict[str, Any],
    in_flight: Iterable[dict[str, Any]] = (),
) -> dict[str, Any] | None:
    """Return the first PENDING task the queue's ordering allows to start.

    The flat order built by _build_processing_tasks encodes: a node's
    DESIGN precedes its IMPLEMENT, and children IMPLEMENT before their
    parent. With parallel draining, a task may additionally never start
    while another task for the same node is in flight, and (enforced in
    task_dependencies_met) a DESIGN waits for its parent's DESIGN and
    for declared dependencies' IMPLEMENTs, and an IMPLEMENT waits for
    its descendants.
    """

    busy_nodes = {str(task.get("node_id", "")) for task in in_flight}
    deferred_task_ids = {
        str(task_id).strip()
        for task_id in (queue_state.get("provider_outage_deferred_task_ids") or [])
        if str(task_id).strip()
    }
    for task in queue_state["tasks"]:
        if task_status(queue_state, task) != TASK_PENDING:
            continue
        if str(task.get("task_id", "")) in deferred_task_ids:
            continue
        if str(task.get("node_id", "")) in busy_nodes:
            continue
        if not task_dependencies_met(queue_state, task):
            continue
        return task
    return None


def next_affinity_task(
    queue_state: dict[str, Any],
    in_flight: Iterable[dict[str, Any]] = (),
) -> dict[str, Any] | None:
    """Pick the next runnable task honouring subtree affinity.

    Tasks group by their affinity-group subtree (top-level by default;
    ARC_AFFINITY_DEPTH splits deeper), and each group owns one reusable
    worktree, so at most one task per group may run at
    once. A freed slot prefers the free group with the most pending work
    (longest-remaining first), counted together with the pending work of
    the other groups that depend on it: a small hub subtree that many
    groups wait on (declared dependencies gate both phases) must not
    be starved behind larger independent subtrees. When a group runs dry
    the slot steals work from another free group. Without an affinity map
    (queues saved before subtree affinity) every node is its own group and
    the pick degenerates to the historical flat order.
    """

    affinity = queue_state.get("affinity") or {}
    in_flight_tasks = list(in_flight)
    busy_nodes = {str(task.get("node_id", "")) for task in in_flight_tasks}
    busy_groups = {affinity.get(node, node) for node in busy_nodes}
    deferred_task_ids = {
        str(task_id).strip()
        for task_id in (queue_state.get("provider_outage_deferred_task_ids") or [])
        if str(task_id).strip()
    }

    pending_weight: dict[str, int] = {}
    for other in queue_state["tasks"]:
        if task_status(queue_state, other) != TASK_PENDING:
            continue
        if str(other.get("task_id", "")) in deferred_task_ids:
            continue
        node_id = str(other.get("node_id", ""))
        if node_id in busy_nodes:
            continue
        group = str(affinity.get(node_id, node_id))
        pending_weight[group] = pending_weight.get(group, 0) + 1

    dependent_groups: dict[str, set[str]] = {}
    for dependent_id, dependency_ids in (queue_state.get("dependencies") or {}).items():
        dependent_group = str(affinity.get(dependent_id, dependent_id))
        for dependency_id in dependency_ids:
            dependency_group = str(affinity.get(dependency_id, dependency_id))
            if dependency_group != dependent_group:
                dependent_groups.setdefault(dependency_group, set()).add(dependent_group)

    best_task: dict[str, Any] | None = None
    best_weight = -1
    for task in queue_state["tasks"]:
        if task_status(queue_state, task) != TASK_PENDING:
            continue
        if str(task.get("task_id", "")) in deferred_task_ids:
            continue
        node_id = str(task.get("node_id", ""))
        if node_id in busy_nodes:
            continue
        group = str(affinity.get(node_id, node_id))
        if group in busy_groups:
            continue
        if not task_dependencies_met(queue_state, task):
            continue
        weight = pending_weight.get(group, 0) + sum(
            pending_weight.get(dependent_group, 0)
            for dependent_group in dependent_groups.get(group, ())
        )
        if weight > best_weight:
            best_task, best_weight = task, weight
    return best_task

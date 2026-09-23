"""Pure scheduling decisions over the compile queue's state (issue #166).

Every "which task may run next" rule lives here as a function of
``queue_state`` alone: dependency eligibility (parent-serial DESIGN,
declared-dependency gating in both the default and the
``ARC_DESIGN_GATE_PIPELINE`` modes, descendants-before-parent IMPLEMENT),
the affinity-group weighted pick for the parallel drain, and the flat-order
serial pick. ``ARCWorkflowManager`` keeps the side-effecting half - the
drain loop, worktrees, port slots, queue persistence, logging - and asks
this module what to start; the module performs no I/O and mutates nothing
(the queue's state math - begin/complete/fail, blocks, retries - stays in
``core.queue_state``).

Import safety: this module must never import ``core.workflow`` (whose
import runs ``load_project_env()``); it reads the pipelining switch through
``os.environ`` directly, with the name from ``core.scheduling_switches``.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from core.queue_state import (
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    design_status_of,
    implement_status_of,
    task_status,
)
from core.scheduling_switches import ARC_DESIGN_GATE_PIPELINE

DESIGN_GATE_PIPELINE_ENV = ARC_DESIGN_GATE_PIPELINE


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

    return os.environ.get(DESIGN_GATE_PIPELINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


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
    for task in queue_state["tasks"]:
        if task_status(queue_state, task) != TASK_PENDING:
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

    pending_weight: dict[str, int] = {}
    for other in queue_state["tasks"]:
        if task_status(queue_state, other) != TASK_PENDING:
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

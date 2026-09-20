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
    PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS,
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


def test_implement_stays_blocked_when_a_descendant_failed() -> None:
    queue = _queue(
        [
            _task("R", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_COMPLETED, 1),
            _task("RB", PHASE_IMPLEMENT, TASK_FAILED, 2),
            _task("R", PHASE_IMPLEMENT, TASK_PENDING, 3),
        ],
        {"R": ["RA", "RB"]},
    )
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][3]) is False


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
# subtree affinity scheduling
# ----------------------------------------------------------------------


def test_affinity_map_groups_by_top_level_subtree() -> None:
    assert ARCWorkflowManager._build_affinity_map(_tree()) == {
        "R": "R",
        "RA": "RA",
        "RA1": "RA",
        "RB": "RB",
    }


# ----------------------------------------------------------------------
# affinity depth split (ARC_AFFINITY_DEPTH)
# ----------------------------------------------------------------------


def _wide_tree() -> dict:
    """simple-keep's pathology in miniature: one wide top-level subtree
    (REQ-2) whose six feature subtrees are mutually independent but share
    one group under the default top-level grouping."""
    return {
        "id": "R",
        "children": [
            {
                "id": "REQ-2",
                "children": [
                    {
                        "id": "REQ-2.5",
                        "children": [
                            {"id": "REQ-2.5.1", "children": []},
                            {"id": "REQ-2.5.2", "children": []},
                        ],
                    },
                    {
                        "id": "REQ-2.7",
                        "children": [
                            {"id": "REQ-2.7.1", "children": []},
                            {
                                "id": "REQ-2.7.6",
                                "children": [{"id": "REQ-2.7.6.1", "children": []}],
                            },
                        ],
                    },
                ],
            },
            {"id": "REQ-3", "children": []},
        ],
    }


def test_affinity_depth_two_splits_feature_subtrees() -> None:
    """The lever for wide trees: sibling feature subtrees under one parent
    each get their own group so they can drain in parallel, while a feature
    subtree's own descendants stay together (their design phases race on the
    same skeleton files)."""
    assert ARCWorkflowManager._build_affinity_map(_wide_tree(), 2) == {
        "R": "R",
        "REQ-2": "REQ-2",
        "REQ-2.5": "REQ-2.5",
        "REQ-2.5.1": "REQ-2.5",
        "REQ-2.5.2": "REQ-2.5",
        "REQ-2.7": "REQ-2.7",
        "REQ-2.7.1": "REQ-2.7",
        "REQ-2.7.6": "REQ-2.7",
        "REQ-2.7.6.1": "REQ-2.7",
        "REQ-3": "REQ-3",
    }


def test_affinity_depth_beyond_tree_height_splits_every_subtree() -> None:
    """The depth is a boundary, not a target: every subtree at depth <= N
    heads its own group, so a depth past the tree's height makes every node
    its own group - maximum parallelism, no worktree sharing, safety resting
    entirely on the merge rails. Monotonic and literal, never clamped."""
    assert ARCWorkflowManager._build_affinity_map(_wide_tree(), 99) == {
        "R": "R",
        "REQ-2": "REQ-2",
        "REQ-2.5": "REQ-2.5",
        "REQ-2.5.1": "REQ-2.5.1",
        "REQ-2.5.2": "REQ-2.5.2",
        "REQ-2.7": "REQ-2.7",
        "REQ-2.7.1": "REQ-2.7.1",
        "REQ-2.7.6": "REQ-2.7.6",
        "REQ-2.7.6.1": "REQ-2.7.6.1",
        "REQ-3": "REQ-3",
    }


def test_affinity_depth_env_degrades_to_default(monkeypatch) -> None:
    """ARC_AFFINITY_DEPTH must always yield a usable map: unparsable values
    and depths below 1 degrade to the default 1 (the grouping every saved
    queue was built under), never to 0 or an error."""
    from core.workflow import _affinity_depth

    monkeypatch.delenv("ARC_AFFINITY_DEPTH", raising=False)
    assert _affinity_depth() == 1

    for raw in ("", "garbage", "0", "-3", "1.5"):
        monkeypatch.setenv("ARC_AFFINITY_DEPTH", raw)
        assert _affinity_depth() == 1, raw

    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")
    assert _affinity_depth() == 2


def test_queue_build_uses_configured_affinity_depth(tmp_path, monkeypatch) -> None:
    """The env reaches the queue's durable affinity map: a fresh queue built
    under ARC_AFFINITY_DEPTH=2 carries the split groups, and a default run
    keeps the historical top-level map."""
    import json

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *a, **k: None,
    )
    queue = manager._load_or_create_processing_queue(_wide_tree())

    assert queue["affinity"]["REQ-2.5.1"] == "REQ-2.5"
    assert queue["affinity"]["REQ-2.7.6"] == "REQ-2.7"

    # An existing queue's affinity map is the durable contract of a started
    # run: a resume with a different depth must not regroup in-flight work.
    queue["affinity"] = {"R": "R", "REQ-2": "REQ-2", "REQ-2.5.1": "REQ-2"}
    (tmp_path / ".arc").mkdir(exist_ok=True)
    (tmp_path / ".arc" / "processing_queue.json").write_text(
        json.dumps(queue), encoding="utf-8"
    )
    restored = manager._load_or_create_processing_queue(_wide_tree())

    assert restored["affinity"]["REQ-2.5.1"] == "REQ-2", (
        "a resumed run keeps the affinity map it started under"
    )


# ----------------------------------------------------------------------
# declared requirement dependencies
# ----------------------------------------------------------------------


def _dependency_tree() -> dict:
    return {
        "id": "R",
        "children": [
            {"id": "RA", "children": [{"id": "RA1", "children": []}]},
            {"id": "RB", "dependencies": ["RA", "RB", "RNOPE", ""], "children": []},
        ],
    }


def test_dependencies_map_keeps_known_edges_and_drops_self_and_unknown() -> None:
    """Declared dependencies model runtime prerequisites; ids that cannot be
    scheduled (self-reference, unknown id) are dropped instead of stalling."""
    assert ARCWorkflowManager._build_dependencies_map(_dependency_tree()) == {"RB": ["RA"]}


def test_break_dependency_cycles_drops_the_cycle_closing_edge() -> None:
    """A cycle would leave every node in it unrunnable; the closing edge is
    dropped so the drain still finishes with reported task states."""
    kept, dropped = ARCWorkflowManager._break_dependency_cycles({"RA": ["RB"], "RB": ["RA"]})

    assert kept == {"RA": ["RB"]}, "the first edge is kept"
    assert dropped == [("RB", "RA")], "the edge that closes the cycle is dropped"


def test_structural_precedence_edges_encode_the_queue_rules() -> None:
    """The queue always runs D-before-I, parents' DESIGN first and
    descendants' IMPLEMENT first; those rules are the baseline a declared
    edge is checked against for cycles."""
    edges = ARCWorkflowManager._structural_precedence_edges(
        {"RA": "R", "RA1": "RA"},
        ["R", "RA", "RA1"],
    )
    assert edges == {
        "D:R": {"I:R", "D:RA"},
        "I:RA": {"I:R"},
        "D:RA": {"I:RA", "D:RA1"},
        "I:RA1": {"I:RA"},
        "D:RA1": {"I:RA1"},
    }


def test_break_dependency_cycles_catches_a_cycle_through_structural_edges() -> None:
    """An edge closing a cycle only through the parent-child rules is caught:
    C depends on B while B's own child A depends on C. Node-level checking
    (main's IMPLEMENT-only gate) misses this shape; with DESIGN gating it
    would deadlock the drain, so the closing edge is dropped here."""
    structural = ARCWorkflowManager._structural_precedence_edges(
        {"B": "R", "C": "R", "A": "B"},
        ["R", "B", "A", "C"],
    )
    # B -> C kept first (I:C precedes D:B is reachable nowhere yet), then
    # C -> A closes the cycle: D:C reaches I:B via D:B -> I:B.
    kept, dropped = ARCWorkflowManager._break_dependency_cycles(
        {"B": ["C"], "C": ["A"]}, structural
    )

    assert kept == {"B": ["C"]}
    assert dropped == [("C", "A")]


def test_drop_ancestor_dependency_edges_drops_both_directions() -> None:
    """An edge between an ancestor and its own descendant deadlocks the drain
    in either direction (the parent-child rules already sequence the pair,
    the dependency gate adds the reverse wait), so it is dropped with its own
    reason rather than surfacing as an anonymous cycle."""
    kept, dropped = ARCWorkflowManager._drop_ancestor_dependency_edges(
        {"RA": ["RA1"], "RA1": ["RA"], "RB": ["RA"]},
        {"RA": "R", "RA1": "RA", "RB": "R"},
    )

    assert kept == {"RB": ["RA"]}
    assert dropped == [("RA", "RA1"), ("RA1", "RA")]


def test_drop_ancestor_dependency_edges_covers_transitive_descendants() -> None:
    """PR #38 review follow-up: the ancestry is derived from the parents map
    (immediate parent per node), so a grandparent<->grandchild edge is
    classified as ``ancestor-descendant`` here - not left to the cycle pass
    as an anonymous drop - without any precomputed-closure convention a
    future map-shape change could silently break."""
    tree = {
        "id": "R",
        "children": [
            {"id": "RA", "children": [{"id": "RA1", "children": []}]},
            {"id": "RB", "children": []},
        ],
    }
    parents = ARCWorkflowManager._build_parents_map(tree)
    assert parents == {"RA": "R", "RA1": "RA", "RB": "R"}

    kept, dropped = ARCWorkflowManager._drop_ancestor_dependency_edges(
        {"R": ["RA1"], "RA1": ["R"], "RB": ["RA1"]},
        parents,
    )

    assert kept == {"RB": ["RA1"]}
    assert dropped == [("R", "RA1"), ("RA1", "R")], (
        "transitive ancestor-descendant edges are dropped with their own reason"
    )


def test_break_dependency_cycles_keeps_a_dag_untouched() -> None:
    graph = {"RB": ["RA"], "RC": ["RB"]}

    kept, dropped = ARCWorkflowManager._break_dependency_cycles(graph)

    assert kept == graph
    assert dropped == []


def _reaches_in_graph(graph: dict, start: str, goal: str, seen: frozenset = frozenset()) -> bool:
    if start == goal:
        return True
    if start in seen:
        return False
    return any(
        _reaches_in_graph(graph, next_id, goal, seen | {start})
        for next_id in graph.get(start, [])
    )


def _is_acyclic(graph: dict) -> bool:
    return not any(
        _reaches_in_graph(graph, dependency_id, dependent_id)
        for dependent_id, dependency_ids in graph.items()
        for dependency_id in dependency_ids
    )


def test_break_dependency_cycles_only_drops_edges_that_close_a_cycle() -> None:
    """Review follow-up: the walk sees one edge at a time, so pin both
    properties on every small graph - the result is always a DAG, and a
    dropped edge always closes a cycle in the *original* graph (never a legal
    dependency that merely looked reachable in a partial view)."""
    import itertools

    nodes = ("RA", "RB", "RC")
    pairs = [(a, b) for a in nodes for b in nodes if a != b]
    for size in range(len(pairs) + 1):
        for edges in itertools.combinations(pairs, size):
            dependencies: dict[str, list[str]] = {}
            for dependent_id, dependency_id in edges:
                dependencies.setdefault(dependent_id, []).append(dependency_id)

            kept, dropped = ARCWorkflowManager._break_dependency_cycles(dependencies)

            assert _is_acyclic(kept), f"cycle survived: {edges} -> {kept}"
            for dependent_id, dependency_id in dropped:
                assert _reaches_in_graph(dependencies, dependency_id, dependent_id), (
                    f"dropped a legal dependency: {edges} -> {dependent_id} -> {dependency_id}"
                )
            survivor_edges = [
                (dependent_id, dependency_id)
                for dependent_id, dependency_ids in kept.items()
                for dependency_id in dependency_ids
            ]
            assert sorted(survivor_edges + list(dropped)) == sorted(edges), "edges are reordered, never invented"


def test_break_dependency_cycles_detects_a_cycle_closed_by_a_later_key() -> None:
    """The closing edge can be a forward edge in map order (RA -> RC while the
    back-edge RC -> RB -> RA is only added later): it is still the edge that
    closes the cycle at the moment it is visited, so it is the one dropped."""
    dependencies = {"RA": ["RC"], "RB": ["RA"], "RC": ["RB"]}

    kept, dropped = ARCWorkflowManager._break_dependency_cycles(dependencies)

    assert (kept, dropped) == ({"RA": ["RC"], "RB": ["RA"]}, [("RC", "RB")])


def test_break_dependency_cycles_result_is_stable_for_a_given_tree_order() -> None:
    """Which edge of a cycle is dropped follows the tree walk order; the
    outcome is deterministic for a tree and every drop is reported."""
    forward = {"RA": ["RC"], "RB": ["RA"], "RC": ["RB"]}
    reversed_order = {"RC": ["RB"], "RB": ["RA"], "RA": ["RC"]}

    _, forward_dropped = ARCWorkflowManager._break_dependency_cycles(dict(forward))
    _, reversed_dropped = ARCWorkflowManager._break_dependency_cycles(dict(reversed_order))

    assert forward_dropped == [("RC", "RB")]
    assert reversed_dropped == [("RA", "RC")]
    assert _is_acyclic(ARCWorkflowManager._break_dependency_cycles(dict(reversed_order))[0])


def test_drop_unschedulable_dependencies_filters_unknown_nodes_and_shapes() -> None:
    """The restored-map guard: only edges whose both endpoints have an
    IMPLEMENT task in this queue survive; malformed values degrade to "no
    dependencies" instead of stalling the gate."""
    queue = _queue([_task("RA", PHASE_IMPLEMENT), _task("RB", PHASE_IMPLEMENT)], {})

    kept, dropped = ARCWorkflowManager._drop_unschedulable_dependencies(
        {"RB": ["RA", "RGHOST"], "RGHOST": ["RA"], "RA": "not-a-list"}, queue
    )

    assert kept == {"RB": ["RA"]}
    assert dropped == [
        ("RB", "RGHOST", "no-implement-task"),
        ("RGHOST", "", "no-implement-task"),
        ("RA", "", "malformed-edges"),
    ]
    assert ARCWorkflowManager._drop_unschedulable_dependencies(None, queue) == ({}, [])
    assert ARCWorkflowManager._drop_unschedulable_dependencies(["RA"], queue) == ({}, [])


def test_implement_waits_for_declared_dependency_implement() -> None:
    queue = _queue(
        [
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 0),
            _task("RB", PHASE_DESIGN, TASK_COMPLETED, 1),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 2),
        ],
        {"R": ["RA", "RB"]},
    )
    queue["dependencies"] = {"RB": ["RA"]}

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_implement_requires_a_successful_dependency() -> None:
    queue = _queue(
        [
            _task("RA", PHASE_IMPLEMENT, TASK_COMPLETED, 0),
            _task("RB", PHASE_DESIGN, TASK_COMPLETED, 1),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 2),
        ],
        {"R": ["RA", "RB"]},
    )
    queue["dependencies"] = {"RB": ["RA"]}
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is True

    queue["tasks"][0]["status"] = TASK_FAILED
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_design_waits_for_declared_dependency_implement() -> None:
    """run7's parallel run: the login node's DESIGN ran while the registration
    node was still implementing, so both designed their own auth routes. A
    node's DESIGN now waits for the IMPLEMENT of every declared dependency -
    which completes only after merging, so the design starts from the
    dependency's real surfaces and reuses them (run8's serial semantics,
    now guaranteed under parallel draining too)."""
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 1),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 2),
        ],
        {"R": ["RA", "RB"]},
    )
    queue["dependencies"] = {"RB": ["RA"]}

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_design_requires_a_successful_dependency() -> None:
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_COMPLETED, 1),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 2),
        ],
        {"R": ["RA", "RB"]},
    )
    queue["dependencies"] = {"RB": ["RA"]}
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is True

    queue["tasks"][1]["status"] = TASK_FAILED
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_design_blocks_when_a_declared_dependency_has_no_task() -> None:
    """Like the IMPLEMENT rule: a dependency without an IMPLEMENT task means
    the queue is inconsistent with its tree, so block rather than design
    against an unknown baseline."""
    queue = _queue(
        [
            _task("RB", PHASE_DESIGN, TASK_PENDING, 0),
        ],
        {"R": ["RA", "RB"]},
    )
    queue["dependencies"] = {"RB": ["RA"]}

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][0]) is False


def test_implement_blocks_when_a_declared_dependency_has_no_task() -> None:
    """A dependencies entry without a matching IMPLEMENT task means the queue
    is inconsistent with its tree: block, like the unknown-parent rule."""
    queue = _queue(
        [
            _task("RB", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 1),
        ],
        {},
    )
    queue["dependencies"] = {"RB": ["RA"]}

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][1]) is False


def test_next_affinity_task_prefers_a_group_other_groups_depend_on() -> None:
    """The hub group is small but everything waits on it: the picker counts the
    pending work of its dependents, so it is not starved behind a larger
    independent group."""
    queue = {
        "tasks": [
            _task("RB1", PHASE_IMPLEMENT, TASK_PENDING, 0),
            _task("RB2", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("RB3", PHASE_IMPLEMENT, TASK_PENDING, 2),
            _task("RB4", PHASE_IMPLEMENT, TASK_PENDING, 3),
            _task("RA", PHASE_IMPLEMENT, TASK_PENDING, 4),
            _task("RC", PHASE_IMPLEMENT, TASK_PENDING, 5),
            _task("RC2", PHASE_IMPLEMENT, TASK_PENDING, 6),
        ],
        "descendants": {},
        "affinity": {"RA": "RA", "RB1": "RB", "RB2": "RB", "RB3": "RB", "RB4": "RB", "RC": "RC", "RC2": "RC"},
        # RC depends on RA: RA's weight is 1 own + 2 dependent = 3, RB's is 4.
        "dependencies": {"RC": ["RA"]},
    }

    pick = ARCWorkflowManager._next_affinity_task(queue, [])

    assert pick["node_id"] == "RB1", "the larger independent group still goes first while it outweighs the hub"
    queue["tasks"][0]["status"] = TASK_RUNNING
    queue["tasks"][1]["status"] = TASK_RUNNING
    pick = ARCWorkflowManager._next_affinity_task(queue, [queue["tasks"][0], queue["tasks"][1]])

    assert pick["node_id"] == "RA", "1 own + 2 dependent pending tasks outweigh the remaining independent group"


def test_affinity_priority_ignores_intra_group_dependency_edges() -> None:
    """Only cross-group edges make a group a hub: an intra-group dependency is
    already satisfied by the group's own sequential order."""
    queue = {
        "tasks": [
            _task("RB1", PHASE_IMPLEMENT, TASK_PENDING, 0),
            _task("RB2", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("RB3", PHASE_IMPLEMENT, TASK_PENDING, 2),
            _task("RA", PHASE_IMPLEMENT, TASK_PENDING, 3),
            _task("RA2", PHASE_IMPLEMENT, TASK_PENDING, 4),
        ],
        "descendants": {},
        "affinity": {"RA": "RA", "RA2": "RA", "RB1": "RB", "RB2": "RB", "RB3": "RB"},
        "dependencies": {"RA2": ["RA"]},
    }

    pick = ARCWorkflowManager._next_affinity_task(queue, [])

    assert pick["node_id"] == "RB1", "no cross-group dependent: the larger group keeps the slot"


def test_affinity_priority_without_dependency_map_matches_pending_count() -> None:
    """Queues saved before dependency gating lack the map; weights stay the
    pure pending counts of the historical rule."""
    queue = {
        "tasks": [
            _task("RA", PHASE_IMPLEMENT, TASK_PENDING, 0),
            _task("RB1", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("RB2", PHASE_IMPLEMENT, TASK_PENDING, 2),
        ],
        "descendants": {},
        "affinity": {"RA": "RA", "RB1": "RB", "RB2": "RB"},
    }

    pick = ARCWorkflowManager._next_affinity_task(queue, [])

    assert pick["node_id"] == "RB1"


def test_next_affinity_task_never_picks_a_busy_group() -> None:
    """One task per group at a time: the group owns the reusable worktree."""
    queue = {
        "tasks": [
            _task("RA1", PHASE_DESIGN, TASK_PENDING, 0),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 1),
        ],
        "descendants": {"RA": ["RA1"]},
        "affinity": {"RA": "RA", "RA1": "RA", "RB": "RB"},
    }
    in_flight = [_task("RA", PHASE_IMPLEMENT, TASK_RUNNING)]

    pick = ARCWorkflowManager._next_affinity_task(queue, in_flight)

    assert pick["task_id"] == "RB:IMPLEMENT"


def test_next_affinity_task_prefers_the_largest_free_group() -> None:
    queue = {
        "tasks": [
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 0),
            _task("RA1", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("RA2", PHASE_DESIGN, TASK_PENDING, 2),
        ],
        "descendants": {},
        "affinity": {"RB": "RB", "RA1": "RA", "RA2": "RA"},
    }

    pick = ARCWorkflowManager._next_affinity_task(queue, [])

    assert pick["node_id"] == "RA1", "longest-remaining group first, even when later in flat order"


def test_next_affinity_task_without_map_matches_flat_order() -> None:
    """Queues saved before affinity lack the map; the pick degenerates to the
    historical first-runnable task."""
    queue = _queue(
        [
            _task("RA", PHASE_IMPLEMENT, TASK_PENDING, 0),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 1),
        ],
        {},
    )

    assert ARCWorkflowManager._next_affinity_task(queue, []) is ARCWorkflowManager._next_runnable_task(queue, [])


def test_next_affinity_task_steals_from_another_free_group() -> None:
    """When the group with runnable work is busy (its subtree's design task is
    in flight), the slot takes another free group's runnable task."""
    queue = {
        "tasks": [
            _task("RA1", PHASE_IMPLEMENT, TASK_PENDING, 0),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 1),
        ],
        "descendants": {"RA": ["RA1"]},
        "affinity": {"RA": "RA", "RA1": "RA", "RB": "RB"},
    }
    in_flight = [_task("RA", PHASE_DESIGN, TASK_RUNNING)]  # RA group busy

    pick = ARCWorkflowManager._next_affinity_task(queue, in_flight)

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
    assert manager._max_concurrent_tasks() == PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS, (
        "parallel mode without a level uses the default"
    )

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

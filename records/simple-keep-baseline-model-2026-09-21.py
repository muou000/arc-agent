"""Task-unit baseline model for the simple-keep acceptance run (issue #84).

Computes the scheduling critical path of ``arc-bench-test/keep`` in task
units (one DESIGN task or one IMPLEMENT task = one unit, equal-duration
assumption, per the 2026-09-20 parallelism diagnosis) using the *real*
scheduler maps (``ARCWorkflowManager._build_*``), not a re-derivation:

- structure edges: own DESIGN -> own IMPLEMENT; parent's DESIGN -> child's
  DESIGN; every descendant's IMPLEMENT -> the node's IMPLEMENT;
- declared dependency edges after the queue's own ancestor-descendant and
  cycle filtering; default gate waits for the dependency's IMPLEMENT,
  pipelined gate (``ARC_DESIGN_GATE_PIPELINE``) waits for its DESIGN;
- affinity-group chains: tasks of one group run serially in flat queue
  order, so consecutive same-group tasks add a chain edge.

Run: ``python records/simple-keep-baseline-model-2026-09-21.py``
(no model calls, no writes outside stdout).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.files import load_requirements  # noqa: E402
from core.workflow import ARCWorkflowManager  # noqa: E402

KEEP_YAML = Path(__file__).resolve().parents[1] / "arc-bench-test" / "keep" / "requirements" / "requirements.yaml"

PHASES = ("DESIGN", "IMPLEMENT")


def task(node_id: str, phase: str) -> str:
    return f"{node_id}:{phase}"


def critical_path_length(edges: dict[str, set[str]]) -> int:
    memo: dict[str, int] = {}

    def longest(t: str, stack: set[str]) -> int:
        if t in memo:
            return memo[t]
        if t in stack:  # pragma: no cover - the real maps are acyclic
            raise ValueError(f"cycle at {t}")
        stack.add(t)
        value = 1 + max((longest(nxt, stack) for nxt in edges.get(t, ())), default=0)
        stack.discard(t)
        memo[t] = value
        return value

    return max(longest(t, set()) for t in edges)


def build_model(tree: dict, *, split_depth: int, pipelined: bool, with_affinity: bool) -> dict:
    # _build_processing_tasks is the one instance method here (it delegates to
    # _make_task); a throwaway manager in a throwaway directory provides it.
    import tempfile

    from core.workflow import ARCWorkflowManager as _M

    with tempfile.TemporaryDirectory() as tmp:
        manager = _M(
            workspace_path=tmp,
            requirement_path="",
            app_type="web",
            web_port=0,
            log_cb=lambda *args, **kwargs: None,
        )
        tasks = manager._build_processing_tasks(tree)
    flat = [(str(t["node_id"]), str(t["phase"])) for t in tasks]
    task_ids = [task(n, p) for n, p in flat]
    parents = ARCWorkflowManager._build_parents_map(tree)
    descendants = ARCWorkflowManager._build_descendants_map(tree)
    affinity = ARCWorkflowManager._build_affinity_map(tree, split_depth)
    declared = ARCWorkflowManager._build_dependencies_map(tree)
    dependencies, ancestor_dropped = ARCWorkflowManager._drop_ancestor_dependency_edges(declared, parents)
    dependencies, cycle_dropped = ARCWorkflowManager._break_dependency_cycles(
        dependencies,
        ARCWorkflowManager._structural_precedence_edges(parents, [n for n, _ in flat]),
    )

    edges: dict[str, set[str]] = {t: set() for t in task_ids}

    def add(src: str, dst: str) -> None:
        edges[src].add(dst)

    for node_id, _phase in flat:
        add(task(node_id, "DESIGN"), task(node_id, "IMPLEMENT"))
        parent = parents.get(node_id)
        if parent:
            add(task(parent, "DESIGN"), task(node_id, "DESIGN"))
        for descendant in descendants.get(node_id, []):
            add(task(descendant, "IMPLEMENT"), task(node_id, "IMPLEMENT"))
        for dependency_id in dependencies.get(node_id, []):
            gate = "DESIGN" if pipelined else "IMPLEMENT"
            add(task(dependency_id, gate), task(node_id, "DESIGN"))
            add(task(dependency_id, "IMPLEMENT"), task(node_id, "IMPLEMENT"))

    if with_affinity:
        last_of_group: dict[str, str] = {}
        for node_id, phase in flat:
            group = affinity.get(node_id, node_id)
            previous = last_of_group.get(group)
            if previous:
                add(previous, task(node_id, phase))
            last_of_group[group] = task(node_id, phase)

    groups: dict[str, int] = {}
    for node_id in dict.fromkeys(n for n, _ in flat):
        groups[affinity.get(node_id, node_id)] = groups.get(affinity.get(node_id, node_id), 0) + 1

    return {
        "critical_path": critical_path_length(edges),
        "total_tasks": len(task_ids),
        "groups": len(groups),
        "largest_group_tasks": max(groups.values()) * 2,
        "dropped_edges": list(ancestor_dropped) + list(cycle_dropped),
    }


def main() -> None:
    tree = load_requirements(str(KEEP_YAML))
    scenarios = [
        ("floor (structure+dependency edges only, gate closed)", dict(split_depth=1, pipelined=False, with_affinity=False)),
        ("old default: depth-1 affinity, gate closed", dict(split_depth=1, pipelined=False, with_affinity=True)),
        ("depth-1 affinity, pipelined gate", dict(split_depth=1, pipelined=True, with_affinity=True)),
        ("acceptance: depth-2 affinity, gate closed", dict(split_depth=2, pipelined=False, with_affinity=True)),
        ("acceptance: depth-2 affinity, pipelined gate", dict(split_depth=2, pipelined=True, with_affinity=True)),
    ]
    print(f"keep tree: {KEEP_YAML}")
    for label, kwargs in scenarios:
        model = build_model(tree, **kwargs)
        print(
            f"- {label}: critical path {model['critical_path']}/{model['total_tasks']} units, "
            f"{model['groups']} groups, largest group {model['largest_group_tasks']} tasks, "
            f"dropped edges {len(model['dropped_edges'])}"
        )


if __name__ == "__main__":
    main()

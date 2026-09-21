"""The keep-req2 fixture (#80): the REQ-2 subtree as a standalone compilable set.

``arc-bench-test/keep-req2`` is the iteration target for the parallelism
redesign (ADR 0001): the REQ-2 subtree of ``arc-bench-test/keep`` — the wide
subtree whose six feature subtrees (delete 2.3 / update 2.4 / archive 2.5 /
coloring 2.6 / labels 2.7 / pinned 2.8) carried 60 of the parent's 90 tasks in
one affinity group. These tests pin the fixture's contract so later levers
(gate pipelining, merge arbitration) have a stable target:

- the yaml loads through the production loader and holds exactly ROOT plus the
  REQ-2 subtree, with node text and declared dependencies verbatim from the
  parent tree;
- a fresh queue builds with no unschedulable dependency drops beyond the
  parent's own (both have none) and keeps every subtree-internal edge;
- ``ARC_AFFINITY_DEPTH=2`` splits the six feature subtrees into six groups on
  the real fixture (the pinned shape the existing hand-built tests describe);
- the default depth reproduces the pathology the fixture exists to exercise:
  the whole REQ-2 subtree serialized in one 60-of-62-task group.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.files import load_requirements
from core.workflow import (
    ARCWorkflowManager,
    PHASE_IMPLEMENT,
    TASK_PENDING,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KEEP_REQ2_YAML = REPO_ROOT / "arc-bench-test" / "keep-req2" / "requirements" / "requirements.yaml"
KEEP_YAML = REPO_ROOT / "arc-bench-test" / "keep" / "requirements" / "requirements.yaml"

FEATURE_SUBTREES = ("REQ-2.3", "REQ-2.4", "REQ-2.5", "REQ-2.6", "REQ-2.7", "REQ-2.8")


def _nodes_by_id(tree: dict) -> dict[str, dict]:
    nodes: dict[str, dict] = {}

    def walk(node: dict) -> None:
        nodes[str(node["id"])] = node
        for child in node.get("children") or []:
            walk(child)

    walk(tree)
    return nodes


def _manager(tmp_path: Path, requirement_path: Path) -> ARCWorkflowManager:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path=str(requirement_path),
        web_port=4100,
        log_cb=lambda *args, **kwargs: None,
    )


def test_keep_req2_holds_the_verbatim_req2_subtree() -> None:
    """ROOT wraps the REQ-2 subtree whole: ids, text and declared dependencies
    are byte-equal to the parent keep tree (the cross-tree REQ-1 edges stay,
    matching the parent's own silent-filter behaviour)."""
    sub_nodes = _nodes_by_id(load_requirements(KEEP_REQ2_YAML))
    full_nodes = _nodes_by_id(load_requirements(KEEP_YAML))

    expected = {"ROOT"} | set(_descendants(full_nodes, "REQ-2")) | {"REQ-2"}
    assert set(sub_nodes) == expected
    assert set(sub_nodes) == {"ROOT", "REQ-2"} | set(_descendants(sub_nodes, "REQ-2"))

    for node_id in sorted(expected - {"ROOT"}):
        sub_node, full_node = sub_nodes[node_id], full_nodes[node_id]
        for field in ("id", "name", "type", "description", "dependencies"):
            assert sub_node.get(field) == full_node.get(field), (node_id, field)
        assert (sub_node.get("scenarios") or []) == (full_node.get("scenarios") or []), node_id

    # The two edges pointing outside the slice are preserved verbatim. In the
    # parent tree they are real scheduling edges (REQ-1 exists there); in this
    # slice the queue builder's unknown-id rule silently filters them, which
    # is the fixture's intended degradation — no drop warning, same as any
    # other out-of-tree reference.
    assert sub_nodes["REQ-2"]["dependencies"] == ["REQ-1"]
    assert sub_nodes["REQ-2.1"]["dependencies"] == ["REQ-1.1"]


def _descendants(nodes: dict[str, dict], node_id: str) -> set[str]:
    out: set[str] = set()
    for child in nodes[node_id].get("children") or []:
        child_id = str(child["id"])
        out.add(child_id)
        out |= _descendants(nodes, child_id)
    return out


def test_keep_req2_queue_builds_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh queue builds on the real fixture: no unschedulable dependency
    drops (the parent tree has none either) and every subtree-internal
    declared edge survives scheduling."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")

    sub_tree = load_requirements(KEEP_REQ2_YAML)
    full_tree = load_requirements(KEEP_YAML)
    # Separate workspace dirs: the create path does not persist the queue
    # today, but the comparison must not lean on that implementation detail.
    queue = _manager(tmp_path / "sub", KEEP_REQ2_YAML)._load_or_create_processing_queue(sub_tree)
    full_queue = _manager(tmp_path / "full", KEEP_YAML)._load_or_create_processing_queue(full_tree)

    assert queue["dropped_dependency_edges"] == []
    assert full_queue["dropped_dependency_edges"] == []

    subtree_ids = set(queue["affinity"]) - {"ROOT"}
    expected_internal = {
        dependent: [dep for dep in deps if dep in subtree_ids]
        for dependent, deps in full_queue["dependencies"].items()
        if dependent in subtree_ids
    }
    # A node whose every declared edge points outside the slice (REQ-2 -> REQ-1,
    # REQ-2.1 -> REQ-1.1) contributes no entry, matching the map builder's
    # "no dependencies kept" rule for filtered-out edges.
    expected_internal = {k: v for k, v in expected_internal.items() if v}
    assert queue["dependencies"] == expected_internal
    # 31 nodes -> 62 DESIGN+IMPLEMENT tasks.
    assert len(queue["tasks"]) == 62
    assert sum(1 for task in queue["tasks"] if task["phase"] == PHASE_IMPLEMENT) == 31


def test_keep_req2_affinity_depth_two_splits_six_feature_subtrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lever's target shape on the real fixture: each of the six feature
    subtrees heads its own group, every descendant stays with its feature
    subtree, and the spine (ROOT/REQ-2/REQ-2.1/REQ-2.2) groups individually."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")

    tree = load_requirements(KEEP_REQ2_YAML)
    queue = _manager(tmp_path, KEEP_REQ2_YAML)._load_or_create_processing_queue(tree)
    affinity: dict[str, str] = queue["affinity"]

    groups: dict[str, set[str]] = {}
    for node_id, group in affinity.items():
        groups.setdefault(group, set()).add(node_id)

    for feature in FEATURE_SUBTREES:
        assert affinity[feature] == feature, f"{feature} must head its own group"
        assert groups[feature] == set(_descendants(_nodes_by_id(tree), feature)) | {feature}
    for spine in ("ROOT", "REQ-2", "REQ-2.1", "REQ-2.2"):
        assert groups[spine] == {spine}
    assert len(groups) == 10


def test_keep_req2_default_depth_reproduces_the_serial_pathology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the lever the fixture keeps the motivating pathology: the whole
    REQ-2 subtree serializes in one affinity group holding 60 of the 62 tasks.
    The fixture must not drift into a shape where the default stops
    exhibiting what the redesign is fixing."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.delenv("ARC_AFFINITY_DEPTH", raising=False)

    tree = load_requirements(KEEP_REQ2_YAML)
    queue = _manager(tmp_path, KEEP_REQ2_YAML)._load_or_create_processing_queue(tree)
    affinity = queue["affinity"]

    assert set(affinity.values()) == {"ROOT", "REQ-2"}
    req2_group_tasks = [
        task
        for task in queue["tasks"]
        if affinity.get(task["node_id"], task["node_id"]) == "REQ-2"
    ]
    assert len(req2_group_tasks) == 60
    assert len(queue["tasks"]) == 62
    # All tasks start pending: nothing about the fixture pre-satisfies the queue.
    assert all(task["status"] == TASK_PENDING for task in queue["tasks"])


def _leaf_ids(tree: dict) -> set[str]:
    leaves: set[str] = set()

    def walk(node: dict) -> None:
        children = node.get("children") or []
        if not children:
            leaves.add(str(node["id"]))
        for child in children:
            walk(child)

    for child in tree.get("children") or []:
        walk(child)
    return leaves


def test_keep_req2_tests_and_reference_are_scoped_to_the_subtree() -> None:
    """tests/ carries exactly one spec per atomic leaf of the REQ-2 subtree
    (plus the shared helpers) and reference/ only the images the subtree's
    descriptions name."""
    base = REPO_ROOT / "arc-bench-test" / "keep-req2"

    sub_tree = load_requirements(KEEP_REQ2_YAML)
    expected_specs = {f"{node_id}.spec.ts" for node_id in _leaf_ids(sub_tree)}
    spec_files = {p.name for p in (base / "tests").glob("*.spec.ts")}
    assert spec_files == expected_specs
    assert (base / "tests" / "helpers.ts").is_file()

    sub_nodes = _nodes_by_id(sub_tree)
    referenced = set()
    for node in sub_nodes.values():
        referenced.update(re.findall(r"reference/([a-z_]+\.png)", str(node.get("description") or "")))
    present = {p.name for p in (base / "requirements" / "reference").glob("*.png")}
    # label_filtered_list.png is referenced by REQ-2.7.6 but absent from the
    # parent fixture's disk too — a pre-existing upstream gap kept as-is.
    assert present == referenced - {"label_filtered_list.png"}
    assert all(
        ((REPO_ROOT / "arc-bench-test" / "keep" / "requirements" / "reference" / name).is_file())
        for name in present
    )

"""The parallel worktree drain must isolate tasks and integrate cleanly.

These tests drive ARCWorkflowManager's parallel drain against real git
worktrees with a stubbed phase execution (no models, no npm). They lock the
contract required before concurrent tasks may run:

- sibling IMPLEMENT tasks run concurrently, each in its own worktree with its
  own web port;
- every merged branch lands in the integration workspace;
- a merge conflict fails exactly the conflicting node and leaves the
  integration workspace (and the other node) untouched;
- a parent's IMPLEMENT waits for all descendant IMPLEMENTs;
- parallel mode is the default; with ARC_NODE_WORKTREES=0 the drain stays
  strictly serial.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.scheduling import task_dependencies_met
from core.workflow import (
    ARCWorkflowManager,
    NODE_BLOCKED_BY_DEPENDENCY,
    NODE_DESIGNED,
    NODE_DESIGNING,
    NODE_FAILED,
    NODE_PASSED,
    NODE_UNSEEN,
    PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
)
from tests.test_workflow.queue_faker import node_maps_from_tasks, settle


class _Traceability:
    def __init__(self, node_ids: list[str]) -> None:
        self.requirements = {
            node_id: {"id": node_id, "name": node_id, "description": "req"}
            for node_id in node_ids
        }
        self.states: dict[str, str] = {}
        self.cleared_design_artifacts: list[str] = []
        self.reset_test_statuses: list[str] = []

    def get_requirement(self, node_id: str) -> dict[str, Any] | None:
        return self.requirements.get(node_id)

    def upsert_node_state(self, node_id: str, state: str) -> None:
        self.states[node_id] = state

    def clear_node_design_artifacts(self, node_id: str) -> None:
        self.cleared_design_artifacts.append(node_id)

    def reset_test_pass_statuses_for_requirement(self, node_id: str) -> None:
        self.reset_test_statuses.append(node_id)


class _Events:
    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            return None

        return record


class _Git:
    def commit(self, message: str) -> bool:
        return False


def _requirement_tree() -> dict[str, Any]:
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [
            {"id": "RA", "name": "leaf a", "description": "a", "children": []},
            {"id": "RB", "name": "leaf b", "description": "b", "children": []},
        ],
    }


def _make_parallel_manager(
    tmp_path: Path, max_tasks: int = 2, node_ids: list[str] | None = None
) -> ARCWorkflowManager:
    workspace = tmp_path / "workspace"
    # The drain opens real git worktrees, so the workspace needs a repo with
    # the runtime's managed ignore block (worktrees live under .arc/worktrees).
    import subprocess

    workspace.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "test@example.com"], ["config", "user.name", "test"]):
        subprocess.run(["git", *args], cwd=str(workspace), check=True, capture_output=True)
    (workspace / ".gitignore").write_text(
        "# >>> arcbench-agent-runtime >>>\n.arc/*\n!.arc/traceability/\n!.arc/traceability/**\n# <<< arcbench-agent-runtime <<<\n",
        encoding="utf-8",
    )
    (workspace / "backend").mkdir()
    (workspace / "frontend").mkdir()
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(workspace), check=True, capture_output=True)

    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(
        traceability=_Traceability(node_ids or ["R", "RA", "RB"]),
        events=_Events(),
        git=_Git(),
    )
    return manager


def _queue_state(manager: ARCWorkflowManager, tree: dict[str, Any]) -> dict[str, Any]:
    return manager._load_or_create_processing_queue(tree)


def test_sibling_implements_run_concurrently_in_separate_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())
    # Mark all DESIGN tasks done so the leaf IMPLEMENTs are runnable.
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
            queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
            task["status"] = TASK_COMPLETED

    active: set[str] = set()
    seen_ports: dict[str, int | None] = {}
    seen_worktrees: dict[str, str] = {}
    overlap = False

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        nonlocal overlap
        assert active.isdisjoint({task["task_id"]})
        if active:
            overlap = True
        active.add(task["task_id"])
        seen_ports[task["node_id"]] = ctx.web_port if ctx else None
        if ctx is not None:
            seen_worktrees[task["node_id"]] = ctx.handle.path
            # Simulate each agent writing a node-private file in its worktree.
            Path(ctx.handle.path, f"{task['node_id']}.feature.js").write_text(
                f"feature {task['node_id']};\n", encoding="utf-8"
            )
        await asyncio.sleep(0.05)
        active.discard(task["task_id"])
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)

    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert overlap, "sibling IMPLEMENT tasks must overlap"
    assert seen_ports["RA"] == 4001 and seen_ports["RB"] == 4002, "distinct port slots"
    assert seen_worktrees["RA"] != seen_worktrees["RB"], "distinct worktrees"
    workspace = Path(manager.workspace_path)
    assert (workspace / "RA.feature.js").exists(), "RA merged into the integration workspace"
    assert (workspace / "RB.feature.js").exists(), "RB merged into the integration workspace"
    assert not list((workspace / ".arc" / "worktrees").iterdir()), "worktrees removed after integration"
    assert queue_state["node_states"]["RA"] == NODE_PASSED
    assert queue_state["node_states"]["RB"] == NODE_PASSED
    assert manager._port_slots == {}, "port slots released"


def test_parent_implement_waits_for_descendant_implements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][0]["children"] = [
        {"id": "RA1", "name": "grandchild", "description": "a1", "children": []}
    ]
    queue_state = _queue_state(manager, tree)
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
            queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
            task["status"] = TASK_COMPLETED

    finished_implements: list[str] = []
    events: list[tuple[str, str]] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        events.append(("start", task["task_id"]))
        if task["phase"] == PHASE_IMPLEMENT:
            await asyncio.sleep(0.02)
            finished_implements.append(task["node_id"])
        events.append(("end", task["task_id"]))
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    order = [task_id for kind, task_id in events if kind == "start"]
    assert order.index("R:IMPLEMENT") > order.index("RA:IMPLEMENT")
    assert order.index("R:IMPLEMENT") > order.index("RB:IMPLEMENT")
    # The parent must start only after every descendant implement finished.
    parent_start = events.index(("start", "R:IMPLEMENT"))
    for node_id in ("RA", "RB", "RA1"):
        assert events.index(("end", f"{node_id}:IMPLEMENT")) < parent_start


def test_implement_waits_for_declared_dependency_in_the_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node whose requirement declares a dependency implements only after the
    dependency's IMPLEMENT finished: its scenarios may read runtime state the
    dependency creates (run6's login node needs the account REQ-1 registers)."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][1]["dependencies"] = ["RA"]
    queue_state = _queue_state(manager, tree)
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
            queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
            task["status"] = TASK_COMPLETED

    events: list[tuple[str, str]] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        events.append(("start", task["task_id"]))
        await asyncio.sleep(0.01)
        events.append(("end", task["task_id"]))
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert events.index(("start", "RB:IMPLEMENT")) > events.index(("end", "RA:IMPLEMENT"))
    assert queue_state["node_states"]["RA"] == NODE_PASSED
    assert queue_state["node_states"]["RB"] == NODE_PASSED


def test_declared_dependency_cycle_is_broken_and_the_drain_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cyclic dependency must not deadlock the drain: the closing edge is
    dropped (and reported) instead of leaving both tasks PENDING forever."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][0]["dependencies"] = ["RB"]
    tree["children"][1]["dependencies"] = ["RA"]
    queue_state = _queue_state(manager, tree)

    assert queue_state["dependencies"] == {"RA": ["RB"]}
    assert queue_state["dropped_dependency_edges"] == [("RB", "RA", "cycle")]

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])


def test_design_waits_for_declared_dependency_in_the_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run7's parallel failure mode: the login node's DESIGN ran beside the
    registration node's IMPLEMENT and both wrote their own auth routes. A
    dependent node's DESIGN now starts only after the dependency's IMPLEMENT
    has finished - i.e. after its merge - so it designs against the merged
    integration HEAD and reuses the dependency's surfaces (run8's serial
    semantics, guaranteed under parallel draining)."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][1]["dependencies"] = ["RA"]
    queue_state = _queue_state(manager, tree)

    events: list[tuple[str, str]] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        events.append(("start", task["task_id"]))
        await asyncio.sleep(0.01)
        events.append(("end", task["task_id"]))
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert events.index(("start", "RB:DESIGN")) > events.index(("end", "RA:IMPLEMENT"))
    assert all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])


def test_ancestor_dependency_edge_is_dropped_and_the_drain_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child declaring a dependency on its own parent (or the reverse)
    would deadlock the drain: the parent-child rules already sequence the
    pair and the dependency gate adds the reverse wait. The edge is dropped
    with a report instead of leaving the tasks PENDING forever."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][0]["children"] = [
        {"id": "RA1", "name": "grandchild", "description": "a1", "children": []}
    ]
    tree["children"][0]["dependencies"] = ["RA1"]  # parent depends on its own child
    queue_state = _queue_state(manager, tree)

    assert queue_state["dependencies"] == {}
    assert queue_state["dropped_dependency_edges"] == [("RA", "RA1", "ancestor-descendant")]

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])


def test_uncle_nephew_dependency_cycle_is_broken_and_the_drain_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared cycle that closes only through the parent-child rules: C
    depends on B while B's own child A depends on C. Node-level cycle
    checking does not see it, but with DESIGN gating it deadlocks the drain
    (D:C waits for I:B which waits for I:A which waits for I:C); the closing
    edge is dropped with a report."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][0]["children"] = [
        {"id": "RA1", "name": "grandchild", "description": "a1", "children": []}
    ]
    tree["children"][0]["dependencies"] = ["RB"]  # B (RA) depends on C (RB)
    tree["children"][1]["dependencies"] = ["RA1"]  # C (RB) depends on A (RA1, B's child)
    queue_state = _queue_state(manager, tree)

    assert queue_state["dependencies"] == {"RA": ["RB"]}
    assert queue_state["dropped_dependency_edges"] == [("RB", "RA1", "cycle")]

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])


def test_resumed_queue_with_dangling_dependency_edge_is_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored queue whose dependency map references a node this queue
    cannot schedule (hand-edited or foreign file) must drop that edge: the
    gate blocks on a dependency without an IMPLEMENT task, so keeping it would
    leave the dependent PENDING forever with no failure to report."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    queue_state = _queue_state(manager, tree)
    legacy_queue = {**queue_state, "dependencies": {"RB": ["RA", "RGHOST"], "RGHOST": ["RA"]}}
    legacy_queue["dropped_dependency_edges"] = []
    Path(manager.queue_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manager.queue_path).write_text(json.dumps(legacy_queue), encoding="utf-8")

    resumed = manager._load_or_create_processing_queue(tree, require_compatible_existing_queue=True)

    assert resumed["dependencies"] == {"RB": ["RA"]}
    assert sorted(resumed["dropped_dependency_edges"]) == [
        ("RB", "RGHOST", "no-implement-task"),
        ("RGHOST", "", "no-implement-task"),
    ]

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(resumed))

    assert all(task["status"] == TASK_COMPLETED for task in resumed["tasks"])


def test_resumed_queue_without_dependency_map_still_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queues saved before dependency gating lack the map; the resumed run
    rebuilds it from the tree instead of failing the compatibility check."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][1]["dependencies"] = ["RA"]
    queue_state = _queue_state(manager, tree)
    legacy_queue = {**queue_state}
    legacy_queue.pop("dependencies")
    legacy_queue.pop("dropped_dependency_edges")
    Path(manager.queue_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manager.queue_path).write_text(json.dumps(legacy_queue), encoding="utf-8")

    resumed = manager._load_or_create_processing_queue(tree, require_compatible_existing_queue=True)

    assert resumed["dependencies"] == {"RB": ["RA"]}
    assert resumed["dropped_dependency_edges"] == []
    assert resumed["tasks"][0]["task_id"] == "R:DESIGN"


def test_merge_conflict_fails_only_the_conflicting_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal IMPLEMENT merge conflict (the one-shot requeue budget
    already burned by an earlier round, e.g. after a resume) fails exactly
    the conflicting node and leaves the integration workspace (and the
    other node) untouched."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
            queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
            task["status"] = TASK_COMPLETED
    # Burn both leaves' one-shot conflict requeue budget so the first
    # conflict below is terminal for whichever node loses the race.
    for node_id in ("RA", "RB"):
        sessions.merge_node_session(node_id, {"merge_conflict_retry_used": True})

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        if ctx is not None and task["node_id"] in {"RA", "RB"}:
            # Both leaves write the same integration file -> guaranteed conflict.
            Path(ctx.handle.path, "shared.js").write_text(
                f"from {task['node_id']};\n", encoding="utf-8"
            )
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    conflicting = [node for node in ("RA", "RB") if states[node] == NODE_FAILED]
    winner = [node for node in ("RA", "RB") if states[node] != NODE_FAILED]
    assert len(conflicting) == 1 and len(winner) == 1, f"exactly one node loses: {states}"
    workspace = Path(manager.workspace_path)
    assert (workspace / "shared.js").exists(), "the winning node's work must be integrated"
    assert (workspace / "shared.js").read_text(encoding="utf-8") == f"from {winner[0]};\n"
    # The conflicting node's worktree is preserved for inspection.
    preserved = list((workspace / ".arc" / "worktrees").iterdir())
    assert len(preserved) == 1 and conflicting[0] in preserved[0].name


def test_parallel_mode_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_NODE_WORKTREES", raising=False)
    manager = _make_parallel_manager(tmp_path)

    assert manager._parallel_mode is True
    assert manager._max_concurrent_tasks() == PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS


def test_serial_drain_with_worktrees_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "0")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())

    running = False

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        nonlocal running
        assert not running, "serial drain must never overlap tasks"
        running = True
        await asyncio.sleep(0.01)
        running = False
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert manager._max_concurrent_tasks() == 1


def test_task_workspace_failure_fails_the_node_without_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())
    design_task = next(t for t in queue_state["tasks"] if t["task_id"] == "RA:DESIGN")

    def broken_prepare(node_id: str, group_key: str | None = None):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(manager._worktree_manager, "prepare", broken_prepare)
    ran = False

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        nonlocal ran
        ran = True
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._execute_task(design_task, queue_state))

    assert ran is False, "the phase must not run without its isolated workspace"
    assert queue_state["node_states"]["RA"] == NODE_FAILED
    assert design_task["status"] == TASK_FAILED
    assert manager._port_slots == {}


def test_failed_phase_is_not_integrated_and_worktree_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())
    task = next(task for task in queue_state["tasks"] if task["task_id"] == "RA:DESIGN")

    async def failed_phase(current_task: dict[str, Any], ctx: Any = None) -> bool:
        assert ctx is not None
        Path(ctx.handle.path, "failed-design.js").write_text("unaccepted;\n", encoding="utf-8")
        return False

    monkeypatch.setattr(manager, "_run_task", failed_phase)
    asyncio.run(manager._execute_task(task, queue_state))

    workspace = Path(manager.workspace_path)
    assert task["status"] == TASK_FAILED
    assert queue_state["node_states"]["RA"] == NODE_FAILED
    assert not (workspace / "failed-design.js").exists()
    preserved = list((workspace / ".arc" / "worktrees").iterdir())
    assert len(preserved) == 1 and "RA" in preserved[0].name


def test_failed_dependency_blocks_only_declared_dependents(tmp_path: Path) -> None:
    manager = _make_parallel_manager(tmp_path)
    state = {
        "tasks": [
            {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_FAILED},
            {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_PENDING},
            {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
            {"task_id": "RC:DESIGN", "node_id": "RC", "phase": PHASE_DESIGN, "status": TASK_PENDING},
            {"task_id": "RC:IMPLEMENT", "node_id": "RC", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
        ],
        "node_states": {"RA": NODE_FAILED, "RB": "UNSEEN", "RC": "UNSEEN"},
        "dependencies": {"RB": ["RA"]},
    }
    manager.runtime.traceability.requirements["RC"] = {
        "id": "RC",
        "name": "RC",
        "description": "req",
    }

    asyncio.run(manager._propagate_dependency_blocks(state))

    rb_tasks = [task for task in state["tasks"] if task["node_id"] == "RB"]
    rc_tasks = [task for task in state["tasks"] if task["node_id"] == "RC"]
    assert all(task["status"] == TASK_BLOCKED for task in rb_tasks)
    assert state["node_states"]["RB"] == NODE_BLOCKED_BY_DEPENDENCY
    assert all(task["status"] == TASK_PENDING for task in rc_tasks)
    assert state["node_states"]["RC"] == "UNSEEN"


def test_dependency_block_propagation_is_idempotent_and_saves_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain calls propagation before every pick, so it must converge.

    A second call over already-blocked state must not re-mark anything or
    touch disk: only a state change saves the queue. A node with in-flight
    work is left untouched - blocking a running task cannot stop it.
    """
    manager = _make_parallel_manager(tmp_path)
    state = {
        "tasks": [
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_FAILED},
            {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_PENDING},
            {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
            {"task_id": "RC:DESIGN", "node_id": "RC", "phase": PHASE_DESIGN, "status": TASK_RUNNING},
            {"task_id": "RC:IMPLEMENT", "node_id": "RC", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
        ],
        "node_states": {"RA": NODE_FAILED, "RB": "UNSEEN", "RC": NODE_DESIGNING},
        "node_design_done": {"RA": False, "RB": False, "RC": False},
        "dependencies": {"RB": ["RA"], "RC": ["RA"]},
    }
    saves: list[int] = []
    original_save = manager._save_processing_queue

    def counting_save(queue_state: dict[str, Any]) -> None:
        saves.append(len(queue_state["tasks"]))
        original_save(queue_state)

    monkeypatch.setattr(manager, "_save_processing_queue", counting_save)

    asyncio.run(manager._propagate_dependency_blocks(state))
    assert len(saves) == 1
    assert [task["status"] for task in state["tasks"]] == [
        TASK_FAILED,
        TASK_BLOCKED,
        TASK_BLOCKED,
        TASK_RUNNING,
        TASK_PENDING,
    ], "RB is blocked; RC keeps its running task and is skipped"
    assert state["node_states"]["RB"] == NODE_BLOCKED_BY_DEPENDENCY
    assert state["node_states"]["RC"] == NODE_DESIGNING

    asyncio.run(manager._propagate_dependency_blocks(state))
    assert len(saves) == 1, "a second pass over blocked state must not save again"
    assert [task["status"] for task in state["tasks"]] == [
        TASK_FAILED,
        TASK_BLOCKED,
        TASK_BLOCKED,
        TASK_RUNNING,
        TASK_PENDING,
    ]

    # Once RC's running task ends, the next pass still blocks its pending work.
    state["node_states"]["RC"] = NODE_DESIGNED
    state["node_design_done"]["RC"] = True
    state["tasks"][3]["status"] = TASK_COMPLETED
    asyncio.run(manager._propagate_dependency_blocks(state))
    assert [task["status"] for task in state["tasks"]] == [
        TASK_FAILED,
        TASK_BLOCKED,
        TASK_BLOCKED,
        TASK_COMPLETED,
        TASK_BLOCKED,
    ]
    assert state["node_states"]["RC"] == NODE_BLOCKED_BY_DEPENDENCY
    assert len(saves) == 2


def test_failed_child_blocks_parent_implementation(tmp_path: Path) -> None:
    manager = _make_parallel_manager(tmp_path)
    state = {
        "tasks": [
            {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_FAILED},
            {"task_id": "R:IMPLEMENT", "node_id": "R", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
        ],
        "node_states": {"R": "DESIGNED", "RA": NODE_FAILED},
        "descendants": {"R": ["RA"]},
        "dependencies": {},
    }

    asyncio.run(manager._propagate_dependency_blocks(state))

    assert state["tasks"][2]["status"] == TASK_BLOCKED
    assert state["node_states"]["R"] == NODE_BLOCKED_BY_DEPENDENCY


# ---------------------------------------------------------------------------
# releasing blocked dependents after a retry reset (the mirror of propagation)
# ---------------------------------------------------------------------------


def _blocked_state() -> dict[str, Any]:
    """Queue state reproducing the 2026-09-20 test1 failure shape.

    RA's IMPLEMENT failed, so propagation blocked RB (declared dependent) and
    R (parent waiting on RA's IMPLEMENT). This is the state the run ended in
    before the auto TDD retry reset RA.
    """
    return {
        "tasks": [
            {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "R:IMPLEMENT", "node_id": "R", "phase": PHASE_IMPLEMENT, "status": TASK_BLOCKED},
            {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_FAILED},
            {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_BLOCKED},
            {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_BLOCKED},
        ],
        "node_states": {"R": NODE_BLOCKED_BY_DEPENDENCY, "RA": NODE_FAILED, "RB": NODE_BLOCKED_BY_DEPENDENCY},
        # R and RA got past their DESIGN before blocking/failing; the typed
        # state keeps that progress explicit so a release restores DESIGNED.
        "node_design_done": {"R": True, "RA": True, "RB": False},
        "descendants": {"R": ["RA", "RB"]},
        "dependencies": {"RB": ["RA"]},
    }


def test_release_returns_blocked_dependents_to_pending_after_retry_reset(
    tmp_path: Path,
) -> None:
    """A retried dependency must un-block its dependents (2026-09-20 test1).

    The run passed REQ-1's auto TDD retry yet finished "blocked: REQ-2, ROOT"
    because propagation is one-way: once marked, BLOCKED tasks were never
    schedulable again. Releasing after the reset restores both the declared
    dependent and the waiting parent.
    """
    manager = _make_parallel_manager(tmp_path)
    state = _blocked_state()
    # The retry reset RA the way an implement retry does: back to DESIGNED.
    state["tasks"][3]["status"] = TASK_PENDING
    state["node_states"]["RA"] = NODE_DESIGNED
    state.setdefault("node_design_done", {})["RA"] = True

    released = asyncio.run(manager._release_dependency_blocks(state))

    assert set(released) == {"R", "RB"}
    tasks = {task["task_id"]: task["status"] for task in state["tasks"]}
    assert tasks["R:IMPLEMENT"] == TASK_PENDING
    assert tasks["RB:DESIGN"] == TASK_PENDING
    assert tasks["RB:IMPLEMENT"] == TASK_PENDING
    # A node whose DESIGN already completed resumes as DESIGNED, not UNSEEN,
    # so the queue node state stays consistent with its task statuses.
    assert state["node_states"]["R"] == NODE_DESIGNED
    assert state["node_states"]["RB"] == NODE_UNSEEN


def test_release_is_transitive_through_blocked_intermediaries(tmp_path: Path) -> None:
    """BLOCKED propagates transitively, so releasing must iterate to a fixpoint.

    RA failed -> RB blocked (declared) -> RC blocked through RB. Resetting RA
    alone must release both RB and RC regardless of dict iteration order.
    """
    manager = _make_parallel_manager(tmp_path)
    state = {
        "tasks": [
            {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
            {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_BLOCKED},
            {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_BLOCKED},
            {"task_id": "RC:DESIGN", "node_id": "RC", "phase": PHASE_DESIGN, "status": TASK_BLOCKED},
            {"task_id": "RC:IMPLEMENT", "node_id": "RC", "phase": PHASE_IMPLEMENT, "status": TASK_BLOCKED},
        ],
        "node_states": {"RA": NODE_DESIGNED, "RB": NODE_BLOCKED_BY_DEPENDENCY, "RC": NODE_BLOCKED_BY_DEPENDENCY},
        "dependencies": {"RB": ["RA"], "RC": ["RB"]},
    }

    released = asyncio.run(manager._release_dependency_blocks(state))

    assert set(released) == {"RB", "RC"}
    assert all(
        task["status"] == TASK_PENDING
        for task in state["tasks"]
        if task["node_id"] in {"RB", "RC"}
    )


def test_release_keeps_nodes_blocked_behind_still_failed_prerequisites(
    tmp_path: Path,
) -> None:
    """Releasing one failed node must not release its still-failed siblings.

    RD stays failed, so RE (its declared dependent) keeps its BLOCKED state
    even though RA was reset and RB was released.
    """
    manager = _make_parallel_manager(tmp_path)
    state = {
        "tasks": [
            {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
            {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_BLOCKED},
            {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_BLOCKED},
            {"task_id": "RD:DESIGN", "node_id": "RD", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RD:IMPLEMENT", "node_id": "RD", "phase": PHASE_IMPLEMENT, "status": TASK_FAILED},
            {"task_id": "RE:DESIGN", "node_id": "RE", "phase": PHASE_DESIGN, "status": TASK_BLOCKED},
            {"task_id": "RE:IMPLEMENT", "node_id": "RE", "phase": PHASE_IMPLEMENT, "status": TASK_BLOCKED},
        ],
        "node_states": {
            "RA": NODE_DESIGNED,
            "RB": NODE_BLOCKED_BY_DEPENDENCY,
            "RD": NODE_FAILED,
            "RE": NODE_BLOCKED_BY_DEPENDENCY,
        },
        "dependencies": {"RB": ["RA"], "RE": ["RD"]},
    }

    released = asyncio.run(manager._release_dependency_blocks(state))

    assert released == ["RB"]
    tasks = {task["task_id"]: task["status"] for task in state["tasks"]}
    assert tasks["RE:DESIGN"] == TASK_BLOCKED
    assert tasks["RE:IMPLEMENT"] == TASK_BLOCKED
    assert state["node_states"]["RE"] == NODE_BLOCKED_BY_DEPENDENCY


def test_release_holds_nested_ancestor_until_every_failed_descendant_is_reset(
    tmp_path: Path,
) -> None:
    """A blocked ancestor waits on ALL failed descendants, not just the deepest.

    Ancestor blocking propagates through ``descendants`` (R waits for RA and
    RA1's IMPLEMENTs), and release checks the same map through the same
    ``failed_prerequisite_ids`` helper, so the two are mirror images: while
    any failed descendant remains — here the middle layer RA after only the
    innermost RA1 was retried — the ancestor keeps its BLOCKED state, and it
    is released only once every failed descendant under it has been reset.
    """
    manager = _make_parallel_manager(tmp_path)
    state = {
        "tasks": [
            {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "R:IMPLEMENT", "node_id": "R", "phase": PHASE_IMPLEMENT, "status": TASK_BLOCKED},
            {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_FAILED},
            {"task_id": "RA1:DESIGN", "node_id": "RA1", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA1:IMPLEMENT", "node_id": "RA1", "phase": PHASE_IMPLEMENT, "status": TASK_FAILED},
        ],
        "node_states": {
            "R": NODE_BLOCKED_BY_DEPENDENCY,
            "RA": NODE_FAILED,
            "RA1": NODE_FAILED,
        },
        "node_design_done": {"R": True, "RA": True, "RA1": True},
        "descendants": {"R": ["RA", "RA1"], "RA": ["RA1"]},
        "dependencies": {},
    }

    # Only the innermost failed descendant is retried; the middle layer RA is
    # still failed, so the ancestor must stay blocked.
    state["tasks"][5]["status"] = TASK_PENDING
    state["node_states"]["RA1"] = NODE_DESIGNED
    state.setdefault("node_design_done", {})["RA1"] = True
    released = asyncio.run(manager._release_dependency_blocks(state))
    assert released == [], "R stays blocked while the middle descendant RA is still failed"
    assert state["tasks"][1]["status"] == TASK_BLOCKED
    assert state["node_states"]["R"] == NODE_BLOCKED_BY_DEPENDENCY

    # Resetting the middle layer too releases the ancestor.
    state["tasks"][3]["status"] = TASK_PENDING
    state["node_states"]["RA"] = NODE_DESIGNED
    state["node_design_done"]["RA"] = True
    released = asyncio.run(manager._release_dependency_blocks(state))
    assert released == ["R"]
    assert state["tasks"][1]["status"] == TASK_PENDING
    assert state["node_states"]["R"] == NODE_DESIGNED


def test_released_block_is_reblocked_when_the_retry_fails_again(
    tmp_path: Path,
) -> None:
    """Releasing must be safe to undo: propagation re-blocks on the next pick.

    The drain runs propagation before every task pick, so a retried node that
    fails again re-blocks its dependents exactly as the first failure did.
    """
    manager = _make_parallel_manager(tmp_path)
    state = {
        "tasks": [
            {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
            {"task_id": "RA:IMPLEMENT", "node_id": "RA", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
            {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_PENDING},
            {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_PENDING},
        ],
        "node_states": {"RA": NODE_DESIGNED, "RB": NODE_UNSEEN},
        "dependencies": {"RB": ["RA"]},
    }
    asyncio.run(manager._release_dependency_blocks(state))

    # The retry fails again.
    state["tasks"][1]["status"] = TASK_FAILED
    state["node_states"]["RA"] = NODE_FAILED
    asyncio.run(manager._propagate_dependency_blocks(state))

    tasks = {task["task_id"]: task["status"] for task in state["tasks"]}
    assert tasks["RB:DESIGN"] == TASK_BLOCKED
    assert tasks["RB:IMPLEMENT"] == TASK_BLOCKED
    assert state["node_states"]["RB"] == NODE_BLOCKED_BY_DEPENDENCY


def test_auto_tdd_retry_releases_blocked_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-run auto retry path must release blocks it un-fails.

    This is the integration path from the 2026-09-20 test1 run: the first
    drain failed RA and blocked RB/R; the auto TDD retry scanned
    runner-events for RA's test/failed, reset it, and the second drain must
    now see RB/R as schedulable instead of finishing "blocked".
    """
    manager = _make_parallel_manager(tmp_path)
    state = _blocked_state()
    # The fake runtime has no paths object; point the retry scan at a real
    # events file under the workspace so _prepare_auto_tdd_retry can read it.
    events_path = Path(manager.workspace_path) / ".arc" / "runner-events.jsonl"
    manager.runtime.paths = SimpleNamespace(runner_events_path=events_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(
            {
                "type": "requirement_state",
                "node_id": "RA",
                "phase": "test",
                "status": "failed",
                "message": "Unit: sessionService failed",
                "timestamp": "2026-09-20 06:19:38",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    retried = asyncio.run(manager._prepare_auto_tdd_retry(state))

    assert retried == ["RA"]
    tasks = {task["task_id"]: task["status"] for task in state["tasks"]}
    assert tasks["R:IMPLEMENT"] == TASK_PENDING, "the waiting parent is schedulable again"
    assert tasks["RB:DESIGN"] == TASK_PENDING, "the declared dependent is schedulable again"
    assert tasks["RB:IMPLEMENT"] == TASK_PENDING
    assert state["node_states"]["R"] == NODE_DESIGNED
    assert state["node_states"]["RB"] == NODE_UNSEEN


def test_auto_tdd_retry_reprompt_carries_previous_attempt_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry reprompt must quote the failed attempt's objective numbers
    (issue #219): model calls (≈ steps), read-only calls, run_tests calls and
    the zero-writes fact, aggregated from the per-call event streams that
    survive even a GraphRecursionError crash (which skips the tdd_handoff
    write). The recorded cursor must fence the attempt so a later auto retry
    (resume after the retry failed again) measures only the newest one.
    """
    from core import sessions

    manager = _make_parallel_manager(tmp_path)
    state = _blocked_state()
    events_path = Path(manager.workspace_path) / ".arc" / "runner-events.jsonl"
    manager.runtime.paths = SimpleNamespace(runner_events_path=events_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)

    def _row(record: dict[str, Any]) -> str:
        return json.dumps({"timestamp": "2026-09-23 06:00:00", **record}) + "\n"

    rows = [
        {"type": "llm_usage", "node_id": "RA", "phase": "IMPLEMENT"},
        {"type": "llm_usage", "node_id": "RA", "phase": "IMPLEMENT"},
        {"type": "llm_usage", "node_id": "RA", "phase": "IMPLEMENT"},
        {"type": "tool_usage", "node_id": "RA", "phase": "IMPLEMENT", "tool": "grep", "status": "ok"},
        {"type": "tool_usage", "node_id": "RA", "phase": "IMPLEMENT", "tool": "grep", "status": "ok"},
        {"type": "tool_usage", "node_id": "RA", "phase": "IMPLEMENT", "tool": "run_tests", "status": "ok"},
        {"type": "tool_usage", "node_id": "RA", "phase": "IMPLEMENT", "tool": "write_file", "status": "blocked"},
        {
            "type": "requirement_state",
            "node_id": "RA",
            "phase": "test",
            "status": "failed",
            "message": "Unit: sessionService failed",
        },
    ]
    events_path.write_text("".join(_row(row) for row in rows), encoding="utf-8")

    assert asyncio.run(manager._prepare_auto_tdd_retry(state)) == ["RA"]

    summary = sessions.load_node_session("RA")["recent_failure_summary"]
    assert "3 model calls" in summary
    assert "4 tool calls" in summary
    assert "2 read-only" in summary
    assert "1 run_tests call" in summary
    assert "NO successful file edits" in summary
    cursor = sessions.load_node_session("RA")["tdd_retry_events_cursor"]
    assert cursor == len(rows)

    # A later auto retry (resume after the retry failed again) measures only
    # the newest attempt: the cursor fences attempt 1's events.
    state["node_states"]["RA"] = NODE_FAILED
    next(task for task in state["tasks"] if task["task_id"] == "RA:IMPLEMENT")["status"] = TASK_FAILED
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(_row({"type": "llm_usage", "node_id": "RA", "phase": "IMPLEMENT"}))
    assert asyncio.run(manager._prepare_auto_tdd_retry(state)) == ["RA"]
    second = sessions.load_node_session("RA")["recent_failure_summary"]
    assert "1 model call," in second
    assert "3 model calls" not in second
    assert sessions.load_node_session("RA")["tdd_retry_events_cursor"] == len(rows) + 1


def test_subtree_tasks_share_one_worktree_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive tasks of one top-level subtree reuse one worktree directory
    (sequential inside the subtree), while different subtrees stay isolated."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    tree = _requirement_tree()
    tree["children"][0]["children"] = [
        {"id": "RA1", "name": "grandchild", "description": "a1", "children": []}
    ]
    queue_state = _queue_state(manager, tree)
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
            queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
            task["status"] = TASK_COMPLETED

    seen_worktrees: dict[str, str] = {}

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        if ctx is not None:
            seen_worktrees[task["node_id"]] = ctx.handle.path
            Path(ctx.handle.path, f"{task['node_id']}.feature.js").write_text(
                f"feature {task['node_id']};\n", encoding="utf-8"
            )
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert seen_worktrees["RA"] == seen_worktrees["RA1"], "same subtree, same worktree"
    assert seen_worktrees["RA"] != seen_worktrees["RB"], "different subtrees stay isolated"
    assert queue_state["node_states"]["RA"] == NODE_PASSED
    assert queue_state["node_states"]["RA1"] == NODE_PASSED
    assert queue_state["node_states"]["RB"] == NODE_PASSED
    assert not list((Path(manager.workspace_path) / ".arc" / "worktrees").iterdir()), (
        "reusable worktrees are cleaned up after the drain"
    )


def _append_to_workspace_file(workspace: Path) -> None:
    """Commit a shared glue file so leaf appends produce a resolvable conflict."""
    import subprocess

    glue = workspace / "backend" / "glue.js"
    glue.write_text("// registry\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "add glue"], cwd=str(workspace), check=True, capture_output=True)


def test_additive_resolution_with_failing_health_gate_fails_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Append-only conflicts are resolved mechanically, but an unhealthy
    post-merge verification must abort the merge and fail the node."""
    import app_type_handler.web as web

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    _append_to_workspace_file(Path(manager.workspace_path))
    queue_state = _queue_state(manager, _requirement_tree())
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
            queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
            task["status"] = TASK_COMPLETED

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        if ctx is not None and task["phase"] == PHASE_IMPLEMENT:
            with open(Path(ctx.handle.path, "backend", "glue.js"), "a", encoding="utf-8") as file:
                file.write(f"// {task['node_id']}\n")
        return True

    async def unhealthy_probe(workspace_path: str, port: int | None = None) -> str | None:
        return "backend unhealthy"

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    monkeypatch.setattr(web, "probe_backend_health", unhealthy_probe)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    failed = [node for node in ("RA", "RB") if states[node] == NODE_FAILED]
    passed = [node for node in ("RA", "RB") if states[node] == NODE_PASSED]
    assert len(failed) == 1 and len(passed) == 1, f"gate must fail exactly the resolved merge: {states}"
    glue = (Path(manager.workspace_path) / "backend" / "glue.js").read_text(encoding="utf-8")
    assert f"// {passed[0]}\n" in glue and f"// {failed[0]}\n" not in glue, (
        "the aborted merge must not land the resolved union"
    )


def test_additive_resolution_passes_gate_and_lands_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app_type_handler.web as web

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    _append_to_workspace_file(Path(manager.workspace_path))
    queue_state = _queue_state(manager, _requirement_tree())
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
            queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
            task["status"] = TASK_COMPLETED

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        if ctx is not None and task["phase"] == PHASE_IMPLEMENT:
            with open(Path(ctx.handle.path, "backend", "glue.js"), "a", encoding="utf-8") as file:
                file.write(f"// {task['node_id']}\n")
        return True

    async def healthy_probe(workspace_path: str, port: int | None = None) -> str | None:
        return None

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    monkeypatch.setattr(web, "probe_backend_health", healthy_probe)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert all(queue_state["node_states"][node] == NODE_PASSED for node in ("RA", "RB"))
    glue = (Path(manager.workspace_path) / "backend" / "glue.js").read_text(encoding="utf-8")
    assert "// RA\n" in glue and "// RB\n" in glue, "both appends must land"


# ---------------------------------------------------------------------------
# conflict-aware DESIGN retry (add/add conflicts on new files)
# ---------------------------------------------------------------------------


def _collect_logs(manager: ARCWorkflowManager) -> list[str]:
    messages: list[str] = []

    async def log(agent: str, message: str, status: str | None = None, node_id: str | None = None) -> None:
        del agent, status, node_id
        messages.append(message)

    manager.log_cb = log
    return messages


def test_design_merge_conflict_requeues_once_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DESIGN add/add conflict re-queues the node once; the retry starts
    from the merged integration state and must deliver disjoint work."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    logs = _collect_logs(manager)
    queue_state = _queue_state(manager, _requirement_tree())

    sibling_wrote = asyncio.Event()
    design_runs: dict[str, int] = {}

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN:
            design_runs[node_id] = design_runs.get(node_id, 0) + 1
            if ctx is not None and node_id == "RB":
                # RB integrates first, so its shared.js owns the path.
                Path(ctx.handle.path, "shared.js").write_text("from RB;\n", encoding="utf-8")
                sibling_wrote.set()
            elif ctx is not None and node_id == "RA":
                await sibling_wrote.wait()
                if design_runs.get("RA", 0) == 1:
                    Path(ctx.handle.path, "shared.js").write_text("from RA;\n", encoding="utf-8")
                else:
                    # The retry must stay off the sibling-owned path.
                    Path(ctx.handle.path, "ra-owned.js").write_text("from RA retry;\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert states["RB"] == NODE_PASSED, "the winning sibling passes"
    assert states["RA"] == NODE_PASSED, "the conflicted node recovers on its one retry"
    assert design_runs.get("RA") == 2, "RA's DESIGN ran exactly twice"
    workspace = Path(manager.workspace_path)
    assert (workspace / "shared.js").read_text(encoding="utf-8") == "from RB;\n"
    assert (workspace / "ra-owned.js").read_text(encoding="utf-8") == "from RA retry;\n"
    assert not list((workspace / ".arc" / "worktrees").iterdir()), "no worktree is left behind"

    session = sessions.load_node_session("RA")
    assert session.get("merge_conflict_retry_used") is True
    assert session.get("merge_conflict_context") == {"paths": ["shared.js"], "phase": "design"}
    assert any("Re-queued RA DESIGN once" in message for message in logs), logs
    assert manager.runtime.traceability.cleared_design_artifacts == ["RA"]

    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["RA:DESIGN"] == TASK_COMPLETED and tasks["RA:IMPLEMENT"] == TASK_COMPLETED


def test_second_design_merge_conflict_fails_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the one-shot retry conflicts again - here against a *new* file
    landed by the sibling's IMPLEMENT while the retry was in flight - the
    node fails for good and its worktree is preserved."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    logs = _collect_logs(manager)
    queue_state = _queue_state(manager, _requirement_tree())

    sibling_wrote = asyncio.Event()
    sibling_implement_integrated = asyncio.Event()
    design_runs: dict[str, int] = {}

    original_integrate = manager._integrate_task_workspace

    async def integrate_and_signal(ctx: Any, node_id: str, phase: str, requirement_data: dict[str, Any]) -> Any:
        result = await original_integrate(ctx, node_id, phase, requirement_data)
        if node_id == "RB" and phase == PHASE_IMPLEMENT:
            sibling_implement_integrated.set()
        return result

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN:
            design_runs[node_id] = design_runs.get(node_id, 0) + 1
            if ctx is not None and node_id == "RB":
                Path(ctx.handle.path, "shared.js").write_text("from RB;\n", encoding="utf-8")
                sibling_wrote.set()
            elif ctx is not None and node_id == "RA":
                if design_runs.get("RA", 0) == 1:
                    await sibling_wrote.wait()
                    Path(ctx.handle.path, "shared.js").write_text("from RA;\n", encoding="utf-8")
                else:
                    # The retry stays off shared.js but collides on a new
                    # file that RB's IMPLEMENT lands while it is in flight.
                    await sibling_implement_integrated.wait()
                    Path(ctx.handle.path, "late.js").write_text("from RA retry;\n", encoding="utf-8")
        elif ctx is not None and task["phase"] == PHASE_IMPLEMENT and node_id == "RB":
            Path(ctx.handle.path, "late.js").write_text("from RB implement;\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_integrate_task_workspace", integrate_and_signal)
    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert states["RB"] == NODE_PASSED
    assert states["RA"] == NODE_FAILED, "the second conflict is terminal"
    assert design_runs.get("RA") == 2, "the retry happened exactly once"
    workspace = Path(manager.workspace_path)
    assert (workspace / "shared.js").read_text(encoding="utf-8") == "from RB;\n"
    assert (workspace / "late.js").read_text(encoding="utf-8") == "from RB implement;\n"

    session = sessions.load_node_session("RA")
    assert session.get("merge_conflict_retry_used") is True
    assert sum(1 for message in logs if "Re-queued RA DESIGN once" in message) == 1

    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["RA:DESIGN"] == TASK_FAILED and tasks["RA:IMPLEMENT"] == TASK_FAILED
    # The terminal failure preserves the node's worktree for inspection.
    preserved = list((workspace / ".arc" / "worktrees").iterdir())
    assert len(preserved) == 1 and "RA" in preserved[0].name


def test_requeued_design_retry_sees_the_merged_sibling_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry's worktree starts at the current integration HEAD, so the
    sibling's merged files are on disk for the retrying agent to read."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())

    sibling_wrote = asyncio.Event()
    design_runs: dict[str, int] = {}
    retry_worktree: dict[str, str] = {}

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN:
            design_runs[node_id] = design_runs.get(node_id, 0) + 1
            if ctx is not None and node_id == "RB":
                Path(ctx.handle.path, "shared.js").write_text("from RB;\n", encoding="utf-8")
                sibling_wrote.set()
            elif ctx is not None and node_id == "RA":
                await sibling_wrote.wait()
                if design_runs.get("RA", 0) == 1:
                    Path(ctx.handle.path, "shared.js").write_text("from RA;\n", encoding="utf-8")
                else:
                    retry_worktree["path"] = ctx.handle.path
                    # The winning sibling's file must already exist there.
                    assert (Path(ctx.handle.path) / "shared.js").read_text(encoding="utf-8") == "from RB;\n"
                    Path(ctx.handle.path, "ra-owned.js").write_text("retry;\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert retry_worktree, "the retry ran"
    assert queue_state["node_states"]["RA"] == NODE_PASSED


def test_declined_requeue_fails_node_without_leaking_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the conflict requeue is declined (here: the node's queue tasks
    are incomplete), the node fails through the regular failure branch and
    the method tail still closes the task workspace: the worktree is
    preserved for --retry, the port slot is released, nothing leaks."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    logs = _collect_logs(manager)
    queue_state = _queue_state(manager, _requirement_tree())
    # Drop RA's IMPLEMENT task so _requeue_design_after_merge_conflict
    # declines after the branch reset.
    queue_state["tasks"] = [t for t in queue_state["tasks"] if t["task_id"] != "RA:IMPLEMENT"]

    sibling_wrote = asyncio.Event()

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN and ctx is not None:
            if node_id == "RB":
                Path(ctx.handle.path, "shared.js").write_text("from RB;\n", encoding="utf-8")
                sibling_wrote.set()
            elif node_id == "RA":
                await sibling_wrote.wait()
                Path(ctx.handle.path, "shared.js").write_text("from RA;\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert states["RB"] == NODE_PASSED
    assert states["RA"] == NODE_FAILED, "the declined requeue fails the node"
    assert any("its queue tasks are incomplete" in message for message in logs), logs
    # The node failed with a merge conflict, so its worktree is preserved
    # for inspection/--retry (not leaked, not removed).
    workspace = Path(manager.workspace_path)
    preserved = list((workspace / ".arc" / "worktrees").iterdir())
    assert len(preserved) == 1 and "RA" in preserved[0].name
    assert manager._port_slots == {}, "the port slot was released by the closing path"
    # The requeue was declined before recording session keys.
    session = sessions.load_node_session("RA")
    assert not session.get("merge_conflict_retry_used")


def test_manual_retry_restores_the_conflict_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual --retry resets the node for a fresh DESIGN pass: the
    one-shot merge-conflict retry budget must be restored and stale
    conflict paths dropped, otherwise the retried run both starts with a
    burned budget and reads misleading prompt guidance."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())
    sessions.merge_node_session(
        "RA",
        {
            "merge_conflict_retry_used": True,
            "merge_conflict_context": {"paths": ["shared.js"], "phase": "design"},
        },
    )

    manager._apply_retry_plan(queue_state, retry_node_ids=["RA"])

    session = sessions.load_node_session("RA")
    assert not session.get("merge_conflict_retry_used"), "the budget is restored"
    assert not session.get("merge_conflict_context"), "stale conflict paths are dropped"
    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["RA:DESIGN"] == TASK_PENDING and tasks["RA:IMPLEMENT"] == TASK_PENDING


# ---------------------------------------------------------------------------
# conflict-aware IMPLEMENT retry (non-additive conflicts on shared files)
# ---------------------------------------------------------------------------


def test_implement_merge_conflict_requeues_once_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IMPLEMENT conflict re-queues the node's IMPLEMENT once; the retry
    starts from the merged integration state (the sibling's files are visible
    on disk) and must deliver disjoint work. The DESIGN phase is not re-run:
    its artifacts are part of the integration HEAD the retry starts from."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    logs = _collect_logs(manager)
    queue_state = _queue_state(manager, _requirement_tree())

    sibling_wrote = asyncio.Event()
    implement_runs: dict[str, int] = {}
    design_phases: list[str] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN:
            design_phases.append(node_id)
            return True
        implement_runs[node_id] = implement_runs.get(node_id, 0) + 1
        if ctx is not None and node_id == "RB":
            # RB integrates first, so its shared.js owns the path.
            Path(ctx.handle.path, "shared.js").write_text("from RB;\n", encoding="utf-8")
            sibling_wrote.set()
        elif ctx is not None and node_id == "RA":
            await sibling_wrote.wait()
            if implement_runs.get("RA", 0) == 1:
                # Add/add on a brand-new path: non-additive, no resolver.
                Path(ctx.handle.path, "shared.js").write_text("from RA;\n", encoding="utf-8")
            else:
                # The retry must stay off the sibling-owned path and must
                # see the sibling's merged file in its worktree.
                assert (Path(ctx.handle.path) / "shared.js").read_text(encoding="utf-8") == "from RB;\n"
                Path(ctx.handle.path, "ra-owned.js").write_text("from RA retry;\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert states["RB"] == NODE_PASSED, "the winning sibling passes"
    assert states["RA"] == NODE_PASSED, "the conflicted node recovers on its one retry"
    assert implement_runs.get("RA") == 2, "RA's IMPLEMENT ran exactly twice"
    assert design_phases.count("RA") == 1, "DESIGN is not re-run for an implement-only requeue"
    workspace = Path(manager.workspace_path)
    assert (workspace / "shared.js").read_text(encoding="utf-8") == "from RB;\n"
    assert (workspace / "ra-owned.js").read_text(encoding="utf-8") == "from RA retry;\n"
    assert not list((workspace / ".arc" / "worktrees").iterdir()), "no worktree is left behind"

    session = sessions.load_node_session("RA")
    assert session.get("merge_conflict_retry_used") is True
    assert session.get("merge_conflict_context") == {"paths": ["shared.js"], "phase": "implement"}
    assert any("Re-queued RA IMPLEMENT once" in message for message in logs), logs
    assert manager.runtime.traceability.cleared_design_artifacts == [], "design artifacts are kept"

    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["RA:DESIGN"] == TASK_COMPLETED and tasks["RA:IMPLEMENT"] == TASK_COMPLETED


def test_second_implement_merge_conflict_fails_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the one-shot IMPLEMENT retry conflicts again - here against a
    *new* file landed by a third sibling's IMPLEMENT while the retry was in
    flight - the node fails for good: node FAILED, the node's tasks failed,
    worktree preserved."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    logs = _collect_logs(manager)
    tree = _requirement_tree()
    tree["children"].append({"id": "RC", "name": "leaf c", "description": "c", "children": []})
    manager.runtime.traceability.requirements["RC"] = {
        "id": "RC", "name": "leaf c", "description": "c"
    }
    queue_state = _queue_state(manager, tree)

    sibling_wrote = asyncio.Event()
    ra_requeued = asyncio.Event()
    rc_integrated = asyncio.Event()
    implement_runs: dict[str, int] = {}

    original_integrate = manager._integrate_task_workspace

    async def integrate_and_signal(ctx: Any, node_id: str, phase: str, requirement_data: dict[str, Any]) -> Any:
        result = await original_integrate(ctx, node_id, phase, requirement_data)
        if node_id == "RC" and phase == PHASE_IMPLEMENT:
            rc_integrated.set()
        return result

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN:
            return True
        implement_runs[node_id] = implement_runs.get(node_id, 0) + 1
        if ctx is not None and node_id == "RB":
            Path(ctx.handle.path, "shared.js").write_text("from RB;\n", encoding="utf-8")
            sibling_wrote.set()
        elif ctx is not None and node_id == "RA":
            if implement_runs.get("RA", 0) == 1:
                await sibling_wrote.wait()
                # Add/add on a brand-new path: non-additive, no resolver.
                Path(ctx.handle.path, "shared.js").write_text("from RA;\n", encoding="utf-8")
            else:
                # The retry stays off shared.js but collides on a new file
                # that RC's IMPLEMENT lands while the retry is in flight.
                ra_requeued.set()
                await rc_integrated.wait()
                Path(ctx.handle.path, "late.js").write_text("from RA retry;\n", encoding="utf-8")
        elif ctx is not None and node_id == "RC":
            await ra_requeued.wait()
            Path(ctx.handle.path, "late.js").write_text("from RC;\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_integrate_task_workspace", integrate_and_signal)
    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert states["RB"] == NODE_PASSED
    assert states["RC"] == NODE_PASSED
    assert states["RA"] == NODE_FAILED, "the second conflict is terminal"
    assert implement_runs.get("RA") == 2, "the retry happened exactly once"
    workspace = Path(manager.workspace_path)
    assert (workspace / "shared.js").read_text(encoding="utf-8") == "from RB;\n"
    assert (workspace / "late.js").read_text(encoding="utf-8") == "from RC;\n"

    session = sessions.load_node_session("RA")
    assert session.get("merge_conflict_retry_used") is True
    assert sum(1 for message in logs if "Re-queued RA IMPLEMENT once" in message) == 1

    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["RA:DESIGN"] == TASK_COMPLETED, "the settled DESIGN is not failed"
    assert tasks["RA:IMPLEMENT"] == TASK_FAILED
    # The terminal failure preserves the node's worktree for inspection.
    preserved = list((workspace / ".arc" / "worktrees").iterdir())
    assert len(preserved) == 1 and "RA" in preserved[0].name


def test_implement_requeue_budget_is_independent_of_the_design_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-shot conflict requeue budget is per phase: a node that
    consumed its DESIGN requeue (and recovered) still gets its first
    IMPLEMENT conflict re-queued. A shared per-node flag would terminally
    fail this node's very first IMPLEMENT conflict."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())

    sibling_wrote = asyncio.Event()
    sibling_implement_wrote = asyncio.Event()
    design_runs: dict[str, int] = {}
    implement_runs: dict[str, int] = {}

    original_integrate = manager._integrate_task_workspace

    async def integrate_and_signal(ctx: Any, node_id: str, phase: str, requirement_data: dict[str, Any]) -> Any:
        result = await original_integrate(ctx, node_id, phase, requirement_data)
        if node_id == "RB" and phase == PHASE_IMPLEMENT:
            sibling_implement_wrote.set()
        return result

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN:
            design_runs[node_id] = design_runs.get(node_id, 0) + 1
            if ctx is not None and node_id == "RB":
                Path(ctx.handle.path, "shared.js").write_text("from RB;\n", encoding="utf-8")
                sibling_wrote.set()
            elif ctx is not None and node_id == "RA":
                await sibling_wrote.wait()
                if design_runs.get("RA", 0) == 1:
                    Path(ctx.handle.path, "shared.js").write_text("from RA;\n", encoding="utf-8")
                else:
                    # DESIGN retry stays off the sibling-owned path.
                    Path(ctx.handle.path, "ra-design.js").write_text("from RA design;\n", encoding="utf-8")
            return True
        implement_runs[node_id] = implement_runs.get(node_id, 0) + 1
        if ctx is not None and node_id == "RB":
            Path(ctx.handle.path, "impl-shared.js").write_text("from RB;\n", encoding="utf-8")
        elif ctx is not None and node_id == "RA":
            await sibling_implement_wrote.wait()
            if implement_runs.get("RA", 0) == 1:
                # First IMPLEMENT conflict: must still re-queue despite the
                # burned DESIGN budget.
                Path(ctx.handle.path, "impl-shared.js").write_text("from RA;\n", encoding="utf-8")
            else:
                Path(ctx.handle.path, "ra-impl.js").write_text("from RA impl retry;\n", encoding="utf-8")
        return True

    monkeypatch.setattr(manager, "_integrate_task_workspace", integrate_and_signal)
    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert states["RB"] == NODE_PASSED
    assert states["RA"] == NODE_PASSED, "the implement requeue fires after the design budget burned"
    assert design_runs.get("RA") == 2 and implement_runs.get("RA") == 2
    workspace = Path(manager.workspace_path)
    assert (workspace / "impl-shared.js").read_text(encoding="utf-8") == "from RB;\n"
    assert (workspace / "ra-impl.js").read_text(encoding="utf-8") == "from RA impl retry;\n"

    session = sessions.load_node_session("RA")
    # The final record reflects the latest (implement) requeue.
    assert session.get("merge_conflict_retry_used") is True
    assert session.get("merge_conflict_context") == {"paths": ["impl-shared.js"], "phase": "implement"}

    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["RA:DESIGN"] == TASK_COMPLETED and tasks["RA:IMPLEMENT"] == TASK_COMPLETED


def test_manual_implement_retry_restores_the_conflict_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual --retry of a node whose IMPLEMENT terminally conflicted takes
    the implement-only reset path: the one-shot conflict retry budget must be
    restored and the stale implement conflict paths dropped, mirroring the
    DESIGN-side contract."""
    from core import sessions

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())
    tasks_by_id = {task["task_id"]: task for task in queue_state["tasks"]}
    queue_state["node_states"]["RA"] = NODE_FAILED
    queue_state.setdefault("node_design_done", {})["RA"] = True
    tasks_by_id["RA:DESIGN"]["status"] = TASK_COMPLETED
    tasks_by_id["RA:IMPLEMENT"]["status"] = TASK_FAILED
    sessions.merge_node_session(
        "RA",
        {
            "merge_conflict_retry_used": True,
            "merge_conflict_context": {"paths": ["shared.js"], "phase": "implement"},
        },
    )

    manager._apply_retry_plan(queue_state, retry_node_ids=["RA"])

    session = sessions.load_node_session("RA")
    assert not session.get("merge_conflict_retry_used"), "the budget is restored"
    assert not session.get("merge_conflict_context"), "stale conflict paths are dropped"
    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["RA:DESIGN"] == TASK_COMPLETED and tasks["RA:IMPLEMENT"] == TASK_PENDING


# ---------------------------------------------------------------------------
# parent-serial DESIGN (children design against the parent's merged shell)
# ---------------------------------------------------------------------------


def _gate_queue_state(parent_status: str, *, with_parents: bool = True) -> dict[str, Any]:
    tasks = [
        {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": parent_status},
        {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_PENDING},
    ]
    state: dict[str, Any] = {"tasks": tasks}
    state["node_states"], state["node_design_done"] = node_maps_from_tasks(tasks)
    if with_parents:
        state["parents"] = {"RA": "R"}
    return state


def test_design_gate_blocks_until_the_parent_design_settles() -> None:
    parent = _gate_queue_state(TASK_RUNNING)["tasks"][0]
    child = _gate_queue_state(TASK_RUNNING)["tasks"][1]
    state = {"tasks": [parent, child], "parents": {"RA": "R"}}
    state["node_states"], state["node_design_done"] = node_maps_from_tasks([parent, child])

    assert task_dependencies_met(state, child) is False, "parent still running"

    settle(state, "R", NODE_UNSEEN)
    assert task_dependencies_met(state, child) is False, "parent still pending"

    settle(state, "R", NODE_DESIGNED)
    assert task_dependencies_met(state, child) is True

    settle(state, "R", NODE_FAILED, design_done=False)
    assert task_dependencies_met(state, child) is True, (
        "a failed parent must not deadlock its children"
    )


def test_design_gate_lets_the_root_and_unmapped_nodes_through() -> None:
    state = _gate_queue_state(TASK_PENDING)
    root = state["tasks"][0]
    assert task_dependencies_met(state, root) is True, "the root has no parent"

    orphan = {"task_id": "X:DESIGN", "node_id": "X", "phase": PHASE_DESIGN, "status": TASK_PENDING}
    assert task_dependencies_met(state, orphan) is True


def test_design_gate_fails_open_for_queues_saved_before_parents() -> None:
    state = _gate_queue_state(TASK_RUNNING, with_parents=False)
    child = state["tasks"][1]
    assert task_dependencies_met(state, child) is True


def test_design_gate_combines_parent_and_dependency_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #38 review follow-up: the parent rule and the dependency rule are
    independent checks and both must hold - a child whose parent DESIGN
    failed is unblocked by the parent rule, but a still-running declared
    dependency keeps its DESIGN blocked; and a completed parent alone does
    not unblock a child whose dependency is still running."""
    # Parent failed, dependency done: the child designs against the
    # integration state without the parent shell, reusing the dependency.
    state = _gate_queue_state(TASK_FAILED)
    state["tasks"] += [
        {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
        {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_COMPLETED},
    ]
    state["node_states"], state["node_design_done"] = node_maps_from_tasks(state["tasks"])
    state["dependencies"] = {"RA": ["RB"]}
    child = next(t for t in state["tasks"] if t["task_id"] == "RA:DESIGN")
    assert task_dependencies_met(state, child) is True, (
        "failed parent unblocks; completed dependency unblocks"
    )

    # Parent failed but the dependency is still implementing: still blocked.
    settle(state, "RB", NODE_DESIGNED)
    assert task_dependencies_met(state, child) is False, (
        "a failed parent must not let the dependency check pass the child through"
    )

    # Parent completed, dependency still implementing: still blocked.
    state["node_states"]["R"] = NODE_DESIGNED
    state["node_design_done"]["R"] = True
    state["tasks"][0]["status"] = TASK_COMPLETED
    assert task_dependencies_met(state, child) is False


def test_design_gate_applies_declared_dependencies_to_the_root() -> None:
    """PR #38 review follow-up: the root has no parent, so its DESIGN goes
    straight to the dependency check - blocked while a declared dependency's
    IMPLEMENT runs, unblocked when it fails (the failed-dependency release
    the IMPLEMENT rule already follows)."""
    tasks = [
        {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": TASK_PENDING},
        {"task_id": "RB:DESIGN", "node_id": "RB", "phase": PHASE_DESIGN, "status": TASK_COMPLETED},
        {"task_id": "RB:IMPLEMENT", "node_id": "RB", "phase": PHASE_IMPLEMENT, "status": TASK_RUNNING},
    ]
    state: dict[str, Any] = {
        "tasks": list(tasks),
        "dependencies": {"R": ["RB"]},
    }
    state["node_states"], state["node_design_done"] = node_maps_from_tasks(tasks)

    root = state["tasks"][0]
    assert task_dependencies_met(state, root) is False

    settle(state, "RB", NODE_PASSED)
    assert task_dependencies_met(state, root) is True

    settle(state, "RB", NODE_FAILED, design_done=True)
    assert task_dependencies_met(state, root) is False, (
        "a failed dependency must block the dependent root"
    )


def test_design_gate_blocks_when_the_parent_design_task_is_missing() -> None:
    # A parents entry without a matching DESIGN task is an inconsistent
    # queue: block the child instead of designing against an unknown
    # baseline.
    state = _gate_queue_state(TASK_COMPLETED)
    state["tasks"] = [state["tasks"][1]]  # drop R:DESIGN, keep RA:DESIGN
    child = state["tasks"][0]
    assert task_dependencies_met(state, child) is False


def test_child_design_waits_for_parent_design_and_leaves_stay_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain must not start a child's DESIGN before its parent's DESIGN
    integrated (the ticket-booking run failed exactly this way: the parent's
    rewrite of shared surfaces merged while the children's additive edits to
    the same files were in flight). Sibling leaf DESIGNs still overlap."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())

    events: list[tuple[str, str]] = []
    leaf_designs_in_flight: set[str] = set()
    second_leaf_started = asyncio.Event()
    leaf_overlap = False

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        nonlocal leaf_overlap
        task_id = task["task_id"]
        is_leaf_design = task["phase"] == PHASE_DESIGN and task["node_id"] != "R"
        if is_leaf_design:
            if leaf_designs_in_flight:
                leaf_overlap = True
            leaf_designs_in_flight.add(task_id)
            if len(leaf_designs_in_flight) == 2:
                second_leaf_started.set()
        events.append(("start", task_id))
        if is_leaf_design:
            # Hold every leaf DESIGN until its sibling starts (bounded, so a
            # serialization regression still fails instead of hanging): the
            # overlap then no longer depends on a sleep outlasting the
            # sibling's real-git worktree preparation, which occasionally
            # exceeds it under load.
            try:
                await asyncio.wait_for(second_leaf_started.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(0.05)
        if is_leaf_design:
            leaf_designs_in_flight.discard(task_id)
        events.append(("end", task_id))
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    order = dict.fromkeys(event for event in events)
    root_design_end = list(order).index(("end", "R:DESIGN"))
    for leaf in ("RA:DESIGN", "RB:DESIGN"):
        assert list(order).index(("start", leaf)) > root_design_end, (
            "a child DESIGN must not start before the parent DESIGN integrated"
        )
    assert leaf_overlap, "sibling leaf DESIGNs must still run in parallel"
    assert all(queue_state["node_states"][node] == NODE_PASSED for node in ("R", "RA", "RB"))
    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert all(status == TASK_COMPLETED for status in tasks.values())


def test_failed_parent_design_unblocks_children_with_an_audit_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed parent DESIGN must not deadlock its children, and the
    failure leaves an auditable trace saying the children proceed against
    the integration state without the parent shell."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_parallel_manager(tmp_path)
    logs = _collect_logs(manager)
    queue_state = _queue_state(manager, _requirement_tree())

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        return task["task_id"] != "R:DESIGN"

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert states["R"] == NODE_FAILED, "the failed parent fails its own node"
    assert states["RA"] == NODE_PASSED and states["RB"] == NODE_PASSED, (
        "children proceed without the parent shell"
    )
    tasks = {task["task_id"]: task["status"] for task in queue_state["tasks"]}
    assert tasks["R:DESIGN"] == TASK_FAILED and tasks["R:IMPLEMENT"] == TASK_FAILED
    assert any(
        "descendant node(s) (RA, RB) will design against the integration state" in message
        for message in logs
    ), logs


# ----------------------------------------------------------------------
# affinity depth split (ARC_AFFINITY_DEPTH)
# ----------------------------------------------------------------------


def _wide_requirement_tree() -> dict[str, Any]:
    """simple-keep's pathology: two feature subtrees under one parent. Under
    the default top-level grouping they serialize in the REQ-2 group's single
    reusable worktree; under ARC_AFFINITY_DEPTH=2 each drains in parallel."""
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [
            {
                "id": "REQ-2",
                "name": "notes",
                "description": "notes",
                "children": [
                    {
                        "id": "REQ-2.5",
                        "name": "archive",
                        "description": "archive",
                        "children": [
                            {"id": "REQ-2.5.1", "name": "archive", "description": "a", "children": []},
                            {"id": "REQ-2.5.2", "name": "undo", "description": "u", "children": []},
                        ],
                    },
                    {
                        "id": "REQ-2.7",
                        "name": "labels",
                        "description": "labels",
                        "children": [
                            {"id": "REQ-2.7.1", "name": "assign", "description": "l", "children": []},
                        ],
                    },
                ],
            },
        ],
    }


def test_affinity_depth_split_runs_feature_subtrees_in_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lever on real git: ARC_AFFINITY_DEPTH=2 gives sibling feature
    subtrees under one parent their own groups, so their IMPLEMENT tasks
    overlap in distinct worktrees with distinct port slots and both merge
    back into the integration workspace."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")
    manager = _make_parallel_manager(
        tmp_path,
        node_ids=["R", "REQ-2", "REQ-2.5", "REQ-2.5.1", "REQ-2.5.2", "REQ-2.7", "REQ-2.7.1"],
    )
    queue_state = _queue_state(manager, _wide_requirement_tree())
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            task["status"] = TASK_COMPLETED

    active: set[str] = set()
    seen_worktrees: dict[str, str] = {}
    seen_ports: dict[str, int | None] = {}
    overlap = False

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        nonlocal overlap
        if active:
            overlap = True
        active.add(task["task_id"])
        if ctx is not None:
            seen_worktrees[task["node_id"]] = ctx.handle.path
            seen_ports[task["node_id"]] = ctx.web_port
            Path(ctx.handle.path, f"{task['node_id']}.feature.js").write_text(
                f"feature {task['node_id']};\n", encoding="utf-8"
            )
        # Generous window: the assertion is "must overlap", so a loaded CI
        # box must not turn a real overlap into a scheduling-looking miss.
        await asyncio.sleep(0.15)
        active.discard(task["task_id"])
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert overlap, "feature subtrees under one parent must overlap under depth 2"
    assert seen_worktrees["REQ-2.5.1"] != seen_worktrees["REQ-2.7.1"], "distinct worktrees"
    assert seen_ports["REQ-2.5.1"] != seen_ports["REQ-2.7.1"], "distinct port slots"
    workspace = Path(manager.workspace_path)
    for node_id in ("REQ-2.5.1", "REQ-2.5.2", "REQ-2.7.1"):
        assert (workspace / f"{node_id}.feature.js").exists(), f"{node_id} merged back"
    states = queue_state["node_states"]
    assert all(states[node] == NODE_PASSED for node in ("R", "REQ-2", "REQ-2.5", "REQ-2.7"))


def test_default_depth_keeps_feature_subtrees_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default must not change: without ARC_AFFINITY_DEPTH the whole
    REQ-2 subtree is one group, so the feature subtrees' IMPLEMENTs stay
    strictly serial in the group's single reusable worktree."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.delenv("ARC_AFFINITY_DEPTH", raising=False)
    manager = _make_parallel_manager(
        tmp_path,
        node_ids=["R", "REQ-2", "REQ-2.5", "REQ-2.5.1", "REQ-2.5.2", "REQ-2.7", "REQ-2.7.1"],
    )
    queue_state = _queue_state(manager, _wide_requirement_tree())
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            task["status"] = TASK_COMPLETED

    active: set[str] = set()

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        assert active.isdisjoint({task["node_id"]}), (
            f"overlap under default depth: {active} vs {task['node_id']}"
        )
        active.add(task["node_id"])
        await asyncio.sleep(0.02)
        active.discard(task["node_id"])
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    states = queue_state["node_states"]
    assert all(states[node] == NODE_PASSED for node in ("R", "REQ-2", "REQ-2.5", "REQ-2.7"))

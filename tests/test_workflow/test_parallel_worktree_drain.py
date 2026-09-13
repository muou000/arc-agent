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
- without ARC_NODE_WORKTREES the drain stays strictly serial.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.workflow import (
    ARCWorkflowManager,
    NODE_FAILED,
    NODE_PASSED,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_FAILED,
)


class _Traceability:
    def __init__(self, node_ids: list[str]) -> None:
        self.requirements = {
            node_id: {"id": node_id, "name": node_id, "description": "req"}
            for node_id in node_ids
        }
        self.states: dict[str, str] = {}

    def get_requirement(self, node_id: str) -> dict[str, Any] | None:
        return self.requirements.get(node_id)

    def upsert_node_state(self, node_id: str, state: str) -> None:
        self.states[node_id] = state


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


def _make_parallel_manager(tmp_path: Path, max_tasks: int = 2) -> ARCWorkflowManager:
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
        traceability=_Traceability(["R", "RA", "RB"]),
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


def test_merge_conflict_fails_only_the_conflicting_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_parallel_manager(tmp_path)
    queue_state = _queue_state(manager, _requirement_tree())
    for task in queue_state["tasks"]:
        if task["phase"] == PHASE_DESIGN:
            task["status"] = TASK_COMPLETED

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


def test_serial_drain_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_NODE_WORKTREES", raising=False)
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

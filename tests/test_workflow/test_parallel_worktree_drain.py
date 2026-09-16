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

from core.workflow import (
    ARCWorkflowManager,
    NODE_FAILED,
    NODE_PASSED,
    PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
)


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
# parent-serial DESIGN (children design against the parent's merged shell)
# ---------------------------------------------------------------------------


def _gate_queue_state(parent_status: str, *, with_parents: bool = True) -> dict[str, Any]:
    tasks = [
        {"task_id": "R:DESIGN", "node_id": "R", "phase": PHASE_DESIGN, "status": parent_status},
        {"task_id": "RA:DESIGN", "node_id": "RA", "phase": PHASE_DESIGN, "status": TASK_PENDING},
    ]
    state: dict[str, Any] = {"tasks": tasks}
    if with_parents:
        state["parents"] = {"RA": "R"}
    return state


def test_design_gate_blocks_until_the_parent_design_settles() -> None:
    parent = _gate_queue_state(TASK_RUNNING)["tasks"][0]
    child = _gate_queue_state(TASK_RUNNING)["tasks"][1]
    state = {"tasks": [parent, child], "parents": {"RA": "R"}}

    assert ARCWorkflowManager._task_dependencies_met(state, child) is False, "parent still running"

    parent["status"] = TASK_PENDING
    assert ARCWorkflowManager._task_dependencies_met(state, child) is False, "parent still pending"

    parent["status"] = TASK_COMPLETED
    assert ARCWorkflowManager._task_dependencies_met(state, child) is True

    parent["status"] = TASK_FAILED
    assert ARCWorkflowManager._task_dependencies_met(state, child) is True, (
        "a failed parent must not deadlock its children"
    )


def test_design_gate_lets_the_root_and_unmapped_nodes_through() -> None:
    state = _gate_queue_state(TASK_PENDING)
    root = state["tasks"][0]
    assert ARCWorkflowManager._task_dependencies_met(state, root) is True, "the root has no parent"

    orphan = {"task_id": "X:DESIGN", "node_id": "X", "phase": PHASE_DESIGN, "status": TASK_PENDING}
    assert ARCWorkflowManager._task_dependencies_met(state, orphan) is True


def test_design_gate_fails_open_for_queues_saved_before_parents() -> None:
    state = _gate_queue_state(TASK_RUNNING, with_parents=False)
    child = state["tasks"][1]
    assert ARCWorkflowManager._task_dependencies_met(state, child) is True


def test_design_gate_blocks_when_the_parent_design_task_is_missing() -> None:
    # A parents entry without a matching DESIGN task is an inconsistent
    # queue: block the child instead of designing against an unknown
    # baseline.
    state = _gate_queue_state(TASK_COMPLETED)
    state["tasks"] = [state["tasks"][1]]  # drop R:DESIGN, keep RA:DESIGN
    child = state["tasks"][0]
    assert ARCWorkflowManager._task_dependencies_met(state, child) is False


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
    active: set[str] = set()
    leaf_overlap = False

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        nonlocal leaf_overlap
        task_id = task["task_id"]
        if task["phase"] == PHASE_DESIGN and task["node_id"] != "R" and active:
            leaf_overlap = True
        events.append(("start", task_id))
        active.add(task_id)
        # Long enough that the sibling's worktree prepare cannot run out the
        # overlap window.
        await asyncio.sleep(0.05)
        active.discard(task_id)
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

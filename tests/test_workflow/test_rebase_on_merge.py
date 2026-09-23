"""Eager rebase-on-merge regression tests (issue #127 / ADR 0003).

Real git, no mocks, no models: the replay machinery (``core.worktree``'s
WIP commit + rebase + conflict completion, ``agents.runtime.rebase_gate``'s
tool-boundary trigger and soft guard) is exercised against actual git
repositories, mirroring ``test_worktree_manager.py``'s contract.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.runtime.rebase_gate import (
    REBASE_ON_MERGE_ENV,
    RebaseOnMergeMiddleware,
    rebase_on_merge_enabled,
)
from core.workflow import (
    ARCWorkflowManager,
    NODE_DESIGNED,
    PHASE_DESIGN,
    TASK_COMPLETED,
)
from core.worktree import (
    NodeWorktreeManager,
    PendingMerge,
    ReplayOutcome,
    WorktreeHandle,
    WorktreeOutcome,
    WorktreeTaskResult,
    normalize_repo_path,
)
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from tests.helpers.faux import FauxChatModel, faux_text  # noqa: F401 - faux fixture availability
from tests.helpers.jsonl import read_jsonl


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and completed.returncode != 0:
        raise AssertionError(f"git {args} failed: {completed.stderr}")
    return completed


def _init_repo(tmp_path: Path) -> tuple[Path, NodeWorktreeManager]:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "test"], repo)
    (repo / ".gitignore").write_text(
        "# >>> arcbench-agent-runtime >>>\n.arc/*\n!.arc/traceability/\n!.arc/traceability/**\n# <<< arcbench-agent-runtime <<<\n",
        encoding="utf-8",
    )
    (repo / "backend").mkdir()
    (repo / "backend" / "shared.js").write_text("base;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo, NodeWorktreeManager(str(repo))


def _make_request(name: str, args: dict[str, Any] | None = None, call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id},
        tool=None,
        state={},
        runtime=None,
    )


def _ok_tool(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(
        content="ok", name=request.tool_call["name"], tool_call_id=request.tool_call["id"]
    )


def _record_pending(
    manager: NodeWorktreeManager,
    handle: WorktreeHandle,
    changed_files: list[str],
    *,
    source: str = "REQ-SIBLING",
) -> None:
    head = _git(["rev-parse", "HEAD"], Path(manager.main_workspace)).stdout.strip()
    manager.record_pending_merge(handle, source, head, changed_files)


# ---------------------------------------------------------------------------
# path normalization helpers
# ---------------------------------------------------------------------------


def test_normalize_repo_path_accepts_tool_call_forms() -> None:
    assert normalize_repo_path("/workspace/backend/shared.js") == "backend/shared.js"
    assert normalize_repo_path("backend/shared.js") == "backend/shared.js"
    assert normalize_repo_path("./backend/shared.js") == "backend/shared.js"
    assert normalize_repo_path("backend\\shared.js") == "backend/shared.js"
    assert normalize_repo_path("") == ""
    assert normalize_repo_path("/workspace") == ""


# ---------------------------------------------------------------------------
# eager trigger: any file call replays pending merges
# ---------------------------------------------------------------------------


def test_replay_without_pending_merges_is_skipped(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    outcome = manager.replay_pending_merges(handle)
    assert outcome.status == ReplayOutcome.SKIPPED


def test_eager_replay_runs_on_an_unrelated_file_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eager contract (issue #127 revision): the in-flight agent's next
    file-tool call replays the pending merges regardless of which path it
    touches - a merge that lands while the agent works on unrelated files
    still reaches it immediately."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    # The agent writes some unrelated file (dirty tree).
    (Path(handle.path) / "backend" / "own.js").write_text("own;\n", encoding="utf-8")

    # A sibling merge lands changing shared.js.
    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")

    _record_pending(manager, handle, ["backend/shared.js"])

    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=manager.replay_pending_merges,
        pending_files=lambda: manager.pending_merges_for(handle),
        is_mid_rebase=manager.is_mid_rebase,
        continue_replay=manager.continue_replay,
        conflict_paths_reader=manager.unresolved_conflict_paths,
        enabled=True,
    )
    # A tool call on an unrelated path still consumes the pending merge and
    # serves the call against the replayed tree.
    result = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/own.js"}), _ok_tool
    )
    assert "[ARC rebase-on-merge" in result.content
    assert not manager.pending_merges_for(handle), (
        "the eager boundary must consume the pending merge"
    )
    assert (
        (Path(handle.path) / "backend" / "shared.js").read_text(encoding="utf-8")
        == "base;\nsibling;\n"
    )
    # The agent's own dirty file survived the replay.
    assert (Path(handle.path) / "backend" / "own.js").read_text(encoding="utf-8") == "own;\n"


# ---------------------------------------------------------------------------
# WIP replay: dirty tree committed, rebased, content correct
# ---------------------------------------------------------------------------


def test_replay_commits_dirty_tree_and_rebases_onto_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    (Path(handle.path) / "backend" / "own.js").write_text("own;\n", encoding="utf-8")

    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])

    outcome = manager.replay_pending_merges(handle)
    assert outcome.status == ReplayOutcome.REPLAYED
    assert "backend/shared.js" in outcome.files
    # The sibling's change is now visible in the working tree.
    assert (
        (Path(handle.path) / "backend" / "shared.js").read_text(encoding="utf-8")
        == "base;\nsibling;\n"
    )
    # The agent's own dirty file survived the replay.
    assert (Path(handle.path) / "backend" / "own.js").read_text(encoding="utf-8") == "own;\n"
    # The dirty content landed in a wip: commit on the node branch.
    log = _git(["log", "--format=%s"], Path(handle.path)).stdout.splitlines()
    assert any(line.startswith("wip:") for line in log), f"no wip commit in {log}"


def test_replay_through_middleware_serves_call_against_fresh_tree(
    tmp_path: Path,
) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])

    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=manager.replay_pending_merges,
        pending_files=lambda: manager.pending_merges_for(handle),
        is_mid_rebase=manager.is_mid_rebase,
        continue_replay=manager.continue_replay,
        conflict_paths_reader=manager.unresolved_conflict_paths,
        enabled=True,
    )
    result = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "[ARC rebase-on-merge" in result.content
    # The pending merge was consumed: a second touch does not replay again.
    second = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert second.content == "ok"


def test_replay_of_already_applied_merge_reports_replayed(tmp_path: Path) -> None:
    """A second wave recorded after the branch already contains the merge
    counts as applied without touching the tree."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])
    first = manager.replay_pending_merges(handle)
    assert first.status == ReplayOutcome.REPLAYED

    # The same merge is recorded again (stale bookkeeping).
    _record_pending(manager, handle, ["backend/shared.js"])
    second = manager.replay_pending_merges(handle)
    assert second.status == ReplayOutcome.REPLAYED
    assert (
        (Path(handle.path) / "backend" / "shared.js").read_text(encoding="utf-8")
        == "base;\nsibling;\n"
    )


# ---------------------------------------------------------------------------
# conflicts: presentation, resolution, completion
# ---------------------------------------------------------------------------


def test_conflicted_replay_leaves_markers_and_resolves_via_continue(
    tmp_path: Path,
) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    # The agent modifies the shared file (dirty tree that will conflict).
    shared = Path(handle.path) / "backend" / "shared.js"
    shared.write_text("base;\nagent;\n", encoding="utf-8")

    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])

    outcome = manager.replay_pending_merges(handle)
    assert outcome.status == ReplayOutcome.CONFLICTS
    assert outcome.files == ["backend/shared.js"]
    assert "<<<<<<<" in shared.read_text(encoding="utf-8")

    # The agent resolves the markers with its file tools.
    shared.write_text("base;\nagent;\nsibling;\n", encoding="utf-8")
    completed = manager.continue_replay(handle)
    assert completed.status == ReplayOutcome.REPLAYED
    assert shared.read_text(encoding="utf-8") == "base;\nagent;\nsibling;\n"
    assert not manager.is_mid_rebase(handle)
    # The branch contains the resolution.
    log = _git(["log", "--format=%s"], Path(handle.path)).stdout.splitlines()
    assert any(line.startswith("wip:") for line in log)


def test_conflicted_replay_annotation_and_completion_at_boundaries(
    tmp_path: Path,
) -> None:
    """The middleware surface: conflict notice rides the tool result, the
    agent's resolving edit completes the rebase at the next boundary."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    shared = Path(handle.path) / "backend" / "shared.js"
    shared.write_text("base;\nagent;\n", encoding="utf-8")

    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])

    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=manager.replay_pending_merges,
        pending_files=lambda: manager.pending_merges_for(handle),
        is_mid_rebase=manager.is_mid_rebase,
        continue_replay=manager.continue_replay,
        conflict_paths_reader=manager.unresolved_conflict_paths,
        enabled=True,
    )
    result = gate.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "merge conflicts" in result.content
    assert "backend/shared.js" in result.content

    # While the markers remain, a write outside the conflict set is refused
    # (forced resolution); reads are exempt so the resolver can consult
    # merge-clean sibling files.
    blocked = gate.wrap_tool_call(
        _make_request("write_file", {"file_path": "/workspace/backend/own.js"}), _ok_tool
    )
    assert blocked.content.startswith("Error: ARC rebase-on-merge")
    assert "unresolved merge conflicts" in blocked.content
    served_read = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/own.js"}), _ok_tool
    )
    assert served_read.content.startswith("ok")

    # The resolving edit (on the conflicted path) completes the replay at
    # its own boundary.
    shared.write_text("base;\nagent;\nsibling;\n", encoding="utf-8")
    completed = gate.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "were applied to this workspace" in completed.content
    assert not manager.is_mid_rebase(handle)
    # After completion, unrelated file work is served again.
    after = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/own.js"}), _ok_tool
    )
    assert after.content == "ok"


# ---------------------------------------------------------------------------
# fail-open
# ---------------------------------------------------------------------------


def test_aborted_replay_restores_the_dirty_tree(tmp_path: Path) -> None:
    """A mechanical failure (no-conflict rebase failure) rolls the worktree
    back to its pre-replay state: dirty files back, markers absent."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    shared = Path(handle.path) / "backend" / "shared.js"
    shared.write_text("base;\nagent;\n", encoding="utf-8")

    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])

    # Break the rebase mechanically: a read-only file the rebase must touch.
    shared.write_text("base;\nagent;\n", encoding="utf-8")
    shared.chmod(0o444)
    try:
        outcome = manager.replay_pending_merges(handle)
        assert outcome.status in {ReplayOutcome.ABORTED, ReplayOutcome.CONFLICTS}
        if outcome.status == ReplayOutcome.ABORTED:
            assert "<<<<<<<" not in shared.read_text(encoding="utf-8")
            assert shared.read_text(encoding="utf-8") == "base;\nagent;\n"
    finally:
        shared.chmod(0o644)


def test_replay_failure_raises_never__middleware_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception inside the replay call is swallowed by the middleware and
    the tool call proceeds against the old tree."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")

    def exploding_replay(_handle: WorktreeHandle) -> ReplayOutcome:
        raise RuntimeError("locked by dev server")

    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=exploding_replay,
        pending_files=lambda: [PendingMerge("SIB", "abc", ["backend/shared.js"])],
        enabled=True,
    )
    result = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert result.content == "ok"


def test_aborted_notice_is_silent(tmp_path: Path) -> None:
    outcome = ReplayOutcome(status=ReplayOutcome.ABORTED, detail="boom")
    gate = RebaseOnMergeMiddleware(
        handle=WorktreeHandle("A", "arc-node/A", "/tmp/x", "/tmp/x"),
        replay=lambda _h: outcome,
        pending_files=lambda: [PendingMerge("SIB", "abc", ["a.js"])],
        enabled=True,
    )
    result = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/a.js"}), _ok_tool
    )
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# soft guard
# ---------------------------------------------------------------------------


def test_soft_guard_disables_after_three_conflict_rounds() -> None:
    handle = WorktreeHandle("A", "arc-node/A", "/tmp/x", "/tmp/x")
    conflicts = ReplayOutcome(
        status=ReplayOutcome.CONFLICTS, files=["a.js"], attempted=True
    )
    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=lambda _h: conflicts,
        pending_files=lambda: [PendingMerge("SIB", "abc", ["a.js"])],
        is_mid_rebase=lambda _h: False,
        enabled=True,
    )
    seen: list[str] = []
    for round_index in range(4):
        result = gate.wrap_tool_call(
            _make_request("read_file", {"file_path": "/workspace/a.js"}, call_id=f"c{round_index}"),
            _ok_tool,
        )
        seen.append(result.content)
    # Rounds 1-3 carry the conflict notice; round 4 is disarmed (plain ok).
    assert seen[:3] == ["ok\n[ARC rebase-on-merge: replaying the sibling merge left merge conflicts in: a.js. "
                        "Resolve every conflict with edit_file/write_file (keep both sides' "
                        "behavior where they are compatible, prefer your node's contract "
                        "for what your requirement owns); reads stay available to inform "
                        "the resolution, and writes outside the conflicted files resume "
                        "once the last conflict is resolved.]"] * 3
    assert seen[3] == "ok"


# ---------------------------------------------------------------------------
# integration gate mutual exclusion
# ---------------------------------------------------------------------------


def test_replay_waits_for_in_flight_integration(tmp_path: Path) -> None:
    """The rebase reads the integration branch's tree; it must hold the
    reader side of the gate and stay out of an in-flight merge's way."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])

    release = threading.Event()
    # Hold the gate as writer the way integrate does.
    gate_ctx = manager.integration_gate.writer()
    gate_ctx.__enter__()

    outcome_box: dict[str, Any] = {}

    def run_replay() -> None:
        outcome_box["outcome"] = manager.replay_pending_merges(handle)

    thread = threading.Thread(target=run_replay, daemon=True)
    thread.start()
    try:
        thread.join(0.5)
        assert not outcome_box, "replay ran while the integration gate was held as writer"
    finally:
        gate_ctx.__exit__(None, None, None)
    thread.join(10)
    assert outcome_box["outcome"].status == ReplayOutcome.REPLAYED


# ---------------------------------------------------------------------------
# env gate
# ---------------------------------------------------------------------------


def test_env_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REBASE_ON_MERGE_ENV, raising=False)
    assert not rebase_on_merge_enabled()
    monkeypatch.setenv(REBASE_ON_MERGE_ENV, "1")
    assert rebase_on_merge_enabled()
    monkeypatch.setenv(REBASE_ON_MERGE_ENV, "off")
    assert not rebase_on_merge_enabled()


def test_middleware_disabled_by_default_skips_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(REBASE_ON_MERGE_ENV, raising=False)
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    calls: list[str] = []

    def recording_replay(_handle: WorktreeHandle) -> ReplayOutcome:
        calls.append("replay")
        return ReplayOutcome(status=ReplayOutcome.REPLAYED)

    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=recording_replay,
        pending_files=lambda: [PendingMerge("SIB", "abc", ["backend/shared.js"])],
        enabled=None,  # reads the env gate
    )
    result = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert result.content == "ok"
    assert calls == []


# ---------------------------------------------------------------------------
# workflow-level: attach point, settle cleanup, resume consistency
# ---------------------------------------------------------------------------


class _DrainTraceability:
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


class _DrainEvents:
    def __init__(self) -> None:
        self.rebase_events: list[dict[str, Any]] = []

    def record_rebase_replay(self, **kwargs: Any) -> None:
        self.rebase_events.append(kwargs)

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            return None

        return record


def _make_manager(tmp_path: Path, *, rebase_on_merge: bool) -> tuple[ARCWorkflowManager, _DrainEvents]:
    import os
    import shutil

    from core.workflow import ARCWorkflowManager

    workspace = tmp_path / "drain-workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    _git(["init", "-q"], workspace)
    _git(["config", "user.email", "test@example.com"], workspace)
    _git(["config", "user.name", "test"], workspace)
    (workspace / ".gitignore").write_text(".arc/*\n", encoding="utf-8")
    (workspace / "backend").mkdir()
    (workspace / "backend" / "shared.js").write_text("base;\n", encoding="utf-8")
    _git(["add", "-A"], workspace)
    _git(["commit", "-q", "-m", "init"], workspace)

    if rebase_on_merge:
        os.environ[REBASE_ON_MERGE_ENV] = "1"
    else:
        os.environ.pop(REBASE_ON_MERGE_ENV, None)

    events = _DrainEvents()
    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path="",
        web_port=4100,
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(
        traceability=_DrainTraceability(["R", "RA", "RB"]),
        events=events,
        git=SimpleNamespace(commit=lambda message: False),
    )
    return manager, events


def _drain_tree() -> dict[str, Any]:
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [
            {"id": "RA", "name": "leaf a", "description": "a", "children": []},
            {"id": "RB", "name": "leaf b", "description": "b", "children": []},
        ],
    }


def test_merge_attaches_pending_merge_to_inflight_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow-level attach: a sibling merge landing while another task
    is executing records the merge's changed files on its worktree."""

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setenv(REBASE_ON_MERGE_ENV, "1")
    manager, events = _make_manager(tmp_path, rebase_on_merge=True)
    try:
        queue_state = manager._load_or_create_processing_queue(_drain_tree())
        for task in queue_state["tasks"]:
            if task["phase"] == PHASE_DESIGN:
                queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
                queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
                task["status"] = TASK_COMPLETED

        replayed: dict[str, ReplayOutcome] = {}

        async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
            if task["node_id"] == "RB":
                # RB polls for RA's merge to land mid-flight (the merge runs
                # in a worker thread; a fixed sleep would race it).
                handle = ctx.handle
                for _ in range(200):
                    if manager._worktree_manager.pending_merges_for(handle):
                        break
                    await asyncio.sleep(0.05)
                pending = manager._worktree_manager.pending_merges_for(handle)
                if pending:
                    replayed["RB"] = manager._worktree_manager.replay_pending_merges(handle)
            else:
                Path(ctx.handle.path, "backend", "shared.js").write_text(
                    "base;\nra;\n", encoding="utf-8"
                )
            return True

        monkeypatch.setattr(manager, "_run_task", fake_run_task)
        asyncio.run(manager._drain_runnable_tasks(queue_state))

        assert replayed.get("RB") is not None
        assert replayed["RB"].status == ReplayOutcome.REPLAYED
        # RB saw RA's change after its replay.
        rb_pending = replayed["RB"].files
        assert "backend/shared.js" in rb_pending
    finally:
        os.environ.pop(REBASE_ON_MERGE_ENV, None)


def test_gate_off_keeps_no_pending_bookkeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-off: merges attach nothing and no replay state exists."""

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.delenv(REBASE_ON_MERGE_ENV, raising=False)
    manager, events = _make_manager(tmp_path, rebase_on_merge=False)
    try:
        assert not manager._rebase_on_merge
        queue_state = manager._load_or_create_processing_queue(_drain_tree())
        for task in queue_state["tasks"]:
            if task["phase"] == PHASE_DESIGN:
                queue_state["node_states"][task["node_id"]] = NODE_DESIGNED
                queue_state.setdefault("node_design_done", {})[task["node_id"]] = True
                task["status"] = TASK_COMPLETED

        async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
            if task["node_id"] == "RA":
                Path(ctx.handle.path, "backend", "shared.js").write_text(
                    "base;\nra;\n", encoding="utf-8"
                )
            else:
                import asyncio

                await asyncio.sleep(0.2)
            return True

        monkeypatch.setattr(manager, "_run_task", fake_run_task)
        asyncio.run(manager._drain_runnable_tasks(queue_state))

        assert not manager._worktree_manager._pending_merges
        assert not manager._worktree_manager._mid_rebase
        assert not manager._inflight
    finally:
        os.environ.pop(REBASE_ON_MERGE_ENV, None)


def test_settle_clears_pending_and_mid_rebase_state(tmp_path: Path) -> None:
    """A finished task never leaks replay bookkeeping into the next run."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    manager.record_pending_merge(handle, "REQ-B", "abc", ["backend/shared.js"])
    manager._mid_rebase.add(str(Path(handle.path)))
    manager._mid_rebase_base[str(Path(handle.path))] = "abc"

    manager.take_pending_merges(handle)
    outcome = manager.settle(handle, result=WorktreeTaskResult.FAILED)
    assert outcome is WorktreeOutcome.PRESERVED
    key = str(Path(handle.path))
    assert not manager.pending_merges_for(handle)
    assert key not in manager._mid_rebase
    assert key not in manager._mid_rebase_base


def test_resume_after_interrupted_replay_keeps_pending_records_consistent(
    tmp_path: Path,
) -> None:
    """``--resume`` semantics: a fresh drain starts with no in-flight
    registrations, and pending records only exist for live tasks."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    # Simulate an interrupted run: pending bookkeeping left behind.
    manager.record_pending_merge(handle, "REQ-B", "abc", ["backend/shared.js"])
    manager._mid_rebase.add(str(Path(handle.path)))

    # The drain's stale-inflight reset contract lives on the workflow; the
    # manager's per-task cleanup is exercised through settle above. Here:
    # a *new* prepare of the same node (resume path) still works and a
    # replay of the stale pending record is fail-open (the merge-base
    # either applies it or aborts; neither raises).
    outcome = manager.replay_pending_merges(handle)
    assert outcome.status in {ReplayOutcome.REPLAYED, ReplayOutcome.ABORTED, ReplayOutcome.CONFLICTS}


def test_task_runner_gate_provider_builds_a_middleware(tmp_path: Path) -> None:
    """The provider closure the adapters hold must build (not name-error).

    This pins the wiring bug the first mini benchmark caught: the closure
    referenced a ``node_id`` name that only exists at task scope, so every
    DESIGN agent build crashed with a NameError before any model call.
    """

    previous = os.environ.get(REBASE_ON_MERGE_ENV)
    os.environ[REBASE_ON_MERGE_ENV] = "1"
    try:
        manager, _events = _make_manager(tmp_path, rebase_on_merge=True)
        handle = manager._worktree_manager.prepare("REQ-A")
        runner = manager._build_task_phase_runner(handle.path, 4101, handle=handle)
        # The provider is attached to every adapter; invoking it must return
        # a middleware bound to this task's handle without raising.
        provider = runner.interface_designer._rebase_gate_provider
        assert provider is not None
        gate = provider()
        assert gate is not None
        assert gate._handle is handle
    finally:
        if previous is None:
            os.environ.pop(REBASE_ON_MERGE_ENV, None)
        else:
            os.environ[REBASE_ON_MERGE_ENV] = previous


# ---------------------------------------------------------------------------
# audit event chain (issue #179)
# ---------------------------------------------------------------------------


def _gate_with_pending_sibling(manager: ARCWorkflowManager) -> Any:
    """A workflow-built gate whose task has one applicable sibling merge.

    The gate comes from ``_build_task_rebase_gate`` - the workflow's own
    wiring of the audit hooks - not a hand-built middleware, so the tests
    below exercise the exact hook -> emit -> events-sink chain that shipped
    broken (every emit raised TypeError, swallowed fail-open, events lost).
    """

    wt = manager._worktree_manager
    handle = wt.prepare("REQ-A")
    sibling = wt.prepare("REQ-B")
    Path(sibling.path, "backend", "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    wt.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(wt, handle, ["backend/shared.js"])
    return manager._build_task_rebase_gate("REQ-A", handle)


def test_workflow_gate_emits_started_and_resolved_audit_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean replay through the workflow-built gate lands the ``started``
    and ``resolved`` (``replayed`` mapping retained) lifecycle events."""

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager, events = _make_manager(tmp_path, rebase_on_merge=True)
    gate = _gate_with_pending_sibling(manager)
    result = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "were applied to this workspace" in result.content
    assert [event["status"] for event in events.rebase_events] == ["started", "resolved"]
    assert events.rebase_events[0] == {
        "node_id": "REQ-A",
        "status": "started",
        "files": [],
        "message": "pending merge touched",
    }
    resolved = events.rebase_events[1]
    assert resolved["node_id"] == "REQ-A"
    assert "backend/shared.js" in resolved["files"]
    # A clean replay's outcome carries no detail: the event's message is None.
    assert resolved["message"] is None


def test_workflow_gate_writes_rebase_replay_events_to_runner_events_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit chain ends on disk: with the run's real ``EventClient`` all
    four lifecycle statuses land in ``.arc/runner-events.jsonl`` (ADR 0003's
    auditable chain), not just in a test sink. Three rounds drive the
    statuses: a clean replay (started, resolved), a conflicting replay
    (conflicts), and a mechanically aborted replay (aborted)."""

    from arcbench_agent_runtime.context import RuntimePaths
    from arcbench_agent_runtime.events import EventClient

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager, _events = _make_manager(tmp_path, rebase_on_merge=True)
    manager.runtime.events = EventClient(
        RuntimePaths.from_env(project_dir=manager.workspace_path)
    )
    wt = manager._worktree_manager

    # Round 1: clean replay.
    gate = _gate_with_pending_sibling(manager)
    gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )

    # Round 2: conflicting replay - the branch rewrites the same line the
    # new sibling merge rewrites.
    handle_c = wt.prepare("REQ-C")
    Path(handle_c.path, "backend", "shared.js").write_text(
        "base;\nrival;\n", encoding="utf-8"
    )
    sibling_d = wt.prepare("REQ-D")
    Path(sibling_d.path, "backend", "shared.js").write_text(
        "base;\nsibling2;\n", encoding="utf-8"
    )
    wt.integrate(sibling_d, "REQ-D (implement): shared")
    _record_pending(wt, handle_c, ["backend/shared.js"])
    gate_c = manager._build_task_rebase_gate("REQ-C", handle_c)
    result = gate_c.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "merge conflicts" in result.content

    # Round 3: aborted replay (stubbed outcome; the abort mechanics are
    # covered by test_aborted_replay_restores_the_dirty_tree).
    handle_e = wt.prepare("REQ-E")
    _record_pending(wt, handle_e, ["backend/shared.js"])
    wt.replay_pending_merges = lambda _h: ReplayOutcome(
        status=ReplayOutcome.ABORTED, detail="boom"
    )
    gate_e = manager._build_task_rebase_gate("REQ-E", handle_e)
    assert gate_e.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    ).content == "ok"

    events_path = Path(manager.workspace_path) / ".arc" / "runner-events.jsonl"
    rebase_events = [
        event for event in read_jsonl(events_path) if event["type"] == "rebase_replay"
    ]
    assert [event["status"] for event in rebase_events] == [
        "started",
        "resolved",
        "started",
        "conflicts",
        "started",
        "aborted",
    ]
    assert [event["node_id"] for event in rebase_events] == [
        "REQ-A",
        "REQ-A",
        "REQ-C",
        "REQ-C",
        "REQ-E",
        "REQ-E",
    ]


def test_workflow_gate_emits_conflicts_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflicting replay through the workflow-built gate lands the
    ``conflicts`` lifecycle event with the conflicted paths."""

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager, events = _make_manager(tmp_path, rebase_on_merge=True)
    wt = manager._worktree_manager
    handle = wt.prepare("REQ-A")
    shared = Path(handle.path, "backend", "shared.js")
    shared.write_text("base;\nagent;\n", encoding="utf-8")
    sibling = wt.prepare("REQ-B")
    Path(sibling.path, "backend", "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    wt.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(wt, handle, ["backend/shared.js"])

    gate = manager._build_task_rebase_gate("REQ-A", handle)
    result = gate.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "merge conflicts" in result.content
    assert [event["status"] for event in events.rebase_events] == ["started", "conflicts"]
    conflicts = events.rebase_events[1]
    assert conflicts["files"] == ["backend/shared.js"]
    assert conflicts["message"]


def test_workflow_gate_emits_aborted_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mechanically aborted replay lands the ``aborted`` event and the call
    still proceeds fail-open. The abort *mechanics* are covered by
    ``test_aborted_replay_restores_the_dirty_tree`` (which tolerates the
    conflict outcome because the rollback is environment-sensitive); here the
    audit chain is under test, so the replay is stubbed to return the ABORTED
    outcome deterministically."""

    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    manager, events = _make_manager(tmp_path, rebase_on_merge=True)
    wt = manager._worktree_manager
    handle = wt.prepare("REQ-A")
    _record_pending(wt, handle, ["backend/shared.js"])
    wt.replay_pending_merges = lambda _h: ReplayOutcome(
        status=ReplayOutcome.ABORTED, detail="boom"
    )
    gate = manager._build_task_rebase_gate("REQ-A", handle)
    result = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert result.content == "ok"
    assert [event["status"] for event in events.rebase_events] == ["started", "aborted"]
    assert events.rebase_events[1]["message"] == "boom"


def test_conflict_notice_includes_opposite_side_contract_cards() -> None:
    """Issue #127 item 4: the conflict notice carries the other side's
    registered interface cards (the arbiter's pruned card shape)."""

    handle = WorktreeHandle("REQ-A", "arc-node/REQ-A", "/tmp/x", "/tmp/x")
    conflicts = ReplayOutcome(status=ReplayOutcome.CONFLICTS, files=["backend/shared.js"])
    cards = {"REQ-B": {"interfaces": [{"interface_id": "REQ-B-api", "type": "API"}]}}
    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=lambda _h: conflicts,
        pending_files=lambda: [PendingMerge("REQ-B", "abc", ["backend/shared.js"])],
        conflict_contract_cards=lambda paths: cards if "backend/shared.js" in paths else {},
        enabled=True,
    )
    result = gate.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "merge conflicts" in result.content
    assert "REQ-B" in result.content and "REQ-B-api" in result.content


def test_conflict_notice_survives_a_failing_card_provider() -> None:
    """The cards are advisory: a provider failure never breaks the notice."""

    handle = WorktreeHandle("REQ-A", "arc-node/REQ-A", "/tmp/x", "/tmp/x")
    conflicts = ReplayOutcome(status=ReplayOutcome.CONFLICTS, files=["backend/shared.js"])
    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=lambda _h: conflicts,
        pending_files=lambda: [PendingMerge("REQ-B", "abc", ["backend/shared.js"])],
        conflict_contract_cards=lambda paths: (_ for _ in ()).throw(RuntimeError("store down")),
        enabled=True,
    )
    result = gate.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "merge conflicts" in result.content
    assert "interface contracts" not in result.content


def _conflicted_replay_scenario(
    tmp_path: Path,
) -> tuple[NodeWorktreeManager, WorktreeHandle, Path, Path]:
    """A prepared repo where the task's replay lands with conflict markers."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    shared = Path(handle.path) / "backend" / "shared.js"
    shared.write_text("base;\nagent;\n", encoding="utf-8")
    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")
    _record_pending(manager, handle, ["backend/shared.js"])
    outcome = manager.replay_pending_merges(handle)
    assert outcome.status == ReplayOutcome.CONFLICTS
    return manager, handle, shared, repo


def test_resolution_then_integrate_merges_cleanly(tmp_path: Path) -> None:
    """「冲突呈现与消解后合并成功」: after the agent resolves the markers and
    the boundary completes the rebase, the phase-end integrate succeeds."""

    manager, handle, shared, repo = _conflicted_replay_scenario(tmp_path)
    shared.write_text("base;\nagent;\nsibling;\n", encoding="utf-8")
    completed = manager.continue_replay(handle)
    assert completed.status == ReplayOutcome.REPLAYED
    # ``committed`` is False here: the replay's WIP/continue commits already
    # hold every change, so the merge commit has nothing new to stage. The
    # merge itself must succeed and the resolved content must land.
    committed, detail = manager.integrate(handle, "REQ-A (implement): resolved")
    assert "merged arc-node/REQ-A" in detail
    assert (repo / "backend" / "shared.js").read_text(encoding="utf-8") == (
        "base;\nagent;\nsibling;\n"
    )


def test_soft_guard_real_git_disarms_and_aborts_the_dangling_rebase(
    tmp_path: Path,
) -> None:
    """Real-git soft guard: three attempted conflict episodes disarm the pass,
    and a rebase left mid-replay at disarm is aborted back to the WIP state
    (no unmerged index reaches the phase-end integrate)."""

    manager, handle, shared, repo = _conflicted_replay_scenario(tmp_path)
    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=manager.replay_pending_merges,
        pending_files=lambda: manager.pending_merges_for(handle),
        is_mid_rebase=manager.is_mid_rebase,
        continue_replay=manager.continue_replay,
        abort_replay=manager.abort_replay,
        enabled=True,
    )

    def touching_call() -> Any:
        return gate.wrap_tool_call(
            _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}),
            _ok_tool,
        )

    # Episode 1: the scenario's mid-rebase conflict observed through the
    # gate (the continue attempt is passive while markers remain: no count
    # yet). Resolve it and let the next boundary complete the rebase.
    assert "merge conflicts" in touching_call().content
    shared.write_text("base;\nagent;\nsibling;\n", encoding="utf-8")
    assert "were applied to this workspace" in touching_call().content
    # The gate observed episode 1's attempted conflict (the scenario's
    # replay ran through gate-less manager calls; re-enter it honestly:
    # fresh conflict waves through the gate from here on).
    for round_index in range(3):
        sibling = manager.prepare(f"REQ-C{round_index}")
        (Path(sibling.path) / "backend" / "shared.js").write_text(
            f"base;\nsibling{round_index};\n", encoding="utf-8"
        )
        manager.integrate(sibling, f"REQ-C{round_index} (implement): shared")
        _record_pending(manager, handle, ["backend/shared.js"])
        # The gate's touch replays the new wave → conflict episode.
        result = touching_call()
        assert "merge conflicts" in result.content, round_index
        # Resolve so the next wave can conflict again (the continue
        # completion must not reset the streak - origin="continue").
        shared.write_text(
            f"base;\nagent;\nsibling;\nsibling{round_index};\n", encoding="utf-8"
        )
        touching_call()

    # The guard is disarmed and the dangling mid-rebase was aborted: the
    # worktree has no unmerged paths and no conflict markers remain.
    assert gate._disarmed
    assert not manager.is_mid_rebase(handle)
    assert "<<<<<<<" not in shared.read_text(encoding="utf-8")
    # A fourth wave records pending, but the disarmed gate never replays.
    sibling = manager.prepare("REQ-D")
    (Path(sibling.path) / "backend" / "new.js").write_text("d;\n", encoding="utf-8")
    manager.integrate(sibling, "REQ-D (implement): new")
    _record_pending(manager, handle, ["backend/new.js"])
    silent = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/new.js"}), _ok_tool
    )
    assert silent.content == "ok"
    # And the phase can still settle through the ordinary failure rails.
    settled = manager.settle(handle, result=WorktreeTaskResult.FAILED)
    assert settled is WorktreeOutcome.PRESERVED


def test_resume_pending_records_are_process_local_and_consistent(
    tmp_path: Path,
) -> None:
    """「--resume 后 pending 记录一致」: a resumed process starts with no
    pending bookkeeping; merges that land before the interruption are part
    of the recorded history and the fresh drain attaches only new merges.
    A stale pending record left in a *live* manager (the crash window) is
    replayed idempotently - the already-merged files report applied."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-A")
    sibling = manager.prepare("REQ-B")
    (Path(sibling.path) / "backend" / "shared.js").write_text(
        "base;\nsibling;\n", encoding="utf-8"
    )
    manager.integrate(sibling, "REQ-B (implement): shared")

    # A resumed process: fresh manager, same repository.
    resumed_manager = NodeWorktreeManager(str(repo))
    assert not any(resumed_manager._pending_merges.values())
    resumed_handle = resumed_manager.prepare("REQ-A")
    # The branch still sits on the pre-merge head: attaching the same merge
    # as pending and replaying brings the tree forward, consistent with the
    # recorded history.
    _record_pending(resumed_manager, resumed_handle, ["backend/shared.js"])
    outcome = resumed_manager.replay_pending_merges(resumed_handle)
    assert outcome.status == ReplayOutcome.REPLAYED
    assert (
        (Path(resumed_handle.path) / "backend" / "shared.js").read_text(encoding="utf-8")
        == "base;\nsibling;\n"
    )
    # Consumed on replay: a second identical attach reports applied.
    _record_pending(resumed_manager, resumed_handle, ["backend/shared.js"])
    second = resumed_manager.replay_pending_merges(resumed_handle)
    assert second.status == ReplayOutcome.REPLAYED


def test_forced_resolution_gates_writes_but_not_reads(tmp_path: Path) -> None:
    """A coordinating resolution may need to read merge-clean sibling files
    (renamed exports, changed signatures) before it can resolve the markers;
    reads endanger nothing mid-rebase and must be served, while writes
    outside the conflict set stay blocked until the markers clear."""

    manager, handle, shared, repo = _conflicted_replay_scenario(tmp_path)
    # A merge-clean sibling file the resolver may need to consult.
    sibling_clean = Path(handle.path) / "backend" / "sibling_clean.js"
    sibling_clean.write_text("export function siblingApi() {}\n", encoding="utf-8")

    gate = RebaseOnMergeMiddleware(
        handle=handle,
        replay=manager.replay_pending_merges,
        pending_files=lambda: manager.pending_merges_for(handle),
        is_mid_rebase=manager.is_mid_rebase,
        continue_replay=manager.continue_replay,
        conflict_paths_reader=manager.unresolved_conflict_paths,
        enabled=True,
    )
    # Read on a path outside the conflict set: served (the resolving agent
    # consults it), and it does not advance or consume anything.
    served_read = gate.wrap_tool_call(
        _make_request("read_file", {"file_path": "/workspace/backend/sibling_clean.js"}),
        _ok_tool,
    )
    assert served_read.content.startswith("ok")
    assert "Error" not in served_read.content
    assert manager.is_mid_rebase(handle)
    # Write outside the conflict set: still blocked.
    blocked_write = gate.wrap_tool_call(
        _make_request("write_file", {"file_path": "/workspace/backend/sibling_clean.js"}),
        _ok_tool,
    )
    assert blocked_write.content.startswith("Error: ARC rebase-on-merge")
    # The blocked message points at the reads-too escape hatch.
    assert "read any file" in blocked_write.content
    # Resolving the conflicted path completes the replay; the coordinating
    # write lands as an ordinary post-replay edit right after.
    shared.write_text("base;\nagent;\nsibling;\n", encoding="utf-8")
    completed = gate.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/shared.js"}), _ok_tool
    )
    assert "were applied to this workspace" in completed.content
    coordinated = gate.wrap_tool_call(
        _make_request("edit_file", {"file_path": "/workspace/backend/sibling_clean.js"}),
        _ok_tool,
    )
    assert coordinated.content == "ok"

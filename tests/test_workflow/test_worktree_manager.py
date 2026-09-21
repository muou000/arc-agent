"""Per-node worktree isolation must survive real git behaviour.

These tests run against actual git repositories (no mocks): worktree
registration, branch reuse after an interrupted run, merge-back into the
integration branch, conflict aborts, and junction safety for the shared
node_modules. They lock the contract the parallel drain relies on.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.worktree import (
    ArbitrationHooks,
    MergeArbitrationError,
    MergeConflictError,
    MergeVerificationError,
    NodeWorktreeManager,
    WorktreeError,
    WorktreeOutcome,
    WorktreeTaskResult,
)
from tests.helpers.faux import FauxChatModel, faux_text


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
    (repo / "frontend").mkdir()
    (repo / "backend" / "src.js").write_text("console.log('v1');\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo, NodeWorktreeManager(str(repo))


def test_prepare_creates_worktree_and_branch(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)

    handle = manager.prepare("REQ-1.1")

    assert (Path(handle.path) / "backend" / "src.js").exists()
    assert handle.branch == "arc-node/REQ-1.1"
    branches = _git(["branch", "--list", handle.branch], repo).stdout
    assert "arc-node/REQ-1.1" in branches
    assert manager._is_registered(Path(handle.path))


def test_prepare_reuses_registered_worktree_after_interruption(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")
    (Path(handle.path) / "note.txt").write_text("in-flight work", encoding="utf-8")

    reused = manager.prepare("REQ-1.1")

    assert Path(reused.path) == Path(handle.path)
    assert (Path(reused.path) / "note.txt").read_text(encoding="utf-8") == "in-flight work"


def test_prepare_reuses_existing_branch_without_worktree(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    _git(["branch", "arc-node/REQ-2"], repo)

    handle = manager.prepare("REQ-2")

    assert handle.branch == "arc-node/REQ-2"
    assert manager._is_registered(Path(handle.path))


def test_commit_and_integrate_merge_into_integration_branch(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")

    (Path(handle.path) / "backend" / "feature.js").write_text("feature;\n", encoding="utf-8")
    committed, detail = manager.integrate(handle, "REQ-1.1 (implement): feature")

    assert committed is True
    assert (repo / "backend" / "feature.js").exists(), "merge must land in the integration workspace"
    assert "merged" in detail


def test_integrate_with_no_changes_merges_cleanly(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")

    committed, _detail = manager.integrate(handle, "REQ-1.1 (implement): empty")

    assert committed is False
    assert (repo / "backend" / "src.js").exists()


def test_merge_conflict_aborts_and_preserves_worktree(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")
    # The worktree edits the same file the integration branch will edit.
    (Path(handle.path) / "backend" / "src.js").write_text("from worktree;\n", encoding="utf-8")
    manager.commit(handle, "wip")
    (repo / "backend" / "src.js").write_text("from integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "integration edit"], repo)

    with pytest.raises(MergeConflictError):
        manager.integrate(handle, "REQ-1.1 (implement): conflict")

    assert (repo / "backend" / "src.js").read_text(encoding="utf-8") == "from integration;\n", (
        "the conflicting merge must be aborted, leaving the integration branch untouched"
    )
    assert manager._is_registered(Path(handle.path)), "the worktree stays for inspection"
    # The repository must be left in a non-merging state.
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_settle_merged_node_worktree_is_deleted(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    shared = repo / "frontend" / "node_modules"
    (shared / "pkg").mkdir(parents=True)
    (shared / "pkg" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    handle = manager.prepare("REQ-1.1")
    worktree_node_modules = Path(handle.path) / "frontend" / "node_modules"
    assert (worktree_node_modules / "pkg" / "index.js").exists(), "junction must expose shared modules"

    outcome = manager.settle(handle, result=WorktreeTaskResult.MERGED)

    assert outcome is WorktreeOutcome.DELETED
    assert not Path(handle.path).exists(), "worktree directory removed"
    assert manager._branch_exists(handle.branch), "branch kept for audit"
    assert (shared / "pkg" / "index.js").exists(), "shared node_modules must survive junction cleanup"


def test_settle_refuses_to_delete_when_the_junction_cannot_be_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git worktree remove --force recurses through junctions and destroys the
    shared node_modules (verified empirically), so a settling that deletes must
    never reach it while a junction survives (PR #8 review)."""
    repo, manager = _init_repo(tmp_path)
    shared = repo / "frontend" / "node_modules"
    (shared / "pkg").mkdir(parents=True)
    (shared / "pkg" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    handle = manager.prepare("REQ-1.1")

    def broken_remove_link(link: Path) -> None:
        raise PermissionError(f"stubbed failure: {link}")

    monkeypatch.setattr("core.worktree._remove_link", broken_remove_link)

    with pytest.raises(WorktreeError, match="refusing to delete the worktree"):
        manager.settle(handle, result=WorktreeTaskResult.MERGED)

    assert (shared / "pkg" / "index.js").exists(), "shared node_modules untouched"
    assert manager._is_registered(Path(handle.path)), "worktree kept, git removal skipped"


def test_settle_failed_task_preserves_the_worktree(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")
    (Path(handle.path) / "scratch.txt").write_text("in-flight diagnostics", encoding="utf-8")

    outcome = manager.settle(handle, result=WorktreeTaskResult.FAILED)

    assert outcome is WorktreeOutcome.PRESERVED
    assert manager._is_registered(Path(handle.path)), "the worktree stays for inspection/retry"
    assert (Path(handle.path) / "scratch.txt").read_text(encoding="utf-8") == "in-flight diagnostics"


def test_settle_merged_reusable_group_worktree_survives(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(handle.path) / "backend" / "sibling.js").write_text("one;\n", encoding="utf-8")
    manager.integrate(handle, "REQ-2.1 design")

    outcome = manager.settle(handle, result=WorktreeTaskResult.MERGED)

    assert outcome is WorktreeOutcome.REUSED
    assert manager._is_registered(Path(handle.path)), "the group directory survives for the next task"


def test_settle_failed_reusable_group_worktree_is_preserved(tmp_path: Path) -> None:
    """A failed task's group directory is preserved like any failed task's:
    the next prepare's dirty check decides reuse vs fallback (unchanged)."""
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(handle.path) / "scratch.txt").write_text("failed state", encoding="utf-8")

    outcome = manager.settle(handle, result=WorktreeTaskResult.FAILED)

    assert outcome is WorktreeOutcome.PRESERVED
    assert manager._is_registered(Path(handle.path))
    assert (Path(handle.path) / "scratch.txt").read_text(encoding="utf-8") == "failed state"


def test_settle_retry_reset_restores_a_quarantined_group_worktree(tmp_path: Path) -> None:
    """A conflict quarantines the group directory; the requeue's reset (via
    settle with RESET_FOR_RETRY) un-quarantines it, resets the branch to the
    integration HEAD, and the directory survives for the subtree's next task."""
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(handle.path) / "backend" / "src.js").write_text("from worktree;\n", encoding="utf-8")
    manager.commit(handle, "wip")
    (repo / "backend" / "src.js").write_text("from integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "integration edit"], repo)
    with pytest.raises(MergeConflictError):
        manager.integrate(handle, "REQ-2.1 conflict")

    outcome = manager.settle(handle, result=WorktreeTaskResult.RESET_FOR_RETRY)

    assert outcome is WorktreeOutcome.REUSED
    assert manager._is_registered(Path(handle.path))
    assert not manager._worktree_dirty(handle.path), "the reset leaves the directory clean"
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    branch_head = _git(["rev-parse", handle.branch], repo).stdout.strip()
    assert branch_head == head, "the retried branch starts at the integration HEAD"
    other = manager.prepare("REQ-2.2", group_key="REQ-2")
    assert Path(other.path) == Path(handle.path), "the un-quarantined group dir is handed out again"


def test_settle_retry_reset_deletes_the_node_worktree_keeps_the_reset_branch(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")
    (Path(handle.path) / "backend" / "src.js").write_text("from worktree;\n", encoding="utf-8")
    manager.commit(handle, "wip")
    (repo / "backend" / "src.js").write_text("from integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "integration edit"], repo)
    with pytest.raises(MergeConflictError):
        manager.integrate(handle, "REQ-1.1 conflict")

    outcome = manager.settle(handle, result=WorktreeTaskResult.RESET_FOR_RETRY)

    assert outcome is WorktreeOutcome.DELETED
    assert not Path(handle.path).exists(), "the node-keyed directory is removed"
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    branch_head = _git(["rev-parse", handle.branch], repo).stdout.strip()
    assert branch_head == head, "the branch was reset to the integration HEAD before removal"
    assert manager._branch_exists(handle.branch), "the branch stays for the retry's prepare"


def test_integrate_commit_failure_names_the_branch(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")

    def broken_commit(_handle: object, _message: str) -> bool:
        raise WorktreeError("git commit failed: index.lock exists")

    manager.commit = broken_commit

    with pytest.raises(WorktreeError, match="commit on branch arc-node/REQ-1.1 failed"):
        manager.integrate(handle, "REQ-1.1 (implement): boom")


def test_worktree_is_invisible_to_the_parent_repository(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")
    (Path(handle.path) / "backend" / "dirty.js").write_text("dirty;\n", encoding="utf-8")

    status = _git(["status", "--short"], repo).stdout.strip()

    assert status == "", f"the parent repo must not see worktree contents, saw: {status!r}"


def test_seed_frontend_dist_copies_build_output(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    dist = repo / "frontend" / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    handle = manager.prepare("REQ-1.1")

    assert (Path(handle.path) / "frontend" / "dist" / "index.html").exists()


def test_prune_clears_stale_registrations(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-1.1")
    shutil.rmtree(handle.path)  # simulate an interrupted deletion

    manager.prune()

    assert not manager._is_registered(Path(handle.path))


def test_git_failure_raises_worktree_error(tmp_path: Path) -> None:
    # A directory that is not a git repository: worktree registration fails.
    plain = tmp_path / "plain"
    plain.mkdir()
    manager = NodeWorktreeManager(str(plain))

    with pytest.raises(WorktreeError):
        manager.prepare("REQ-1.1")


# ----------------------------------------------------------------------
# subtree worktree reuse
# ----------------------------------------------------------------------


def test_group_worktree_is_reused_across_sibling_nodes(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)

    first = manager.prepare("REQ-2.1", group_key="REQ-2")
    assert Path(first.path).name == "REQ-2", "the group directory is keyed by the subtree"
    assert first.reusable is True
    (Path(first.path) / "backend" / "sibling.js").write_text("one;\n", encoding="utf-8")
    manager.integrate(first, "REQ-2.1 design")

    second = manager.prepare("REQ-2.2", group_key="REQ-2")

    assert Path(second.path) == Path(first.path), "sibling tasks reuse the group worktree"
    assert second.branch == "arc-node/REQ-2.2", "branches stay per node"
    assert (Path(second.path) / "backend" / "sibling.js").exists(), (
        "the new node's branch starts at the latest integration HEAD"
    )
    assert not manager._worktree_dirty(second.path), "the reused worktree must be clean"
    branches = _git(["branch", "--list", "arc-node/REQ-2.1"], repo).stdout
    assert "arc-node/REQ-2.1" in branches, "per-node branches stay for audit"


def test_node_keyed_prepare_is_unaffected_by_group_reuse(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)

    handle = manager.prepare("REQ-2.1")

    assert Path(handle.path).name == "REQ-2.1"
    assert handle.reusable is False
    assert manager._is_registered(Path(handle.path))


def test_conflict_quarantines_group_dir_and_falls_back(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(handle.path) / "backend" / "src.js").write_text("from worktree;\n", encoding="utf-8")
    manager.commit(handle, "wip")
    (repo / "backend" / "src.js").write_text("from integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "integration edit"], repo)

    with pytest.raises(MergeConflictError):
        manager.integrate(handle, "REQ-2.1 conflict")

    other = manager.prepare("REQ-2.2", group_key="REQ-2")

    assert Path(other.path).name == "REQ-2.2", "a quarantined group dir is not handed to another node"
    assert manager._is_registered(Path(handle.path)), "the quarantined dir stays for inspection"


def test_dirty_group_dir_falls_back_without_touching_leftovers(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(handle.path) / "scratch.txt").write_text("half-written", encoding="utf-8")

    other = manager.prepare("REQ-2.2", group_key="REQ-2")

    assert Path(other.path).name == "REQ-2.2", "a dirty group dir must not mix states"
    assert (Path(handle.path) / "scratch.txt").read_text(encoding="utf-8") == "half-written", (
        "the crashed sibling's leftovers stay untouched"
    )


def test_same_node_retry_reuses_its_dirty_group_dir(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(handle.path) / "scratch.txt").write_text("in-flight work", encoding="utf-8")

    retried = manager.prepare("REQ-2.1", group_key="REQ-2")

    assert Path(retried.path) == Path(handle.path), "a retried node keeps its in-flight state"
    assert (Path(retried.path) / "scratch.txt").read_text(encoding="utf-8") == "in-flight work"


def test_cleanup_reusable_worktrees_keeps_quarantined_and_dirty(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)

    ok = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(ok.path) / "backend" / "ok.js").write_text("ok;\n", encoding="utf-8")
    manager.integrate(ok, "REQ-2.1 ok")

    conflicted = manager.prepare("REQ-3.1", group_key="REQ-3")
    (Path(conflicted.path) / "backend" / "src.js").write_text("from worktree;\n", encoding="utf-8")
    manager.commit(conflicted, "wip")
    (repo / "backend" / "src.js").write_text("from integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "integration edit"], repo)
    with pytest.raises(MergeConflictError):
        manager.integrate(conflicted, "REQ-3.1 conflict")

    dirty = manager.prepare("REQ-4.1", group_key="REQ-4")
    (Path(dirty.path) / "scratch.txt").write_text("half-written", encoding="utf-8")

    removed = manager.cleanup_reusable_worktrees()

    assert not Path(ok.path).exists(), "clean successful group worktrees are removed"
    assert manager._is_registered(Path(conflicted.path)), "quarantined worktrees stay"
    assert manager._is_registered(Path(dirty.path)), "dirty worktrees stay"
    assert Path(ok.path).name in [Path(path).name for path in removed]


# ----------------------------------------------------------------------
# additive conflict resolution
# ----------------------------------------------------------------------


def test_integrate_resolves_append_only_conflicts(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('v1');\nconst a = require('./a');\nroute('/a', a);\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('v1');\nconst b = require('./b');\nroute('/b', b);\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "append b"], repo)

    committed, detail = manager.integrate(handle, "REQ-2.1 append")

    assert committed is True
    merged = (repo / "backend" / "src.js").read_text(encoding="utf-8")
    assert "require('./a')" in merged and "route('/a', a)" in merged
    assert "require('./b')" in merged and "route('/b', b)" in merged
    assert "console.log('v1')" in merged, "the common base stays exactly once"
    assert "additive" in detail
    assert not (repo / ".git" / "MERGE_HEAD").exists(), "the merge commit completes"


def test_integrate_additive_resolution_fails_closed_on_health_gate(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('v1');\nconst a = require('./a');\n", encoding="utf-8"
    )
    manager.commit(handle, "append a")
    (repo / "backend" / "src.js").write_text(
        "console.log('v1');\nconst b = require('./b');\n", encoding="utf-8"
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "append b"], repo)

    with pytest.raises(MergeVerificationError, match="post-merge verification"):
        manager.integrate(handle, "REQ-2.1 append", verify=lambda: "backend unhealthy")

    assert (repo / "backend" / "src.js").read_text(encoding="utf-8") == (
        "console.log('v1');\nconst b = require('./b');\n"
    ), "the aborted merge restores the integration workspace"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert manager._is_registered(Path(handle.path)), "the worktree stays for inspection"


def test_integrate_additive_resolution_with_passing_gate_commits(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('v1');\nconst a = require('./a');\n", encoding="utf-8"
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('v1');\nconst b = require('./b');\n", encoding="utf-8"
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "append b"], repo)

    committed, _detail = manager.integrate(handle, "REQ-2.1 append", verify=lambda: None)

    assert committed is True
    merged = (repo / "backend" / "src.js").read_text(encoding="utf-8")
    assert "require('./a')" in merged and "require('./b')" in merged


def test_integrate_does_not_union_new_file_conflicts(tmp_path: Path) -> None:
    """add/add of a brand-new file is a genuine semantic conflict (two agents
    authored different content for the same path) and must not be merged by
    concatenation."""
    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "new.js").write_text("from worktree;\n", encoding="utf-8")
    manager.commit(handle, "add new file")
    (repo / "backend" / "new.js").write_text("from integration;\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "add same file on integration"], repo)

    with pytest.raises(MergeConflictError):
        manager.integrate(handle, "REQ-2.1 add/add")

    assert not (repo / ".git" / "MERGE_HEAD").exists()


# ----------------------------------------------------------------------
# integration-workspace services (merge-arbitration hand-off)
# ----------------------------------------------------------------------


def test_stage_paths_and_commit_integration_follow_up_commit(tmp_path: Path) -> None:
    """The post-merge arbitration repair lands as its own follow-up commit on
    the integration branch; a byte-identical repair reports nothing to commit."""
    repo, manager = _init_repo(tmp_path)
    (repo / "backend" / "drift.js").write_text("repaired;\n", encoding="utf-8")

    manager.stage_paths(["backend/drift.js"])
    assert manager.commit_integration("repair anchors") is True
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert (repo / "backend" / "drift.js").exists()

    manager.stage_paths(["backend/drift.js"])
    assert manager.commit_integration("repair anchors") is False, (
        "an already-landed tree has nothing to commit"
    )
    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head


# ----------------------------------------------------------------------
# LLM merge arbitration (issue #81)
# ----------------------------------------------------------------------


def _real_arbitration_hooks(
    manager: NodeWorktreeManager,
    model: FauxChatModel,
    traceability: Any,
    *,
    budget_spent: bool = False,
) -> tuple[ArbitrationHooks, dict[str, Any]]:
    """Production-shaped hooks: real index reads through the manager's public
    interface, real contract-card collection, one shared budget flag."""

    from core.merge_arbitration import (
        ArbitrationInput,
        MergeArbiter,
        TRIGGER_HEALTH_GATE,
        collect_contract_cards,
        read_workspace_file,
    )

    state = {"budget_spent": budget_spent}
    events: list[dict[str, Any]] = []

    def collect_input(conflict_paths: list[str], trigger: str, gate_failure: str = "") -> Any:
        if state["budget_spent"]:
            return None
        stages = manager.read_conflict_stages(conflict_paths)
        if trigger == TRIGGER_HEALTH_GATE:
            # Mirror the workflow's collect hook: the mechanical resolution
            # already staged the files, so the arbiter repairs the resolved
            # content from the working tree.
            for path, entry in stages.items():
                entry["resolved"] = read_workspace_file(manager.main_workspace, path)
        return ArbitrationInput(
            trigger=trigger,
            ours_label="REQ-2",
            theirs_label="REQ-3",
            files=stages,
            contract_cards=collect_contract_cards(traceability, ["REQ-2", "REQ-3"]),
            gate_failure=gate_failure,
        )

    def run(arbitration_input: Any, conflict_paths: list[str], trigger: str) -> str | None:
        state["budget_spent"] = True
        arbiter = MergeArbiter(
            model=model,
            workspace_path=manager.main_workspace,
            emit_event=events.append,
        )
        result = asyncio.run(
            arbiter.arbitrate("worktree", "master", arbitration_input, node_id="REQ-3")
        )
        if not result.accepted:
            return result.detail
        manager.stage_paths(result.applied)
        return None

    hooks = ArbitrationHooks(collect_input=collect_input, run=run)
    return hooks, {"state": state, "events": events}


class _FakeTraceability:
    """Minimal traceability surface for contract-card collection."""

    def __init__(self, interfaces_by_req: dict[str, list[dict[str, Any]]]) -> None:
        self._interfaces_by_req = interfaces_by_req

    def list_interfaces(self, req_id: str | None = None) -> list[dict[str, Any]]:
        if req_id is None:
            return [row for rows in self._interfaces_by_req.values() for row in rows]
        return list(self._interfaces_by_req.get(req_id, []))

    def get_node_contract(self, req_id: str) -> dict[str, Any] | None:
        return None


def test_integrate_arbitration_resolves_semantic_conflict(tmp_path: Path) -> None:
    """Two nodes write different implementations of the same lines (a
    non-additive conflict). With arbitration enabled the arbiter's merged
    version lands, the health gate passes, and the merge commit completes."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    # Non-additive: the worktree *rewrites* the base line, integration rewrote
    # it differently - no side only appends, so the mechanical resolver
    # refuses and arbitration is the only path to a merge.
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('integration side wins routes');\nroute('/a', a);\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('worktree side wins routes');\nroute('/b', b);\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "sibling rewrite"], repo)
    resolved_content = (
        "console.log('both sides win routes');\nroute('/a', a);\nroute('/b', b);\n"
    )
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": resolved_content}))])
    traceability = _FakeTraceability(
        {
            "REQ-2": [{"interface_id": "IF-A", "type": "API", "content": "GET /a", "file_path": "backend/src.js"}],
            "REQ-3": [{"interface_id": "IF-B", "type": "API", "content": "GET /b", "file_path": "backend/src.js"}],
        }
    )
    hooks, audit = _real_arbitration_hooks(manager, model, traceability)

    committed, detail = manager.integrate(
        handle,
        "REQ-2.1 semantic conflict",
        verify=lambda: None,
        arbiter=hooks,
    )

    assert committed is True
    assert (repo / "backend" / "src.js").read_text(encoding="utf-8") == resolved_content
    assert "LLM arbitration" in detail
    assert not (repo / ".git" / "MERGE_HEAD").exists(), "the merge commit completes"
    # The arbitration prompt saw the three-way contents and both cards.
    prompt = str(model.calls[0][0].content)
    assert "route('/a', a)" in prompt and "route('/b', b)" in prompt
    assert "IF-A" in prompt and "IF-B" in prompt
    # Audit trail: one applied arbitration record.
    applied = [event for event in audit["events"] if event["outcome"] == "applied"]
    assert len(applied) == 1
    assert applied[0]["trigger"] == "conflict"


def test_integrate_arbitration_health_gate_repairs_boot_failure(tmp_path: Path) -> None:
    """An additively resolved merge whose health gate fails (backend will not
    boot) is repaired by the arbiter and re-verifies green before landing."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('v1');\nconst a = require('./a');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('v1');\nconst b = require('./b');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "append b"], repo)

    gate_state = {"calls": 0}

    def verify() -> str | None:
        gate_state["calls"] += 1
        # First probe: the mechanical resolution is broken (e.g. duplicate
        # route mounts). After arbitration the tree boots and serves.
        if gate_state["calls"] == 1:
            return "backend runtime failed to start on the merged workspace"
        return None

    fixed_content = (
        "console.log('v1');\nconst a = require('./a');\nconst b = require('./b');\n"
    )
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": fixed_content}))])
    hooks, audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}))

    committed, detail = manager.integrate(
        handle, "REQ-2.1 boot repair", verify=verify, arbiter=hooks
    )

    assert committed is True
    assert (repo / "backend" / "src.js").read_text(encoding="utf-8") == fixed_content
    assert gate_state["calls"] == 2, "the gate re-verified after the repair"
    assert "health gate passed after LLM arbitration repair" in detail
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    applied = [event for event in audit["events"] if event["outcome"] == "applied"]
    assert len(applied) == 1
    assert applied[0]["trigger"] == "health-gate"


def test_integrate_arbitration_budget_is_one_call_per_node(tmp_path: Path) -> None:
    """The second arbitration trigger for the same node never calls the model:
    it goes straight to the existing failure path."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('integration side wins');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('worktree side wins');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "sibling rewrite"], repo)
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "resolved;\n"}))])
    hooks, audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}))

    committed, _detail = manager.integrate(
        handle, "REQ-2.1 first conflict", arbiter=hooks
    )
    assert committed is True

    # A second conflicting merge for the same node's budget: the collect hook
    # declines (budget spent) and integrate must fail with the plain
    # MergeConflictError, exactly like the pre-arbitration behavior.
    handle2 = manager.prepare("REQ-3.1")
    (Path(handle2.path) / "backend" / "src.js").write_text(
        "console.log('another rewrite');\n",
        encoding="utf-8",
    )
    manager.commit(handle2, "another rewrite")
    (repo / "backend" / "src.js").write_text(
        "console.log('resolved;\nchanged again');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "rewrite again"], repo)

    with pytest.raises(MergeConflictError) as excinfo:
        manager.integrate(handle2, "REQ-3.1 second conflict", arbiter=hooks)

    assert not isinstance(excinfo.value, MergeArbitrationError)
    assert model.call_count == 1, "the model is called exactly once across both triggers"
    assert audit["state"]["budget_spent"] is True


def test_integrate_arbitration_failure_aborts_and_preserves_worktree(tmp_path: Path) -> None:
    """An arbitration whose output still fails the health gate aborts the
    merge, preserves the worktree, and raises MergeArbitrationError."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    # Additive conflict (both sides only append): the mechanical resolution
    # runs, the gate fails, and arbitration's repair must also fail the gate.
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('v1');\nconst a = require('./a');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('v1');\nconst b = require('./b');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "append b"], repo)
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "still broken;\n"}))])
    hooks, _audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}))

    with pytest.raises(MergeArbitrationError, match="post-merge verification"):
        manager.integrate(
            handle,
            "REQ-2.1 arbitration fails gate",
            verify=lambda: "backend unhealthy",
            arbiter=hooks,
        )

    assert (repo / "backend" / "src.js").read_text(encoding="utf-8") == (
        "console.log('v1');\nconst b = require('./b');\n"
    ), "the aborted merge restores the integration workspace"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert manager._is_registered(Path(handle.path)), "the worktree stays for inspection"


def test_integrate_conflict_arbitration_spends_the_only_budget(tmp_path: Path) -> None:
    """Trigger 1 (conflict arbitration) and trigger 2 (health-gate repair)
    share one budget: a conflict resolved by arbitration has no budget left
    when the health gate then fails, so the plain MergeVerificationError
    aborts the merge."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('worktree side wins');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('integration side wins');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "sibling rewrite"], repo)
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "arbitrated;\n"}))])
    hooks, audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}))

    with pytest.raises(MergeVerificationError, match="post-merge verification"):
        manager.integrate(
            handle,
            "REQ-2.1 conflict then gate failure",
            verify=lambda: "backend unhealthy",
            arbiter=hooks,
        )

    assert model.call_count == 1, "only the conflict arbitration consumed a model call"
    assert audit["state"]["budget_spent"] is True
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert manager._is_registered(Path(handle.path)), "the worktree stays for inspection"


def test_integrate_arbitration_declined_keeps_plain_conflict_error(tmp_path: Path) -> None:
    """When the hooks decline (budget spent) on a conflict, the raised error
    is the plain MergeConflictError — the pre-arbitration behavior."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('worktree side wins');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('integration side wins');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "sibling rewrite"], repo)
    model = FauxChatModel(responses=[])
    hooks, _audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}), budget_spent=True)

    with pytest.raises(MergeConflictError) as excinfo:
        manager.integrate(handle, "REQ-2.1 declined", arbiter=hooks)

    assert not isinstance(excinfo.value, MergeArbitrationError)
    assert model.call_count == 0, "a declined arbitration never reaches the model"
    assert "The worktree is preserved for inspection." in str(excinfo.value)


def test_integrate_arbitration_rejects_echoed_conflict_markers(tmp_path: Path) -> None:
    """An arbiter that echoes git conflict markers back instead of resolving
    them is rejected; the merge aborts with the plain conflict semantics."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('worktree side wins');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('integration side wins');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "sibling rewrite"], repo)
    echoed = (
        "<<<<<<< HEAD\nconsole.log('integration side wins');\n"
        "=======\nconsole.log('worktree side wins');\n>>>>>>>\n"
    )
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": echoed}))])
    hooks, _audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}))

    with pytest.raises(MergeConflictError, match="did not resolve") as excinfo:
        manager.integrate(handle, "REQ-2.1 marker echo", arbiter=hooks)

    assert isinstance(excinfo.value, MergeConflictError)
    assert model.call_count == 1
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_integrate_arbitration_reports_reverify_result(tmp_path: Path) -> None:
    """The post-repair re-verification result is reported through
    ``on_reverified`` so the audit trail records it."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('v1');\nconst a = require('./a');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('v1');\nconst b = require('./b');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "append b"], repo)
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "fixed;\n"}))])
    hooks, _audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}))
    reverify_results: list[str | None] = []
    hooks.on_reverified = reverify_results.append
    gate_calls = {"count": 0}

    def verify() -> str | None:
        gate_calls["count"] += 1
        # First probe fails (the mechanical resolution is broken); after the
        # arbitration repair the tree boots and serves.
        return "boot failed" if gate_calls["count"] == 1 else None

    committed, _detail = manager.integrate(
        handle, "REQ-2.1 boot repair", verify=verify, arbiter=hooks
    )

    assert committed is True
    assert reverify_results == [None], "the passing re-verification is reported"


def test_integrate_arbitration_reports_failed_reverify_result(tmp_path: Path) -> None:
    """A failing post-repair re-verification is reported (and the merge
    aborts with MergeArbitrationError)."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "src.js").write_text(
        "console.log('v1');\nconst a = require('./a');\n",
        encoding="utf-8",
    )
    (repo / "backend" / "src.js").write_text(
        "console.log('v1');\nconst b = require('./b');\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "append b"], repo)
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "still broken;\n"}))])
    hooks, _audit = _real_arbitration_hooks(manager, model, _FakeTraceability({}))
    reverify_results: list[str | None] = []
    hooks.on_reverified = reverify_results.append

    with pytest.raises(MergeArbitrationError):
        manager.integrate(
            handle, "REQ-2.1 boot repair fails", verify=lambda: "backend unhealthy", arbiter=hooks
        )

    assert reverify_results == ["backend unhealthy"]


# ----------------------------------------------------------------------
# Issue #91: prepare/reset must exclude integrate on the integration gate
# ----------------------------------------------------------------------


def _hold_gate(gate: Any, mode: str, release: threading.Event) -> None:
    """Hold the integration gate in ``mode`` ("reader"/"writer") on a worker
    thread until ``release`` is set; return once the gate is held."""

    held = threading.Event()

    def run() -> None:
        with getattr(gate, mode)():
            held.set()
            release.wait(10)

    threading.Thread(target=run, daemon=True).start()
    assert held.wait(10), f"gate never entered {mode}"


def _prepare_in_thread(manager: NodeWorktreeManager, node_id: str, group_key: str):
    """Run manager.prepare on a worker thread; return (thread, result box)."""

    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["handle"] = manager.prepare(node_id, group_key=group_key)
        except Exception as exc:  # pragma: no cover - failure is the signal
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_prepare_waits_for_in_flight_integration(tmp_path: Path) -> None:
    """Issue #91's window, pinned as a mutual-exclusion contract.

    A group worktree's prepare checks out the integration branch into the
    worktree while another group's integrate may be moving that branch. A
    checkout that lost that race materialized its index without its files and
    the task's ``git add -A .`` staged sibling-owned files as deletions. The
    gate's writer side must keep prepare out of integrate's critical section.
    """

    repo, manager = _init_repo(tmp_path)
    first = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(first.path) / "backend" / "one.js").write_text("one;\n", encoding="utf-8")
    manager.integrate(first, "REQ-2.1 (implement): one")

    release = threading.Event()
    _hold_gate(manager.integration_gate, "writer", release)

    # REQ-2.2 has no branch yet, so prepare takes the checkout -B path that
    # lost the race in issue #91.
    thread, outcome = _prepare_in_thread(manager, "REQ-2.2", "REQ-2")
    try:
        thread.join(0.5)
        assert not outcome, "prepare ran while the integration gate was held as writer"
    finally:
        release.set()
    thread.join(10)
    assert "handle" in outcome, f"prepare never completed: {outcome.get('error')}"
    # The checkout -B started from the integration HEAD that already holds the
    # merged sibling file.
    assert (Path(outcome["handle"].path) / "backend" / "one.js").exists()


def test_integrate_waits_for_in_flight_prepare(tmp_path: Path) -> None:
    """The writer side pinned through integrate itself: while a prepare holds
    the gate as a reader, the merge must wait (without this, deleting
    integrate's writer acquisition would pass every other test)."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")
    (Path(handle.path) / "backend" / "feature.js").write_text("feature;\n", encoding="utf-8")

    release = threading.Event()
    _hold_gate(manager.integration_gate, "reader", release)

    outcome: dict[str, Any] = {}

    def run_integrate() -> None:
        try:
            outcome["result"] = manager.integrate(handle, "REQ-2.1 (implement): feature")
        except Exception as exc:  # pragma: no cover - failure is the signal
            outcome["error"] = exc

    thread = threading.Thread(target=run_integrate, daemon=True)
    thread.start()
    try:
        thread.join(0.5)
        assert not outcome, "integrate ran while a prepare held the gate as reader"
    finally:
        release.set()
    thread.join(10)
    assert "result" in outcome, f"integrate never completed: {outcome.get('error')}"
    assert (repo / "backend" / "feature.js").exists(), "merge must land after waiting"


def test_retry_reset_settle_waits_for_in_flight_integration(
    tmp_path: Path,
) -> None:
    """The conflict-requeue reset checks out the integration tree too, so the
    manager's settle for ``RESET_FOR_RETRY`` holds the same exclusion against
    an in-flight integrate (issue #91, exercised through the #105 seam)."""

    repo, manager = _init_repo(tmp_path)
    handle = manager.prepare("REQ-2.1")

    release = threading.Event()
    _hold_gate(manager.integration_gate, "writer", release)

    outcome: dict[str, Any] = {}

    def run_settle() -> None:
        try:
            outcome["result"] = manager.settle(
                handle, result=WorktreeTaskResult.RESET_FOR_RETRY
            )
        except Exception as exc:  # pragma: no cover - failure is the signal
            outcome["error"] = exc

    reset_thread = threading.Thread(target=run_settle, daemon=True)
    reset_thread.start()
    try:
        reset_thread.join(0.5)
        assert not outcome, "settle ran while the integration gate was held as writer"
    finally:
        release.set()
    reset_thread.join(10)
    assert "result" in outcome, f"settle never completed: {outcome.get('error')}"
    assert outcome["result"] is WorktreeOutcome.DELETED


def test_prepare_raises_when_the_checkout_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swallowed checkout failure is what let the index hold files the disk
    never materialized (issue #91's corruption precondition); it must raise."""

    repo, manager = _init_repo(tmp_path)
    first = manager.prepare("REQ-2.1", group_key="REQ-2")
    (Path(first.path) / "backend" / "one.js").write_text("one;\n", encoding="utf-8")
    manager.integrate(first, "REQ-2.1 (implement): one")

    # A new node in the registered group directory takes the checkout -B
    # path; point it at a ref that cannot exist so the checkout fails.
    monkeypatch.setattr(manager, "_integration_branch", lambda: "no-such-integration-ref")
    with pytest.raises(WorktreeError):
        manager.prepare("REQ-2.2", group_key="REQ-2")


def test_concurrent_prepares_share_the_integration_gate(tmp_path: Path) -> None:
    """The reader side of the gate: concurrent prepares must NOT serialize.

    Sibling tasks of different subtrees start concurrently, each preparing
    its worktree with a checkout from the integration branch. Readers touch
    disjoint worktrees and read a branch no reader moves, so one prepare
    holding the gate as a reader must not block another (serializing them
    broke the sibling-overlap contract the drain tests pin).
    """

    repo, manager = _init_repo(tmp_path)
    first = manager.prepare("REQ-2.1", group_key="REQ-2")

    with manager.integration_gate.reader():
        thread, outcome = _prepare_in_thread(manager, "REQ-3.1", "REQ-3")
        thread.join(10)
        assert "handle" in outcome, (
            f"a reader prepare must not wait for another reader: {outcome.get('error')}"
        )
        assert Path(outcome["handle"].path) != Path(first.path)

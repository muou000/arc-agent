"""Per-node worktree isolation must survive real git behaviour.

These tests run against actual git repositories (no mocks): worktree
registration, branch reuse after an interrupted run, merge-back into the
integration branch, conflict aborts, and junction safety for the shared
node_modules. They lock the contract the parallel drain relies on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.worktree import (
    MergeConflictError,
    MergeVerificationError,
    NodeWorktreeManager,
    WorktreeError,
)


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


def test_discard_removes_worktree_keeps_branch_and_shared_node_modules(tmp_path: Path) -> None:
    repo, manager = _init_repo(tmp_path)
    shared = repo / "frontend" / "node_modules"
    (shared / "pkg").mkdir(parents=True)
    (shared / "pkg" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    handle = manager.prepare("REQ-1.1")
    worktree_node_modules = Path(handle.path) / "frontend" / "node_modules"
    assert (worktree_node_modules / "pkg" / "index.js").exists(), "junction must expose shared modules"

    manager.discard(handle)

    assert not Path(handle.path).exists(), "worktree directory removed"
    assert manager._branch_exists(handle.branch), "branch kept for audit"
    assert (shared / "pkg" / "index.js").exists(), "shared node_modules must survive junction cleanup"


def test_discard_refuses_when_the_junction_cannot_be_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git worktree remove --force recurses through junctions and destroys the
    shared node_modules (verified empirically), so discard() must never reach
    it while a junction survives (PR #8 review)."""
    repo, manager = _init_repo(tmp_path)
    shared = repo / "frontend" / "node_modules"
    (shared / "pkg").mkdir(parents=True)
    (shared / "pkg" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    handle = manager.prepare("REQ-1.1")

    def broken_remove_link(link: Path) -> None:
        raise PermissionError(f"stubbed failure: {link}")

    monkeypatch.setattr("core.worktree._remove_link", broken_remove_link)

    with pytest.raises(WorktreeError, match="refusing to delete the worktree"):
        manager.discard(handle)

    assert (shared / "pkg" / "index.js").exists(), "shared node_modules untouched"
    assert manager._is_registered(Path(handle.path)), "worktree kept, git removal skipped"


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

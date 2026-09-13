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

from core.worktree import MergeConflictError, NodeWorktreeManager, WorktreeError


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

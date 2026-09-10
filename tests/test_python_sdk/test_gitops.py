"""Tests for ``arcbench_agent_runtime.gitops.GitClient``.

These tests run real ``git`` commands against a temporary directory. The git
executable must be available on the PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from arcbench_agent_runtime.context import RuntimePaths
from arcbench_agent_runtime.events import EventClient
from arcbench_agent_runtime.gitops import (
    ARC_GITIGNORE_END,
    ARC_GITIGNORE_START,
    DEFAULT_GIT_USER_EMAIL,
    DEFAULT_GIT_USER_NAME,
    GitClient,
)


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _git_available(), reason="git executable not available"
)


@pytest.fixture
def git_paths(tmp_project_dir: Path) -> RuntimePaths:
    return RuntimePaths.from_env(project_dir=str(tmp_project_dir))


@pytest.fixture
def git_client(git_paths: RuntimePaths) -> GitClient:
    return GitClient(git_paths, EventClient(git_paths))


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_default_identity(
        self,
        git_client: GitClient,
        git_paths: RuntimePaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for key in (
            "ARC_GIT_USER_NAME",
            "ARC_GIT_USER_EMAIL",
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            monkeypatch.delenv(key, raising=False)
        git_client.ensure_repo(create_initial_commit=False)
        result = _git(git_paths.project_dir, "config", "user.name")
        assert result.stdout.strip() == DEFAULT_GIT_USER_NAME
        result = _git(git_paths.project_dir, "config", "user.email")
        assert result.stdout.strip() == DEFAULT_GIT_USER_EMAIL

    def test_arc_git_user_overrides_default(
        self,
        git_client: GitClient,
        git_paths: RuntimePaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ARC_GIT_USER_NAME", "Alice")
        monkeypatch.setenv("ARC_GIT_USER_EMAIL", "alice@example.com")
        git_client.ensure_repo(create_initial_commit=False)
        assert _git(git_paths.project_dir, "config", "user.name").stdout.strip() == "Alice"
        assert _git(git_paths.project_dir, "config", "user.email").stdout.strip() == "alice@example.com"

    def test_git_author_env_fallback(
        self,
        git_client: GitClient,
        git_paths: RuntimePaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Bob")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "bob@example.com")
        git_client.ensure_repo(create_initial_commit=False)
        assert _git(git_paths.project_dir, "config", "user.name").stdout.strip() == "Bob"


# ---------------------------------------------------------------------------
# ensure_repo + .gitignore
# ---------------------------------------------------------------------------


class TestEnsureRepo:
    def test_initialises_new_repo(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        assert (git_paths.project_dir / ".git").is_dir()
        # initial commit created -> HEAD must resolve
        head = _git(git_paths.project_dir, "rev-parse", "HEAD")
        assert head.returncode == 0
        assert head.stdout.strip() != ""

    def test_idempotent(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        first_head = git_client.current_head()
        git_client.ensure_repo()
        second_head = git_client.current_head()
        assert first_head == second_head

    def test_create_initial_commit_false(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo(create_initial_commit=False)
        # .git exists but HEAD is unborn
        assert (git_paths.project_dir / ".git").is_dir()
        result = _git(git_paths.project_dir, "rev-parse", "HEAD")
        assert result.returncode != 0

    def test_emits_refresh_signal(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        events_path = git_paths.runner_events_path
        git_client.ensure_repo()
        content = events_path.read_text(encoding="utf-8")
        assert "git_initialized" in content
        assert "git_init_commit" in content
        assert "git_identity_configured" in content


class TestGitignore:
    def test_block_added_to_empty_gitignore(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        gitignore = git_paths.project_dir / ".gitignore"
        gitignore.write_text("", encoding="utf-8")
        git_client.ensure_arc_gitignore()
        text = gitignore.read_text(encoding="utf-8")
        assert text.startswith(ARC_GITIGNORE_START)
        assert text.rstrip().endswith(ARC_GITIGNORE_END)

    def test_block_merged_with_existing_content(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        gitignore = git_paths.project_dir / ".gitignore"
        gitignore.write_text("node_modules/\n", encoding="utf-8")
        git_client.ensure_arc_gitignore()
        text = gitignore.read_text(encoding="utf-8")
        assert "node_modules/" in text
        assert ARC_GITIGNORE_START in text
        assert ARC_GITIGNORE_END in text

    def test_block_replaced_when_re_invoked(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        gitignore = git_paths.project_dir / ".gitignore"
        git_client.ensure_arc_gitignore()
        git_client.ensure_arc_gitignore()
        text = gitignore.read_text(encoding="utf-8")
        # exactly one occurrence of the start sentinel
        assert text.count(ARC_GITIGNORE_START) == 1
        assert text.count(ARC_GITIGNORE_END) == 1

    def test_managed_paths_excluded_but_traceability_included(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_arc_gitignore()
        text = (git_paths.project_dir / ".gitignore").read_text(encoding="utf-8")
        assert "backend/node_modules/" in text
        assert "frontend/node_modules/" in text
        assert "*.db" in text
        assert ".env" in text
        assert ".arc/*" in text
        assert "!.arc/traceability/" in text
        assert "!.arc/traceability/**" in text


# ---------------------------------------------------------------------------
# commit / status / reset / restore / clean
# ---------------------------------------------------------------------------


class TestCommit:
    def test_commit_returns_true_with_changes(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "new.txt").write_text("hello", encoding="utf-8")
        assert git_client.commit("add new file") is True
        head = _git(git_paths.project_dir, "log", "--oneline").stdout
        assert "add new file" in head

    def test_commit_returns_false_when_nothing_to_commit(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        assert git_client.commit("no change") is False


class TestStatus:
    def test_status_porcelain_clean(
        self, git_client: GitClient
    ) -> None:
        git_client.ensure_repo()
        assert git_client.status_porcelain() == ""

    def test_status_porcelain_dirty(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "x.txt").write_text("y", encoding="utf-8")
        assert "x.txt" in git_client.status_porcelain()


class TestResetAndRestore:
    def test_rollback_last_commit_soft(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "f.txt").write_text("a", encoding="utf-8")
        git_client.commit("add f")
        before = git_client.current_head()
        git_client.rollback_last_commit(hard=False)
        after = git_client.current_head()
        assert before != after
        # file still present after soft reset
        assert (git_paths.project_dir / "f.txt").exists()

    def test_rollback_last_commit_hard(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "f.txt").write_text("a", encoding="utf-8")
        git_client.commit("add f")
        git_client.rollback_last_commit(hard=True)
        assert not (git_paths.project_dir / "f.txt").exists()

    def test_reset_to_commit(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "f.txt").write_text("a", encoding="utf-8")
        git_client.commit("add f")
        target = git_client.current_head()
        (git_paths.project_dir / "g.txt").write_text("b", encoding="utf-8")
        git_client.commit("add g")
        git_client.reset_to_commit(target, hard=True)
        assert not (git_paths.project_dir / "g.txt").exists()
        assert (git_paths.project_dir / "f.txt").exists()

    def test_reset_to_commit_blank_raises(self, git_client: GitClient) -> None:
        git_client.ensure_repo()
        with pytest.raises(ValueError, match="commit_oid is required"):
            git_client.reset_to_commit("   ")

    def test_restore_worktree_discards_tracked_changes(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "f.txt").write_text("a", encoding="utf-8")
        git_client.commit("add f")
        # Modify tracked file (working-tree change) - must be discarded
        (git_paths.project_dir / "f.txt").write_text("modified", encoding="utf-8")
        git_client.restore_worktree()
        assert (git_paths.project_dir / "f.txt").read_text(encoding="utf-8") == "a"

    def test_restore_worktree_preserves_untracked(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        # Documented behaviour: ``git reset --hard`` does not touch untracked
        # files; that is what ``clean_untracked`` is for.
        git_client.ensure_repo()
        (git_paths.project_dir / "untracked.txt").write_text("x", encoding="utf-8")
        git_client.restore_worktree()
        assert (git_paths.project_dir / "untracked.txt").exists()


class TestClean:
    def test_clean_untracked_removes_only_untracked(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "kept.txt").write_text("k", encoding="utf-8")
        git_client.commit("add kept")
        (git_paths.project_dir / "tmp.txt").write_text("t", encoding="utf-8")
        git_client.clean_untracked()
        assert (git_paths.project_dir / "kept.txt").exists()
        assert not (git_paths.project_dir / "tmp.txt").exists()


class TestCurrentHead:
    def test_returns_oid_after_commit(
        self, git_client: GitClient, git_paths: RuntimePaths
    ) -> None:
        git_client.ensure_repo()
        (git_paths.project_dir / "f.txt").write_text("a", encoding="utf-8")
        git_client.commit("add f")
        head = git_client.current_head()
        assert head is not None
        assert len(head) >= 7

    def test_returns_none_on_unborn_head(
        self, git_client: GitClient
    ) -> None:
        git_client.ensure_repo(create_initial_commit=False)
        assert git_client.current_head() is None
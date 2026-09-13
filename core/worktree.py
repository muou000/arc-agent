"""Per-node git worktrees that isolate concurrent compilation tasks.

Serial compilation ran every node against one shared working tree. That tree is
the reason ``ARC_MAX_CONCURRENT_TASKS`` is hard-clamped to 1: two agents editing
the same checkout, two test runners binding the same port, and ``git add .``
checkpoints capturing each other's half-finished changes all break under
concurrency. This module gives every in-flight task its own ``git worktree``
branched from the integration HEAD so those three hazards disappear:

- The agent's filesystem root is the worktree, so its writes are private until
  the phase commits.
- Every worktree gets its own web port and E2E database path, so test runs do
  not collide.
- The phase checkpoint commits inside the worktree; ``git add .`` there only
  ever stages that node's changes.

When the task finishes, the worktree's branch is merged back into the
integration branch under the workflow's merge lock. Sibling nodes touch
disjoint files in the common case, so merges are clean; a conflict aborts the
merge, fails the node with an explicit reason, and keeps the worktree on disk
for inspection and ``--retry``.

Worktrees live under ``<workspace>/.arc/worktrees/<node_id>``. The runtime's
managed ``.gitignore`` block already ignores ``.arc/*`` (everything except
``.arc/traceability/``), so the parent repository never stages a worktree, and
``git worktree add`` inside the main working tree is safe (verified against
git on Windows).

``node_modules`` is shared with the main workspace through an NTFS junction
(or a symlink elsewhere): worktrees check out only tracked files, and a fresh
``npm install`` per node would erase most of the parallelism win. Junctions are
removed with ``os.rmdir`` - which deletes the link, never the target - before
any worktree deletion, so the shared install directory stays intact.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


BRANCH_PREFIX = "arc-node"

DEFAULT_GIT_USER_NAME = "ARC Bench Agent"
DEFAULT_GIT_USER_EMAIL = "arcbench@example.com"


class WorktreeError(RuntimeError):
    """A worktree lifecycle operation failed."""


class MergeConflictError(WorktreeError):
    """The node branch cannot be merged into the integration branch."""


@dataclass
class WorktreeHandle:
    """An in-flight task's isolated workspace."""

    node_id: str
    branch: str
    path: str
    main_workspace: str


def sanitize_node_id(node_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip())
    return normalized or "node"


class NodeWorktreeManager:
    """Create, integrate, and dispose per-node worktrees for one workspace."""

    def __init__(self, workspace_path: str) -> None:
        self.main_workspace = str(Path(workspace_path).expanduser().resolve())
        self.worktrees_root = Path(self.main_workspace) / ".arc" / "worktrees"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def prepare(self, node_id: str) -> WorktreeHandle:
        """Create (or reuse) the worktree and branch for ``node_id``."""

        safe_id = sanitize_node_id(node_id)
        branch = f"{BRANCH_PREFIX}/{safe_id}"
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        worktree_path = self.worktrees_root / safe_id

        if worktree_path.exists() and not self._is_registered(worktree_path):
            # Leftover directory from an unregistered worktree (crash between
            # directory creation and registration): start clean.
            shutil.rmtree(worktree_path, ignore_errors=True)

        if self._is_registered(worktree_path):
            # Reuse after an interrupted run so the agent keeps its artifacts.
            self._git(["checkout", branch], check=False)
        elif self._branch_exists(branch):
            self._git(["worktree", "add", str(worktree_path), branch])
        else:
            self._git(["worktree", "add", "-b", branch, str(worktree_path)])

        handle = WorktreeHandle(
            node_id=node_id,
            branch=branch,
            path=str(worktree_path),
            main_workspace=self.main_workspace,
        )
        self._link_node_modules(handle)
        self._seed_frontend_dist(handle)
        return handle

    def commit(self, handle: WorktreeHandle, message: str) -> bool:
        """Commit the worktree's changes. Returns False when nothing changed."""

        self._git(["add", "-A", "."], cwd=handle.path)
        result = self._git(["commit", "-m", message], cwd=handle.path, check=False)
        output = (result.stdout + result.stderr).lower()
        if result.returncode == 0:
            return True
        if "nothing to commit" in output:
            return False
        raise WorktreeError(
            f"git commit failed in worktree {handle.path}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    def integrate(self, handle: WorktreeHandle, message: str) -> tuple[bool, str]:
        """Commit the worktree and merge its branch into the integration branch.

        Returns ``(committed, detail)``. Raises ``MergeConflictError`` when the
        merge conflicts; the merge is aborted and the worktree left in place so
        the operator (or ``--retry``) can inspect the overlap. A failed commit
        raises ``WorktreeError`` naming the branch: the worktree keeps its
        staged state, so the next retry of the node picks up exactly where the
        commit stopped.
        """

        try:
            committed = self.commit(handle, message)
        except WorktreeError as exc:
            raise WorktreeError(
                f"commit on branch {handle.branch} failed; the worktree keeps its "
                f"staged state for retry: {exc}"
            ) from exc
        current_branch = self._integration_branch()
        merge = self._git(
            ["merge", "--no-ff", handle.branch, "-m", f"merge {handle.branch} into {current_branch}"],
            cwd=self.main_workspace,
            check=False,
        )
        if merge.returncode == 0:
            return committed, f"merged {handle.branch} into {current_branch}"

        conflict_markers = self._git(
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=self.main_workspace,
            check=False,
        ).stdout.strip()
        self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
        files = ", ".join(conflict_markers.splitlines()[:8]) or "unknown files"
        raise MergeConflictError(
            f"Merging {handle.branch} conflicted with {current_branch} on: {files}. "
            "The worktree is preserved for inspection."
        )

    def discard(self, handle: WorktreeHandle, *, preserve: bool = False) -> None:
        """Remove the worktree directory, keeping the branch for audit.

        A preserved worktree keeps its node_modules link so a retry can run
        tests immediately; a deleted one must have the link disconnected first
        (see ``_unlink_node_modules``).
        """

        if preserve:
            return
        self._unlink_node_modules(handle)
        if self._is_registered(Path(handle.path)):
            self._git(["worktree", "remove", "--force", handle.path], cwd=self.main_workspace, check=False)
        else:
            shutil.rmtree(handle.path, ignore_errors=True)

    def prune(self) -> None:
        """Drop stale worktree registrations from interrupted runs."""

        self._git(["worktree", "prune"], cwd=self.main_workspace, check=False)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def _integration_branch(self) -> str:
        result = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.main_workspace)
        return result.stdout.strip() or "HEAD"

    def _branch_exists(self, branch: str) -> bool:
        result = self._git(["rev-parse", "--verify", "--quiet", branch], cwd=self.main_workspace, check=False)
        return result.returncode == 0

    def _is_registered(self, worktree_path: Path) -> bool:
        result = self._git(["worktree", "list", "--porcelain"], cwd=self.main_workspace, check=False)
        normalized = str(worktree_path).replace("\\", "/").lower()
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                registered = line[len("worktree "):].strip().replace("\\", "/").lower()
                if registered == normalized:
                    return True
        return False

    # ------------------------------------------------------------------
    # workspace conveniences
    # ------------------------------------------------------------------

    def _link_node_modules(self, handle: WorktreeHandle) -> None:
        main = Path(self.main_workspace)
        worktree = Path(handle.path)
        for relative in ("frontend/node_modules", "backend/node_modules"):
            target = main / relative
            link = worktree / relative
            if not target.is_dir():
                continue
            if link.exists() or link.is_symlink():
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            _create_junction(str(target), str(link))

    def _unlink_node_modules(self, handle: WorktreeHandle) -> None:
        """Disconnect the shared node_modules links before any worktree removal.

        ``git worktree remove --force`` recurses through NTFS junctions and
        directory symlinks (verified: it deletes the shared target's content),
        so a surviving link must never reach it. Links are removed first and
        the removal is verified; a link that cannot be disconnected is a hard
        error - the worktree stays registered for retry instead of risking the
        main workspace's node_modules. Real (worktree-local) directories are
        left for the worktree deletion.
        """

        for relative in ("frontend/node_modules", "backend/node_modules"):
            link = Path(handle.path) / relative
            if not (link.exists() or link.is_symlink()):
                continue
            if not _is_link(link):
                continue
            try:
                _remove_link(link)
            except OSError as exc:
                raise WorktreeError(_link_survival_error(link)) from exc
            if link.exists() or link.is_symlink():
                raise WorktreeError(_link_survival_error(link))

    def _seed_frontend_dist(self, handle: WorktreeHandle) -> None:
        """Copy the main workspace's built frontend into the worktree.

        The build fingerprint stored inside ``dist`` guards correctness: a
        worktree branched from the current HEAD has identical frontend sources,
        so a matching fingerprint lets ``_build_frontend_dist`` skip the first
        rebuild exactly like the serial flow did across nodes.
        """

        source = Path(self.main_workspace) / "frontend" / "dist"
        destination = Path(handle.path) / "frontend" / "dist"
        if not source.is_dir() or destination.exists():
            return
        shutil.copytree(source, destination)

    # ------------------------------------------------------------------
    # git plumbing
    # ------------------------------------------------------------------

    def _git(self, args: list[str], *, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        user_name = (
            os.environ.get("ARC_GIT_USER_NAME")
            or os.environ.get("GIT_AUTHOR_NAME")
            or DEFAULT_GIT_USER_NAME
        ).strip()
        user_email = (
            os.environ.get("ARC_GIT_USER_EMAIL")
            or os.environ.get("GIT_COMMITTER_EMAIL")
            or DEFAULT_GIT_USER_EMAIL
        ).strip()
        env["GIT_AUTHOR_NAME"] = user_name
        env["GIT_AUTHOR_EMAIL"] = user_email
        env["GIT_COMMITTER_NAME"] = user_name
        env["GIT_COMMITTER_EMAIL"] = user_email
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd or self.main_workspace,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise WorktreeError(f"git {' '.join(args)} failed: {stderr}")
        return completed


def _create_junction(target: str, link: str) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise WorktreeError(
                f"Failed to create junction {link} -> {target}: {completed.stderr.strip()}"
            )
        return
    os.symlink(target, link, target_is_directory=True)


def _is_link(path: Path) -> bool:
    """True for NTFS junctions and symlinks (Path.is_symlink() misses junctions)."""

    try:
        os.readlink(path)
        return True
    except OSError:
        return False


def _remove_link(link: Path) -> None:
    """Remove a junction or directory symlink without touching its target."""

    try:
        os.rmdir(link)
    except NotADirectoryError:
        # POSIX directory symlinks: rmdir refuses, unlink removes the link.
        os.unlink(link)


def _link_survival_error(link: Path) -> str:
    return (
        f"Failed to disconnect {link} from the shared node_modules; "
        "refusing to delete the worktree while the link survives."
    )

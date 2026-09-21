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
disjoint files in the common case, so merges are clean. Two guards cover the
overlapping cases: ``core.file_claims`` blocks a stage agent from creating a
new file a parallel sibling already created (the add/add case no resolver
can fix), and the workflow re-queues a node once per phase after a merge
conflict (``reset_branch_to_integration`` puts the retry back at the merged
integration HEAD, so the winning sibling's files are visible to it). A
conflict that survives both guards fails the node with an explicit reason
and keeps the worktree on disk for inspection and ``--retry``. One narrow
conflict class is resolved mechanically instead: when every side of a
conflict only *appends* lines to an existing file (the shared glue-file
registration pattern, e.g. ``app.js`` route blocks), the additions are
replayed onto the common base and the caller may run a health check on the
resolved tree before the merge is committed.

Tasks may share one worktree *directory* per subtree (``group_key`` in
``prepare``): consecutive tasks of a subtree run sequentially in the same
directory, so siblings within a subtree never race on shared files at all.
Branches stay per node; each new node's branch starts at the latest
integration HEAD. A directory holding a conflicting or crashed task is
quarantined and later tasks fall back to their own node-keyed directory.

Parent and child DESIGN phases are serialized by the workflow's dependency
gate (a child's DESIGN waits for its parent's DESIGN to settle), so a child
always branches from an integration HEAD that already contains the parent's
shell: the child's additive edits to shared surfaces (app entry, route
registration, layout) merge cleanly instead of colliding with a concurrent
parent rewrite.

Worktrees live under ``<workspace>/.arc/worktrees/<group-or-node-id>``. The runtime's
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

import difflib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


BRANCH_PREFIX = "arc-node"

DEFAULT_GIT_USER_NAME = "ARC Bench Agent"
DEFAULT_GIT_USER_EMAIL = "arcbench@example.com"

# Files larger than this never go through the additive conflict resolver.
MAX_ADDITIVE_RESOLUTION_BYTES = 2_000_000


class WorktreeError(RuntimeError):
    """A worktree lifecycle operation failed."""


class MergeConflictError(WorktreeError):
    """The node branch cannot be merged into the integration branch.

    ``files`` carries the conflicting paths so callers can react to them
    (the workflow's conflict-aware DESIGN retry uses them as guidance).
    """

    def __init__(self, message: str, files: list[str] | None = None) -> None:
        super().__init__(message)
        self.files = list(files or [])


class MergeVerificationError(MergeConflictError):
    """An additively resolved merge failed its post-merge verification."""


@dataclass
class WorktreeHandle:
    """An in-flight task's isolated workspace."""

    node_id: str
    branch: str
    path: str
    main_workspace: str
    reusable: bool = False


def sanitize_node_id(node_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip())
    return normalized or "node"


class NodeWorktreeManager:
    """Create, integrate, and dispose per-node worktrees for one workspace."""

    def __init__(self, workspace_path: str) -> None:
        self.main_workspace = str(Path(workspace_path).expanduser().resolve())
        self.worktrees_root = Path(self.main_workspace) / ".arc" / "worktrees"
        # Worktree directories that must not be handed to another node: they
        # hold a conflicting or crashed task's state for inspection/retry.
        self._quarantined: set[str] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def prepare(self, node_id: str, group_key: str | None = None) -> WorktreeHandle:
        """Create (or reuse) the worktree and branch for ``node_id``.

        With ``group_key`` the worktree *directory* is keyed by the group (the
        node's top-level subtree) instead of the node, so consecutive tasks of
        one subtree reuse one directory, one node_modules junction and one
        seeded ``frontend/dist``. Branches stay per node. A group worktree
        starts each new node's branch at the latest integration HEAD, which is
        also the per-task sync; a dirty or quarantined group directory falls
        back to a node-keyed directory instead of mixing states.
        """

        safe_id = sanitize_node_id(node_id)
        branch = f"{BRANCH_PREFIX}/{safe_id}"
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        worktree_path = self._select_worktree_path(safe_id, branch, group_key)
        reusable = worktree_path != self.worktrees_root / safe_id

        if worktree_path.exists() and not self._is_registered(worktree_path):
            # Leftover directory from an unregistered worktree (crash between
            # directory creation and registration): start clean.
            shutil.rmtree(worktree_path, ignore_errors=True)

        if self._is_registered(worktree_path):
            if self._branch_exists(branch):
                # Reuse after an interrupted run so the agent keeps its
                # artifacts and committed work.
                self._git(["checkout", branch], cwd=str(worktree_path), check=False)
            else:
                # A clean reusable directory starts the new node's branch from
                # the latest integration HEAD.
                integration = self._integration_branch()
                self._git(
                    ["checkout", "-B", branch, integration],
                    cwd=str(worktree_path),
                    check=False,
                )
        elif self._branch_exists(branch):
            self._detach_branch_elsewhere(branch, keep_path=worktree_path)
            self._git(["worktree", "add", str(worktree_path), branch])
        else:
            self._git(["worktree", "add", "-b", branch, str(worktree_path)])

        handle = WorktreeHandle(
            node_id=node_id,
            branch=branch,
            path=str(worktree_path),
            main_workspace=self.main_workspace,
            reusable=reusable,
        )
        self._link_node_modules(handle)
        self._seed_frontend_dist(handle)
        return handle

    def _select_worktree_path(self, safe_id: str, branch: str, group_key: str | None) -> Path:
        """Choose the directory for the node's worktree.

        Without a group key this is the historical node-keyed path. With a
        group key the shared group directory is used unless it is quarantined
        or holds another node's uncommitted state.
        """

        node_path = self.worktrees_root / safe_id
        if not group_key:
            return node_path
        group_path = self.worktrees_root / sanitize_node_id(group_key)
        if group_path == node_path or self._same_path(group_path, node_path):
            return node_path
        if any(self._same_path_str(group_path, q) for q in self._quarantined):
            return node_path
        if group_path.exists() and not self._is_registered(group_path):
            shutil.rmtree(group_path, ignore_errors=True)
        if self._is_registered(group_path):
            dirty = self._worktree_dirty(group_path)
            current_branch = self._worktree_branch(group_path)
            if dirty and current_branch != branch:
                # Another node's uncommitted state lives here; never mix it
                # into this node's checkpoint or checkout.
                self._quarantined.add(str(group_path))
                return node_path
        return group_path

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

    def integrate(
        self,
        handle: WorktreeHandle,
        message: str,
        *,
        verify: Callable[[], str | None] | None = None,
    ) -> tuple[bool, str]:
        """Commit the worktree and merge its branch into the integration branch.

        Returns ``(committed, detail)``. Raises ``MergeConflictError`` when the
        merge conflicts; the merge is aborted and the worktree left in place so
        the operator (or ``--retry``) can inspect the overlap. A failed commit
        raises ``WorktreeError`` naming the branch: the worktree keeps its
        staged state, so the next retry of the node picks up exactly where the
        commit stopped.

        Conflicts where every side only *appends* to an existing file (the
        typical shared glue-file registration, e.g. ``app.js`` route blocks)
        are resolved mechanically: both sides' additions are replayed onto the
        common base. ``verify`` is called with the resolved working tree still
        mid-merge and before the merge commit is created; returning a string
        (or raising) aborts the merge via ``MergeVerificationError``.
        """

        try:
            committed = self.commit(handle, message)
        except WorktreeError as exc:
            raise WorktreeError(
                f"commit on branch {handle.branch} failed; the worktree keeps its "
                f"staged state for retry: {exc}"
            ) from exc
        current_branch = self._integration_branch()
        merge_message = f"merge {handle.branch} into {current_branch}"
        merge = self._git(
            ["merge", "--no-ff", handle.branch, "-m", merge_message],
            cwd=self.main_workspace,
            check=False,
        )
        if merge.returncode == 0:
            return committed, f"merged {handle.branch} into {current_branch}"

        unmerged = [
            line.strip()
            for line in self._git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=self.main_workspace,
                check=False,
            ).stdout.splitlines()
            if line.strip()
        ]
        resolved = self._resolve_conflicts_by_addition(unmerged)
        if resolved is None:
            self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
            self._quarantined.add(str(Path(handle.path)))
            files = ", ".join(unmerged[:8]) or "unknown files"
            raise MergeConflictError(
                f"Merging {handle.branch} conflicted with {current_branch} on: {files}. "
                "The worktree is preserved for inspection.",
                files=unmerged,
            )

        failure: str | None = None
        if verify is not None:
            try:
                failure = verify()
            except Exception as exc:
                failure = f"verification crashed: {type(exc).__name__}: {exc}"
        if failure:
            self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
            self._quarantined.add(str(Path(handle.path)))
            raise MergeVerificationError(
                f"Merging {handle.branch} required an additive resolution of "
                f"{', '.join(resolved[:8])}, but the post-merge verification failed: {failure}. "
                "The merge was aborted and the worktree preserved."
            )

        commit = self._git(["commit", "--no-edit"], cwd=self.main_workspace, check=False)
        if commit.returncode != 0:
            self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
            self._quarantined.add(str(Path(handle.path)))
            raise WorktreeError(
                f"completing the resolved merge of {handle.branch} failed: "
                f"{commit.stderr.strip() or commit.stdout.strip()}"
            )
        return (
            committed,
            f"merged {handle.branch} into {current_branch} with additive conflict "
            f"resolution of: {', '.join(resolved)}",
        )

    def _resolve_conflicts_by_addition(self, paths: list[str]) -> list[str] | None:
        """Resolve every listed conflict when all sides are append-only.

        Returns the resolved paths, or ``None`` when any file cannot be
        resolved safely (in which case nothing is written and the caller
        aborts the merge).
        """

        resolved: list[str] = []
        for path in paths:
            merged_lines = self._additive_merge_file(path)
            if merged_lines is None:
                return None
            target = Path(self.main_workspace) / path
            with open(target, "w", encoding="utf-8", newline="") as file:
                file.write("".join(merged_lines))
            add = self._git(["add", "--", path], cwd=self.main_workspace, check=False)
            if add.returncode != 0:
                return None
            resolved.append(path)
        return resolved

    def _additive_merge_file(self, path: str) -> list[str] | None:
        """Three-way merge of one file restricted to pure additions.

        Reads the merge index stages (1=base, 2=ours, 3=theirs). The merge is
        accepted only when both sides only ever *inserted* lines relative to
        an existing base file; the result is the base with ours' insertions
        and theirs' insertions replayed at their original anchors. Anything
        else (modifications, deletions, add/add of a new file, binary content)
        is left unresolved.
        """

        stages: dict[int, list[str]] = {}
        for stage in (1, 2, 3):
            result = self._git(
                ["show", f":{stage}:{path}"],
                cwd=self.main_workspace,
                check=False,
                binary=True,
            )
            if result.returncode != 0 or not isinstance(result.stdout, bytes):
                return None
            raw: bytes = result.stdout
            if stage == 1 and len(raw) > MAX_ADDITIVE_RESOLUTION_BYTES:
                return None
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
            stages[stage] = text.splitlines(keepends=True)
        base_lines = stages[1]
        if not base_lines:
            # add/add of a new file is a genuine semantic conflict, and an
            # empty base file has no registration block to append to.
            return None
        ours_inserts = _collect_insertions(base_lines, stages[2])
        theirs_inserts = _collect_insertions(base_lines, stages[3])
        if ours_inserts is None or theirs_inserts is None:
            return None
        merged: list[str] = []
        for position in range(len(base_lines) + 1):
            merged.extend(ours_inserts.get(position, []))
            merged.extend(theirs_inserts.get(position, []))
            if position < len(base_lines):
                merged.append(base_lines[position])
        return merged

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

    def reset_branch_to_integration(self, handle: WorktreeHandle) -> None:
        """Reset a conflicted node's branch to the current integration HEAD.

        After a merge conflict the node branch holds the losing (conflicting)
        commits. A conflict-aware DESIGN or IMPLEMENT retry re-runs the node
        from the current integration state - which already contains the
        winning sibling's files - so its branch is reset here and the
        worktree directory is un-quarantined for reuse. The discarded
        commits stay reachable through git's reflog for inspection.
        """

        self._quarantined.discard(str(Path(handle.path)))
        integration = self._integration_branch()
        reset = self._git(
            ["checkout", "-B", handle.branch, integration],
            cwd=handle.path,
            check=False,
        )
        if reset.returncode != 0:
            raise WorktreeError(
                f"resetting branch {handle.branch} to {integration} failed: "
                f"{reset.stderr.strip() or reset.stdout.strip()}"
            )

    def cleanup_reusable_worktrees(self) -> list[str]:
        """Remove reusable worktree directories left over after a run.

        Only clean, non-quarantined worktrees under ``.arc/worktrees`` are
        removed (successful group directories survive their tasks for reuse).
        Quarantined and dirty directories stay on disk for inspection and
        ``--retry``; branches are always kept.
        """

        removed: list[str] = []
        listing = self._git(
            ["worktree", "list", "--porcelain"],
            cwd=self.main_workspace,
            check=False,
        ).stdout
        entries = _parse_worktree_entries(listing)
        root = str(self.worktrees_root)
        for path, _branch in entries:
            normalized = path.replace("\\", "/").rstrip("/").lower()
            if not normalized.startswith(root.replace("\\", "/").rstrip("/").lower() + "/"):
                continue
            if any(self._same_path_str(Path(path), q) for q in self._quarantined):
                continue
            if self._worktree_dirty(path):
                continue
            handle = WorktreeHandle(
                node_id=Path(path).name,
                branch=_branch or "",
                path=path,
                main_workspace=self.main_workspace,
            )
            try:
                self._unlink_node_modules(handle)
            except WorktreeError:
                # The link could not be disconnected; keep the worktree rather
                # than risk the shared node_modules.
                continue
            self._git(
                ["worktree", "remove", "--force", path],
                cwd=self.main_workspace,
                check=False,
            )
            if not Path(path).exists():
                removed.append(path)
        return removed

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def _integration_branch(self) -> str:
        result = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.main_workspace)
        return result.stdout.strip() or "HEAD"

    def _branch_exists(self, branch: str) -> bool:
        result = self._git(["rev-parse", "--verify", "--quiet", branch], cwd=self.main_workspace, check=False)
        return result.returncode == 0

    def _worktree_dirty(self, worktree_path: str | Path) -> bool:
        result = self._git(["status", "--porcelain"], cwd=str(worktree_path), check=False)
        return bool(result.stdout.strip())

    def _worktree_branch(self, worktree_path: str | Path) -> str:
        result = self._git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree_path),
            check=False,
        )
        return result.stdout.strip()

    def _detach_branch_elsewhere(self, branch: str, *, keep_path: Path) -> None:
        """Detach another worktree that holds ``branch`` so it can be added.

        Fallback worktree creation for a retried node can collide with a
        quarantined group directory that is still checked out on the node's
        branch. Detaching preserves the directory's content and commit exactly
        while freeing the branch name for the new worktree.
        """

        listing = self._git(
            ["worktree", "list", "--porcelain"],
            cwd=self.main_workspace,
            check=False,
        ).stdout
        for path, worktree_branch in _parse_worktree_entries(listing):
            if not worktree_branch:
                continue
            if worktree_branch.removeprefix("refs/heads/") != branch:
                continue
            if self._same_path(Path(path), keep_path):
                continue
            self._git(["checkout", "--detach", "--quiet"], cwd=path, check=False)

    def _same_path(self, left: Path, right: Path) -> bool:
        return self._same_path_str(left, str(right))

    def _same_path_str(self, left: Path | str, right: Path | str) -> bool:
        normalize = lambda value: os.path.normcase(str(Path(value).resolve()))
        return normalize(left) == normalize(right)

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

    def _git(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        check: bool = True,
        binary: bool = False,
    ) -> subprocess.CompletedProcess:
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
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            stdout = completed.stdout
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            stderr = stderr.strip() or stdout.strip() or "git command failed"
            raise WorktreeError(f"git {' '.join(args)} failed: {stderr}")
        return completed


def _parse_worktree_entries(porcelain_output: str) -> list[tuple[str, str | None]]:
    """Parse ``git worktree list --porcelain`` into (path, branch) pairs."""

    entries: list[tuple[str, str | None]] = []
    current_path: str | None = None
    for line in porcelain_output.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
            entries.append((current_path, None))
        elif line.startswith("branch ") and current_path is not None:
            branch = line[len("branch "):].strip() or None
            entries[-1] = (current_path, branch)
    return entries


def _collect_insertions(
    base_lines: list[str], side_lines: list[str]
) -> dict[int, list[str]] | None:
    """Map base line positions to a side's inserted lines.

    Returns ``None`` when the side did anything besides insert: a replace or
    delete means the two sides changed the same content in incompatible ways
    and no mechanical merge may claim otherwise.
    """

    matcher = difflib.SequenceMatcher(a=base_lines, b=side_lines, autojunk=False)
    insertions: dict[int, list[str]] = {}
    for tag, i1, _, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag != "insert":
            return None
        insertions.setdefault(i1, []).extend(side_lines[j1:j2])
    return insertions


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

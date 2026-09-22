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
integration branch under the workflow's merge lock. Every operation that
reads the integration branch's tree into a worktree (``prepare``'s
checkouts, the conflict-requeue reset) shares the manager's
``integration_gate`` as a reader, while the operations that move it
(``integrate``'s merge) hold it as the writer: a checkout that raced a
concurrent merge could materialize its index without its files, and the
task's ``git add -A .`` then staged sibling-owned files as deletions
(issue #91). Sibling nodes touch
disjoint files in the common case, so merges are clean. Two guards cover the
overlapping cases: ``core.file_claims`` blocks a stage agent from creating a
new file a parallel sibling already created (the add/add case no resolver
can fix), and the workflow re-queues a node once per phase after a merge
conflict (the manager's ``settle`` with ``RESET_FOR_RETRY`` puts the retry
back at the merged integration HEAD, so the winning sibling's files are
visible to it). A conflict that survives both guards fails the node with an
explicit reason and keeps the worktree on disk for inspection and
``--retry``. One narrow conflict class is resolved mechanically instead:
when every side of a conflict only *appends* lines to an existing file (the
shared glue-file registration pattern, e.g. ``app.js`` route blocks), the
additions are replayed onto the common base and the caller may run a health
check on the resolved tree before the merge is committed.

Lifecycle ownership (issue #105): the reuse / quarantine / preserve /
delete decisions live entirely in this module. The scheduler reports what
happened to a task (``WorktreeTaskResult``) and receives one outcome per
task (``WorktreeOutcome``) from ``settle``; the quarantine set is private
manager state, written when a merge fails inside ``integrate`` and cleared
by the manager's own retry reset - never touched across the seam. What the
merge-arbitration paths need from the integration workspace (conflict
stages, staging, a follow-up commit) is exposed as manager methods too, so
no caller reaches for the manager's git handle.

Tasks may share one worktree *directory* per subtree (``group_key`` in
``prepare``): consecutive tasks of a subtree run sequentially in the same
directory, so siblings within a subtree never race on shared files at all.
Branches stay per node; each new node's branch starts at the latest
integration HEAD. A directory holding a conflicting or crashed task is
quarantined and later tasks fall back to their own node-keyed directory.

While a task executes, a sibling's merge landing on the integration branch
does not interrupt it: the manager records a ``PendingMerge`` (the merge's
changed-file set) against the in-flight worktree, and only when the task's
agent touches one of those paths does the mid-phase replay run at that tool
boundary - a ``wip:`` commit of the dirty tree, a rebase onto the new
integration HEAD (under the ``integration_gate`` as a reader, like every
other tree-reading operation), and either a fresh tree or conflict markers
the resolving agent settles with its ordinary file tools. The replay is
demand-pull (ADR 0003) and fail-open: any mechanical failure aborts it,
restores the pre-replay state and leaves the overlap to the merge rails.

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
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from core.merge_arbitration import (
    TRIGGER_CONFLICT,
    TRIGGER_HEALTH_GATE,
    read_conflict_stages as _read_conflict_stages,
)


BRANCH_PREFIX = "arc-node"

DEFAULT_GIT_USER_NAME = "ARC Bench Agent"
DEFAULT_GIT_USER_EMAIL = "arcbench@example.com"

# Files larger than this never go through the additive conflict resolver.
MAX_ADDITIVE_RESOLUTION_BYTES = 2_000_000

# ``wip:``-prefixed commits on node branches are the mid-phase replay
# checkpoints (issue #127); they are audit markers, never integration output.
WIP_COMMIT_PREFIX = "wip:"


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


class MergeArbitrationError(MergeConflictError):
    """An arbitrated merge failed: arbitration ran but the tree still did not verify.

    The merge is aborted and the worktree preserved, exactly like a
    ``MergeVerificationError``; the separate class lets callers and tests
    distinguish "arbitration was tried and its output failed the gate" from
    "mechanical resolution failed the gate".
    """


class WorktreeTaskResult(str, Enum):
    """What the scheduler reports about a finished task: facts, not decisions.

    The scheduler knows whether the task's work merged, failed, or was
    re-queued after a merge conflict; every consequence for the worktree is
    the manager's to decide (``settle`` maps these onto ``WorktreeOutcome``).
    """

    MERGED = "merged"
    FAILED = "failed"
    RESET_FOR_RETRY = "reset_for_retry"


class WorktreeOutcome(str, Enum):
    """The one lifecycle outcome the manager returns per finished task.

    - ``PRESERVED``: the directory stays on disk exactly as the task left it
      (including any quarantine ``integrate`` recorded) for inspection and
      ``--retry``; the node_modules link stays connected so a retry can run
      tests immediately.
    - ``REUSED``: a reusable group directory survives its task for the
      subtree's next task; the end-of-drain cleanup removes it once the run
      no longer needs it.
    - ``DELETED``: the directory is removed; the branch is always kept for
      audit and retry.
    """

    PRESERVED = "preserved"
    REUSED = "reused"
    DELETED = "deleted"


@dataclass
class _ArbitrationOutcome:
    """Result of one escalation attempt inside ``integrate``.

    ``arbitrated`` distinguishes "the LLM arbiter actually ran" (True) from
    "no arbiter was configured or its input hook declined" (False). Only a
    genuinely attempted arbitration escalates the raised error class to
    ``MergeArbitrationError``; a declined hook keeps the pre-arbitration
    failure (``MergeConflictError`` / ``MergeVerificationError``) so the
    default-off behavior is byte-for-byte identical to main.
    """

    success: bool
    arbitrated: bool
    detail: str

    @classmethod
    def ran(cls, success: bool, detail: str = "") -> "_ArbitrationOutcome":
        return cls(success=success, arbitrated=True, detail=detail)

    @classmethod
    def not_configured(cls) -> "_ArbitrationOutcome":
        return cls(success=False, arbitrated=False, detail="")


@dataclass
class ArbitrationHooks:
    """Optional LLM escalation hooks for merge-layer failures (issue #81).

    All callables run inside the merge lock's worker thread while the merge
    is mid-flight. ``collect_input`` returns ``None`` when the escalation is
    unavailable (budget spent); ``run`` returns a failure-detail string when
    the escalation should be abandoned (the caller then aborts the merge) and
    ``None`` when the arbiter rewrote the conflict files in the mid-merge
    working tree. ``on_reverified`` receives the health gate's re-verification
    result after a health-gate-triggered repair, so the audit trail can
    record it. ``None`` hooks keep the pre-arbitration behavior.
    """

    collect_input: Callable[[list[str], str], Any] | None = None
    """(conflict_paths, trigger) -> ArbitrationInput or None when unavailable."""

    run: Callable[[Any, list[str], str], str | None] | None = None
    """(arbitration_input, conflict_paths, trigger) -> failure detail or None.

    Returning ``None`` means the arbiter rewrote the conflict files in the
    mid-merge working tree and the caller should re-verify; any string is a
    failure detail that aborts the merge.
    """

    on_reverified: Callable[[str | None], None] | None = None
    """(gate_result) -> None, called with the post-repair re-verification result."""


@dataclass
class WorktreeHandle:
    """An in-flight task's isolated workspace."""

    node_id: str
    branch: str
    path: str
    main_workspace: str
    reusable: bool = False


@dataclass
class PendingMerge:
    """A sibling merge that landed while this task was still executing.

    The workflow records one of these per sibling merge whose changed files
    overlap paths an in-flight task may touch. Nothing happens until the
    task's agent actually touches one of the changed paths (read/edit/write/
    delete): at that tool-call boundary the middleware replays the task onto
    the new integration HEAD (WIP commit + rebase), then serves the call
    against the fresh tree. This is the *demand-pull* half of ADR 0003 - the
    alternative (replaying eagerly at merge time) was rejected there.
    """

    source_node_id: str
    integration_head: str
    changed_files: list[str]

    def touches(self, rel_path: str) -> bool:
        return rel_path in self._changed_set

    @property
    def _changed_set(self) -> frozenset[str]:
        # PendingMerge instances are created once per merge and consulted on
        # every file tool call afterwards, so the frozen set is cached.
        cached = getattr(self, "_cached_changed_set", None)
        if cached is None:
            normalized = {normalize_repo_path(path) for path in self.changed_files}
            cached = frozenset(path for path in normalized if path)
            self._cached_changed_set = cached
        return cached


@dataclass
class ReplayOutcome:
    """What one mid-phase replay did (issue #127 / ADR 0003).

    ``status`` is one of ``"replayed"`` (the rebase landed; ``files`` carries
    the applied pending merges' changed files for result annotation),
    ``"conflicts"`` (the rebase landed with conflict markers in the tree;
    ``files`` carries the conflicted paths for the agent to resolve),
    ``"aborted"`` (a mechanical failure rolled the worktree back to its
    pre-replay state; the call proceeds against the old tree and the merge
    rails own the overlap), or ``"skipped"`` (no replay was needed - no
    pending merges, nothing touched, or replay disabled for this stage).

    ``attempted`` marks outcomes where git state actually moved (a rebase
    ran, or a ``rebase --continue`` round executed): the soft guard counts
    only attempted conflict rounds, not the passive "still unresolved"
    observation a boundary call makes while the agent works on other
    files.

    ``origin`` distinguishes a fresh replay (``"replay"``) from a conflict-
    resolution round (``"continue"``): the soft guard's consecutive streak
    resets on a clean fresh replay (a conflict-free wave breaks the run)
    but not on a continue's completion, which only resolves a conflict the
    same replay already carried.
    """

    REPLAYED = "replayed"
    CONFLICTS = "conflicts"
    ABORTED = "aborted"
    SKIPPED = "skipped"

    status: str
    files: list[str] = field(default_factory=list)
    detail: str = ""
    attempted: bool = False
    origin: str = "replay"


def normalize_repo_path(value: object) -> str:
    """Normalize a git-reported repo-relative path for path-set matching.

    Git reports forward-slash repo-relative paths; tool-call paths arrive as
    virtual (``/workspace/a/b``), slash-prefixed or relative forms. This maps
    them onto the same comparison space (``a/b``). Empty and workspace-root
    forms return ``""``.

    Deliberately not shared with ``agents.runtime.capabilities``'s
    ``normalize_manifest_path``: that one maps onto manifest-declared test
    paths with its own semantics, and importing from ``agents`` here would
    invert the layering (``agents.runtime.rebase_gate`` already imports
    from this module). The shapes coincide today; a future divergence is
    fine because the two never compare paths against each other.
    """

    path = str(value or "").replace("\\", "/").strip()
    if not path:
        return ""
    while path.startswith("./"):
        path = path[2:]
    if path in {"/workspace", "/workspace/"}:
        return ""
    if path.startswith("/workspace/"):
        path = path[len("/workspace/"):]
    return path.strip("/")


def touches_pending_file(pending: list[PendingMerge], rel_path: str) -> bool:
    """Whether any pending merge changed ``rel_path`` (normalized)."""

    normalized = normalize_repo_path(rel_path)
    return bool(normalized) and any(merge.touches(normalized) for merge in pending)


def sanitize_node_id(node_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip())
    return normalized or "node"


class _IntegrationGate:
    """Reader/writer exclusion over the integration branch (issue #91).

    Readers materialize the integration tree into their own worktree
    (``prepare``'s checkouts, the conflict-requeue reset): they write only
    their own worktree and their own node branch, and they read a branch that
    no other reader moves, so concurrent readers are safe and must stay
    concurrent - serializing them would serialize sibling task starts. The
    single writer (``integrate``'s merge) moves the integration branch and
    excludes every reader. Writers are preferred so continuous task churn
    cannot starve a merge.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @contextmanager
    def reader(self) -> Iterator[None]:
        with self._condition:
            while self._writer or self._writers_waiting:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def writer(self) -> Iterator[None]:
        with self._condition:
            self._writers_waiting += 1
            while self._writer or self._readers:
                self._condition.wait()
            self._writers_waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


class NodeWorktreeManager:
    """Create, integrate, and dispose per-node worktrees for one workspace."""

    def __init__(self, workspace_path: str) -> None:
        self.main_workspace = str(Path(workspace_path).expanduser().resolve())
        self.worktrees_root = Path(self.main_workspace) / ".arc" / "worktrees"
        # Worktree directories that must not be handed to another node: they
        # hold a conflicting or crashed task's state for inspection/retry.
        self._quarantined: set[str] = set()
        # Landed sibling merges awaiting demand-pull replay, keyed by the
        # in-flight task's worktree path (issue #127). Populated by
        # ``record_pending_merge``, consumed by ``replay_pending_merges`` and
        # the task's settle.
        self._pending_merges: dict[str, list[PendingMerge]] = {}
        # Worktrees sitting mid-rebase after a conflicted replay: the agent
        # is resolving conflict markers with its file tools, and the next
        # tool boundary may complete the rebase. Keyed like _pending_merges.
        self._mid_rebase: set[str] = set()
        # Pre-replay branch head per mid-rebase worktree, for restoring the
        # uncommitted (dirty) view when a stuck rebase is aborted.
        self._mid_rebase_base: dict[str, str] = {}
        # Issue #91: ``prepare`` (and the conflict-requeue reset) materialize
        # the integration branch's tree into a group worktree while another
        # group's ``integrate`` may be moving that same branch. A checkout
        # losing that race left the index holding files the disk never got,
        # and the task's ``git add -A .`` staged them as deletions of
        # sibling-owned files. Readers share the gate (concurrent prepares
        # touch disjoint worktrees and read a stable branch); the merge
        # excludes them. The workflow's asyncio merge lock keeps running for
        # its own bookkeeping on top of this gate.
        self.integration_gate = _IntegrationGate()

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

        The git section runs under ``integration_gate`` as a reader: every
        checkout here reads the integration branch's tree, which must not
        change underneath it (issue #91). A failed checkout raises instead of
        being swallowed - a swallowed failure is how the index ended up
        holding files the disk never materialized.
        """

        safe_id = sanitize_node_id(node_id)
        branch = f"{BRANCH_PREFIX}/{safe_id}"
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        with self.integration_gate.reader():
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
                    self._git(["checkout", branch], cwd=str(worktree_path))
                else:
                    # A clean reusable directory starts the new node's branch from
                    # the latest integration HEAD.
                    integration = self._integration_branch()
                    self._git(
                        ["checkout", "-B", branch, integration],
                        cwd=str(worktree_path),
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
        arbiter: ArbitrationHooks | None = None,
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

        ``arbiter`` (issue #81, gated by ``ARC_MERGE_ARBITRATION`` upstream)
        adds two LLM escalation points: a non-additive conflict that the
        mechanical resolver refuses is first handed to the arbiter, and an
        additively resolved merge whose ``verify`` fails is handed to the
        arbiter for one repair attempt. In both cases the arbiter may rewrite
        only the conflict files, and the result must pass ``verify`` (or the
        plain conflict check when ``verify`` is None) before the merge commit;
        otherwise the merge aborts with ``MergeArbitrationError``.

        The whole sequence runs under ``integration_gate`` as the writer: the
        merge moves the integration branch while another group's
        ``prepare``/reset may be materializing that same branch into a
        worktree (issue #91).
        """

        with self.integration_gate.writer():
            return self._integrate(handle, message, verify=verify, arbiter=arbiter)

    def _integrate(
        self,
        handle: WorktreeHandle,
        message: str,
        *,
        verify: Callable[[], str | None] | None = None,
        arbiter: ArbitrationHooks | None = None,
    ) -> tuple[bool, str]:
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
        arbitration_note = ""
        if resolved is None:
            outcome = self._arbitrate_conflicts(handle, unmerged, current_branch, arbiter)
            if outcome.success:
                # The arbiter rewrote the conflict files; the plain conflict
                # check below decides whether its output is acceptable.
                resolved = list(unmerged)
                arbitration_note = " with LLM arbitration of: " + ", ".join(unmerged[:8])
            else:
                self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
                self._quarantined.add(str(Path(handle.path)))
                files = ", ".join(unmerged[:8]) or "unknown files"
                if outcome.arbitrated:
                    raise MergeConflictError(
                        f"Merging {handle.branch} conflicted with {current_branch} on: {files}. "
                        f"The worktree is preserved for inspection. Arbitration failed: {outcome.detail}",
                        files=unmerged,
                    )
                # No arbiter ran: the pre-arbitration failure, byte for byte.
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
            outcome = self._arbitrate_health_gate_failure(
                handle, resolved or unmerged, current_branch, failure, verify, arbiter
            )
            if not outcome.success:
                self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
                self._quarantined.add(str(Path(handle.path)))
                if outcome.arbitrated:
                    raise MergeArbitrationError(
                        f"Merging {handle.branch} required an additive resolution of "
                        f"{', '.join(resolved[:8])}, but the post-merge verification failed: {failure}. "
                        f"{outcome.detail} "
                        "The merge was aborted and the worktree preserved.",
                        files=resolved or unmerged,
                    )
                # No arbiter was configured (or it declined to run): the
                # pre-arbitration behavior, byte for byte.
                raise MergeVerificationError(
                    f"Merging {handle.branch} required an additive resolution of "
                    f"{', '.join(resolved[:8])}, but the post-merge verification failed: {failure}. "
                    "The merge was aborted and the worktree preserved."
                )
            arbitration_note = " and the health gate passed after LLM arbitration repair"

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
            f"resolution of: {', '.join(resolved)}{arbitration_note}",
        )

    def _arbitrate_conflicts(
        self,
        handle: WorktreeHandle,
        unmerged: list[str],
        current_branch: str,
        arbiter: ArbitrationHooks | None,
    ) -> "_ArbitrationOutcome":
        """Escalate a non-additive conflict to the LLM arbiter.

        Returns ``ran(True)`` when the arbiter resolved every conflict file
        (no unmerged index paths, no literal conflict markers; the merge state
        stays mid-merge and the caller proceeds to the health gate); a failed
        outcome carries a ``detail`` that aborts the merge.
        """

        if arbiter is None or arbiter.collect_input is None or arbiter.run is None:
            return _ArbitrationOutcome.not_configured()
        if not unmerged:
            return _ArbitrationOutcome.ran(False, "The conflict file set is empty.")
        arbitration_input = arbiter.collect_input(unmerged, TRIGGER_CONFLICT)
        if arbitration_input is None:
            # The workflow's collect hook declines (budget spent / gate off):
            # the plain conflict failure, unchanged from the pre-arbitration
            # behavior.
            return _ArbitrationOutcome.not_configured()
        failure = arbiter.run(arbitration_input, unmerged, TRIGGER_CONFLICT)
        if failure:
            return _ArbitrationOutcome.ran(False, failure)
        # The run hook stages what it rewrote; a path it never touched stays
        # unmerged in the index, and a file that still carries literal git
        # conflict markers is a hand-off the health gate should not have to
        # diagnose. Both abort here.
        still_unresolved = self._unresolved_paths()
        marker_tainted = [
            path
            for path in unmerged
            if _carries_conflict_markers(Path(self.main_workspace) / path)
        ]
        if still_unresolved or marker_tainted:
            details = still_unresolved or marker_tainted
            return _ArbitrationOutcome.ran(
                False,
                "The arbiter did not resolve: " + ", ".join(details[:8]) + ".",
            )
        return _ArbitrationOutcome.ran(True, "")

    def _arbitrate_health_gate_failure(
        self,
        handle: WorktreeHandle,
        resolved: list[str],
        current_branch: str,
        gate_failure: str,
        verify: Callable[[], str | None] | None,
        arbiter: ArbitrationHooks | None,
    ) -> "_ArbitrationOutcome":
        """Escalate a failed post-merge health gate to the LLM arbiter.

        The arbiter may rewrite the resolved files; ``verify`` then re-runs
        (its result reported through ``arbiter.on_reverified`` for the audit
        trail). Returns ``ran(True)`` when the re-verification passes.
        """

        if arbiter is None or arbiter.collect_input is None or arbiter.run is None:
            return _ArbitrationOutcome.not_configured()
        arbitration_input = arbiter.collect_input(resolved, TRIGGER_HEALTH_GATE, gate_failure=gate_failure)
        if arbitration_input is None:
            return _ArbitrationOutcome.not_configured()
        failure = arbiter.run(arbitration_input, resolved, TRIGGER_HEALTH_GATE)
        if failure:
            return _ArbitrationOutcome.ran(False, failure)
        if verify is None:
            if arbiter.on_reverified is not None:
                arbiter.on_reverified(None)
            return _ArbitrationOutcome.ran(True, "")
        try:
            recheck = verify()
        except Exception as exc:
            recheck = f"re-verification crashed: {type(exc).__name__}: {exc}"
        if arbiter.on_reverified is not None:
            try:
                arbiter.on_reverified(recheck)
            except Exception:  # noqa: BLE001 - audit reporting must not break the merge
                pass
        if recheck:
            return _ArbitrationOutcome.ran(
                False, f"re-verification still failed after arbitration: {recheck}"
            )
        return _ArbitrationOutcome.ran(True, "")

    def _unresolved_paths(self) -> list[str]:
        """Paths still carrying conflict markers in the mid-merge index."""

        return [
            line.strip()
            for line in self._git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=self.main_workspace,
                check=False,
            ).stdout.splitlines()
            if line.strip()
        ]

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

    def settle(self, handle: WorktreeHandle, *, result: WorktreeTaskResult) -> WorktreeOutcome:
        """Settle a finished task's worktree: one outcome per task.

        The manager owns the reuse / quarantine / preserve / delete decision;
        the scheduler only reports the task's result:

        - ``FAILED`` → ``PRESERVED``: the worktree keeps its state for
          inspection and ``--retry`` exactly as the task left it - whatever
          quarantine ``integrate`` recorded stays in force (a conflicted
          merge keeps the directory out of other nodes' hands); a plain
          failure stays un-quarantined and the next ``prepare``'s dirty
          check falls back exactly as before.
        - ``RESET_FOR_RETRY`` → the branch is reset to the current integration
          HEAD and the directory un-quarantined first (the conflict-aware
          requeue re-runs the node against the winning sibling's merged
          files), then the same decision as a merged task: a reusable group
          directory survives (``REUSED``), a node-keyed directory is removed
          (``DELETED``) and the retry's ``prepare`` recreates it from the
          branch - which now sits at the integration HEAD. Once the reset
          has succeeded a failed removal must not fail the requeue (the
          conflicted commits are already reset away), so the directory is
          kept in its now-clean, reusable state and reported as ``REUSED``.
        - ``MERGED`` → ``REUSED`` for a reusable group directory,
          ``DELETED`` otherwise (branch kept).

        The end-of-drain bulk cleanup (``cleanup_reusable_worktrees``) maps
        onto the same outcomes per worktree: clean reusable directories are
        deleted once the run no longer needs them; dirty and quarantined
        directories are preserved for inspection and ``--retry``.
        """

        if result is WorktreeTaskResult.FAILED:
            self._clear_mid_rebase_marks(handle)
            self._pending_merges.pop(str(Path(handle.path)), None)
            return WorktreeOutcome.PRESERVED
        if result is WorktreeTaskResult.RESET_FOR_RETRY:
            self._clear_mid_rebase_marks(handle)
            self._pending_merges.pop(str(Path(handle.path)), None)
            self._reset_branch_to_integration(handle)
            if handle.reusable:
                return WorktreeOutcome.REUSED
            try:
                self._remove_worktree(handle)
            except WorktreeError:
                # The reset already succeeded, so the requeue must proceed:
                # the directory is clean at the integration HEAD - keep it as
                # genuinely reusable instead of failing the requeue over a
                # removal.
                return WorktreeOutcome.REUSED
            return WorktreeOutcome.DELETED
        # MERGED: a merged task cannot be mid-rebase (its integrate commits
        # the tree), but the bookkeeping is dropped unconditionally so a
        # reused group directory never inherits a stale mark.
        self._clear_mid_rebase_marks(handle)
        self._pending_merges.pop(str(Path(handle.path)), None)
        if handle.reusable:
            return WorktreeOutcome.REUSED
        self._remove_worktree(handle)
        return WorktreeOutcome.DELETED

    def _remove_worktree(self, handle: WorktreeHandle) -> None:
        """Remove the worktree directory, keeping the branch for audit.

        The shared node_modules link must be disconnected first (see
        ``_unlink_node_modules``); a link that cannot be disconnected keeps
        the worktree registered instead of risking the shared install
        directory.
        """

        self._unlink_node_modules(handle)
        if self._is_registered(Path(handle.path)):
            self._git(["worktree", "remove", "--force", handle.path], cwd=self.main_workspace, check=False)
        else:
            shutil.rmtree(handle.path, ignore_errors=True)

    def prune(self) -> None:
        """Drop stale worktree registrations from interrupted runs."""

        self._git(["worktree", "prune"], cwd=self.main_workspace, check=False)

    def _reset_branch_to_integration(self, handle: WorktreeHandle) -> None:
        """Reset a conflicted node's branch to the current integration HEAD.

        After a merge conflict the node branch holds the losing (conflicting)
        commits. A conflict-aware DESIGN or IMPLEMENT retry re-runs the node
        from the current integration state - which already contains the
        winning sibling's files - so its branch is reset here (the manager's
        own ``settle`` for ``RESET_FOR_RETRY``) and the worktree directory is
        un-quarantined for reuse. The discarded commits stay reachable through
        git's reflog for inspection.

        Runs under ``integration_gate`` as a reader like ``prepare``: the
        reset checks out the integration branch's tree into this worktree
        (issue #91).
        """

        with self.integration_gate.reader():
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

    def read_integration_diff(self, base_ref: str) -> list[str]:
        """Changed (repo-relative) paths between ``base_ref`` and the integration HEAD.

        The workflow's pending-merge attach reads the just-landed merge's
        changed-file set through this instead of the manager's git handle.
        An empty ``base_ref`` returns an empty list (nothing to diff).
        """

        if not str(base_ref or "").strip():
            return []
        diff = self._git(
            ["diff", "--name-only", f"{base_ref}..HEAD"], check=False
        )
        if diff.returncode != 0:
            raise WorktreeError(
                f"git diff {base_ref}..HEAD failed: {diff.stderr.strip() or diff.stdout.strip()}"
            )
        return [line.strip() for line in diff.stdout.splitlines() if line.strip()]

    def integration_head_sha(self) -> str:
        """Current integration HEAD sha (empty when git cannot answer)."""

        result = self._git(["rev-parse", "HEAD"], check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    # ------------------------------------------------------------------
    # mid-phase replay (issue #127 / ADR 0003: rebase-on-merge, demand-pull)
    # ------------------------------------------------------------------

    def record_pending_merge(
        self,
        handle: WorktreeHandle,
        source_node_id: str,
        integration_head: str,
        changed_files: list[str],
    ) -> None:
        """Attach a landed sibling merge to an in-flight task (best effort).

        Called by the workflow right after a sibling's merge committed, with
        the merge's changed-file set. The pending record only matters if the
        in-flight task's agent later touches one of those paths; otherwise it
        is silently consumed by the task's settle.
        """

        self._pending_merges.setdefault(str(Path(handle.path)), []).append(
            PendingMerge(
                source_node_id=source_node_id,
                integration_head=str(integration_head or ""),
                changed_files=[str(path) for path in changed_files if str(path)],
            )
        )

    def take_pending_merges(self, handle: WorktreeHandle) -> list[PendingMerge]:
        """Consume the task's pending merges (settle-time bookkeeping)."""

        return self._pending_merges.pop(str(Path(handle.path)), [])

    def pending_merges_for(self, handle: WorktreeHandle) -> list[PendingMerge]:
        """The task's current pending merges without consuming them.

        The replay gate's touch check reads this on every file tool call;
        the consuming pop stays inside ``replay_pending_merges`` so a merge
        landing between the check and the replay is never lost.
        """

        return list(self._pending_merges.get(str(Path(handle.path)), []))

    def replay_pending_merges(self, handle: WorktreeHandle) -> ReplayOutcome:
        """Replay the task's pending merges at a tool-call boundary.

        Demand-pull rebase-on-merge (ADR 0003): the agent just touched a file
        a landed sibling merge also changed. At this quiescent point - no
        filesystem tool is mid-flight - the dirty tree is committed as a
        ``wip:`` checkpoint, the node branch is rebased onto the current
        integration HEAD, and the working tree comes back either clean
        (``replayed``) or with conflict markers in the touched files
        (``conflicts``; the resolving agent continues its phase against
        them). Every mechanical step runs under ``integration_gate`` as a
        *reader*: the rebase reads the integration branch's tree, exactly
        like ``prepare``'s checkouts (issue #91) - it never moves that
        branch, so it must not exclude a concurrent sibling merge.

        Fail-open everywhere (ADR 0003's hard contract): any mechanical
        failure - including the Windows leftover dev-server lock files that
        block ``git rebase --autostash``'s working-tree bookkeeping - aborts
        the replay, restores the pre-replay state and returns
        ``ReplayOutcome.ABORTED``. The caller serves the tool call against
        the old tree; the overlap stays owned by the existing merge rails
        (additive resolution / arbitration / conflict requeue). This method
        never raises for git-level failures and never adds a terminal task
        state.
        """

        pending = self._pending_merges.pop(str(Path(handle.path)), [])
        if not pending:
            return ReplayOutcome(status=ReplayOutcome.SKIPPED)
        return self._replay_onto_integration(handle, pending)

    def _replay_onto_integration(
        self, handle: WorktreeHandle, pending: list[PendingMerge]
    ) -> ReplayOutcome:
        applied_files = sorted(
            {path for merge in pending for path in merge.changed_files if path}
        )
        try:
            with self.integration_gate.reader():
                return self._replay_git_section(handle, pending, applied_files)
        except Exception as exc:  # noqa: BLE001 - fail-open is the contract
            return ReplayOutcome(
                status=ReplayOutcome.ABORTED,
                files=applied_files,
                detail=f"{type(exc).__name__}: {exc}",
                attempted=True,
            )

    def _replay_git_section(
        self,
        handle: WorktreeHandle,
        pending: list[PendingMerge],
        applied_files: list[str],
    ) -> ReplayOutcome:
        integration = self._integration_branch()
        # No common history is the one genuine mechanical precondition;
        # everything else is decided by the rebase itself.
        merge_bases = self._git(
            ["merge-base", handle.branch, integration], cwd=handle.path, check=False
        )
        if merge_bases.returncode != 0:
            return ReplayOutcome(
                status=ReplayOutcome.ABORTED,
                files=applied_files,
                detail=f"merge-base failed: {merge_bases.stderr.strip()}",
                attempted=True,
            )
        integration_sha = (
            self._git(["rev-parse", integration], cwd=handle.path, check=False).stdout.strip()
        )
        branch_head_sha = self._git(["rev-parse", handle.branch], cwd=handle.path).stdout.strip()
        if integration_sha and integration_sha == branch_head_sha:
            # Every pending merge already sits under the branch (a second
            # wave arrived between the merge landing and this touch): the
            # rebase is a no-op and the merges count as applied.
            return ReplayOutcome(status=ReplayOutcome.REPLAYED, files=applied_files)

        dirty = self._worktree_dirty(handle.path)
        pre_replay_head = branch_head_sha
        if dirty:
            self._wip_commit(handle)
        rebase = self._git(["rebase", integration], cwd=handle.path, check=False)
        if rebase.returncode != 0:
            conflicted = self._unmerged_paths_in(handle.path)
            if conflicted:
                # The rebase stops mid-replay with markers in the tree; the
                # resolving agent continues against them (``continue_replay``
                # at the next tool boundary completes the replay).
                self._mid_rebase.add(str(Path(handle.path)))
                self._mid_rebase_base[str(Path(handle.path))] = pre_replay_head
                return ReplayOutcome(
                    status=ReplayOutcome.CONFLICTS,
                    files=conflicted,
                    detail=f"rebase onto {integration} conflicted",
                    attempted=True,
                )
            # A failed rebase with no conflict paths is a mechanical failure
            # (locked file, index damage): roll back to the pre-replay state.
            self._git(["rebase", "--abort"], cwd=handle.path, check=False)
            self._abort_replay(handle, pre_replay_head)
            failure = (rebase.stderr or rebase.stdout or "").strip()
            return ReplayOutcome(
                status=ReplayOutcome.ABORTED,
                files=applied_files,
                detail=failure,
                attempted=True,
            )
        return ReplayOutcome(
            status=ReplayOutcome.REPLAYED, files=applied_files, attempted=True
        )

    def _wip_commit(self, handle: WorktreeHandle) -> None:
        """Commit the dirty tree as a ``wip:`` checkpoint on the node branch.

        Mid-phase there is no prior commit to replay over (the phase's only
        commit happens at integrate), so a dirty tree must land before a
        rebase can start. The commit stays on the node branch; the phase-end
        ``integrate`` merges the branch as usual, so WIP commits are audit
        markers on the node branch, never integration output.
        """

        self._git(["add", "-A", "."], cwd=handle.path)
        result = self._git(
            ["commit", "-m", f"{WIP_COMMIT_PREFIX} mid-phase replay checkpoint"],
            cwd=handle.path,
            check=False,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).lower()
            if "nothing to commit" not in output:
                raise WorktreeError(
                    f"WIP commit failed in worktree {handle.path}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )

    def _abort_replay(self, handle: WorktreeHandle, pre_replay_head: str) -> None:
        """Restore the pre-replay state after a mechanical failure.

        The WIP checkpoint (if the tree was dirty) is reset back into the
        working tree so the agent's uncommitted view is byte-identical to
        before the replay; failures here are swallowed by the caller's
        fail-open envelope. ``pre_replay_head`` is the branch head captured
        *before* the WIP commit, so the reset lands on the exact prior
        commit even when a stale WIP from an earlier round sits in between.
        """

        current_head = (
            self._git(["rev-parse", "HEAD"], cwd=handle.path, check=False).stdout.strip()
        )
        if not pre_replay_head or current_head == pre_replay_head:
            return
        # Only the replay's own WIP commit is rolled back; a divergence the
        # replay did not create is left for the failure rails to explain.
        parents = self._git(
            ["rev-list", "--parents", "-n", "1", "HEAD"], cwd=handle.path, check=False
        ).stdout.split()
        if len(parents) >= 2 and parents[1] == pre_replay_head:
            self._git(
                ["reset", "--mixed", pre_replay_head], cwd=handle.path, check=False
            )

    def _unmerged_paths_in(self, worktree_path: str) -> list[str]:
        """Conflicted (unmerged) repo-relative paths inside a worktree."""

        return [
            line.strip()
            for line in self._git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=worktree_path,
                check=False,
            ).stdout.splitlines()
            if line.strip()
        ]

    def is_mid_rebase(self, handle: WorktreeHandle) -> bool:
        """Whether a prior replay left this worktree mid-rebase."""

        return str(Path(handle.path)) in self._mid_rebase

    def abort_replay(self, handle: WorktreeHandle) -> None:
        """Abort a mid-rebase worktree and restore its pre-replay state.

        The disarm path of the soft guard (issue #127): after three
        attempted conflict rounds the mid-phase replay stands down for the
        rest of the pass, and a rebase still sitting mid-replay must not
        dangle into the phase-end integrate - it is aborted here, the
        branch returns to the pre-replay head and the WIP checkpoint's
        content back into the working tree, so the merge rails own the
        overlap exactly like a mechanical failure. Never raises.
        """

        if str(Path(handle.path)) not in self._mid_rebase:
            return
        try:
            self._recover_aborted_rebase(handle)
        except Exception:  # noqa: BLE001 - fail-open is the contract
            self._clear_mid_rebase_marks(handle)

    def continue_replay(self, handle: WorktreeHandle) -> ReplayOutcome:
        """Advance or complete a mid-rebase worktree at a tool boundary.

        After a conflicted replay the agent resolves the markers with its
        ordinary file tools (edit/write). This method runs at the *next*
        tool boundary: when every conflicted path is resolved, the edits are
        staged, the rebase is continued (replaying any further commits), and
        the outcome is ``replayed`` (or ``conflicts`` again when a later
        commit of the replay also conflicts - the agent keeps resolving).
        Unresolved paths leave everything untouched. A mechanical failure
        aborts the rebase and restores the pre-replay WIP state, exactly
        like ``replay_pending_merges`` (fail-open; ``_wip_backup_commit``
        bookkeeping rides along). Never raises.
        """

        if str(Path(handle.path)) not in self._mid_rebase:
            return ReplayOutcome(status=ReplayOutcome.SKIPPED)
        try:
            with self.integration_gate.reader():
                return self._continue_rebase_section(handle)
        except Exception as exc:  # noqa: BLE001 - fail-open is the contract
            self._recover_aborted_rebase(handle)
            return ReplayOutcome(
                status=ReplayOutcome.ABORTED,
                detail=f"{type(exc).__name__}: {exc}",
                attempted=True,
            )
    def _continue_rebase_section(self, handle: WorktreeHandle) -> ReplayOutcome:
        conflict_paths = self._rebase_conflict_paths(handle)
        if not conflict_paths:
            # No unmerged index entries: the worktree may still be dirty with
            # the agent's ongoing edits. Continue the replay directly.
            return self._run_rebase_continue(handle, conflict_paths=[])
        # Unresolved-marker check first: staging a file that still carries
        # conflict markers would "resolve" the index with broken content
        # (the arbitration hand-off rejects the same shape).
        marker_tainted = [
            path
            for path in conflict_paths
            if _carries_conflict_markers(Path(handle.path) / path)
        ]
        if marker_tainted:
            return ReplayOutcome(status=ReplayOutcome.CONFLICTS, files=marker_tainted)
        # Stage the agent's resolutions: ``git add`` of an edited unmerged
        # path collapses the index entry and clears the unmerged state.
        for path in conflict_paths:
            add = self._git(["add", "--", path], cwd=handle.path, check=False)
            if add.returncode != 0:
                return ReplayOutcome(
                    status=ReplayOutcome.CONFLICTS,
                    files=conflict_paths,
                    detail=f"staging {path} failed: {add.stderr.strip()}",
                )
        still_unmerged = self._unmerged_paths_in(handle.path)
        if still_unmerged:
            return ReplayOutcome(status=ReplayOutcome.CONFLICTS, files=still_unmerged)
        return self._run_rebase_continue(handle, conflict_paths=conflict_paths)

    def _run_rebase_continue(
        self, handle: WorktreeHandle, *, conflict_paths: list[str]
    ) -> ReplayOutcome:
        """Run ``git rebase --continue`` and classify its outcome."""

        continue_result = self._git(
            ["rebase", "--continue"],
            cwd=handle.path,
            check=False,
            env_override={"GIT_EDITOR": ":"},
        )
        if continue_result.returncode == 0:
            self._clear_mid_rebase_marks(handle)
            self._wip_backup_commit(handle)
            # ``conflict_paths`` (the resolved round) ride along so the
            # boundary notice can name what was applied.
            return ReplayOutcome(
                status=ReplayOutcome.REPLAYED,
                files=conflict_paths,
                detail="rebase continued after conflict resolution",
                attempted=True,
                origin="continue",
            )
        unresolved = self._unmerged_paths_in(handle.path)
        if unresolved:
            # The next commit of the replay conflicts: another round.
            return ReplayOutcome(
                status=ReplayOutcome.CONFLICTS,
                files=unresolved,
                attempted=True,
                origin="continue",
            )
        self._recover_aborted_rebase(handle)
        failure = (continue_result.stderr or continue_result.stdout or "").strip()
        return ReplayOutcome(
            status=ReplayOutcome.ABORTED,
            detail=failure,
            attempted=True,
            origin="continue",
        )

    def _rebase_conflict_paths(self, handle: WorktreeHandle) -> list[str]:
        """The paths the in-progress rebase wants resolved.

        ``git status --porcelain`` lists them as ``UU``/``AA``/... entries
        plus unmerged index entries; the ``diff-filter=U`` read is empty at
        this point only when called before staging, so both reads union.
        """

        status = self._git(
            ["status", "--porcelain", "--untracked-files=no"],
            cwd=handle.path,
            check=False,
        ).stdout
        paths = {
            line[3:].strip().strip('"')
            for line in status.splitlines()
            if line and line[:2] in {"UU", "AA", "DU", "UD", "AU", "UA", "DD"}
        }
        return sorted(path for path in paths if path)

    def _wip_backup_commit(self, handle: WorktreeHandle) -> None:
        """Commit any post-replay working-tree edits as a WIP checkpoint.

        The agent may have resolved conflicts (or made further edits) in the
        working tree after the replay; landing them as a ``wip:`` commit
        keeps the branch self-describing for audit and the next replay.
        """

        if not self._worktree_dirty(handle.path):
            return
        try:
            self._wip_commit(handle)
        except WorktreeError:
            # Fail-open: an uncommittable tree stays dirty; the phase-end
            # integrate's own commit picks it up.
            pass

    def _recover_aborted_rebase(self, handle: WorktreeHandle) -> None:
        """Abort a stuck mid-rebase and restore the pre-replay state.

        ``git rebase --abort`` returns the branch to the pre-replay head -
        which includes the replay's WIP checkpoint, leaving a clean tree
        where the agent's view was dirty. The mixed reset onto the recorded
        pre-replay base puts the WIP content back into the working tree, so
        the agent's files are byte-identical to before the replay attempt.
        """

        self._git(["rebase", "--abort"], cwd=handle.path, check=False)
        base = self._mid_rebase_base.get(str(Path(handle.path)))
        self._clear_mid_rebase_marks(handle)
        if base:
            self._abort_replay(handle, base)

    def _clear_mid_rebase_marks(self, handle: WorktreeHandle) -> None:
        self._mid_rebase.discard(str(Path(handle.path)))
        self._mid_rebase_base.pop(str(Path(handle.path)), None)

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
    # integration-workspace services (merge-arbitration hand-off)
    # ------------------------------------------------------------------

    def read_conflict_stages(self, paths: list[str]) -> dict[str, dict[str, str | None]]:
        """The mid-merge index stages for the conflict paths.

        Reads ``base``/``ours``/``theirs`` from the integration workspace's
        merge index - the arbitration input's three-way content. Exposing it
        here keeps the index plumbing behind the manager's public interface
        instead of callers reaching for its git handle.
        """

        return _read_conflict_stages(lambda args: self._git(args, check=False), paths)

    def stage_paths(self, paths: list[str]) -> None:
        """Stage rewritten conflict/drift files in the integration workspace.

        Stage failures are ignored, matching the arbitration hand-off
        contract: the merge's own verification - the plain conflict check or
        the health gate - rejects an unstaged or broken tree.
        """

        for path in paths:
            self._git(["add", "--", path], check=False)

    def commit_integration(self, message: str) -> bool:
        """Commit the staged changes on the integration branch.

        Returns ``True`` when a commit was created and ``False`` when there
        was nothing to commit (the staged tree already matches HEAD - an
        accepted repair can be byte-identical to what landed). A real failure
        raises ``WorktreeError``.
        """

        result = self._git(["commit", "-m", message], check=False)
        if result.returncode == 0:
            return True
        output = (result.stdout + result.stderr).lower()
        if "nothing to commit" in output:
            return False
        raise WorktreeError(
            "git commit failed in the integration workspace: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

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
        env_override: dict[str, str] | None = None,
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
        if env_override:
            env.update(env_override)
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


# Git writes these at line starts in conflicted files. ``=======`` is
# deliberately absent: it is a legal Markdown setext underline, and a false
# positive here would burn the node's only arbitration budget.
_CONFLICT_MARKERS = (
    "<<<<<<<",
    ">>>>>>>",
    "|||||||",
)


def _carries_conflict_markers(path: Path) -> bool:
    """Whether a file still contains git conflict markers.

    An arbiter that echoes the markers back (instead of resolving them)
    produces a syntactically staged but semantically broken tree; the health
    gate would report a confusing boot failure instead of the honest cause.
    """

    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                stripped = line.rstrip("\r\n")
                if stripped.startswith(_CONFLICT_MARKERS):
                    return True
    except OSError:
        return False
    return False


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

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
from dataclasses import dataclass
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
            return WorktreeOutcome.PRESERVED
        if result is WorktreeTaskResult.RESET_FOR_RETRY:
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

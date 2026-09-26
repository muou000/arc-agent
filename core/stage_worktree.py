"""Stage-level Git worktrees and immutable publication envelopes.

Stage worktrees are deliberately separate from the older per-node worktree
mode.  A stage owns one branch and one directory, publishes a small immutable
envelope, and only the coordinator merges that branch into the integration
checkout.  Runtime state under ``.arc`` is ignored by Git and therefore never
becomes part of the stage artifact.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.queue_state import STAGE_VISUAL_ANALYSIS
from core.worktree import (
    MergeConflictError,
    MergeVerificationError,
    NodeWorktreeManager,
    WorktreeError,
    WorktreeOutcome,
    sanitize_node_id,
)


STAGE_BRANCH_PREFIX = "arc-stage"


class StagePublicationError(WorktreeError):
    """A stage cannot publish an auditable artifact envelope."""


@dataclass
class StageWorktreeHandle:
    """An isolated workspace for one formal stage."""

    node_id: str
    stage: str
    branch: str
    path: str
    main_workspace: str
    base_commit: str
    declared_write_set: tuple[str, ...] | None = None


@dataclass(frozen=True)
class StagePublication:
    """Immutable metadata returned by a successfully completed stage."""

    node_id: str
    stage: str
    base_commit: str
    artifact_commit: str
    declared_write_set: tuple[str, ...]
    contract_hash: str
    test_manifest_hash: str
    validation_evidence: dict[str, Any]
    changed_files: tuple[str, ...] = ()
    branch: str = ""
    worktree_path: str = ""

    def __post_init__(self) -> None:
        for field_name in ("node_id", "stage", "base_commit", "artifact_commit"):
            if not str(getattr(self, field_name) or "").strip():
                raise StagePublicationError(f"publication field {field_name} is required")
        for field_name in ("contract_hash", "test_manifest_hash"):
            if not str(getattr(self, field_name) or "").strip():
                raise StagePublicationError(f"publication field {field_name} is required")
        if not isinstance(self.validation_evidence, dict):
            raise StagePublicationError("validation_evidence must be an object")

        normalized_write_set = _normalize_write_set(self.declared_write_set, required=True)
        normalized_changed = _normalize_write_set(self.changed_files, required=True)
        object.__setattr__(self, "declared_write_set", normalized_write_set or ())
        object.__setattr__(self, "changed_files", normalized_changed or ())
        object.__setattr__(self, "validation_evidence", copy.deepcopy(self.validation_evidence))
        unexpected = sorted(set(normalized_changed or ()) - set(normalized_write_set or ()))
        if unexpected:
            raise StagePublicationError(
                "publication changed files are outside the declared write set: "
                + ", ".join(unexpected)
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe copy suitable for queue persistence."""

        return {
            "node_id": self.node_id,
            "stage": self.stage,
            "base_commit": self.base_commit,
            "artifact_commit": self.artifact_commit,
            "declared_write_set": list(self.declared_write_set),
            "contract_hash": self.contract_hash,
            "test_manifest_hash": self.test_manifest_hash,
            "validation_evidence": copy.deepcopy(self.validation_evidence),
            "changed_files": list(self.changed_files),
            "branch": self.branch,
            "worktree_path": self.worktree_path,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
        stage: str | None = None,
    ) -> "StagePublication":
        """Decode a persisted publication without retaining caller mutability."""

        if not isinstance(payload, Mapping):
            raise StagePublicationError("publication must be an object")
        return cls(
            node_id=str(payload.get("node_id") or node_id or ""),
            stage=str(payload.get("stage") or stage or ""),
            base_commit=str(payload.get("base_commit") or ""),
            artifact_commit=str(payload.get("artifact_commit") or ""),
            declared_write_set=tuple(payload.get("declared_write_set") or ()),
            contract_hash=str(payload.get("contract_hash") or ""),
            test_manifest_hash=str(payload.get("test_manifest_hash") or ""),
            validation_evidence=dict(payload.get("validation_evidence") or {}),
            changed_files=tuple(payload.get("changed_files") or ()),
            branch=str(payload.get("branch") or ""),
            worktree_path=str(payload.get("worktree_path") or ""),
        )


def stable_payload_hash(value: Any) -> str:
    """Hash a JSON-compatible value with deterministic key and list encoding."""

    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class StageWorktreeManager(NodeWorktreeManager):
    """Create, publish, merge, and settle isolated stage worktrees."""

    def __init__(self, workspace_path: str) -> None:
        super().__init__(workspace_path)
        self.worktrees_root = Path(self.main_workspace) / ".arc" / "stage-worktrees"
        # Probed on every stage scheduling decision; the workspace's
        # git-ness cannot flip during a run, so memoize the spawn.
        self._available: bool | None = None

    def is_available(self) -> bool:
        """Whether the workspace is a Git checkout ready for stage worktrees."""

        if self._available is None:
            result = self._git(
                ["rev-parse", "--is-inside-work-tree"], cwd=self.main_workspace, check=False
            )
            self._available = (
                result.returncode == 0 and result.stdout.strip().lower() == "true"
            )
        return self._available

    def prepare_stage(
        self,
        node_id: str,
        stage: str,
        *,
        declared_write_set: Iterable[str] | None = None,
        base_commit: str | None = None,
        restart_failed_attempt: bool = False,
    ) -> StageWorktreeHandle:
        """Create or resume a stage; restart only a newly scheduled failed attempt.

        Recovery of a READY_TO_MERGE publication must retain its committed
        branch and workspace. A new attempt instead starts at integration
        HEAD without inheriting uncommitted test files from the failed run.
        """

        normalized_stage = str(stage or "").strip().upper()
        if not normalized_stage or normalized_stage == STAGE_VISUAL_ANALYSIS:
            raise StagePublicationError("visual analysis does not use a product stage worktree")
        safe_node = sanitize_node_id(node_id)
        safe_stage = sanitize_node_id(normalized_stage)
        branch = f"{STAGE_BRANCH_PREFIX}/{safe_node}/{safe_stage}"
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        worktree_path = self.worktrees_root / f"{safe_node}--{safe_stage}"
        declared = _normalize_write_set(declared_write_set, required=False)

        with self.integration_gate.reader():
            if worktree_path.exists() and not self._is_registered(worktree_path):
                _remove_unregistered_stage_path(worktree_path, self.worktrees_root)
            registered = self._is_registered(worktree_path)
            if registered and not worktree_path.exists():
                # Stale registration: the directory is already gone, and a
                # plain ``worktree add`` refuses "missing but already
                # registered" paths. Prune clears the residue (same
                # self-heal as ``NodeWorktreeManager.prepare``).
                self._git(["worktree", "prune"], cwd=self.main_workspace, check=False)
                registered = False

            integration_branch = self._integration_branch()
            integration_head = _git_text(
                self._git(["rev-parse", integration_branch], cwd=self.main_workspace),
                f"rev-parse {integration_branch}",
            )
            if restart_failed_attempt:
                if registered:
                    # The existing removal helper unlinks shared node_modules
                    # before Git recurses through the worktree on Windows.
                    handle = StageWorktreeHandle(
                        node_id=str(node_id), stage=normalized_stage, branch=branch,
                        path=str(worktree_path), main_workspace=self.main_workspace,
                        base_commit=integration_head,
                    )
                    self._unlink_node_modules(handle)  # type: ignore[arg-type]
                    self._git(["worktree", "remove", "--force", str(worktree_path)], cwd=self.main_workspace)
                self._detach_branch_elsewhere(branch, keep_path=worktree_path)
                self._git(
                    ["worktree", "add", "-B", branch, str(worktree_path), integration_branch],
                    cwd=self.main_workspace,
                )
                base_commit = integration_head
            elif registered:
                if self._branch_exists(branch):
                    self._git(["checkout", branch], cwd=str(worktree_path))
                else:
                    self._git(["checkout", "-B", branch, integration_branch], cwd=str(worktree_path))
                if base_commit is None:
                    base_commit = _git_text(
                        self._git(["merge-base", branch, integration_branch], cwd=self.main_workspace),
                        f"merge-base {branch} {integration_branch}",
                    )
            elif self._branch_exists(branch):
                self._detach_branch_elsewhere(branch, keep_path=worktree_path)
                self._git(["worktree", "add", str(worktree_path), branch], cwd=self.main_workspace)
                if base_commit is None:
                    base_commit = _git_text(
                        self._git(["merge-base", branch, integration_branch], cwd=self.main_workspace),
                        f"merge-base {branch} {integration_branch}",
                    )
            else:
                self._git(
                    ["worktree", "add", "-b", branch, str(worktree_path), integration_branch],
                    cwd=self.main_workspace,
                )

        handle = StageWorktreeHandle(
            node_id=str(node_id),
            stage=normalized_stage,
            branch=branch,
            path=str(worktree_path),
            main_workspace=self.main_workspace,
            base_commit=str(base_commit or integration_head),
            declared_write_set=declared,
        )
        self._link_node_modules(handle)  # type: ignore[arg-type]
        self._seed_frontend_dist(handle)  # type: ignore[arg-type]
        return handle

    def changed_files(self, handle: StageWorktreeHandle) -> tuple[str, ...]:
        """Return tracked and non-ignored untracked changes in a stage tree."""

        tracked = self._git(
            ["diff", "--name-only", handle.base_commit],
            cwd=handle.path,
            check=False,
        )
        if tracked.returncode != 0:
            raise StagePublicationError(
                f"cannot inspect stage changes: {tracked.stderr.strip() or tracked.stdout.strip()}"
            )
        untracked = self._git(
            ["ls-files", "--others", "--exclude-standard"],
            cwd=handle.path,
            check=False,
        )
        if untracked.returncode != 0:
            raise StagePublicationError(
                f"cannot inspect untracked stage changes: {untracked.stderr.strip() or untracked.stdout.strip()}"
            )
        paths = {
            path
            for raw in (tracked.stdout.splitlines() + untracked.stdout.splitlines())
            if (path := _normalize_repo_path(raw))
        }
        return tuple(sorted(paths))

    def publish(
        self,
        handle: StageWorktreeHandle,
        message: str,
        *,
        declared_write_set: Iterable[str] | None = None,
        contract_hash: str,
        test_manifest_hash: str,
        validation_evidence: Mapping[str, Any],
    ) -> StagePublication:
        """Validate and commit a stage, returning an immutable publication."""

        declared = _normalize_write_set(
            handle.declared_write_set if declared_write_set is None else declared_write_set,
            required=True,
        )
        if declared is None:
            raise StagePublicationError("stage publication requires a declared write set")

        try:
            self._git(["add", "-A", "."], cwd=handle.path)
            staged_result = self._git(["diff", "--cached", "--name-only"], cwd=handle.path)
            staged = tuple(
                sorted(
                    path
                    for raw in staged_result.stdout.splitlines()
                    if (path := _normalize_repo_path(raw))
                )
            )
            protected = _coordinator_conflicts(staged_result.stdout)
            if protected:
                raise StagePublicationError(
                    "stage worktree attempted to publish coordinator files: " + ", ".join(protected)
                )
            unexpected = sorted(set(staged) - set(declared))
            if unexpected:
                raise StagePublicationError(
                    "stage changes are outside the declared write set: " + ", ".join(unexpected)
                )
            commit = self._git(["commit", "-m", message], cwd=handle.path, check=False)
            output = (commit.stdout + commit.stderr).lower()
            if commit.returncode != 0 and "nothing to commit" not in output:
                raise StagePublicationError(
                    f"stage commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
                )
        except Exception:
            self._git(["reset", "--quiet"], cwd=handle.path, check=False)
            raise

        artifact_commit = _git_text(
            self._git(["rev-parse", "HEAD"], cwd=handle.path),
            "rev-parse stage artifact",
        )
        return StagePublication(
            node_id=handle.node_id,
            stage=handle.stage,
            base_commit=handle.base_commit,
            artifact_commit=artifact_commit,
            declared_write_set=declared,
            contract_hash=contract_hash,
            test_manifest_hash=test_manifest_hash,
            validation_evidence=dict(validation_evidence),
            changed_files=staged,
            branch=handle.branch,
            worktree_path=handle.path,
        )

    def integrate_stage(
        self,
        handle: StageWorktreeHandle,
        publication: StagePublication,
        message: str,
        *,
        verify: Callable[[], str | None] | None = None,
    ) -> tuple[bool, str]:
        """Merge one publication under the integration writer gate.

        A stale publication gets one mechanical rebase onto the current
        integration HEAD.  Rebase or merge conflicts preserve the stage tree;
        no semantic arbitration is installed at this layer.
        """

        if publication.node_id != handle.node_id or publication.stage != handle.stage:
            raise StagePublicationError("publication does not belong to the supplied stage handle")
        if publication.branch and publication.branch != handle.branch:
            raise StagePublicationError("publication branch does not match the stage handle")
        branch_head = _git_text(
            self._git(["rev-parse", handle.branch], cwd=self.main_workspace),
            f"rev-parse {handle.branch}",
        )
        if branch_head != publication.artifact_commit:
            raise StagePublicationError("stage branch changed after publication")

        with self.integration_gate.writer():
            integration_branch = self._integration_branch()
            current_head = _git_text(
                self._git(["rev-parse", integration_branch], cwd=self.main_workspace),
                f"rev-parse {integration_branch}",
            )
            # An already-integrated branch has nothing to replay, so it must
            # not be rebased: a fast-forward would move the branch head away
            # from the publication's artifact commit and break resume replays.
            already_integrated = self._git(
                ["merge-base", "--is-ancestor", handle.branch, current_head],
                cwd=self.main_workspace,
                check=False,
            ).returncode == 0
            rebased = False
            if not already_integrated and current_head != publication.base_commit:
                rebased = self._rebase_stage(handle, integration_branch)

            branch_head_after_rebase = _git_text(
                self._git(["rev-parse", handle.branch], cwd=self.main_workspace),
                f"rev-parse {handle.branch} after rebase",
            )
            already_integrated = self._git(
                ["merge-base", "--is-ancestor", branch_head_after_rebase, current_head],
                cwd=self.main_workspace,
                check=False,
            ).returncode == 0
            if branch_head_after_rebase == current_head or already_integrated:
                failure: str | None = None
                if verify is not None:
                    try:
                        failure = verify()
                    except Exception as exc:  # noqa: BLE001 - report a health-gate failure
                        failure = f"verification crashed: {type(exc).__name__}: {exc}"
                if failure:
                    self._quarantined.add(str(Path(handle.path)))
                    raise MergeVerificationError(
                        f"stage {handle.node_id}:{handle.stage} failed the health gate: {failure}"
                    )
                detail = f"stage {handle.branch} is already at {integration_branch}"
                if rebased:
                    detail += " after one rebase"
                return False, detail

            merge = self._git(
                ["merge", "--no-ff", "--no-commit", handle.branch, "-m", message],
                cwd=self.main_workspace,
                check=False,
            )
            resolved: list[str] = []
            if merge.returncode != 0:
                conflict_paths = self._unresolved_paths()
                if not conflict_paths or (additions := self._resolve_conflicts_by_addition(conflict_paths)) is None:
                    self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
                    self._quarantined.add(str(Path(handle.path)))
                    files = ", ".join(conflict_paths[:8]) or "unknown files"
                    raise MergeConflictError(
                        f"merging stage {handle.node_id}:{handle.stage} conflicted on: {files}. "
                        "The stage worktree is preserved for inspection.",
                        files=conflict_paths,
                    )
                resolved = additions

            # Last line of defense before coordinator state lands: publish
            # rejects ``.arc`` writes, but a stage branch that carries them
            # anyway must never merge them into the shared workspace.
            staged_result = self._git(
                ["diff", "--cached", "--name-only"],
                cwd=self.main_workspace,
            )
            coordinator = _coordinator_conflicts(staged_result.stdout)
            if coordinator:
                self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
                self._quarantined.add(str(Path(handle.path)))
                raise StagePublicationError(
                    f"stage {handle.node_id}:{handle.stage} attempted to merge "
                    "coordinator files into integration: " + ", ".join(coordinator)
                )

            failure: str | None = None
            if verify is not None:
                try:
                    failure = verify()
                except Exception as exc:  # noqa: BLE001 - health gates are reported as merge failures
                    failure = f"verification crashed: {type(exc).__name__}: {exc}"
            if failure:
                self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
                self._quarantined.add(str(Path(handle.path)))
                raise MergeVerificationError(
                    f"stage {handle.node_id}:{handle.stage} failed the post-merge health gate: {failure}"
                )

            completed = self._git(["commit", "--no-edit"], cwd=self.main_workspace, check=False)
            if completed.returncode != 0:
                self._git(["merge", "--abort"], cwd=self.main_workspace, check=False)
                self._quarantined.add(str(Path(handle.path)))
                raise WorktreeError(
                    f"completing stage merge failed: {completed.stderr.strip() or completed.stdout.strip()}"
                )
            detail = f"merged {handle.branch} into {integration_branch}"
            if rebased:
                detail += " after one rebase"
            if resolved:
                detail += " with additive conflict resolution of: " + ", ".join(resolved)
            return True, detail

    def settle_stage(self, handle: StageWorktreeHandle, *, published: bool) -> WorktreeOutcome:
        """Delete a landed stage tree or preserve a failed one for inspection."""

        if not published:
            return WorktreeOutcome.PRESERVED
        self._quarantined.discard(str(Path(handle.path)))
        self._remove_worktree(handle)  # type: ignore[arg-type]
        return WorktreeOutcome.DELETED

    def _rebase_stage(self, handle: StageWorktreeHandle, integration_branch: str) -> bool:
        rebased = self._git(
            ["rebase", integration_branch],
            cwd=handle.path,
            check=False,
        )
        if rebased.returncode == 0:
            return True
        conflict_paths = self._unmerged_paths_in(handle.path)
        aborted = self._git(["rebase", "--abort"], cwd=handle.path, check=False)
        if aborted.returncode != 0:
            self._quarantined.add(str(Path(handle.path)))
            raise WorktreeError(
                f"could not abort stage rebase {handle.branch}: {aborted.stderr.strip() or aborted.stdout.strip()}"
            )
        if not conflict_paths:
            self._quarantined.add(str(Path(handle.path)))
            raise WorktreeError(
                f"rebasing stage {handle.branch} failed: {rebased.stderr.strip() or rebased.stdout.strip()}"
            )
        # The existing integration merge layer can resolve pure additions
        # mechanically even if Git's rebase could not; semantic conflicts
        # remain fatal and preserve the original stage branch for inspection.
        return False


def _normalize_repo_path(value: object) -> str:
    path = str(value or "").replace("\\", "/").strip()
    if not path:
        return ""
    if path.startswith("/workspace/"):
        path = path[len("/workspace/") :]
    if path.startswith("/") or ":" in path.split("/", 1)[0]:
        return ""
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return ""
    return "/".join(parts)


def _git_text(result: Any, operation: str) -> str:
    if result.returncode != 0:
        raise StagePublicationError(
            f"git {operation} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    value = result.stdout.strip()
    if not value:
        raise StagePublicationError(f"git {operation} returned no value")
    return value


def _normalize_write_set(
    values: Iterable[str] | None,
    *,
    required: bool,
) -> tuple[str, ...] | None:
    if values is None:
        if required:
            raise StagePublicationError("stage publication requires a declared write set")
        return None
    if isinstance(values, (str, bytes)):
        raise StagePublicationError("declared_write_set must be a sequence of repository paths")
    normalized: set[str] = set()
    for value in values:
        path = _normalize_repo_path(value)
        if not path:
            raise StagePublicationError(f"invalid stage write-set path: {value!r}")
        normalized.add(path)
    return tuple(sorted(normalized))


def _is_coordinator_path(path: str) -> bool:
    normalized = path.casefold()
    return normalized == ".arc" or normalized.startswith(".arc/") or normalized.endswith(
        "/processing_queue.json"
    )


def _coordinator_conflicts(staged_output: str) -> list[str]:
    """Coordinator paths within ``git diff --cached --name-only`` output.

    Shared by the publish-time and merge-time guards: runtime state under
    ``.arc`` belongs to the coordinator and must neither be published from
    a stage worktree nor merged out of one.
    """

    return sorted(
        {
            path
            for raw in staged_output.splitlines()
            if (path := _normalize_repo_path(raw)) and _is_coordinator_path(path)
        }
    )


def _remove_unregistered_stage_path(path: Path, root: Path) -> None:
    resolved_root = root.expanduser().resolve()
    resolved_path = path.expanduser().resolve()
    if resolved_path == resolved_root or not resolved_path.is_relative_to(resolved_root):
        raise StagePublicationError(f"refusing to remove path outside stage worktree root: {path}")
    shutil.rmtree(resolved_path, ignore_errors=True)

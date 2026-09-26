"""Declaration and enforcement state for a staged agent write set.

The stage pipeline must know a stage's conflict domain before the stage starts.
This lock is intentionally in-memory: the coordinator owns publication and
queue persistence, while the stage agent can only write paths it declared in
its first tool call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from agents.runtime.capabilities import (
    is_node_test_path,
    is_shared_test_resource,
    is_test_asset,
    node_test_namespace_hint,
    normalize_manifest_path,
)
from agents.tools.declaration_budget import ConsecutiveRejectionBudget, rejected_message


def normalize_write_set_path(value: object) -> str:
    """Normalize one declared path and reject traversal/absolute forms."""

    raw = str(value or "").strip().replace("\\", "/")
    if raw.startswith("/workspace/"):
        raw = raw[len("/workspace/") :]
    elif raw == "/workspace":
        raw = ""
    elif raw.startswith("/"):
        return ""
    while raw.startswith("./"):
        raw = raw[2:]
    windows_path = PureWindowsPath(raw)
    if windows_path.is_absolute() or windows_path.drive:
        return ""
    if not raw or any(part in {"", ".", ".."} for part in raw.split("/")):
        return ""
    normalized = normalize_manifest_path(raw)
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        return ""
    return normalized


@dataclass
class StageWriteSetLock:
    """Declared workspace-relative writable paths for one staged pass.

    The implementation pass has one narrow escape from the initial complete
    declaration: it may append current-node test assets discovered after a
    test failure. Product paths, shared test resources, sibling namespaces,
    removals, and renames remain rejected.
    """

    stage: str = ""
    node_id: str = ""
    declared_paths: set[str] = field(default_factory=set)
    _locked: bool = False

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.declared_paths))

    def declare(self, paths: list[str]) -> str | None:
        """Lock the complete set, returning a deterministic error on drift."""

        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            return "The stage write set must be an array of workspace-relative path strings."
        normalized: list[str] = []
        invalid: list[str] = []
        for raw in paths:
            path = normalize_write_set_path(raw)
            if not path:
                invalid.append(str(raw or ""))
            elif path not in normalized:
                normalized.append(path)
        if invalid:
            return "Invalid stage write-set path(s): " + ", ".join(repr(item) for item in invalid)
        for path in normalized:
            if is_shared_test_resource(path):
                error = (
                    f"Shared test resource `{path}` is read-only and cannot be declared for a stage write."
                )
                return self._with_append_guidance(error)
            if self.node_id and is_test_asset(path) and not is_node_test_path(path, self.node_id):
                error = (
                    f"Test asset `{path}` is outside node `{self.node_id}`'s stable test namespace. "
                    f"{node_test_namespace_hint(self.node_id)} "
                    "Sibling test paths cannot be declared for this stage."
                )
                return self._with_append_guidance(error)
        proposed = set(normalized)
        if self._locked:
            if proposed == self.declared_paths:
                return None
            if self.stage.strip().lower() != "implementation" or not proposed.issuperset(
                self.declared_paths
            ):
                return self._with_append_guidance(
                    "The stage write set is already locked; a later declaration cannot add, "
                    "remove, or rename paths. Re-declare the exact original set."
                )
            additions = proposed - self.declared_paths
            for path in additions:
                if not is_test_asset(path):
                    return self._with_append_guidance(
                        f"Product path `{path}` cannot be appended to the implementation stage write set."
                    )
                if not self.node_id or not is_node_test_path(path, self.node_id):
                    return self._with_append_guidance(
                        f"Test asset `{path}` cannot be appended without the current node's stable test namespace."
                    )
            self.declared_paths.update(additions)
            return None
        self.declared_paths = proposed
        self._locked = True
        return None

    def _with_append_guidance(self, error: str) -> str:
        """Point failed implementation declarations at the only late escape."""

        if self._locked and self.stage.strip().lower() == "implementation":
            return (
                f"{error} To extend an implementation write set, append only new test assets "
                "inside the current node's stable test namespace; product paths remain closed."
            )
        return error

    def contains(self, path: str) -> bool:
        normalized = normalize_write_set_path(path)
        return bool(normalized) and normalized in self.declared_paths


def build_declare_stage_write_set_tool(*, stage: str, lock: StageWriteSetLock):
    """Build the model-facing declaration tool for one stage pass."""

    rejection_budget = ConsecutiveRejectionBudget()
    implementation_append_note = (
        "Implementation may append only new test assets inside the current node's stable test namespace."
        if str(stage or "").strip().lower() == "implementation"
        else "The stage write set remains fixed after the successful declaration."
    )

    async def declare_stage_write_set(paths: list[str]) -> str:
        """Declare every workspace-relative path this stage may write.

        Call this before the first file write. A path omitted here is otherwise
        rejected at the middleware boundary.
        Shared runner configuration and fixtures are never valid write targets
        even when a model includes them in this declaration.
        """

        if not isinstance(paths, list):
            return _tool_error(
                rejected_message(
                    rejection_budget,
                    "declare_stage_write_set",
                    "The stage write set must be a JSON array of workspace-relative paths.",
                )
            )
        error = lock.declare(paths)
        if error:
            return _tool_error(
                rejected_message(rejection_budget, "declare_stage_write_set", error)
            )
        rejection_budget.record_acceptance()
        return json.dumps(
            {
                "status": "locked",
                "stage": str(stage or "").strip().upper(),
                "declared_write_set": list(lock.paths),
                "note": (
                    "The stage write set is locked. Every later write, edit, append, or delete "
                    f"must target one of these paths. {implementation_append_note}"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    declare_stage_write_set.__doc__ = (declare_stage_write_set.__doc__ or "") + (
        " " + implementation_append_note
    )
    if lock.node_id:
        # The stable segment is a sha256 digest of the node id — unguessable
        # for the model, so the tool description carries the concrete value
        # from the first turn instead of leaving it to a rejection.
        declare_stage_write_set.__doc__ = (declare_stage_write_set.__doc__ or "") + (
            " " + node_test_namespace_hint(lock.node_id)
        )
    declare_stage_write_set.__name__ = "declare_stage_write_set"
    return declare_stage_write_set


def _tool_error(message: str) -> str:
    return json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2)

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
    normalize_manifest_path,
)


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
    """Immutable-after-declaration set of workspace-relative writable paths."""

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
                return f"Shared test resource `{path}` is read-only and cannot be declared for a stage write."
            if self.node_id and is_test_asset(path) and not is_node_test_path(path, self.node_id):
                return (
                    f"Test asset `{path}` is outside node `{self.node_id}`'s stable test namespace; "
                    "sibling test paths cannot be declared for this stage."
                )
        proposed = set(normalized)
        if self._locked:
            if proposed != self.declared_paths:
                return (
                    "The stage write set is already locked; a later declaration cannot add, "
                    "remove, or rename paths. Re-declare the exact original set."
                )
            return None
        self.declared_paths = proposed
        self._locked = True
        return None

    def contains(self, path: str) -> bool:
        normalized = normalize_write_set_path(path)
        return bool(normalized) and normalized in self.declared_paths


def build_declare_stage_write_set_tool(*, stage: str, lock: StageWriteSetLock):
    """Build the model-facing declaration tool for one stage pass."""

    async def declare_stage_write_set(paths: list[str]) -> str:
        """Declare every workspace-relative path this stage may write.

        Call this before the first file write.  The set is immutable for the
        pass; a path omitted here is rejected at the middleware boundary.
        Shared runner configuration and fixtures are never valid write targets
        even when a model includes them in this declaration.
        """

        if not isinstance(paths, list):
            return _tool_error("The stage write set must be a JSON array of workspace-relative paths.")
        error = lock.declare(paths)
        if error:
            return _tool_error(error)
        return json.dumps(
            {
                "status": "locked",
                "stage": str(stage or "").strip().upper(),
                "declared_write_set": list(lock.paths),
                "note": (
                    "The stage write set is locked. Every later write, edit, append, or delete "
                    "must target one of these paths."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    declare_stage_write_set.__name__ = "declare_stage_write_set"
    return declare_stage_write_set


def _tool_error(message: str) -> str:
    return json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2)

"""Test-manifest declaration tool and lock for the TestGenerator stage.

Manifest-first test generation: the agent must declare the full test-file
manifest (path + type + covered interfaces) through ``declare_test_manifest``
before any test file may be written, edited, or deleted. The declaration is
then locked for the rest of the stage run — subsequent writes to test files
are valid only on declared paths, which removes the whole class of
rename/duplicate-file churn (an undeclared path cannot be created at all, so
"try a different name" and "write the same coverage in a second format" both
become hard errors at write time instead of manifest drift discovered
downstream).

The lock is deliberately an in-memory object shared by value between the
declare tool (which fills it) and ``StageDisciplineMiddleware`` (which
enforces it on every file tool call). It never touches the workspace, so a
crashed or retried run leaves no stale state behind.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.runtime.capabilities import is_test_file_path, normalize_manifest_path
from core.test_types import CANONICAL_TEST_TYPES, canonical_test_type

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

_TOOL_NAME = "declare_test_manifest"
TEST_COVERAGE_SCOPES = frozenset({"owned", "dependency", "shared"})


def normalize_coverage_scope(value: Any) -> str:
    """Return the manifest coverage scope, defaulting old manifests to owned."""

    scope = str(value or "owned").strip().lower() or "owned"
    return scope if scope in TEST_COVERAGE_SCOPES else ""


@dataclass
class DeclaredTestFile:
    """One declared manifest row: a file, scope, type, and interfaces."""

    file_path: str
    test_type: str
    interface_ids: list[str] = field(default_factory=list)
    coverage_scope: str = "owned"


@dataclass
class TestManifestLock:
    """Locked set of test-file paths the TestGenerator may touch.

    ``declared_files`` maps a workspace-relative path to its declared row. The
    path key is the canonical form produced by :func:`normalize_manifest_path`
    (virtual prefix stripped, backslashes converted); the discipline compares
    every tool-call path through the same normalization, so the comparison is
    immune to ``/workspace/`` prefix and separator drift.
    """

    declared_files: dict[str, DeclaredTestFile] = field(default_factory=dict)

    @property
    def locked(self) -> bool:
        return bool(self.declared_files)

    def declare(self, declared_files: list[DeclaredTestFile]) -> None:
        for item in declared_files:
            self.declared_files.setdefault(item.file_path, item)

    def contains(self, file_path: str) -> bool:
        return normalize_manifest_path(file_path) in self.declared_files


def build_declare_test_manifest_tool(
    *,
    node_id: str,
    manifest_lock: TestManifestLock,
    validate_test_path: Callable[[str, str], str | None] | None = None,
    log_cb: LogCallback | None = None,
    current_interface_ids: list[str] | set[str] | None = None,
    require_interface_coverage: bool = False,
):
    """Build the ``declare_test_manifest`` tool for the current stage run.

    ``validate_test_path`` is the app-type handler's placement validator (the
    same one ``WorkflowPhaseRunner._prepare_tests`` applies later); declaring
    runs it up front so an invalid placement is rejected at declaration time
    with the exact rule text, instead of failing the whole DESIGN phase after
    the files were already written. ``None`` disables that early check (unit
    tests, handlers without placement rules).

    ``current_interface_ids`` contains DESIGN-stage contracts that are already
    present in the node session but not yet committed to the traceability DB.
    ``require_interface_coverage`` rejects empty coverage rows when the current
    node owns interfaces, preventing a model from using ``[]`` to bypass a
    failed or temporarily unavailable interface lookup.
    """

    staged_interface_ids = {
        str(value).strip() for value in current_interface_ids or [] if str(value or "").strip()
    }

    async def declare_test_manifest(files: list[dict[str, Any]]) -> str:
        """Declare and lock the test-file manifest for this stage run.

        Call this exactly once, before writing any test file, with one entry
        per test file you intend to create or update. ``coverage_scope`` is
        ``owned`` for behavior introduced by this node, ``dependency`` for a
        dependency regression, and ``shared`` for a shared-contract check:
        [{"file_path": "backend/tests/unit/auth_service.test.js", "type": "Unit", "coverage_scope": "owned", "interface_ids": ["IF-AUTH-SERVICE"]}].

        The declaration is validated (placement rules, interface ids, type)
        and then LOCKED for the rest of this stage run: write_file, edit_file
        and delete on a test-file path that is not in the declared manifest
        are rejected. Test files are paths with a `.test.`/`.spec.` name, a
        Python `test_*.py`/`*_test.py` name, or any source file under a
        `test-e2e` directory. Include every planned test file in this one
        call; a second declaration may only add paths that failed validation
        earlier. Test helpers and runner configs do not belong in the
        manifest and stay writable without being declared.
        """

        if not isinstance(files, list) or not files:
            return _tool_error(
                "The manifest declaration must be a non-empty list of "
                "{file_path, type, coverage_scope, interface_ids} entries. If this node should "
                "own no local tests, skip declaring and return an empty `tests` "
                "manifest instead."
            )

        rows: list[DeclaredTestFile] = []
        errors: list[str] = []
        known_paths: set[str] = set()
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                errors.append(f"Entry {index} is not an object.")
                continue
            file_path = normalize_manifest_path(item.get("file_path"))
            raw_type = str(item.get("type", "") or "").strip()
            raw_interface_ids = item.get("interface_ids")
            unwrapped_ids = _unwrap_interface_id_shapes(raw_interface_ids)
            interface_ids = [
                str(value).strip()
                for value in (unwrapped_ids if unwrapped_ids is not None else raw_interface_ids or [])
                if str(value or "").strip()
            ]
            coverage_scope = normalize_coverage_scope(item.get("coverage_scope"))
            if not file_path:
                errors.append(f"Entry {index} is missing `file_path`.")
                continue
            if file_path in known_paths:
                errors.append(f"Entry {index} duplicates the path `{file_path}`.")
                continue
            known_paths.add(file_path)
            test_type = canonical_test_type(raw_type)
            if test_type is None:
                errors.append(
                    f"Entry {index} (`{file_path}`): `type` must be one of "
                    f"{', '.join(CANONICAL_TEST_TYPES)} (received `{raw_type or 'empty'}`)."
                )
                continue
            if not coverage_scope:
                errors.append(
                    f"Entry {index} (`{file_path}`): `coverage_scope` must be one of "
                    "owned, dependency, or shared."
                )
                continue
            if not is_test_file_path(file_path):
                errors.append(
                    f"Entry {index}: `{file_path}` does not look like a test file. "
                    "Manifest entries must be test files (`.test.`/`.spec.` in the "
                    "name); helpers and runner configs are written without a "
                    "manifest entry."
                )
                continue
            if validate_test_path is not None:
                validation_error = validate_test_path(test_type, file_path)
                if validation_error:
                    errors.append(f"Entry {index} (`{file_path}`): {validation_error}")
                    continue
            rows.append(
                DeclaredTestFile(
                    file_path=file_path,
                    test_type=test_type,
                    interface_ids=interface_ids,
                    coverage_scope=coverage_scope,
                )
            )

        if require_interface_coverage and staged_interface_ids:
            for row in rows:
                if not row.interface_ids:
                    errors.append(
                        f"Entry `{row.file_path}` has empty `interface_ids`, but the current node "
                        "owns interface contracts. Map this test file to the exact current interface id(s); "
                        "do not use an empty list to bypass coverage validation."
                    )

        unknown_interfaces = _unknown_interface_ids(rows, known_interface_ids=staged_interface_ids)
        if unknown_interfaces:
            valid_interfaces = _valid_interface_ids(
                known_interface_ids=staged_interface_ids,
                referenced_ids={interface_id for row in rows for interface_id in row.interface_ids},
            )
            hint = ""
            if valid_interfaces:
                hint = (
                    " The current interface contract defines these valid id(s): "
                    + ", ".join(valid_interfaces)
                    + ". Re-map the offending entries to these ids."
                )
            errors.append(
                "Unknown interface id(s) not present in the traceability DB: "
                + ", ".join(sorted(unknown_interfaces))
                + f".{hint} Use ids returned by InterfaceDesigner or the traceability tools."
                + _MANIFEST_SHAPE_EXAMPLE
            )

        if errors:
            return _tool_error(
                "The manifest declaration was rejected. Fix every issue and "
                "re-declare the complete manifest:\n- " + "\n- ".join(errors)
            )

        first_declaration = not manifest_lock.locked
        manifest_lock.declare(rows)
        await _emit_log(
            log_cb,
            "TestGenerator",
            (
                f"Declared test-file manifest for `{node_id}`: "
                f"{len(manifest_lock.declared_files)} file(s) locked "
                f"({', '.join(sorted(manifest_lock.declared_files))})."
            ),
            node_id=node_id,
        )
        return json.dumps(
            {
                "status": "locked",
                "manifest": [
                    {
                        "file_path": row.file_path,
                        "type": row.test_type,
                        "coverage_scope": row.coverage_scope,
                        "interface_ids": row.interface_ids,
                    }
                    for row in sorted(manifest_lock.declared_files.values(), key=lambda row: row.file_path)
                ],
                "note": (
                    "Proceed to write the declared test files. The manifest is now "
                    "locked: test-file writes, edits and deletes are only valid on "
                    "the declared paths above."
                )
                + (
                    ""
                    if first_declaration
                    else " (Rows that failed an earlier declaration attempt were added.)"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    declare_test_manifest.__name__ = _TOOL_NAME
    return declare_test_manifest


def _unknown_interface_ids(
    rows: list[DeclaredTestFile],
    *,
    known_interface_ids: set[str] | None = None,
) -> set[str]:
    """Interface ids that do not exist in the traceability DB.

    Empty-string and placeholder ids are ignored (they never reach here — the
    row builder drops empty values). A traceability store that cannot answer
    (missing runtime) fails open: the workflow's own manifest validation
    remains the last line of defense, and a tool error here would only invite
    retry loops on infrastructure hiccups.
    """

    # Imported lazily: module-level ``core.service`` import would be circular
    # (stage_discipline imports this module; core.service imports agent
    # runtime modules that read stage_discipline constants).
    from core.service import get_runtime

    try:
        store = get_runtime().traceability
    except Exception:
        return set()
    known = known_interface_ids or set()
    unknown: set[str] = set()
    for row in rows:
        for interface_id in row.interface_ids:
            if interface_id in known:
                continue
            if interface_id not in unknown and store.get_interface(interface_id) is None:
                unknown.add(interface_id)
    return unknown


#: Cap on how many valid interface ids the rejection hint lists. The staged
#: current-node ids come first (the ones the model should be mapping to); the
#: tail is cut, not the head, so oversized DBs never bury the actionable ids.
_MANIFEST_HINT_MAX_IDS = 12


#: Appended to the unknown-interface-id rejection. The run that motivated the
#: unwrap (29 consecutive redeclarations) showed that listing valid ids is
#: not enough — without the expected parameter shape the model blind-tries
#: wrapper objects one key at a time.
_MANIFEST_SHAPE_EXAMPLE = (
    " Expected shape: `interface_ids` is a flat JSON array of id strings — "
    'e.g. "interface_ids": ["IF-AUTH-SERVICE"] — never a wrapper object '
    '({"item": ...}, {"id": ...}, {"value": ...}, {"interface_id": ...}, '
    '{"entries": {"entry": ...}}) or a bare string.'
)


#: Wrapper keys that ToolStrategy XML serialization has been observed to wrap
#: the ``interface_ids`` array in (easy-ticketbooking run 2026-09-21, REQ-2
#: DESIGN: 29 declarations across 8 shapes before the model produced the flat
#: array). ``item``/``id``/``value``/``interface_id`` are the wrappers named
#: by issue #113; ``entries``/``entry`` is the doubly-nested list form seen in
#: the same run.
_INTERFACE_ID_WRAPPER_KEYS = frozenset(
    {"item", "id", "value", "interface_id", "entries", "entry"}
)

#: Depth cap on wrapper unwrapping. Observed wrappers nest at most two
#: single-key dicts deep; the cap is generous headroom (a deeper nest still
#: falls through to rejection) and exists to bound the recursion, not to
#: match the evidence exactly.
_INTERFACE_ID_UNWRAP_MAX_DEPTH = 5


def _unwrap_interface_id_shapes(value: Any, *, depth: int = 0) -> list[str] | None:
    """Mechanically unwrap known wrapper shapes around ``interface_ids``.

    ToolStrategy structured output occasionally serializes the flat id array
    as a wrapper object (``{"item": id}``, ``{"id": id}``, ``{"entries":
    {"entry": id}}``, ...) or a bare string; the validator then read the
    wrapper key — or every character of the string — as an interface id and
    rejected, and the model burned ~25 declarations blind-trying shapes.
    Known wrappers are unwrapped here so validation sees the ids the model
    meant; id validity and coverage checks run on the unwrapped list
    unchanged.

    Returns the unwrapped id list, or ``None`` when the value is not a known
    wrapper shape: the caller then applies the legacy per-element handling,
    which covers proper arrays and keeps unknown-key wrappers rejected.
    """

    if depth > _INTERFACE_ID_UNWRAP_MAX_DEPTH:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, dict) and len(value) == 1:
        key, inner = next(iter(value.items()))
        if key not in _INTERFACE_ID_WRAPPER_KEYS:
            return None
        if isinstance(inner, list):
            return inner
        if isinstance(inner, (str, dict)):
            return _unwrap_interface_id_shapes(inner, depth=depth + 1)
    return None


def _valid_interface_ids(
    *,
    known_interface_ids: set[str],
    referenced_ids: set[str],
) -> list[str]:
    """Valid replacement ids for the rejection hint, most actionable first.

    The staged current-node ids lead (the offending rows should map to them),
    then traceability-DB ids excluding any the model already referenced
    correctly. Sorted for deterministic output. Empty when nothing valid is
    discoverable (missing runtime / empty DB and no staged ids), in which case
    the caller omits the hint instead of guessing.
    """

    import logging

    from core.service import get_runtime

    valid: set[str] = set(known_interface_ids)
    try:
        store = get_runtime().traceability
    except Exception:
        store = None
    if store is not None:
        try:
            db_ids = {
                interface_id
                for row in store.list_interfaces()
                if (interface_id := str(row.get("interface_id") or "").strip())
            }
            valid |= db_ids
        except Exception:
            # The runtime resolved but the DB would not answer (locked file,
            # concurrent writer): the hint degrades to staged ids only. That
            # degradation is exactly what an online post-mortem needs to see,
            # so log it instead of swallowing silently.
            logging.getLogger(__name__).debug(
                "manifest hint: traceability list_interfaces failed; "
                "falling back to staged interface ids only",
                exc_info=True,
            )
    candidates = sorted(valid - referenced_ids)
    staged = [interface_id for interface_id in sorted(known_interface_ids) if interface_id in set(candidates)]
    rest = [interface_id for interface_id in candidates if interface_id not in set(staged)]
    ordered = staged + rest
    if len(ordered) > _MANIFEST_HINT_MAX_IDS:
        return ordered[:_MANIFEST_HINT_MAX_IDS] + [f"... (+{len(ordered) - _MANIFEST_HINT_MAX_IDS} more; query with the traceability tools)"]
    return ordered


def reconcile_declared_manifest(
    *,
    manifest_items: list[dict[str, Any]],
    manifest_lock: TestManifestLock,
    written_paths: list[str],
    node_id: str = "",
) -> dict[str, Any]:
    """Reconcile the returned manifest against the declaration and the disk.

    Must be called on the payload of the FIRST (non-repair) generation pass:

    - an entry for a path that was never declared is a contract violation —
      the model returned tests it was forbidden to write (or forgot to declare
      them), so the run must fail loudly instead of registering phantom
      coverage;
    - a declared-and-written path with no manifest entry means the model wrote
      the file but dropped its row; the row is re-attached from the declaration
      (path + type + interfaces survive), with ``req_id`` and ``test_id``
      derived mechanically from ``node_id`` — the node prefix keeps the
      generated ``test_id`` globally unique and traceable, and losing a real
      test file's registration over a serialization slip is strictly worse;
    - entries for declared-but-unwritten paths are dropped with a diagnostic:
      the file does not exist, so registering it would poison the baseline RED
      gate with a phantom run.

    ``written_paths`` are the discipline's materialized paths (virtual
    ``/workspace/...`` form) from the same run. An unlocked manifest (no
    declaration happened — e.g. the node owns no local tests) is a
    passthrough: nothing was declared, so there is no declaration to enforce
    and the returned manifest is handed on untouched.
    """

    if not manifest_lock.locked:
        return {
            "tests": [item for item in manifest_items if isinstance(item, dict)],
            "undeclared_paths": [],
            "unwritten_paths": [],
            "reattached_paths": [],
        }

    written = {normalize_manifest_path(path) for path in written_paths if str(path or "").strip()}
    reconciled: list[dict[str, Any]] = []
    undeclared: list[str] = []
    unwritten: list[str] = []
    seen_paths: set[str] = set()
    for item in manifest_items:
        if not isinstance(item, dict):
            continue
        file_path = normalize_manifest_path(item.get("file_path"))
        if not file_path:
            continue
        if file_path in seen_paths:
            continue
        seen_paths.add(file_path)
        if not manifest_lock.contains(file_path):
            undeclared.append(file_path)
            continue
        if file_path not in written:
            unwritten.append(file_path)
            continue
        reconciled.append(item)

    reattached: list[str] = []
    declared_written = [path for path in manifest_lock.declared_files if path in written]
    for path in declared_written:
        if path in seen_paths:
            continue
        row = manifest_lock.declared_files[path]
        reconciled.append(
            {
                "test_id": _mechanical_test_id(node_hint=node_id, file_path=path),
                "req_id": str(node_id or "").strip(),
                "interface_ids": list(row.interface_ids),
                "coverage_scope": row.coverage_scope,
                "type": row.test_type,
                "file_path": path,
                "first_line": "",
                "manifest_reattached": True,
            }
        )
        reattached.append(path)

    return {
        "tests": reconciled,
        "undeclared_paths": sorted(set(undeclared)),
        "unwritten_paths": sorted(set(unwritten)),
        "reattached_paths": sorted(reattached),
    }


def _mechanical_test_id(node_hint: str, *, file_path: str) -> str:
    """Deterministic test id for a mechanically re-attached manifest row.

    Shape: ``<NODE>-T-<FILE-STEM>`` (``T-<STEM>`` without a node hint). The
    node prefix keeps ids from different nodes apart in the tests table even
    when two nodes each re-attach a same-named file, and satisfies the
    manifest contract that every ``test_id`` names its owning node.
    """

    stem = Path(file_path).stem
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").upper() or "TEST"
    prefix = re.sub(r"[^A-Za-z0-9]+", "-", str(node_hint or "").strip()).strip("-").upper()
    return f"{prefix}-T-{cleaned}" if prefix else f"T-{cleaned}"


def _tool_error(message: str) -> str:
    return json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2)


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result

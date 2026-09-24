"""Design-artifact registration for the traceability store.

Single owner of the registration invariants a DESIGN pass produces:
interface contracts (merge with existing rows, type validation, path
normalization, ownership attachment), test manifests (id dedup, path and
scope validation, normalization), the cross-requirement call edges derived
from callers/callees, and the ``implemented`` flag flip. ``WorkflowPhaseRunner``
calls this module's narrow interface; ``TraceabilityStore`` stays the
persistence implementation for the seven tables (runtime SDK contract,
unchanged by design).

The one ordering rule callers must preserve: successful DESIGN stores pass
through :meth:`DesignArtifactRegistry.register_design` *after* the phase
runner cleared the node's previous design artifacts — the clear is an
orchestration decision (which retries and conflict re-queues take), the
module never clears on its own.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from agents.tools.test_manifest import normalize_coverage_scope
from core.path_compat import normalize_workspace_relative_path

#: Valid interface contract types (UI/API/FUNC/DB).
ALLOWED_INTERFACE_TYPES = {"UI", "API", "FUNC", "DB"}


def infer_interface_type_from_id(interface_id: str) -> str:
    """Infer the contract type from the interface_id's type segment.

    Models systematically omit `type` when the id already encodes it
    (`REQ-2-UI-LoginPage`), so the segment is the last deterministic backfill
    source before a record is judged invalid. The match is segment-exact
    (`REQ-2-UI-APIKeys` must not read as API), case-insensitive.
    """

    segments = {segment.strip().upper() for segment in str(interface_id).split("-")}
    for candidate in ("UI", "API", "FUNC", "DB"):
        if candidate in segments:
            return candidate
    return ""


def resolve_interface_type(
    interface: dict[str, Any], stored_row: dict[str, Any] | None = None
) -> str:
    """Resolve an interface record's `type` through the backfill ladder.

    Ordered sources: the record's own field, the stored traceability row for
    a reused interface_id, then the interface_id's type segment. Returns ""
    when every source fails — the caller owns the judgment.
    """

    for source in (
        str(interface.get("type") or "").strip(),
        str((stored_row or {}).get("type") or "").strip(),
    ):
        candidate = source.upper()
        if candidate in ALLOWED_INTERFACE_TYPES:
            return candidate
    return infer_interface_type_from_id(interface.get("interface_id", ""))


def unresolvable_interface_types(
    interfaces: list[dict[str, Any]],
    *,
    get_stored_interface: Callable[[str], dict[str, Any] | None],
) -> list[str]:
    """Interface ids whose `type` no backfill source can resolve.

    The same ladder :func:`resolve_interface_type` enforces, as a pure
    pre-check: the DESIGN adapter uses it to spend its one repair ask only on
    records the registration layer would actually reject.
    """

    missing: list[str] = []
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        interface_id = str(interface.get("interface_id", "")).strip()
        if not interface_id:
            continue
        if resolve_interface_type(interface, stored_row=get_stored_interface(interface_id)):
            continue
        missing.append(interface_id)
    return missing


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def summarize_interface_artifacts(interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(interfaces),
        "items": [
            {
                "id": str(item.get("interface_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "responsibility": str(
                    item.get("responsibility", "") or item.get("name", "") or ""
                ).strip(),
            }
            for item in interfaces
            if isinstance(item, dict)
        ],
    }


def summarize_test_artifacts(tests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(tests),
        "items": [
            {
                "id": str(item.get("test_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "interfaces": normalize_string_list(item.get("interface_ids")),
            }
            for item in tests
            if isinstance(item, dict)
        ],
    }


def _strip_internal_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}


class DesignArtifactRegistry:
    """Register design facts (contracts, tests, edges, implemented flips)."""

    def __init__(self, traceability: Any, app_handler: Any, workspace_path: str) -> None:
        self.traceability = traceability
        self.app_handler = app_handler
        self.workspace_path = str(workspace_path)

    # -- interfaces ---------------------------------------------------------

    def prepare_interfaces(
        self,
        node_id: str,
        interfaces: list[dict[str, Any]],
        *,
        on_dropped_entry: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate and normalize interface contracts for one node.

        Reused contracts merge the stored row's content under the new pass
        (cross-node reuse keeps ``req_ids`` and ``implemented`` through the
        ``_existing_*`` markers, re-attached by :meth:`register_design`).
        Each record's ``type`` resolves through the backfill ladder (own
        field, stored row, interface_id segment) before it may fail. Raises
        ``ValueError`` with an agent-facing message on the first invalid
        contract; records without an ``interface_id`` are dropped and
        reported through ``on_dropped_entry`` so the caller can surface them
        (issue #230: the drop used to be silent).
        """

        prepared: list[dict[str, Any]] = []
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                if on_dropped_entry is not None:
                    on_dropped_entry(interface)
                continue
            existing = self.traceability.get_interface(interface_id)
            if existing:
                try:
                    existing_content = json.loads(str(existing.get("content") or "{}"))
                except json.JSONDecodeError:
                    existing_content = {}
                if isinstance(existing_content, dict):
                    interface = {**existing_content, **interface}
            interface_type = resolve_interface_type(interface, stored_row=existing)
            if not interface_type:
                raise ValueError(
                    f"Generated interface `{interface_id}` has invalid `type` {interface.get('type')!r}. "
                    "Interface type must be one of UI, API, FUNC, or DB; no stored contract "
                    "or interface_id type segment (-UI-/-API-/-FUNC-/-DB-) supplies one either."
                )
            normalized = {
                **interface,
                "interface_id": interface_id,
                "req_id": node_id,
                "type": interface_type,
                "file_path": (
                    normalize_workspace_relative_path(
                        interface.get("file_path"), self.workspace_path
                    )
                    or ((existing or {}).get("file_path") if existing else "")
                ),
                "first_line": str(
                    interface.get("first_line") or (existing or {}).get("first_line") or ""
                ).strip(),
                "callers": normalize_string_list(interface.get("callers"))
                or normalize_string_list((existing or {}).get("callers")),
                "callees": normalize_string_list(interface.get("callees"))
                or normalize_string_list((existing or {}).get("callees")),
                "_existing_req_ids": list(existing.get("req_ids", [])) if existing else [],
                "_existing_implemented": bool(existing.get("implemented")) if existing else False,
            }
            prepared.append(normalized)
        return prepared

    def register_design(
        self, node_id: str, interfaces: list[dict[str, Any]], tests: list[dict[str, Any]]
    ) -> None:
        """Persist prepared contracts and tests, then derive call edges.

        ``interfaces`` must come from :meth:`prepare_interfaces` (same pass);
        ``tests`` must come from :meth:`prepare_tests`. Both are stored in
        that order so ``register_design`` stays atomic per DESIGN store point
        for every path that reaches it (main pass, conflict re-queue, retry).
        """

        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue
            req_ids = normalize_string_list(interface.get("_existing_req_ids"))
            if node_id not in req_ids:
                req_ids.append(node_id)
            self.traceability.upsert_interface(
                interface_id=interface_id,
                req_ids=req_ids,
                type=str(interface.get("type", "") or "").strip().upper(),
                content=json.dumps(_strip_internal_fields(interface), ensure_ascii=False),
                file_path=str(interface.get("file_path", "") or "").strip() or None,
                first_line=str(interface.get("first_line", "") or "").strip() or None,
                implemented=bool(interface.get("_existing_implemented")),
                callers=normalize_string_list(interface.get("callers")),
                callees=normalize_string_list(interface.get("callees")),
            )
            self._register_interface_edges(node_id, interface_id, interface)
        for test in tests:
            self.traceability.upsert_test(
                test_id=str(test.get("test_id", "") or "").strip(),
                req_id=str(test.get("req_id", "") or "").strip(),
                interface_ids=normalize_string_list(test.get("interface_ids")),
                type=str(test.get("type", "") or "").strip(),
                file_path=str(test.get("file_path", "") or "").strip() or None,
                first_line=str(test.get("first_line", "") or "").strip() or None,
                passed=None,
            )

    def _register_interface_edges(
        self, node_id: str, interface_id: str, interface: dict[str, Any]
    ) -> None:
        for caller_id in normalize_string_list(interface.get("callers")):
            caller = self.traceability.get_interface(caller_id)
            if not caller:
                continue
            for source_req_id in caller.get("req_ids", []):
                if source_req_id and source_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=source_req_id,
                        target_req_id=node_id,
                        from_interface_id=caller_id,
                        to_interface_id=interface_id,
                        edge_type="cross_req",
                    )
        for callee_id in normalize_string_list(interface.get("callees")):
            callee = self.traceability.get_interface(callee_id)
            if not callee:
                continue
            for target_req_id in callee.get("req_ids", []):
                if target_req_id and target_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=node_id,
                        target_req_id=target_req_id,
                        from_interface_id=interface_id,
                        to_interface_id=callee_id,
                        edge_type="cross_req",
                    )

    # -- tests --------------------------------------------------------------

    def prepare_tests(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Validate and normalize a generated test manifest for one node.

        Drops non-dict rows and empty ids; raises ``ValueError`` with an
        agent-facing message on missing type/path, app-type-invalid paths,
        invalid coverage scope, or duplicate ids.
        """

        stored: list[dict[str, Any]] = []
        generated_ids: set[str] = set()
        for test in tests:
            if not isinstance(test, dict):
                continue
            raw_test_id = str(test.get("test_id", "")).strip()
            if not raw_test_id:
                continue
            file_path = normalize_workspace_relative_path(
                test.get("file_path"), self.workspace_path
            )
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `type`.")
            if not file_path:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `file_path`.")
            validation_error = self.app_handler.validate_test_path(test_type, file_path)
            if validation_error:
                raise ValueError(
                    f"Generated test `{raw_test_id}` has an invalid path. {validation_error}"
                )
            coverage_scope = normalize_coverage_scope(test.get("coverage_scope"))
            if not coverage_scope:
                raise ValueError(
                    f"Generated test `{raw_test_id}` has invalid `coverage_scope`; "
                    "expected owned, dependency, or shared."
                )
            if raw_test_id in generated_ids:
                raise ValueError(f"Generated duplicate test id `{raw_test_id}`.")
            generated_ids.add(raw_test_id)
            stored_item = {
                **test,
                "test_id": raw_test_id,
                "req_id": node_id,
                "type": test_type,
                "file_path": file_path,
                "coverage_scope": coverage_scope,
                "interface_ids": normalize_string_list(test.get("interface_ids")),
                "first_line": str(test.get("first_line", "")).strip(),
            }
            stored.append(stored_item)
        return stored

    # -- implemented flag ---------------------------------------------------

    def mark_interfaces_implemented(self, interfaces: list[dict[str, Any]]) -> None:
        """Flip ``implemented`` for contracts (non-leaf completion and
        IMPLEMENT success share this one implementation)."""

        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if interface_id:
                self.traceability.set_interface_implemented(interface_id, True)

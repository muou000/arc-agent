"""Unit tests for ``agents/tools/test_manifest.py``.

Covers the declare tool's validation (placement rules, type, duplicates,
unknown interface ids), the path normalization used by both the tool and the
discipline gate, and the first-pass reconciliation contract (undeclared
entries fail, dropped rows are re-attached, unwritten entries are removed).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents.runtime.capabilities import is_test_file_path, normalize_manifest_path
from agents.tools.test_manifest import (
    DeclaredTestFile,
    TestManifestLock,
    build_declare_test_manifest_tool,
    reconcile_declared_manifest,
)


def _declare(declaration: Any, *, declared: list[str] | None = None, validator: Any = None) -> str:
    lock = TestManifestLock(
        declared_files={
            path: DeclaredTestFile(file_path=path, test_type="Unit", interface_ids=[])
            for path in declared or []
        }
    )
    tool = build_declare_test_manifest_tool(
        node_id="REQ-X",
        manifest_lock=lock,
        validate_test_path=validator,
    )
    return str(asyncio.run(tool(files=declaration)))


def _parse(content: str) -> dict[str, Any]:
    return json.loads(content)


# ---------------------------------------------------------------------------
# path normalization
# ---------------------------------------------------------------------------


def test_normalize_manifest_path_accepts_virtual_relative_and_dot_forms() -> None:
    for value, expected in (
        ("/workspace/tests/unit/a.test.ts", "tests/unit/a.test.ts"),
        ("tests/unit/a.test.ts", "tests/unit/a.test.ts"),
        ("./tests/unit/a.test.ts", "tests/unit/a.test.ts"),
        ("/workspace//tests/a.test.ts", "tests/a.test.ts"),
        ("", ""),
        ("/workspace", ""),
    ):
        assert normalize_manifest_path(value) == expected, value


def test_is_test_file_path_matches_test_names_only() -> None:
    assert is_test_file_path("tests/unit/a.test.ts")
    assert is_test_file_path("backend/test-e2e/login.spec.js")
    assert is_test_file_path("/workspace/src/app.spec.tsx")
    assert is_test_file_path("tests/unit/test_calc.py")
    assert is_test_file_path("tests/unit/calc_test.py")
    # The `.test.`/`.spec.` marker rule and the Python conventions are a
    # union: a Python file carrying a JS-style test marker is still a test
    # file (the `.py` suffix must not short-circuit the marker check).
    assert is_test_file_path("tests/unit/b.test.py")
    # Web E2E accepts any JS/TS source name under test-e2e directories.
    assert is_test_file_path("backend/test-e2e/login.js")
    assert is_test_file_path("/workspace/backend/test-e2e/flows/auth.ts")
    assert not is_test_file_path("tests/setup-tests.ts")
    assert not is_test_file_path("backend/vitest.config.js")
    assert not is_test_file_path("src/helper.ts")
    assert not is_test_file_path("src/e2e-helper.ts")
    assert not is_test_file_path("")


# ---------------------------------------------------------------------------
# declare tool validation
# ---------------------------------------------------------------------------


def test_declaration_locks_the_manifest() -> None:
    content = _declare(
        [
            {"file_path": "tests/unit/a.test.ts", "type": "Unit", "interface_ids": []},
            {"file_path": "backend/test-e2e/login.spec.js", "type": "E2E", "interface_ids": ["IF-LOGIN"]},
        ]
    )
    payload = _parse(content)
    assert payload["status"] == "locked"
    assert [row["file_path"] for row in payload["manifest"]] == [
        "backend/test-e2e/login.spec.js",
        "tests/unit/a.test.ts",
    ]
    assert all(row["coverage_scope"] == "owned" for row in payload["manifest"])


def test_declaration_preserves_explicit_coverage_scope() -> None:
    payload = _parse(
        _declare(
            [
                {
                    "file_path": "tests/unit/dependency.test.ts",
                    "type": "Unit",
                    "coverage_scope": "dependency",
                },
                {
                    "file_path": "tests/unit/shared.test.ts",
                    "type": "Unit",
                    "coverage_scope": "shared",
                },
            ]
        )
    )
    assert payload["status"] == "locked"
    assert {row["coverage_scope"] for row in payload["manifest"]} == {"dependency", "shared"}


def test_declaration_rejects_unknown_coverage_scope() -> None:
    payload = _parse(
        _declare(
            [{"file_path": "tests/unit/a.test.ts", "type": "Unit", "coverage_scope": "fixture"}]
        )
    )
    assert payload["status"] == "error"
    assert "coverage_scope` must be one of" in payload["error"]


def test_declaration_rejects_bad_type_and_missing_path() -> None:
    payload = _parse(
        _declare(
            [
                {"file_path": "tests/unit/a.test.ts", "type": "unit-test"},
                {"type": "Unit"},
            ]
        )
    )
    assert payload["status"] == "error"
    assert "type` must be one of" in payload["error"]
    assert "missing `file_path`" in payload["error"]


def test_declaration_rejects_duplicate_paths() -> None:
    payload = _parse(
        _declare(
            [
                {"file_path": "tests/unit/a.test.ts", "type": "Unit", "interface_ids": []},
                {"file_path": "tests/unit/a.test.ts", "type": "Unit", "interface_ids": []},
            ]
        )
    )
    assert payload["status"] == "error"
    assert "duplicates the path" in payload["error"]


def test_declaration_rejects_non_test_files() -> None:
    payload = _parse(
        _declare([{"file_path": "src/helper.ts", "type": "Unit", "interface_ids": []}])
    )
    assert payload["status"] == "error"
    assert "does not look like a test file" in payload["error"]


def test_declaration_applies_placement_validator() -> None:
    def web_placement(test_type: str, file_path: str) -> str | None:
        # Stand-in for the web handler's rule: E2E under backend/test-e2e,
        # vitest layers under frontend|backend/tests.
        if test_type == "E2E" and not file_path.startswith("backend/test-e2e/"):
            return f"Web E2E tests must live under `backend/test-e2e/...`. Received: {file_path}"
        return None

    payload_ok = _parse(
        _declare(
            [{"file_path": "backend/test-e2e/login.spec.js", "type": "E2E", "interface_ids": []}],
            validator=web_placement,
        )
    )
    assert payload_ok["status"] == "locked"

    payload_bad = _parse(
        _declare(
            [{"file_path": "tests/unit/login.spec.js", "type": "E2E", "interface_ids": []}],
            validator=web_placement,
        )
    )
    assert payload_bad["status"] == "error"
    assert "must live under `backend/test-e2e/...`" in payload_bad["error"]


def test_declaration_fails_open_without_runtime_for_interface_ids() -> None:
    # No configured runtime in this process: the interface check must degrade
    # to open (no crash), leaving the workflow's validation authoritative.
    payload = _parse(
        _declare([{"file_path": "tests/unit/a.test.ts", "type": "Unit", "interface_ids": ["IF-UNKNOWN"]}])
    )
    assert payload["status"] == "locked"


def test_declaration_accepts_staged_current_interface_ids() -> None:
    lock = TestManifestLock()
    tool = build_declare_test_manifest_tool(
        node_id="REQ-X",
        manifest_lock=lock,
        current_interface_ids=["REQ-X-FUNC-CALC"],
        require_interface_coverage=True,
    )

    payload = _parse(
        str(
            asyncio.run(
                tool(
                    files=[
                        {
                            "file_path": "tests/unit/calc.test.ts",
                            "type": "Unit",
                            "interface_ids": ["REQ-X-FUNC-CALC"],
                        }
                    ]
                )
            )
        )
    )

    assert payload["status"] == "locked"


def test_declaration_rejects_empty_coverage_when_current_interfaces_exist() -> None:
    lock = TestManifestLock()
    tool = build_declare_test_manifest_tool(
        node_id="REQ-X",
        manifest_lock=lock,
        current_interface_ids=["REQ-X-FUNC-CALC"],
        require_interface_coverage=True,
    )

    payload = _parse(
        str(
            asyncio.run(
                tool(
                    files=[
                        {
                            "file_path": "tests/unit/calc.test.ts",
                            "type": "Unit",
                            "interface_ids": [],
                        }
                    ]
                )
            )
        )
    )

    assert payload["status"] == "error"
    assert "empty `interface_ids`" in payload["error"]
    assert "bypass coverage validation" in payload["error"]


def test_second_declaration_extends_the_lock_without_reset() -> None:
    lock = TestManifestLock()
    tool = build_declare_test_manifest_tool(node_id="REQ-X", manifest_lock=lock)
    first = _parse(str(asyncio.run(tool(files=[{"file_path": "tests/unit/a.test.ts", "type": "Unit", "interface_ids": []}]))))
    assert [row["file_path"] for row in first["manifest"]] == ["tests/unit/a.test.ts"]
    second = _parse(str(asyncio.run(tool(files=[{"file_path": "tests/unit/b.test.ts", "type": "Unit", "interface_ids": []}]))))
    assert [row["file_path"] for row in second["manifest"]] == ["tests/unit/a.test.ts", "tests/unit/b.test.ts"]


def test_tool_description_documents_redeclaration_merge() -> None:
    """Issue #183: the tool description itself said "Call this exactly once"
    while the mechanical contract (pinned above) supports re-declaration
    merge after a rejection. The docstring is the model-facing tool
    description, read at exactly the retry-decision point, so it must state
    the recovery path instead of an absolutist call count.
    """

    tool = build_declare_test_manifest_tool(node_id="REQ-X", manifest_lock=TestManifestLock())

    doc = " ".join((tool.__doc__ or "").split())
    assert "exactly once" not in doc
    assert "re-declare" in doc
    assert "the lock merges, adding only paths whose earlier declaration failed" in doc


def test_declaration_rejects_empty_list() -> None:
    payload = _parse(_declare([]))
    assert payload["status"] == "error"
    assert "non-empty list" in payload["error"]


def test_failed_redeclaration_keeps_the_existing_lock_intact() -> None:
    """A rejected re-declaration must not disturb already-locked paths.

    The state machine: a successful declaration locks its rows; a later
    declaration may only add new valid rows. A failing attempt leaves the
    lock exactly as it was (no partial state, no lost rows) — otherwise a
    model could erase its own locked set by submitting one bad row.
    """
    lock = TestManifestLock()
    tool = build_declare_test_manifest_tool(node_id="REQ-X", manifest_lock=lock)
    first = _parse(str(asyncio.run(tool(files=[{"file_path": "tests/unit/a.test.ts", "type": "Unit", "interface_ids": []}]))))
    assert first["status"] == "locked"

    rejected = _parse(
        str(
            asyncio.run(
                tool(
                    files=[
                        {"file_path": "tests/unit/b.test.ts", "type": "Unit", "interface_ids": []},
                        {"file_path": "src/not-a-test.ts", "type": "Unit", "interface_ids": []},
                    ]
                )
            )
        )
    )
    assert rejected["status"] == "error"
    # Neither the invalid row nor the valid row of the failed attempt landed;
    # the original lock is untouched.
    assert sorted(lock.declared_files) == ["tests/unit/a.test.ts"]

    retry = _parse(str(asyncio.run(tool(files=[{"file_path": "tests/unit/b.test.ts", "type": "Unit", "interface_ids": []}]))))
    assert retry["status"] == "locked"
    assert sorted(lock.declared_files) == ["tests/unit/a.test.ts", "tests/unit/b.test.ts"]


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_flags_undeclared_and_unwritten_entries() -> None:
    lock = TestManifestLock(
        declared_files={
            "tests/unit/a.test.ts": DeclaredTestFile(file_path="tests/unit/a.test.ts", test_type="Unit"),
            "tests/unit/b.test.ts": DeclaredTestFile(file_path="tests/unit/b.test.ts", test_type="Unit"),
        }
    )
    result = reconcile_declared_manifest(
        manifest_items=[
            {"test_id": "T-A", "file_path": "tests/unit/a.test.ts", "type": "Unit"},
            {"test_id": "T-PHANTOM", "file_path": "tests/unit/ghost.test.ts", "type": "Unit"},
            {"test_id": "T-NEVER-WRITTEN", "file_path": "tests/unit/b.test.ts", "type": "Unit"},
        ],
        manifest_lock=lock,
        written_paths=["/workspace/tests/unit/a.test.ts"],
    )
    assert result["undeclared_paths"] == ["tests/unit/ghost.test.ts"]
    assert result["unwritten_paths"] == ["tests/unit/b.test.ts"]
    assert [item["test_id"] for item in result["tests"]] == ["T-A"]


def test_reconcile_reattaches_written_files_dropped_from_the_answer() -> None:
    lock = TestManifestLock(
        declared_files={
            "tests/unit/a.test.ts": DeclaredTestFile(
                file_path="tests/unit/a.test.ts", test_type="Unit", interface_ids=["IF-A"]
            ),
        }
    )
    result = reconcile_declared_manifest(
        manifest_items=[],
        manifest_lock=lock,
        written_paths=["/workspace/tests/unit/a.test.ts"],
        node_id="REQ-7",
    )
    assert result["reattached_paths"] == ["tests/unit/a.test.ts"]
    assert len(result["tests"]) == 1
    reattached = result["tests"][0]
    assert reattached["file_path"] == "tests/unit/a.test.ts"
    assert reattached["type"] == "Unit"
    assert reattached["interface_ids"] == ["IF-A"]
    assert reattached["coverage_scope"] == "owned"
    assert reattached["manifest_reattached"] is True
    # The node prefix keeps ids globally unique and traceable per the
    # manifest contract; req_id names the owning node.
    assert reattached["test_id"] == "REQ-7-T-A-TEST"
    assert reattached["req_id"] == "REQ-7"


def test_reconcile_reattached_ids_do_not_collide_across_nodes() -> None:
    def reattach(node_id: str) -> str:
        lock = TestManifestLock(
            declared_files={
                "backend/tests/unit/auth.test.js": DeclaredTestFile(
                    file_path="backend/tests/unit/auth.test.js", test_type="Unit"
                ),
            }
        )
        result = reconcile_declared_manifest(
            manifest_items=[],
            manifest_lock=lock,
            written_paths=["/workspace/backend/tests/unit/auth.test.js"],
            node_id=node_id,
        )
        return result["tests"][0]["test_id"]

    assert reattach("REQ-1") != reattach("REQ-2")


def test_reconcile_backfills_dropped_identity_fields_from_the_declaration() -> None:
    """A returned row that dropped `type` or mangled `coverage_scope` is
    restored from its declaration instead of reaching the registration layer
    incomplete; the restoration is reported for the caller's log (issue #233
    audit backfill)."""

    lock = TestManifestLock(
        declared_files={
            "tests/unit/a.test.ts": DeclaredTestFile(
                file_path="tests/unit/a.test.ts", test_type="Unit", coverage_scope="owned"
            ),
        }
    )
    result = reconcile_declared_manifest(
        manifest_items=[
            # `type` dropped entirely; scope mangled beyond the vocabulary.
            {"test_id": "T-A", "file_path": "tests/unit/a.test.ts", "coverage_scope": "Ownned"},
        ],
        manifest_lock=lock,
        written_paths=["/workspace/tests/unit/a.test.ts"],
    )
    assert result["backfilled_fields"] == {"tests/unit/a.test.ts": ["type", "coverage_scope"]}
    row = result["tests"][0]
    assert row["type"] == "Unit"
    assert row["coverage_scope"] == "owned"


def test_reconcile_leaves_returned_identity_fields_untouched() -> None:
    """A row that carries its own valid values is not overwritten by the
    declaration — the response is the model's latest word for what it kept."""

    lock = TestManifestLock(
        declared_files={
            "tests/unit/a.test.ts": DeclaredTestFile(
                file_path="tests/unit/a.test.ts", test_type="Unit", coverage_scope="owned"
            ),
        }
    )
    result = reconcile_declared_manifest(
        manifest_items=[
            {"test_id": "T-A", "file_path": "tests/unit/a.test.ts", "type": "Integration", "coverage_scope": "shared"},
        ],
        manifest_lock=lock,
        written_paths=["/workspace/tests/unit/a.test.ts"],
    )
    assert result["backfilled_fields"] == {}
    assert result["tests"][0]["type"] == "Integration"
    assert result["tests"][0]["coverage_scope"] == "shared"


def test_reconcile_empty_lock_is_a_passthrough() -> None:
    # No declaration happened (e.g. node owns no tests): the returned manifest
    # is passed through untouched so an empty manifest stays empty.
    items = [{"test_id": "T-A", "file_path": "tests/unit/a.test.ts", "type": "Unit"}]
    result = reconcile_declared_manifest(
        manifest_items=items,
        manifest_lock=TestManifestLock(),
        written_paths=[],
    )
    assert result["tests"] == items
    assert result["undeclared_paths"] == []


# ---------------------------------------------------------------------------
# Unknown interface id rejection: the error must teach the valid ids
# ---------------------------------------------------------------------------


class _StubTraceability:
    """Minimal store stand-in: get_interface/list_interfaces over a dict."""

    def __init__(self, interfaces: dict[str, dict[str, Any]]) -> None:
        self._interfaces = interfaces

    def get_interface(self, interface_id: str) -> dict[str, Any] | None:
        return self._interfaces.get(interface_id)

    def list_interfaces(self) -> list[dict[str, Any]]:
        return [dict(row, interface_id=interface_id) for interface_id, row in self._interfaces.items()]


class _StubRuntime:
    def __init__(self, traceability: _StubTraceability) -> None:
        self.traceability = traceability


def _declare_with_store(
    declaration: Any,
    *,
    store_interfaces: dict[str, dict[str, Any]],
    current_interface_ids: list[str] | None = None,
    require_interface_coverage: bool = False,
) -> str:
    import agents.tools.test_manifest as test_manifest_module

    lock = TestManifestLock()
    tool = build_declare_test_manifest_tool(
        node_id="REQ-X",
        manifest_lock=lock,
        current_interface_ids=current_interface_ids,
        require_interface_coverage=require_interface_coverage,
    )
    original_get_runtime = test_manifest_module.__dict__.get("get_runtime")
    # Both the unknown-id check and the hint resolve the runtime lazily via
    # core.service.get_runtime; patch the module they import it from.
    import core.service as service_module

    stub = _StubRuntime(_StubTraceability(store_interfaces))
    monkey_runtime = service_module.get_runtime
    service_module.get_runtime = lambda: stub
    try:
        return str(asyncio.run(tool(files=declaration)))
    finally:
        service_module.get_runtime = monkey_runtime
        del original_get_runtime


def test_unknown_interface_error_lists_valid_ids_staged_first() -> None:
    """The rejection must name the currently valid interface ids (staged
    current-node ids first), so the model can re-map in one round instead of
    guessing — the online run showed 5+ redeclarations per node."""

    content = _declare_with_store(
        [
            {
                "file_path": "tests/unit/calc.test.ts",
                "type": "Unit",
                "interface_ids": ["REQ-X-FUNC-MISSPELLED"],
            }
        ],
        store_interfaces={
            "REQ-W-API-NOTES": {"req_ids": ["REQ-W"]},
            "REQ-V-DB-USERS": {"req_ids": ["REQ-V"]},
        },
        current_interface_ids=["REQ-X-FUNC-CALC"],
    )

    assert "Unknown interface id(s)" in content
    assert "REQ-X-FUNC-MISSPELLED" in content
    # Staged current-node ids lead the hint; DB ids follow.
    assert "REQ-X-FUNC-CALC" in content
    assert "REQ-W-API-NOTES" in content
    assert content.index("REQ-X-FUNC-CALC") < content.index("REQ-W-API-NOTES")


def test_unknown_interface_error_omits_hint_without_any_valid_ids() -> None:
    """No staged ids and an empty DB: the hint is omitted instead of
    misleading the model with an empty id list."""

    content = _declare_with_store(
        [
            {
                "file_path": "tests/unit/calc.test.ts",
                "type": "Unit",
                "interface_ids": ["IF-GHOST"],
            }
        ],
        store_interfaces={},
    )

    assert "Unknown interface id(s)" in content
    assert "valid id(s)" not in content


def test_unknown_interface_error_excludes_correctly_referenced_ids() -> None:
    """Ids the model already mapped correctly are not repeated in the hint."""

    content = _declare_with_store(
        [
            {
                "file_path": "tests/unit/a.test.ts",
                "type": "Unit",
                "interface_ids": ["REQ-W-API-NOTES", "IF-GHOST"],
            },
            {
                "file_path": "tests/unit/b.test.ts",
                "type": "Unit",
                "interface_ids": ["IF-GHOST"],
            },
        ],
        store_interfaces={"REQ-W-API-NOTES": {"req_ids": ["REQ-W"]}},
    )

    assert "Unknown interface id(s)" in content
    assert "IF-GHOST" in content
    # REQ-W-API-NOTES is referenced correctly and excluded from the hint.
    assert "valid id(s): REQ-W-API-NOTES" not in content


def test_unknown_interface_hint_caps_long_id_lists() -> None:
    """A large DB must not flood the error; the hint caps at 12 ids with a
    pointer to the traceability query tools."""

    store = {f"REQ-N{i:02d}-FUNC-X": {"req_ids": [f"REQ-N{i:02d}"]} for i in range(20)}
    content = _declare_with_store(
        [
            {
                "file_path": "tests/unit/calc.test.ts",
                "type": "Unit",
                "interface_ids": ["IF-GHOST"],
            }
        ],
        store_interfaces=store,
    )

    assert "valid id(s)" in content
    assert "+8 more" in content
    assert "traceability tools" in content


# ---------------------------------------------------------------------------
# Wrapper-shape unwrap: ToolStrategy structured output occasionally serializes
# the interface_ids array as a wrapper object or bare string instead of the
# flat array (easy-ticketbooking run 2026-09-21, REQ-2 DESIGN: 29 declarations
# across 8 shapes before the model locked). Known wrappers are unwrapped
# mechanically before validation; id validity and coverage semantics are
# unchanged, unknown-key wrappers are still rejected, and the rejection now
# shows the expected shape.
# ---------------------------------------------------------------------------

_STORE_IDS = {
    "REQ-2-FUNC-AuthLoginService": {"req_ids": ["REQ-2"]},
    "REQ-2-UI-LoginPage": {"req_ids": ["REQ-2"]},
    "REQ-2-API-AuthLogin": {"req_ids": ["REQ-2"]},
}


def _declare_ids_wrapped(raw_interface_ids: Any) -> str:
    """Declare one file whose `interface_ids` is the raw (wrapped) value."""

    return _declare_with_store(
        [
            {
                "file_path": "backend/tests/authLoginService.test.js",
                "type": "Unit",
                "coverage_scope": "owned",
                "interface_ids": raw_interface_ids,
            }
        ],
        store_interfaces=_STORE_IDS,
        current_interface_ids=["REQ-2-FUNC-AuthLoginService"],
    )


@pytest.mark.parametrize(
    ("raw_interface_ids", "expected_ids"),
    [
        pytest.param(
            {"item": "REQ-2-FUNC-AuthLoginService"},
            ["REQ-2-FUNC-AuthLoginService"],
            id="item-str",
        ),
        pytest.param(
            {"item": ["REQ-2-UI-LoginPage", "REQ-2-API-AuthLogin"]},
            ["REQ-2-UI-LoginPage", "REQ-2-API-AuthLogin"],
            id="item-list",
        ),
        pytest.param(
            "REQ-2-FUNC-AuthLoginService",
            ["REQ-2-FUNC-AuthLoginService"],
            id="bare-string",
        ),
        pytest.param(
            {"id": "REQ-2-FUNC-AuthLoginService"},
            ["REQ-2-FUNC-AuthLoginService"],
            id="id",
        ),
        pytest.param(
            {"interface_id": "REQ-2-FUNC-AuthLoginService"},
            ["REQ-2-FUNC-AuthLoginService"],
            id="interface-id",
        ),
        pytest.param(
            {"value": "REQ-2-FUNC-AuthLoginService"},
            ["REQ-2-FUNC-AuthLoginService"],
            id="value",
        ),
        pytest.param(
            {"entries": {"entry": "REQ-2-FUNC-AuthLoginService"}},
            ["REQ-2-FUNC-AuthLoginService"],
            id="entries-entry",
        ),
        pytest.param(
            {"item": {"interface_id": "REQ-2-FUNC-AuthLoginService"}},
            ["REQ-2-FUNC-AuthLoginService"],
            id="item-interface-id",
        ),
        pytest.param(
            {"item": {"value": "REQ-2-FUNC-AuthLoginService"}},
            ["REQ-2-FUNC-AuthLoginService"],
            id="item-value",
        ),
        pytest.param(
            {"item": {"id": "REQ-2-FUNC-AuthLoginService"}},
            ["REQ-2-FUNC-AuthLoginService"],
            id="item-id",
        ),
    ],
)
def test_declare_unwraps_observed_wrapper_shapes(
    raw_interface_ids: Any, expected_ids: list[str]
) -> None:
    """Every wrapper shape observed in the run evidence unwraps to the flat
    id array and locks in one declaration instead of triggering the
    29-declaration shape fight."""

    result = _parse(_declare_ids_wrapped(raw_interface_ids))
    assert result["status"] == "locked"
    assert result["manifest"][0]["interface_ids"] == expected_ids


def test_declare_still_rejects_unknown_key_wrapper() -> None:
    """A wrapper under an unrecognized key is not unwrapped; the rejection
    now also shows the expected shape so the model stops blind-trying."""

    content = _declare_ids_wrapped({"wrapper": "REQ-2-FUNC-AuthLoginService"})
    result = _parse(content)
    assert result["status"] == "error"
    assert "Unknown interface id(s)" in result["error"]
    assert '"interface_ids": ["IF-AUTH-SERVICE"]' in result["error"]


def test_declare_still_rejects_multi_key_wrapper() -> None:
    # A dict with several keys is ambiguous serialization, not a known
    # wrapper: rejected via the same unknown-id path as before.
    content = _declare_ids_wrapped(
        {"item": "REQ-2-FUNC-AuthLoginService", "id": "REQ-2-UI-LoginPage"}
    )
    assert "Unknown interface id(s)" in _parse(content)["error"]


def test_unwrap_does_not_bypass_id_validation() -> None:
    # Unwrapping recovers the shape, not the ids: an unknown id inside a
    # known wrapper is still rejected.
    content = _declare_ids_wrapped({"item": "IF-GHOST"})
    result = _parse(content)
    assert result["status"] == "error"
    assert "IF-GHOST" in result["error"]


def test_unwrap_does_not_bypass_coverage_requirement() -> None:
    # A known wrapper that unwraps to nothing still trips the coverage gate.
    content = _declare_with_store(
        [
            {
                "file_path": "backend/tests/authLoginService.test.js",
                "type": "Unit",
                "coverage_scope": "owned",
                "interface_ids": {"item": []},
            }
        ],
        store_interfaces=_STORE_IDS,
        current_interface_ids=["REQ-2-FUNC-AuthLoginService"],
        require_interface_coverage=True,
    )
    result = _parse(content)
    assert result["status"] == "error"
    assert "empty `interface_ids`" in result["error"]


def test_declare_preserves_legacy_non_wrapper_outcomes() -> None:
    """Non-wrapper values keep their exact legacy behavior: a proper array
    still declares, and the id-as-key dict (the shape the run finally locked
    with) still resolves through dict-key iteration."""

    content = _declare_ids_wrapped(["REQ-2-FUNC-AuthLoginService"])
    assert _parse(content)["status"] == "locked"

    content = _declare_ids_wrapped({"REQ-2-FUNC-AuthLoginService": "x"})
    assert _parse(content)["status"] == "locked"


def test_unwrap_recursion_is_depth_bounded() -> None:
    # Observed wrappers nest at most two single-key dicts deep; a deeper
    # nest is not a serialization accident and stays rejected.
    deep: Any = "REQ-2-FUNC-AuthLoginService"
    for _ in range(8):
        deep = {"item": deep}
    content = _declare_ids_wrapped(deep)
    assert "Unknown interface id(s)" in _parse(content)["error"]


def test_shape_example_names_every_wrapper_key() -> None:
    """The rejection's shape example and the unwrap key set must not drift
    apart: every key the validator unwraps is named in the message, so the
    model is never told one thing and forgiven another."""

    import agents.tools.test_manifest as test_manifest_module

    for key in test_manifest_module._INTERFACE_ID_WRAPPER_KEYS:
        assert key in test_manifest_module._MANIFEST_SHAPE_EXAMPLE, key


def test_unwrapped_empty_wrapper_matches_empty_array_semantics() -> None:
    # With the coverage gate off, a wrapper that unwraps to nothing declares
    # exactly like the flat empty array always has (gate-on path is pinned
    # by test_unwrap_does_not_bypass_coverage_requirement).
    content = _declare_ids_wrapped({"item": []})
    result = _parse(content)
    assert result["status"] == "locked"
    assert result["manifest"][0]["interface_ids"] == []

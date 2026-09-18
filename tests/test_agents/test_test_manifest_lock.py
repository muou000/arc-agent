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

from agents.tools.test_manifest import (
    DeclaredTestFile,
    TestManifestLock,
    build_declare_test_manifest_tool,
    is_test_file_path,
    normalize_manifest_path,
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


def test_second_declaration_extends_the_lock_without_reset() -> None:
    lock = TestManifestLock()
    tool = build_declare_test_manifest_tool(node_id="REQ-X", manifest_lock=lock)
    first = _parse(str(asyncio.run(tool(files=[{"file_path": "tests/unit/a.test.ts", "type": "Unit", "interface_ids": []}]))))
    assert [row["file_path"] for row in first["manifest"]] == ["tests/unit/a.test.ts"]
    second = _parse(str(asyncio.run(tool(files=[{"file_path": "tests/unit/b.test.ts", "type": "Unit", "interface_ids": []}]))))
    assert [row["file_path"] for row in second["manifest"]] == ["tests/unit/a.test.ts", "tests/unit/b.test.ts"]


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

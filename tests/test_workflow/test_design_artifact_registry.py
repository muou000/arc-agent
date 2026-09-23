"""``core.design_artifacts`` — design-artifact registration module.

Unit-level teeth for the invariants the module owns: interface contract
merge/validation/normalization, cross-requirement call-edge derivation, test
manifest validation/dedup, and the ``implemented`` flip shared by non-leaf
completion and IMPLEMENT success. Phase-level behavior (DESIGN store points,
retry, conflict re-queue) stays covered by the ``test_workflow`` and
``test_agents`` suites that drive ``run_design_phase`` /
``run_implement_phase``.
"""

from __future__ import annotations

import json

import pytest

from core.design_artifacts import DesignArtifactRegistry
from tests.helpers.faux import FakeAppHandler

# Reuse the process-wide runtime fixture so the real traceability store
# resolves inside tmp_project_dir.
from tests.test_agents.conftest import arc_runtime  # noqa: F401


def _make_registry(tmp_project_dir, runtime) -> DesignArtifactRegistry:
    return DesignArtifactRegistry(
        traceability=runtime.traceability,
        app_handler=FakeAppHandler(),
        workspace_path=str(tmp_project_dir),
    )


def _iface(interface_id: str, **overrides) -> dict:
    row = {
        "interface_id": interface_id,
        "type": "FUNC",
        "name": interface_id.lower(),
        "responsibility": "Does something",
        "file_path": "src/mod.py",
        "first_line": "def handler():",
        "callers": [],
        "callees": [],
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# prepare_interfaces
# ---------------------------------------------------------------------------


def test_prepare_interfaces_attaches_ownership_and_normalizes(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    prepared = registry.prepare_interfaces("REQ-1", [_iface("IF-A", file_path="src/a.py")])

    assert len(prepared) == 1
    row = prepared[0]
    assert row["req_id"] == "REQ-1"
    assert row["type"] == "FUNC"
    assert row["file_path"] == "src/a.py"
    # internal merge markers are present pre-registration, stripped at store time
    assert row["_existing_req_ids"] == []
    assert row["_existing_implemented"] is False


def test_prepare_interfaces_merges_existing_content_under_new_pass(tmp_project_dir, arc_runtime) -> None:
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces("REQ-1", [_iface("IF-SHARED", responsibility="Original")]),
        [],
    )
    # REQ-2 reuses the same contract with an updated responsibility.
    prepared = registry.prepare_interfaces(
        "REQ-2", [_iface("IF-SHARED", responsibility="Updated", file_path="src/other.py")]
    )

    assert len(prepared) == 1
    row = prepared[0]
    assert row["responsibility"] == "Updated"
    # cross-node reuse preserves ownership and implemented from the stored row
    assert row["_existing_req_ids"] == ["REQ-1"]
    assert row["_existing_implemented"] is False
    # fields the new pass left empty fall back to the stored row
    assert row["file_path"] == "src/other.py"


def test_prepare_interfaces_rejects_invalid_type(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    with pytest.raises(ValueError, match="invalid `type`"):
        registry.prepare_interfaces("REQ-1", [_iface("IF-BAD", type="SCHEDULE")])


def test_prepare_interfaces_drops_rows_without_id(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    prepared = registry.prepare_interfaces(
        "REQ-1", [{"type": "FUNC", "file_path": "src/a.py"}, _iface("IF-A")]
    )
    assert [row["interface_id"] for row in prepared] == ["IF-A"]


# ---------------------------------------------------------------------------
# register_design: ownership, implemented preservation, call edges
# ---------------------------------------------------------------------------


def test_register_design_appends_node_to_req_ids_and_preserves_implemented(
    tmp_project_dir, arc_runtime
) -> None:
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    first = registry.prepare_interfaces("REQ-1", [_iface("IF-SHARED")])
    registry.register_design("REQ-1", first, [])
    registry.mark_interfaces_implemented(first)

    second = registry.prepare_interfaces("REQ-2", [_iface("IF-SHARED")])
    registry.register_design("REQ-2", second, [])

    stored = runtime.traceability.get_interface("IF-SHARED")
    assert stored["req_ids"] == ["REQ-1", "REQ-2"]
    # implemented survives the cross-node reuse registration
    assert stored["implemented"] is True
    # content JSON carries no internal merge markers
    assert not [key for key in json.loads(stored["content"]) if key.startswith("_")]


def test_register_design_derivates_cross_req_call_edges(tmp_project_dir, arc_runtime) -> None:
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    # REQ-1 owns IF-CORE; REQ-2 owns IF-APP which calls it.
    registry.register_design("REQ-1", registry.prepare_interfaces("REQ-1", [_iface("IF-CORE")]), [])
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("IF-APP", callees=["IF-CORE"])]),
        [],
    )

    edges = runtime.traceability.list_call_edges()
    assert [
        (edge["source_req_id"], edge["target_req_id"], edge["edge_type"]) for edge in edges
    ] == [("REQ-2", "REQ-1", "cross_req")]
    assert edges[0]["from_interface_id"] == "IF-APP"
    assert edges[0]["to_interface_id"] == "IF-CORE"


def test_register_design_ignores_unknown_and_same_node_edges(tmp_project_dir, arc_runtime) -> None:
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    # IF-GHOST was never registered; IF-SAME belongs to the same node.
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces(
            "REQ-1",
            [
                _iface("IF-A", callees=["IF-GHOST"]),
                _iface("IF-SAME", callers=["IF-A"]),
            ],
        ),
        [],
    )

    assert runtime.traceability.list_call_edges() == []


def test_register_design_stores_tests(tmp_project_dir, arc_runtime) -> None:
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    tests = registry.prepare_tests(
        node_id="REQ-1",
        tests=[
            {
                "test_id": "T-1",
                "type": "Unit",
                "file_path": "tests/unit/test_a.py",
                "coverage_scope": "owned",
                "interface_ids": ["IF-A"],
                "first_line": "def test_a():",
            }
        ],
    )
    registry.register_design("REQ-1", registry.prepare_interfaces("REQ-1", [_iface("IF-A")]), tests)

    stored = runtime.traceability.list_tests(req_id="REQ-1")
    assert len(stored) == 1
    assert stored[0]["test_id"] == "T-1"
    assert stored[0]["file_path"] == "tests/unit/test_a.py"
    assert stored[0]["passed"] is None


# ---------------------------------------------------------------------------
# prepare_tests
# ---------------------------------------------------------------------------


def _test_row(test_id: str, **overrides) -> dict:
    row = {
        "test_id": test_id,
        "type": "Unit",
        "file_path": f"tests/unit/{test_id.lower()}.py",
        "coverage_scope": "owned",
        "interface_ids": [],
        "first_line": "def test_x():",
    }
    row.update(overrides)
    return row


def test_prepare_tests_normalizes_and_attaches_req(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    stored = registry.prepare_tests(node_id="REQ-1", tests=[_test_row("T-1")])
    assert stored[0]["req_id"] == "REQ-1"
    assert stored[0]["type"] == "Unit"
    assert stored[0]["coverage_scope"] == "owned"


def test_prepare_tests_drops_invalid_rows(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    stored = registry.prepare_tests(
        node_id="REQ-1",
        tests=[{"file_path": "x.py"}, "not-a-dict", _test_row("T-1"), _test_row("")],
    )
    assert [row["test_id"] for row in stored] == ["T-1"]


@pytest.mark.parametrize(
    "row,match",
    [
        (_test_row("T-1", type=""), "missing `type`"),
        (_test_row("T-1", file_path=""), "missing `file_path`"),
        (_test_row("T-1", coverage_scope="bogus"), "invalid `coverage_scope`"),
    ],
)
def test_prepare_tests_raises_agent_facing_errors(tmp_project_dir, arc_runtime, row, match) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    with pytest.raises(ValueError, match=match):
        registry.prepare_tests(node_id="REQ-1", tests=[row])


def test_prepare_tests_rejects_duplicate_ids(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    with pytest.raises(ValueError, match="duplicate test id"):
        registry.prepare_tests(node_id="REQ-1", tests=[_test_row("T-DUP"), _test_row("T-DUP")])


def test_prepare_tests_routes_through_app_handler_validation(tmp_project_dir, arc_runtime) -> None:
    class RejectingHandler(FakeAppHandler):
        def validate_test_path(self, test_type: str, file_path: str) -> str:
            return "Unit tests must live under tests/unit."

    registry = DesignArtifactRegistry(
        traceability=arc_runtime.traceability,
        app_handler=RejectingHandler(),
        workspace_path=str(tmp_project_dir),
    )
    with pytest.raises(ValueError, match="invalid path. Unit tests must live under tests/unit."):
        registry.prepare_tests(node_id="REQ-1", tests=[_test_row("T-1")])


# ---------------------------------------------------------------------------
# mark_interfaces_implemented
# ---------------------------------------------------------------------------


def test_mark_interfaces_implemented_flips_stored_rows(tmp_project_dir, arc_runtime) -> None:
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    prepared = registry.prepare_interfaces("REQ-1", [_iface("IF-A"), _iface("IF-B")])
    registry.register_design("REQ-1", prepared, [])

    # The stored-row shape the IMPLEMENT/non-leaf paths pass in.
    interfaces = runtime.traceability.list_interfaces(req_id="REQ-1")
    registry.mark_interfaces_implemented(interfaces)

    for interface_id in ("IF-A", "IF-B"):
        assert runtime.traceability.get_interface(interface_id)["implemented"] is True

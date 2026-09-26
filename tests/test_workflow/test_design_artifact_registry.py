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

from core.design_artifacts import DesignArtifactRegistry, unresolvable_interface_types
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


def test_reused_api_card_keeps_unchanged_routes_when_post_is_added(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    old = _iface(
        "REQ-1-API-Workbooks", type="API", file_path="backend/src/routes/workbooks.js",
        specification=(
            "GET /api/workbooks/:id returns 200 {workbook}, 404 {missing}. "
            "PATCH /api/workbooks/:id returns 200 {workbook}, 400 {bad}, "
            "404 {missing}, 409 {conflict}."
        ),
    )
    registry.register_design("REQ-1", registry.prepare_interfaces("REQ-1", [old]), [])
    reused = registry.prepare_interfaces("REQ-2", [{
        "interface_id": old["interface_id"],
        "type": "API", "file_path": old["file_path"],
        "specification": "POST /api/workbooks returns 201 {created}, 400 {invalid}, 500 {failure}.",
    }])[0]
    assert "POST /api/workbooks returns 201" in reused["specification"]
    assert "GET /api/workbooks/:id returns 200 {workbook}, 404 {missing}" in reused["specification"]
    assert "PATCH /api/workbooks/:id returns 200 {workbook}, 400 {bad}, 404 {missing}, 409 {conflict}" in reused["specification"]


def test_prepare_interfaces_rejects_invalid_type(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    with pytest.raises(ValueError, match="invalid `type`"):
        registry.prepare_interfaces("REQ-1", [_iface("IF-BAD", type="SCHEDULE")])


# ---------------------------------------------------------------------------
# prepare_interfaces: `type` backfill ladder (issue #230, serial-5 incident)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interface_id,expected",
    [
        ("REQ-2-UI-LoginPage", "UI"),
        ("REQ-2-API-AuthApi", "API"),
        ("REQ-2-FUNC-AuthService", "FUNC"),
        ("REQ-2-DB-UsersTable", "DB"),
        ("req-2-ui-navbar", "UI"),
    ],
)
def test_prepare_interfaces_backfills_type_from_id_prefix(
    tmp_project_dir, arc_runtime, interface_id, expected
) -> None:
    """A typeless entry whose interface_id carries the type segment resolves
    from it — the serial-5 incident's dominant recoverable shape."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    prepared = registry.prepare_interfaces(
        "REQ-2", [{"interface_id": interface_id, "file_path": "src/x.py"}]
    )
    assert prepared[0]["type"] == expected


def test_prepare_interfaces_backfills_type_from_stored_row(tmp_project_dir, arc_runtime) -> None:
    """A reused id without a type segment still resolves from its stored row."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    registry.register_design("REQ-1", registry.prepare_interfaces("REQ-1", [_iface("IF-SHARED")]), [])

    prepared = registry.prepare_interfaces(
        "REQ-2", [{"interface_id": "IF-SHARED", "responsibility": "Updated"}]
    )
    assert prepared[0]["type"] == "FUNC"


def test_prepare_interfaces_type_ladder_prefers_own_field_over_stored(
    tmp_project_dir, arc_runtime
) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    registry.register_design(
        "REQ-1", registry.prepare_interfaces("REQ-1", [_iface("REQ-1-UI-Shell", type="FUNC")]), []
    )

    prepared = registry.prepare_interfaces("REQ-2", [_iface("REQ-1-UI-Shell", type="API")])
    assert prepared[0]["type"] == "API"


def test_prepare_interfaces_type_ladder_prefers_stored_row_over_id_prefix(
    tmp_project_dir, arc_runtime
) -> None:
    """A reused id whose segment disagrees with its stored type resolves from
    the stored row: the registry's record outranks the id's self-description.
    The row is seeded with type only in its column (empty content JSON) so the
    merge cannot hand the value to the own-field source."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    arc_runtime.traceability.upsert_interface(
        interface_id="REQ-1-UI-Shell",
        req_ids=["REQ-1"],
        type="FUNC",
        content="{}",
        file_path="frontend/src/App.tsx",
    )

    prepared = registry.prepare_interfaces(
        "REQ-2", [{"interface_id": "REQ-1-UI-Shell", "responsibility": "Updated"}]
    )
    assert prepared[0]["type"] == "FUNC"


def test_prepare_interfaces_invalid_type_falls_back_before_raising(
    tmp_project_dir, arc_runtime
) -> None:
    """A non-empty but invalid own type goes through the ladder instead of
    dying on it: stored row first, then the id prefix."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    registry.register_design("REQ-1", registry.prepare_interfaces("REQ-1", [_iface("IF-SHARED")]), [])

    from_stored = registry.prepare_interfaces("REQ-2", [_iface("IF-SHARED", type="SCHEDULE")])
    assert from_stored[0]["type"] == "FUNC"

    from_prefix = registry.prepare_interfaces("REQ-2", [_iface("REQ-2-UI-Nav", type="SCHEDULE")])
    assert from_prefix[0]["type"] == "UI"


def test_prepare_interfaces_raises_only_after_ladder_exhausted(tmp_project_dir, arc_runtime) -> None:
    registry = _make_registry(tmp_project_dir, arc_runtime)
    with pytest.raises(ValueError, match="no stored contract or interface_id type segment"):
        registry.prepare_interfaces("REQ-2", [_iface("IF-ORPHAN", type="")])


def test_prepare_interfaces_reports_dropped_entries_to_callback(
    tmp_project_dir, arc_runtime
) -> None:
    """Entries without an interface_id are dropped, but no longer silently:
    the caller receives them for an observable warning (issue #230)."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    dropped: list[dict] = []
    prepared = registry.prepare_interfaces(
        "REQ-1",
        [{"type": "FUNC", "file_path": "src/ghost.py", "name": "Ghost"}, _iface("IF-A")],
        on_dropped_entry=dropped.append,
    )

    assert [row["interface_id"] for row in prepared] == ["IF-A"]
    assert len(dropped) == 1
    assert dropped[0]["name"] == "Ghost"


def test_unresolvable_interface_types_lists_only_unbackfillable_ids(
    tmp_project_dir, arc_runtime
) -> None:
    """The DESIGN adapter's pre-check: only entries that every backfill source
    fails qualify for the one-shot type repair nudge."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    registry.register_design("REQ-1", registry.prepare_interfaces("REQ-1", [_iface("IF-SHARED")]), [])

    missing = unresolvable_interface_types(
        [
            {"interface_id": "REQ-2-UI-Nav", "file_path": "src/nav.py"},
            {"interface_id": "IF-SHARED", "responsibility": "Reused"},
            "not-a-dict",
            {"file_path": "src/no-id.py"},
            {"interface_id": "IF-GHOST", "file_path": "src/orphan.py"},
        ],
        get_stored_interface=arc_runtime.traceability.get_interface,
    )
    assert missing == ["IF-GHOST"]


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


def test_register_design_resolves_same_pass_callee_reference(tmp_project_dir, arc_runtime) -> None:
    """Every row is upserted before edge derivation: a callee pointing at a
    sibling contract declared later in the same pass resolves instead of
    silently missing the edge (issue #233)."""

    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces(
            "REQ-1",
            [
                _iface("REQ-1-FUNC-First", callees=["REQ-1-FUNC-Second"]),
                _iface("REQ-1-FUNC-Second"),
            ],
        ),
        [],
    )

    # Same-node edges are never recorded (cross_req only), so register the
    # callee from a second node to observe the edge the forward reference
    # now creates.
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("REQ-2-FUNC-User", callees=["REQ-1-FUNC-Second"])]),
        [],
    )
    edges = runtime.traceability.list_call_edges()
    assert [(edge["source_req_id"], edge["target_req_id"]) for edge in edges] == [("REQ-2", "REQ-1")]


def test_register_design_resolves_same_pass_cross_node_reference(tmp_project_dir, arc_runtime) -> None:
    """The real forward-reference fix: within one registration the callee's
    row exists by the time the caller's edges are derived, so the reciprocal
    listing is no longer required for the edge to exist."""

    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces(
            "REQ-1",
            [
                _iface("REQ-1-FUNC-First", callees=["REQ-1-FUNC-Second"]),
                _iface("REQ-1-FUNC-Second", file_path="src/second.py"),
            ],
        ),
        [],
    )
    # REQ-2 reuses REQ-1-FUNC-Second and calls the FIRST interface — a
    # reference that, pre-#233, resolved only through the callee's own
    # caller listing.
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("REQ-2-FUNC-User", callees=["REQ-1-FUNC-First"])]),
        [],
    )
    edges = runtime.traceability.list_call_edges()
    assert [(edge["source_req_id"], edge["target_req_id"], edge["to_interface_id"]) for edge in edges] == [
        ("REQ-2", "REQ-1", "REQ-1-FUNC-First")
    ]


def test_register_design_reports_unresolved_edge_references(tmp_project_dir, arc_runtime) -> None:
    """A caller/callee id that resolves to no stored contract used to be a
    silent no-edge; it is now reported to the caller (issue #233)."""

    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    unresolved: list[tuple[str, str, list[str]]] = []
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces(
            "REQ-1",
            [_iface("IF-A", callers=["IF-GHOST-IN"], callees=["IF-GHOST-OUT", "IF-GHOST-OUT"])],
        ),
        [],
        on_unresolved_edge_reference=lambda interface_id, kind, ids: unresolved.append((interface_id, kind, ids)),
    )

    assert runtime.traceability.list_call_edges() == []
    assert sorted(unresolved) == [
        ("IF-A", "callees", ["IF-GHOST-OUT"]),
        ("IF-A", "callers", ["IF-GHOST-IN"]),
    ]


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


def test_prepare_tests_drops_unregistrable_rows_observably(tmp_project_dir, arc_runtime) -> None:
    """Rows that cannot be registered at all — non-objects, rows without a
    file path — are dropped, but no longer silently: the caller receives each
    with a reason (issue #233 audit)."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    dropped: list[tuple[object, str]] = []
    stored = registry.prepare_tests(
        node_id="REQ-1",
        tests=[{"test_id": "T-NOPATH", "type": "Unit"}, "not-a-dict", _test_row("T-1")],
        on_dropped_entry=lambda item, reason: dropped.append((item, reason)),
    )
    assert [row["test_id"] for row in stored] == ["T-1"]
    assert sorted(reason for _item, reason in dropped) == ["missing file_path", "not an object"]
    assert dropped[0][0] == {"test_id": "T-NOPATH", "type": "Unit"}


def test_prepare_tests_backfills_missing_test_id_mechanically(tmp_project_dir, arc_runtime) -> None:
    """A row without a ``test_id`` but with a usable file path takes the
    mechanical node-prefixed id — the same generator the reconciliation's
    re-attach uses — instead of vanishing (issue #233 audit: backfill)."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    backfilled: list[tuple[str, str]] = []
    stored = registry.prepare_tests(
        node_id="REQ-1",
        tests=[{"type": "Unit", "file_path": "tests/unit/login.test.ts"}],
        on_backfilled_row=lambda test_id, field: backfilled.append((test_id, field)),
    )
    assert stored[0]["test_id"] == "REQ-1-T-LOGIN-TEST"
    assert sorted(backfilled) == [
        ("REQ-1-T-LOGIN-TEST", "coverage_scope"),
        ("REQ-1-T-LOGIN-TEST", "test_id"),
    ]
    assert stored[0]["req_id"] == "REQ-1"


def test_prepare_tests_defaults_missing_coverage_scope_to_owned(tmp_project_dir, arc_runtime) -> None:
    """An absent ``coverage_scope`` backfills to ``owned`` — the default the
    decode (TestManifestItem) and declaration (normalize_coverage_scope)
    layers already establish. owned is the conservative choice: a
    misclassified test stays subject to the baseline RED gate and the
    foreign-owned check, both of which fail loudly; defaulting to
    dependency/shared would silently exempt it (issue #233 semantic
    decision)."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    backfilled: list[tuple[str, str]] = []
    stored = registry.prepare_tests(
        node_id="REQ-1",
        tests=[_test_row("T-1", coverage_scope="")],
        on_backfilled_row=lambda test_id, field: backfilled.append((test_id, field)),
    )
    assert stored[0]["coverage_scope"] == "owned"
    assert backfilled == [("T-1", "coverage_scope")]


def test_prepare_tests_backfilled_ids_participate_in_duplicate_check(
    tmp_project_dir, arc_runtime
) -> None:
    """A backfilled mechanical id colliding with a model-minted id is judged
    by the same duplicate rule — loud, not silent overwrite."""

    registry = _make_registry(tmp_project_dir, arc_runtime)
    with pytest.raises(ValueError, match="duplicate test id"):
        registry.prepare_tests(
            node_id="REQ-1",
            tests=[
                _test_row("REQ-1-T-LOGIN-TEST"),
                {"type": "Unit", "file_path": "tests/unit/login.test.ts"},
            ],
        )


@pytest.mark.parametrize(
    "row,match",
    [
        (_test_row("T-1", type=""), "missing `type`"),
        (_test_row("T-1", coverage_scope="bogus"), "invalid `coverage_scope`"),
    ],
)
def test_prepare_tests_raises_agent_facing_errors(tmp_project_dir, arc_runtime, row, match) -> None:
    """The keep-failing branches of the #233 audit ladder: no backfill source
    exists for an empty `type` or a non-empty invalid `coverage_scope`, and
    the declaration loop already gave the model validated chances at both."""

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


# ---------------------------------------------------------------------------
# reconcile_call_edges: compile-wrap-up dangling-reference sweep (issue #238)
# ---------------------------------------------------------------------------


def _edge_quads(traceability) -> set[tuple[str, str, str, str]]:
    return {
        (
            edge["source_req_id"],
            edge["target_req_id"],
            edge["from_interface_id"],
            edge["to_interface_id"],
        )
        for edge in traceability.list_call_edges()
    }


def test_reconcile_backfills_forward_callee_reference(tmp_project_dir, arc_runtime) -> None:
    """The ticket's core shape: REQ-1 declares a callee whose contract is
    designed later; the later side never declares the reverse, so registration
    leaves the cross_req edge permanently missing and the wrap-up sweep
    backfills it through the same edge rule."""
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces("REQ-1", [_iface("IF-A", callees=["IF-B"], file_path="src/a.py")]),
        [],
    )
    assert runtime.traceability.list_call_edges() == []
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("IF-B", file_path="src/b.py")]),
        [],
    )
    # The later registration only sweeps its own callers/callees: still no edge.
    assert runtime.traceability.list_call_edges() == []

    report = registry.reconcile_call_edges()

    assert _edge_quads(runtime.traceability) == {("REQ-1", "REQ-2", "IF-A", "IF-B")}
    stored = runtime.traceability.list_call_edges()[0]
    assert stored["edge_type"] == "cross_req"
    assert report["unresolved"] == []
    assert report["backfilled"] == [
        {
            "interface_id": "IF-A",
            "kind": "callees",
            "ref_id": "IF-B",
            "edges": [{"source_req_id": "REQ-1", "target_req_id": "REQ-2"}],
        }
    ]


def test_reconcile_backfills_forward_caller_reference(tmp_project_dir, arc_runtime) -> None:
    """The mirrored declaration side: the early interface lists its caller in
    `callers` before the caller's contract exists."""
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces("REQ-1", [_iface("IF-A", callers=["IF-B"], file_path="src/a.py")]),
        [],
    )
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("IF-B", file_path="src/b.py")]),
        [],
    )

    report = registry.reconcile_call_edges()

    # The caller's requirement depends on the declared interface's requirement.
    assert _edge_quads(runtime.traceability) == {("REQ-2", "REQ-1", "IF-B", "IF-A")}
    assert report["unresolved"] == []


def test_reconcile_backfills_every_reusing_requirement_of_a_late_contract(
    tmp_project_dir, arc_runtime
) -> None:
    """Req-id pairs come from each side's req_ids: an interface reused by two
    requirements contributes one edge per reuser once the referenced contract
    finally registers (registration never created either edge here)."""
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces("REQ-1", [_iface("IF-A", callees=["IF-B"], file_path="src/a.py")]),
        [],
    )
    registry.register_design(
        "REQ-3",
        registry.prepare_interfaces("REQ-3", [_iface("IF-A", file_path="src/a.py")]),
        [],
    )
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("IF-B", file_path="src/b.py")]),
        [],
    )
    assert runtime.traceability.list_call_edges() == []

    registry.reconcile_call_edges()

    assert _edge_quads(runtime.traceability) == {
        ("REQ-1", "REQ-2", "IF-A", "IF-B"),
        ("REQ-3", "REQ-2", "IF-A", "IF-B"),
    }


def test_reconcile_reports_still_unresolved_references(tmp_project_dir, arc_runtime) -> None:
    """References that resolve to no stored contract at compile end come back
    for the caller's final warning; no edge is fabricated for them."""
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces(
            "REQ-1", [_iface("IF-A", callers=["IF-GHOST"], file_path="src/a.py")]
        ),
        [],
    )

    report = registry.reconcile_call_edges()

    assert report["unresolved"] == [
        {"interface_id": "IF-A", "kind": "callers", "ref_id": "IF-GHOST"}
    ]
    assert report["backfilled"] == []
    assert runtime.traceability.list_call_edges() == []


def test_reconcile_skips_edges_the_registration_already_created(
    tmp_project_dir, arc_runtime
) -> None:
    """Both endpoints registered and the edge present: nothing is re-inserted
    (the stored row, `created_at` included, stays byte-identical)."""
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces("REQ-1", [_iface("IF-A", file_path="src/a.py")]),
        [],
    )
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("IF-B", callers=["IF-A"], file_path="src/b.py")]),
        [],
    )
    edges_before = runtime.traceability.list_call_edges()
    assert len(edges_before) == 1

    report = registry.reconcile_call_edges()

    assert report["backfilled"] == []
    assert report["unresolved"] == []
    assert runtime.traceability.list_call_edges() == edges_before


def test_reconcile_is_idempotent(tmp_project_dir, arc_runtime) -> None:
    runtime = arc_runtime
    registry = _make_registry(tmp_project_dir, runtime)
    registry.register_design(
        "REQ-1",
        registry.prepare_interfaces("REQ-1", [_iface("IF-A", callees=["IF-B"], file_path="src/a.py")]),
        [],
    )
    registry.register_design(
        "REQ-2",
        registry.prepare_interfaces("REQ-2", [_iface("IF-B", file_path="src/b.py")]),
        [],
    )

    first = registry.reconcile_call_edges()
    second = registry.reconcile_call_edges()

    assert first["backfilled"]
    assert second == {"backfilled": [], "unresolved": []}
    assert len(runtime.traceability.list_call_edges()) == 1

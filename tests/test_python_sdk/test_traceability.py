"""Tests for ``arcbench_agent_runtime.traceability.TraceabilityStore``.

These tests lock the on-disk JSON schemas for the seven ARC-Bench traceability
tables. The frontend depends on these exact shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcbench_agent_runtime.context import RuntimePaths
from arcbench_agent_runtime.events import EventClient
from arcbench_agent_runtime.traceability import (
    TABLE_NAMES,
    TraceabilityStore,
)


@pytest.fixture
def store_paths(tmp_project_dir: Path) -> RuntimePaths:
    return RuntimePaths.from_env(project_dir=str(tmp_project_dir))


@pytest.fixture
def store(store_paths: RuntimePaths) -> TraceabilityStore:
    return TraceabilityStore(store_paths, EventClient(store_paths))


@pytest.fixture
def initialized_store(store: TraceabilityStore) -> TraceabilityStore:
    store.init_db(reset=True)
    return store


def _read_table(paths: RuntimePaths, name: str) -> dict:
    p = paths.traceability_dir / f"{name}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


class TestInitStore:
    def test_creates_all_seven_tables(self, store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        store.init_db(reset=True)
        for name in TABLE_NAMES:
            assert (store_paths.traceability_dir / f"{name}.json").is_file()
            assert _read_table(store_paths, name) == {}

    def test_reset_false_does_not_overwrite_existing(
        self, store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        store.init_db(reset=True)
        store.upsert_requirement(req_id="R1", name="Login")
        store.init_db(reset=False)
        assert _read_table(store_paths, "requirements") == {
            "R1": {
                "req_id": "R1",
                "name": "Login",
                "description": "",
                "visual_reference": [],
                "scenarios": [],
                "parent_id": None,
                "children_ids": [],
                "dependencies": [],
            }
        }

    def test_reset_true_clears_existing(
        self, store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        store.init_db(reset=True)
        store.upsert_requirement(req_id="R1", name="Login")
        store.init_db(reset=True)
        assert _read_table(store_paths, "requirements") == {}

    def test_table_path_rejects_unknown_name(self, store: TraceabilityStore) -> None:
        with pytest.raises(ValueError, match="Unknown traceability table"):
            store.table_path("not_a_table")


# ---------------------------------------------------------------------------
# Requirements CRUD
# ---------------------------------------------------------------------------


class TestRequirements:
    def test_upsert_creates_row_with_all_fields(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_requirement(
            req_id="R1",
            name="Login",
            description="desc",
            visual_reference=["img1.png", "img2.png"],
            scenarios=[{"id": "R1-S1", "name": "Happy", "steps": [{"keyword": "GIVEN", "content": "x"}]}],
            parent_id="ROOT",
            children_ids=["R1.1", "R1.2"],
            dependencies=["R0"],
        )
        row = _read_table(store_paths, "requirements")["R1"]
        assert row == {
            "req_id": "R1",
            "name": "Login",
            "description": "desc",
            "visual_reference": ["img1.png", "img2.png"],
            "scenarios": [
                {"id": "R1-S1", "name": "Happy", "steps": [{"keyword": "GIVEN", "content": "x"}]}
            ],
            "parent_id": "ROOT",
            "children_ids": ["R1.1", "R1.2"],
            "dependencies": ["R0"],
        }

    def test_upsert_overwrites_existing(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_requirement(req_id="R1", name="old")
        initialized_store.upsert_requirement(req_id="R1", name="new")
        assert initialized_store.get_requirement("R1")["name"] == "new"

    def test_upsert_blank_id_raises(self, initialized_store: TraceabilityStore) -> None:
        with pytest.raises(ValueError, match="req_id is required"):
            initialized_store.upsert_requirement(req_id="   ", name="x")

    def test_get_returns_none_for_missing(self, initialized_store: TraceabilityStore) -> None:
        assert initialized_store.get_requirement("missing") is None

    def test_list_returns_sorted_rows(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_requirement(req_id="B", name="b")
        initialized_store.upsert_requirement(req_id="A", name="a")
        rows = initialized_store.list_requirements()
        # tables are stored sorted by key
        assert [row["req_id"] for row in rows] == ["A", "B"]

    def test_update_requirement_fields_merges(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_requirement(req_id="R1", name="old", description="d")
        initialized_store.update_requirement_fields("R1", name="new")
        row = initialized_store.get_requirement("R1")
        assert row["name"] == "new"
        assert row["description"] == "d"

    def test_update_missing_raises(self, initialized_store: TraceabilityStore) -> None:
        with pytest.raises(ValueError, match="Requirement not found"):
            initialized_store.update_requirement_fields("nope", name="x")

    def test_delete_cascades_to_all_related_tables(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_requirement(
            req_id="R1",
            name="Login",
            scenarios=[{"id": "R1-S1", "name": "Happy", "steps": []}],
        )
        initialized_store.upsert_interface(
            interface_id="IF1",
            req_ids=["R1", "R2"],
            type="api",
            content="GET /x",
        )
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        initialized_store.insert_call_edge(
            source_req_id="R1",
            target_req_id="R2",
            from_interface_id="IF1",
            to_interface_id="IF2",
        )
        initialized_store.upsert_node_state("R1", "DESIGNED")
        initialized_store.upsert_node_contract("R1", {"schema": "x"})

        initialized_store.delete_requirement("R1")

        assert "R1" not in _read_table(store_paths, "requirements")
        assert _read_table(store_paths, "scenarios") == {}
        assert _read_table(store_paths, "tests") == {}
        assert _read_table(store_paths, "call_edges") == {}
        assert _read_table(store_paths, "node_states") == {}
        assert _read_table(store_paths, "node_contracts") == {}
        # IF1 references both R1 and R2; row must remain because R2 still references it
        assert "IF1" in _read_table(store_paths, "interfaces")


# ---------------------------------------------------------------------------
# visual_reference structure preservation (#214)
# ---------------------------------------------------------------------------


class TestVisualReferenceStructure:
    """Dict-shaped visual references (the visual analysis path's payloads)
    must round-trip through the table instead of being coerced to Python
    repr strings, while plain string entries keep the string-list semantics.
    """

    def test_upsert_round_trips_dict_entries(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        references = [
            {"image_path": "./reference/login.png", "analysis": "nav bar with auth cluster"},
            {"image_path": "./register.png", "analysis": "two-column form", "resolved_image_path": "/tmp/x"},
        ]
        initialized_store.upsert_requirement(req_id="R1", name="Login", visual_reference=references)
        row = initialized_store.get_requirement("R1")
        assert row["visual_reference"] == references
        # The on-disk payload holds real JSON objects, not repr strings.
        assert _read_table(store_paths, "requirements")["R1"]["visual_reference"][0]["image_path"] == "./reference/login.png"

    def test_tree_walk_preserves_dict_visual_reference(
        self, initialized_store: TraceabilityStore
    ) -> None:
        tree = {
            "id": "ROOT",
            "visual_reference": [{"image_path": "./reference/home.png", "analysis": "hero layout"}],
            "children": [{"id": "R1", "visual_reference": ["./reference/child.png"]}],
        }
        initialized_store.store_requirement_tree(tree)
        assert initialized_store.get_requirement("ROOT")["visual_reference"] == [
            {"image_path": "./reference/home.png", "analysis": "hero layout"}
        ]
        assert initialized_store.get_requirement("R1")["visual_reference"] == ["./reference/child.png"]

    def test_update_requirement_fields_round_trips_dict_entries(
        self, initialized_store: TraceabilityStore
    ) -> None:
        """The visual precompute path's mount point: analyzed payloads are
        attached through ``update_requirement_fields``."""
        initialized_store.store_requirement_tree({"id": "R1", "name": "Login"})
        initialized_store.update_requirement_fields(
            "R1",
            visual_reference=[{"image_path": "./reference/login.png", "analysis": "form on the left"}],
        )
        assert initialized_store.get_requirement("R1")["visual_reference"] == [
            {"image_path": "./reference/login.png", "analysis": "form on the left"}
        ]

    def test_update_of_other_fields_keeps_dict_entries(
        self, initialized_store: TraceabilityStore
    ) -> None:
        """The merge path must not re-coerce visual_reference when a sibling
        field is updated."""
        initialized_store.upsert_requirement(
            req_id="R1",
            name="old",
            visual_reference=[{"image_path": "./reference/login.png", "analysis": "a"}],
        )
        initialized_store.update_requirement_fields("R1", name="new")
        row = initialized_store.get_requirement("R1")
        assert row["name"] == "new"
        assert row["visual_reference"] == [{"image_path": "./reference/login.png", "analysis": "a"}]

    def test_mixed_string_and_dict_entries_are_both_kept(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_requirement(
            req_id="R1",
            visual_reference=["./reference/home.png", {"image_path": "./login.png", "analysis": "x"}],
        )
        assert initialized_store.get_requirement("R1")["visual_reference"] == [
            "./reference/home.png",
            {"image_path": "./login.png", "analysis": "x"},
        ]

    def test_string_entries_keep_strip_and_drop_empty(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_requirement(req_id="R1", visual_reference=["  img.png  ", "", "  "])
        assert initialized_store.get_requirement("R1")["visual_reference"] == ["img.png"]

    def test_legacy_repr_entries_read_back_without_crash(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        """Old workspaces carry repr-string entries; the read path must
        surface them as plain strings (forward fix, no migration)."""
        legacy = {"image_path": "./reference/login.png", "analysis": "old text"}
        rows = {
            "R1": {
                "req_id": "R1",
                "name": "Login",
                "description": "",
                "visual_reference": [repr(legacy)],
                "scenarios": [],
                "parent_id": None,
                "children_ids": [],
                "dependencies": [],
            }
        }
        path = store_paths.traceability_dir / "requirements.json"
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        row = initialized_store.get_requirement("R1")
        assert row["visual_reference"] == [repr(legacy)]


# ---------------------------------------------------------------------------
# Requirement tree
# ---------------------------------------------------------------------------


class TestRequirementTree:
    def test_walk_persists_nested_children(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        tree = {
            "id": "ROOT",
            "name": "Root",
            "children": [
                {
                    "id": "R1",
                    "name": "Login",
                    "children": [
                        {"id": "R1.1", "name": "Form"},
                        {"id": "R1.2", "name": "Submit"},
                    ],
                    "scenarios": [{"id": "R1-S1", "name": "Happy", "steps": []}],
                },
                {"id": "R2", "name": "Search"},
            ],
        }
        initialized_store.store_requirement_tree(tree)

        reqs = _read_table(store_paths, "requirements")
        assert "ROOT" in reqs
        assert "R1" in reqs
        assert "R1.1" in reqs
        assert "R1.2" in reqs
        assert "R2" in reqs
        assert reqs["R1"]["parent_id"] == "ROOT"
        assert reqs["R1"]["children_ids"] == ["R1.1", "R1.2"]
        assert reqs["R1.1"]["parent_id"] == "R1"
        scens = _read_table(store_paths, "scenarios")
        assert "R1-S1" in scens
        assert scens["R1-S1"]["req_id"] == "R1"

    def test_walk_ignores_nodes_without_id(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.store_requirement_tree({"id": "ROOT", "children": [{"name": "no-id"}]})
        assert "ROOT" in _read_table(store_paths, "requirements")

    def test_walk_accepts_req_id_alias(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.store_requirement_tree({"req_id": "ROOT", "children": [{"id": "R1"}]})
        reqs = _read_table(store_paths, "requirements")
        assert "ROOT" in reqs and "R1" in reqs


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class TestScenarios:
    def test_upsert_writes_row_and_syncs_requirement(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_requirement(req_id="R1", name="x")
        initialized_store.upsert_scenario(
            scenario_id="R1-S1",
            req_id="R1",
            name="Happy",
            steps=[{"keyword": "GIVEN", "content": "x"}],
        )
        assert _read_table(store_paths, "scenarios")["R1-S1"] == {
            "scenario_id": "R1-S1",
            "name": "Happy",
            "req_id": "R1",
            "steps": [{"keyword": "GIVEN", "content": "x"}],
        }
        # requirement.scenarios list is kept in sync
        assert _read_table(store_paths, "requirements")["R1"]["scenarios"] == [
            {"id": "R1-S1", "name": "Happy", "steps": [{"keyword": "GIVEN", "content": "x"}]}
        ]

    def test_upsert_requires_ids(self, initialized_store: TraceabilityStore) -> None:
        with pytest.raises(ValueError):
            initialized_store.upsert_scenario(
                scenario_id="", req_id="R1", name="x", steps=[]
            )
        with pytest.raises(ValueError):
            initialized_store.upsert_scenario(
                scenario_id="S1", req_id="", name="x", steps=[]
            )

    def test_upsert_replaces_existing_for_same_req(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_requirement(
            req_id="R1",
            name="x",
            scenarios=[{"id": "S1", "name": "old", "steps": []}],
        )
        initialized_store.upsert_scenario(
            scenario_id="S1", req_id="R1", name="new", steps=[]
        )
        scen = initialized_store.get_scenario("S1")
        assert scen["name"] == "new"

    def test_list_filters_by_req_id(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_scenario(scenario_id="S1", req_id="R1", name="a", steps=[])
        initialized_store.upsert_scenario(scenario_id="S2", req_id="R2", name="b", steps=[])
        assert {s["scenario_id"] for s in initialized_store.list_scenarios(req_id="R1")} == {"S1"}

    def test_delete_removes_scenario_and_unsyncs(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_requirement(req_id="R1", name="x")
        initialized_store.upsert_scenario(scenario_id="S1", req_id="R1", name="a", steps=[])
        initialized_store.delete_scenario("S1")
        assert "S1" not in _read_table(store_paths, "scenarios")
        assert _read_table(store_paths, "requirements")["R1"]["scenarios"] == []


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------


class TestInterfaces:
    def test_upsert_persists_all_fields(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_interface(
            interface_id="IF1",
            req_ids=["R1", "R2"],
            type="api",
            content="POST /x",
            file_path="backend/routes/x.py",
            first_line="10",
            implemented=False,
            callers=["IF0"],
            callees=["IF2"],
        )
        row = _read_table(store_paths, "interfaces")["IF1"]
        assert row == {
            "interface_id": "IF1",
            "req_ids": ["R1", "R2"],
            "type": "api",
            "content": "POST /x",
            "file_path": "backend/routes/x.py",
            "first_line": "10",
            "implemented": False,
            "callers": ["IF0"],
            "callees": ["IF2"],
        }

    def test_upsert_requires_interface_id(self, initialized_store: TraceabilityStore) -> None:
        with pytest.raises(ValueError, match="interface_id is required"):
            initialized_store.upsert_interface(
                interface_id="   ", req_ids=["R1"], type="api", content="x"
            )

    def test_set_interface_implemented_toggles_flag(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_interface(
            interface_id="IF1", req_ids=["R1"], type="api", content="x"
        )
        initialized_store.set_interface_implemented("IF1", True)
        assert initialized_store.get_interface("IF1")["implemented"] is True
        initialized_store.set_interface_implemented("IF1", False)
        assert initialized_store.get_interface("IF1")["implemented"] is False

    def test_set_interface_implemented_missing_raises(
        self, initialized_store: TraceabilityStore
    ) -> None:
        with pytest.raises(ValueError, match="Interface not found"):
            initialized_store.set_interface_implemented("missing", True)

    def test_update_interface_fields_merges(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_interface(
            interface_id="IF1", req_ids=["R1"], type="api", content="old"
        )
        initialized_store.update_interface_fields("IF1", content="new", file_path="x.py")
        row = initialized_store.get_interface("IF1")
        assert row["content"] == "new"
        assert row["file_path"] == "x.py"

    def test_list_filters_by_req_id(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_interface(
            interface_id="IF1", req_ids=["R1"], type="api", content="x"
        )
        initialized_store.upsert_interface(
            interface_id="IF2", req_ids=["R2"], type="api", content="y"
        )
        rows = initialized_store.list_interfaces(req_id="R1")
        assert [r["interface_id"] for r in rows] == ["IF1"]

    def test_delete_removes_row(self, initialized_store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        initialized_store.upsert_interface(
            interface_id="IF1", req_ids=["R1"], type="api", content="x"
        )
        initialized_store.delete_interface("IF1")
        assert "IF1" not in _read_table(store_paths, "interfaces")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTests:
    def test_upsert_persists_all_fields(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_test(
            test_id="T1",
            req_id="R1",
            type="E2E",
            file_path="tests/x.spec.ts",
            first_line="5",
            interface_ids=["IF1"],
            passed=False,
            scenario_id="R1-S1",
        )
        row = _read_table(store_paths, "tests")["T1"]
        assert row == {
            "test_id": "T1",
            "req_id": "R1",
            "interface_ids": ["IF1"],
            "type": "E2E",
            "file_path": "tests/x.spec.ts",
            "passed": False,
            "first_line": "5",
            "scenario_id": "R1-S1",
        }

    def test_upsert_requires_ids(self, initialized_store: TraceabilityStore) -> None:
        with pytest.raises(ValueError):
            initialized_store.upsert_test(test_id="", req_id="R1", type="unit")
        with pytest.raises(ValueError):
            initialized_store.upsert_test(test_id="T1", req_id="", type="unit")

    def test_upsert_rejects_cross_req_conflict(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        with pytest.raises(ValueError, match="Test id collision"):
            initialized_store.upsert_test(test_id="T1", req_id="R2", type="unit")

    def test_set_test_pass_status_updates_value(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        initialized_store.set_test_pass_status("T1", True)
        assert initialized_store.get_test("T1")["passed"] is True
        initialized_store.set_test_pass_status("T1", None)
        assert initialized_store.get_test("T1")["passed"] is None

    def test_set_test_pass_statuses_updates_many(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        initialized_store.upsert_test(test_id="T2", req_id="R1", type="unit")
        initialized_store.set_test_pass_statuses({"T1": True, "T2": False})
        assert initialized_store.get_test("T1")["passed"] is True
        assert initialized_store.get_test("T2")["passed"] is False

    def test_set_test_pass_statuses_skips_unknown(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        initialized_store.set_test_pass_statuses({"T1": True, "missing": True})
        assert initialized_store.get_test("T1")["passed"] is True

    def test_reset_for_requirement_clears_only_matching(
        self, initialized_store: TraceabilityStore
    ) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit", passed=True)
        initialized_store.upsert_test(test_id="T2", req_id="R2", type="unit", passed=True)
        initialized_store.reset_test_pass_statuses_for_requirement("R1")
        assert initialized_store.get_test("T1")["passed"] is None
        assert initialized_store.get_test("T2")["passed"] is True

    def test_list_filters_by_req_id(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        initialized_store.upsert_test(test_id="T2", req_id="R2", type="unit")
        rows = initialized_store.list_tests(req_id="R1")
        assert [r["test_id"] for r in rows] == ["T1"]

    def test_delete_removes_row(self, initialized_store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        initialized_store.delete_test("T1")
        assert "T1" not in _read_table(store_paths, "tests")


# ---------------------------------------------------------------------------
# Call edges
# ---------------------------------------------------------------------------


class TestCallEdges:
    def test_insert_dedupes_via_composite_key(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.insert_call_edge(
            source_req_id="R1",
            target_req_id="R2",
            from_interface_id="IF1",
            to_interface_id="IF2",
        )
        initialized_store.insert_call_edge(
            source_req_id="R1",
            target_req_id="R2",
            from_interface_id="IF1",
            to_interface_id="IF2",
            edge_type="custom",
        )
        edges = initialized_store.list_call_edges()
        assert len(edges) == 1

    def test_list_filters_by_req_id(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.insert_call_edge(
            source_req_id="R1", target_req_id="R2",
            from_interface_id="IF1", to_interface_id="IF2",
        )
        initialized_store.insert_call_edge(
            source_req_id="R3", target_req_id="R4",
            from_interface_id="IF3", to_interface_id="IF4",
        )
        rows = initialized_store.list_call_edges(req_id="R1")
        assert len(rows) == 1
        assert rows[0]["source_req_id"] == "R1"

    def test_delete_removes_row(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.insert_call_edge(
            source_req_id="R1", target_req_id="R2",
            from_interface_id="IF1", to_interface_id="IF2",
        )
        initialized_store.delete_call_edge(
            source_req_id="R1", target_req_id="R2",
            from_interface_id="IF1", to_interface_id="IF2",
        )
        assert initialized_store.list_call_edges() == []

    def test_edge_default_type(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.insert_call_edge(
            source_req_id="R1", target_req_id="R2",
            from_interface_id="IF1", to_interface_id="IF2",
        )
        edge = initialized_store.list_call_edges()[0]
        assert edge["edge_type"] == "parent_child"


# ---------------------------------------------------------------------------
# Node states
# ---------------------------------------------------------------------------


class TestNodeStates:
    def test_upsert_writes_row(self, initialized_store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        initialized_store.upsert_node_state("R1", "DESIGNED", phase="design")
        row = _read_table(store_paths, "node_states")["R1"]
        assert row["req_id"] == "R1"
        assert row["state"] == "DESIGNED"
        assert row["phase"] == "design"
        assert "updated_at" in row

    def test_upsert_blank_id_raises(self, initialized_store: TraceabilityStore) -> None:
        with pytest.raises(ValueError):
            initialized_store.upsert_node_state("", "x")

    def test_get_returns_none_for_missing(self, initialized_store: TraceabilityStore) -> None:
        assert initialized_store.get_node_state("missing") is None

    def test_set_requirement_state_aliases(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.set_requirement_state("R1", "PASSED")
        assert initialized_store.get_requirement_state("R1")["state"] == "PASSED"

    def test_list_returns_all(self, initialized_store: TraceabilityStore) -> None:
        initialized_store.upsert_node_state("R1", "DESIGNED")
        initialized_store.upsert_node_state("R2", "IMPLEMENTED")
        states = {row["req_id"]: row["state"] for row in initialized_store.list_node_states()}
        assert states == {"R1": "DESIGNED", "R2": "IMPLEMENTED"}

    def test_delete_removes_row(self, initialized_store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        initialized_store.upsert_node_state("R1", "DESIGNED")
        initialized_store.delete_node_state("R1")
        assert "R1" not in _read_table(store_paths, "node_states")


# ---------------------------------------------------------------------------
# Node contracts
# ---------------------------------------------------------------------------


class TestNodeContracts:
    def test_upsert_persists_dict(self, initialized_store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        initialized_store.upsert_node_contract("R1", {"schema": "x"})
        row = _read_table(store_paths, "node_contracts")["R1"]
        assert row["req_id"] == "R1"
        assert row["content"] == {"schema": "x"}

    def test_upsert_normalizes_non_dict_to_empty(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_node_contract("R1", "not a dict")  # type: ignore[arg-type]
        assert _read_table(store_paths, "node_contracts")["R1"]["content"] == {}

    def test_delete_removes_row(self, initialized_store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        initialized_store.upsert_node_contract("R1", {"x": 1})
        initialized_store.delete_node_contract("R1")
        assert "R1" not in _read_table(store_paths, "node_contracts")


# ---------------------------------------------------------------------------
# Clear node design artifacts
# ---------------------------------------------------------------------------


class TestClearNodeDesignArtifacts:
    def test_drops_interfaces_only_with_target_req_id(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.upsert_interface(
            interface_id="IF1", req_ids=["R1"], type="api", content="x"
        )
        initialized_store.upsert_interface(
            interface_id="IF2", req_ids=["R1", "R2"], type="api", content="y"
        )
        initialized_store.upsert_interface(
            interface_id="IF3", req_ids=["R3"], type="api", content="z"
        )
        initialized_store.clear_node_design_artifacts("R1")
        rows = _read_table(store_paths, "interfaces")
        assert "IF1" not in rows
        # IF2 keeps R2 reference
        assert "R2" in rows["IF2"]["req_ids"]
        assert "R1" not in rows["IF2"]["req_ids"]
        # IF3 untouched
        assert rows["IF3"]["req_ids"] == ["R3"]

    def test_drops_all_tests_for_req(self, initialized_store: TraceabilityStore, store_paths: RuntimePaths) -> None:
        initialized_store.upsert_test(test_id="T1", req_id="R1", type="unit")
        initialized_store.upsert_test(test_id="T2", req_id="R2", type="unit")
        initialized_store.clear_node_design_artifacts("R1")
        rows = _read_table(store_paths, "tests")
        assert "T1" not in rows
        assert "T2" in rows

    def test_drops_all_call_edges_for_req(
        self, initialized_store: TraceabilityStore, store_paths: RuntimePaths
    ) -> None:
        initialized_store.insert_call_edge(
            source_req_id="R1", target_req_id="R2",
            from_interface_id="IF1", to_interface_id="IF2",
        )
        initialized_store.insert_call_edge(
            source_req_id="R3", target_req_id="R4",
            from_interface_id="IF3", to_interface_id="IF4",
        )
        initialized_store.clear_node_design_artifacts("R1")
        edges = _read_table(store_paths, "call_edges")
        assert len(edges) == 1
        assert "R3::R4::IF3::IF4" in edges


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestExportSnapshot:
    def test_returns_all_seven_tables(self, initialized_store: TraceabilityStore) -> None:
        snapshot = initialized_store.export_snapshot()
        assert set(snapshot.keys()) == {
            "requirements",
            "scenarios",
            "interfaces",
            "tests",
            "call_edges",
            "node_states",
            "node_contracts",
        }
        for value in snapshot.values():
            assert value == []
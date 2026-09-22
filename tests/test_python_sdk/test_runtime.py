"""Integration tests for the ``AgentRuntime`` facade.

These tests verify that ``EventClient`` ↔ ``TraceabilityStore`` callbacks are
wired correctly and that a realistic sequence of operations leaves the
expected JSONL + JSON artefacts behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcbench_agent_runtime import (
    AgentRuntime,
    InterfaceRecord,
    RequirementRecord,
    RuntimePaths,
    ScenarioRecord,
    TestRecord,
)
from arcbench_agent_runtime.context import RuntimePaths as _RuntimePaths  # noqa: F401
from tests.helpers.jsonl import read_jsonl


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class TestPublicExports:
    def test_package_exports_expected_names(self) -> None:
        from arcbench_agent_runtime import __all__

        assert set(__all__) == {
            "AgentRuntime",
            "InterfaceRecord",
            "RequirementRecord",
            "RuntimePaths",
            "ScenarioRecord",
            "TestRecord",
        }

    def test_record_types_are_dataclasses(self) -> None:
        assert hasattr(RequirementRecord, "__dataclass_fields__")
        assert hasattr(ScenarioRecord, "__dataclass_fields__")
        assert hasattr(InterfaceRecord, "__dataclass_fields__")
        assert hasattr(TestRecord, "__dataclass_fields__")


class TestRuntimeAssembly:
    def test_from_env_wires_components(self, runtime: AgentRuntime, tmp_project_dir: Path) -> None:
        assert isinstance(runtime.paths, RuntimePaths)
        assert runtime.paths.project_dir == tmp_project_dir
        assert hasattr(runtime.events, "mark_design_done")
        assert hasattr(runtime.traceability, "upsert_requirement")
        assert hasattr(runtime.git, "ensure_repo")

    def test_ensure_parent_dirs_runs(self, runtime: AgentRuntime) -> None:
        runtime.paths.ensure_parent_dirs()
        assert runtime.paths.runner_events_path.parent.is_dir()
        assert runtime.paths.traceability_dir.is_dir()


class TestEventTraceabilityWiring:
    def test_mark_design_done_creates_node_state(
        self, runtime: AgentRuntime, tmp_project_dir: Path
    ) -> None:
        runtime.events.mark_design_done("R1", "ok")
        node_states = _read_json(tmp_project_dir / ".arc" / "traceability" / "node_states.json")
        assert "R1" in node_states
        assert node_states["R1"]["state"] == "DESIGNED"

    def test_mark_implementation_done_creates_node_state(
        self, runtime: AgentRuntime, tmp_project_dir: Path
    ) -> None:
        runtime.events.mark_implementation_done("R1", "ok")
        node_states = _read_json(tmp_project_dir / ".arc" / "traceability" / "node_states.json")
        assert node_states["R1"]["state"] == "IMPLEMENTED"

    def test_mark_test_passed_creates_node_state(
        self, runtime: AgentRuntime, tmp_project_dir: Path
    ) -> None:
        runtime.events.mark_test_passed("R1", "ok")
        node_states = _read_json(tmp_project_dir / ".arc" / "traceability" / "node_states.json")
        assert node_states["R1"]["state"] == "PASSED"


class TestFullWorkflow:
    def test_end_to_end_records_all_seven_tables_and_events(
        self, runtime: AgentRuntime, tmp_project_dir: Path
    ) -> None:
        runtime.traceability.init_db(reset=True)
        runtime.events.mark_design_done("R1", "Designed")
        runtime.traceability.upsert_requirement(
            req_id="R1",
            name="Login",
            description="User logs in",
            scenarios=[{"id": "R1-S1", "name": "Happy", "steps": []}],
        )
        runtime.traceability.upsert_interface(
            interface_id="IF-LOGIN",
            req_ids=["R1"],
            type="API",
            content="POST /api/auth/login",
            file_path="backend/src/routes/auth.py",
            first_line="18",
            implemented=False,
        )
        runtime.traceability.upsert_test(
            test_id="TEST-LOGIN",
            req_id="R1",
            type="E2E",
            file_path="tests/login.spec.ts",
            first_line="7",
            interface_ids=["IF-LOGIN"],
        )
        runtime.traceability.set_interface_implemented("IF-LOGIN", True, "impl")
        runtime.traceability.set_test_pass_status("TEST-LOGIN", True)
        runtime.traceability.insert_call_edge(
            source_req_id="R1",
            target_req_id="R2",
            from_interface_id="IF-LOGIN",
            to_interface_id="IF-SEARCH",
        )
        runtime.traceability.upsert_node_state("R1", "PASSED", phase="test")
        runtime.traceability.upsert_node_contract("R1", {"schema": "Login"})

        trace_dir = tmp_project_dir / ".arc" / "traceability"
        assert "R1" in _read_json(trace_dir / "requirements.json")
        assert "R1-S1" in _read_json(trace_dir / "scenarios.json")
        assert "IF-LOGIN" in _read_json(trace_dir / "interfaces.json")
        assert "TEST-LOGIN" in _read_json(trace_dir / "tests.json")
        assert "R1::R2::IF-LOGIN::IF-SEARCH" in _read_json(trace_dir / "call_edges.json")
        assert "R1" in _read_json(trace_dir / "node_states.json")
        assert "R1" in _read_json(trace_dir / "node_contracts.json")

        # runner-events.jsonl must include at least one event of each kind
        events = read_jsonl(tmp_project_dir / ".arc" / "runner-events.jsonl")
        event_types = {e.get("type") for e in events}
        assert "requirement_state" in event_types
        assert "signal" in event_types

        # IF-LOGIN must be marked implemented
        ifaces = _read_json(trace_dir / "interfaces.json")
        assert ifaces["IF-LOGIN"]["implemented"] is True

        # TEST-LOGIN must be passed
        tests = _read_json(trace_dir / "tests.json")
        assert tests["TEST-LOGIN"]["passed"] is True
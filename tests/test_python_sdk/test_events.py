"""Tests for ``arcbench_agent_runtime.events.EventClient``.

These tests pin the on-disk JSONL schema that ARC-Bench frontend reads from
``.arc/runner-events.jsonl``. Any change to event shape will break these tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcbench_agent_runtime.context import RuntimePaths
from arcbench_agent_runtime.events import EventClient
from tests.helpers.jsonl import read_jsonl


@pytest.fixture
def event_paths(tmp_project_dir: Path) -> RuntimePaths:
    return RuntimePaths.from_env(project_dir=str(tmp_project_dir))


@pytest.fixture
def events(event_paths: RuntimePaths) -> EventClient:
    return EventClient(event_paths)


class TestRequirementStateEvents:
    @pytest.mark.parametrize(
        "method_name,phase,status",
        [
            ("mark_design_started", "design", "running"),
            ("mark_design_done", "design", "completed"),
            ("mark_design_failed", "design", "failed"),
            ("mark_implementation_started", "implement", "running"),
            ("mark_implementation_done", "implement", "completed"),
            ("mark_implementation_failed", "implement", "failed"),
            ("mark_test_passed", "test", "passed"),
            ("mark_test_failed", "test", "failed"),
        ],
    )
    def test_mark_methods_emit_requirement_state(
        self,
        events: EventClient,
        event_paths: RuntimePaths,
        method_name: str,
        phase: str,
        status: str,
    ) -> None:
        method = getattr(events, method_name)
        method("REQ-1", "msg")

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        event = lines[0]
        assert event["type"] == "requirement_state"
        assert event["node_id"] == "REQ-1"
        assert event["phase"] == phase
        assert event["status"] == status
        assert event["message"] == "msg"
        assert "timestamp" in event

    def test_empty_node_id_is_silently_ignored(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.mark_design_done("", "ignored")
        events.mark_design_done("   ", "ignored")
        assert read_jsonl(event_paths.runner_events_path) == []

    def test_whitespace_node_id_is_stripped(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.mark_design_done("  REQ-1  ", "msg")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["node_id"] == "REQ-1"

    def test_default_message_is_null(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.mark_design_done("REQ-1")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["message"] is None


class TestRequirementStateWriter:
    """Verify the EventClient invokes the registered state writer."""

    def test_writer_receives_state_for_every_phase_status(
        self, events: EventClient
    ) -> None:
        recorded: list[tuple[str, str, str]] = []

        def writer(req_id: str, state: str, phase: str) -> None:
            recorded.append((req_id, state, phase))

        events.set_requirement_state_writer(writer)

        events.mark_design_started("R1")  # -> DESIGNING
        events.mark_design_done("R1")  # -> DESIGNED
        events.mark_design_failed("R1")  # -> FAILED
        events.mark_implementation_started("R2")  # -> IMPLEMENTING
        events.mark_implementation_done("R2")  # -> IMPLEMENTED
        events.mark_implementation_failed("R2")  # -> FAILED
        events.mark_test_passed("R3")  # -> PASSED
        events.mark_test_failed("R3")  # -> FAILED

        assert ("R1", "DESIGNING", "design") in recorded
        assert ("R1", "DESIGNED", "design") in recorded
        assert ("R1", "FAILED", "design") in recorded
        assert ("R2", "IMPLEMENTING", "implement") in recorded
        assert ("R2", "IMPLEMENTED", "implement") in recorded
        assert ("R2", "FAILED", "implement") in recorded
        assert ("R3", "PASSED", "test") in recorded
        assert ("R3", "FAILED", "test") in recorded

    def test_no_writer_is_safe(self, events: EventClient) -> None:
        # Without a writer registered, marking must not raise.
        events.mark_design_done("REQ-1")


class TestVisualAnalysisEvents:
    def test_record_visual_analysis_writes_stage_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_visual_analysis(
            node_id=" REQ-1 ",
            status="retry_wait",
            attempt=2,
            retry_at="2026-09-24T12:00:00+00:00",
            message="provider unavailable",
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0] == {
            "type": "visual_analysis",
            "node_id": "REQ-1",
            "status": "retry_wait",
            "attempt": 2,
            "retry_at": "2026-09-24T12:00:00+00:00",
            "message": "provider unavailable",
            "timestamp": lines[0]["timestamp"],
        }

    def test_record_visual_analysis_normalizes_optional_fields(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_visual_analysis(
            node_id="REQ-1",
            status="ready",
            attempt=-1,
            retry_at="  ",
        )

        event = read_jsonl(event_paths.runner_events_path)[0]
        assert event["attempt"] == 0
        assert event["retry_at"] is None
        assert event["message"] is None

    def test_record_visual_analysis_ignores_empty_node_id(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_visual_analysis(status="failed")
        assert read_jsonl(event_paths.runner_events_path) == []


class TestTestContractPreflightEvents:
    def test_record_test_contract_preflight_writes_diagnostic_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_test_contract_preflight(
            node_id=" REQ-1 ",
            status="blocked",
            classification="deterministic",
            files=["backend/tests/login.test.js", "  "],
            issues=[{"kind": "runner_global", "classification": "deterministic"}, "ignored"],
            message="blocked before TDD",
        )

        event = read_jsonl(event_paths.runner_events_path)[0]
        assert event == {
            "type": "test_contract_preflight",
            "node_id": "REQ-1",
            "status": "blocked",
            "classification": "deterministic",
            "files": ["backend/tests/login.test.js"],
            "issues": [{"kind": "runner_global", "classification": "deterministic"}],
            "message": "blocked before TDD",
            "timestamp": event["timestamp"],
        }

    def test_record_test_contract_preflight_ignores_empty_node_id(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_test_contract_preflight(status="blocked")
        assert read_jsonl(event_paths.runner_events_path) == []


class TestRunnerStateEvents:
    @pytest.mark.parametrize(
        "method_name,state",
        [
            ("mark_run_started", "running"),
            ("mark_run_completed", "completed"),
            ("mark_run_failed", "failed"),
            ("mark_run_paused", "paused"),
            ("mark_run_resumed", "resumed"),
        ],
    )
    def test_runner_state_writes(
        self,
        events: EventClient,
        event_paths: RuntimePaths,
        method_name: str,
        state: str,
    ) -> None:
        getattr(events, method_name)("hello")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[-1] == {
            "type": "runner_state",
            "state": state,
            "timestamp": lines[-1]["timestamp"],
            "message": "hello",
        }


class TestRefreshSignals:
    def test_notify_traceability_changed_default_flags(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.notify_traceability_changed("interfaces_updated")
        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        event = lines[0]
        assert event["type"] == "signal"
        assert event["reason"] == "interfaces_updated"
        assert event["refresh"] == {
            "submission": True,
            "logs": False,
            "commit_history": False,
            "traceability_selected": True,
            "traceability_all": True,
            "preview": False,
        }

    def test_notify_commit_history_changed_with_preview(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.notify_commit_history_changed("git_commit", preview=True)
        lines = read_jsonl(event_paths.runner_events_path)
        event = lines[0]
        assert event["reason"] == "git_commit"
        assert event["refresh"]["commit_history"] is True
        assert event["refresh"]["preview"] is True

    def test_notify_commit_history_changed_without_preview(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.notify_commit_history_changed("git_initialized")
        lines = read_jsonl(event_paths.runner_events_path)
        event = lines[0]
        assert event["refresh"]["commit_history"] is True
        assert event["refresh"]["preview"] is False

    def test_reason_defaults_when_blank(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.notify_traceability_changed("   ")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["reason"] == "arcbench_agent_runtime"


class TestLLMUsageEvents:
    """Pin the ``llm_usage`` schema: one event per model call, pi-style usage."""

    def test_record_llm_usage_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        cost = {
            "input": 0.000225,
            "output": 0.0003,
            "cache_read": 0.000025,
            "cache_write": 0.0,
            "total": 0.00055,
        }
        events.record_llm_usage(
            node_id=" REQ-1 ",
            phase="DESIGN",
            model="gpt-4o",
            api_mode="chat_completions",
            source="reported",
            input_tokens=90,
            output_tokens=30,
            cache_read_tokens=20,
            cache_write_tokens=10,
            cache_write_1h_tokens=None,
            reasoning_tokens=5,
            total_tokens=150,
            cost=cost,
            duration_s=12.5,
            transport="streamed",
            attempts=2,
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "llm_usage",
            "node_id": "REQ-1",
            "phase": "DESIGN",
            "model": "gpt-4o",
            "api_mode": "chat_completions",
            "source": "reported",
            "usage": {
                "input": 90,
                "output": 30,
                "cache_read": 20,
                "cache_write": 10,
                "cache_write_1h": None,
                "reasoning": 5,
                "total": 150,
            },
            "latency": {"duration_s": 12.5, "transport": "streamed", "attempts": 2},
            "cost": cost,
            "timestamp": lines[0]["timestamp"],
        }

    def test_latency_defaults_when_unreported(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        """Calls without telemetry (older callers, external runners) keep a
        null-ish latency block so readers can treat all three fields as
        optional without keying on the block's presence."""

        events.record_llm_usage(node_id="REQ-1")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["latency"] == {"duration_s": None, "transport": "", "attempts": None}

    def test_latency_invalid_values_are_normalized(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_llm_usage(
            node_id="REQ-1",
            duration_s=-3.0,
            transport="carrier-pigeon",
            attempts=0,
        )
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["latency"] == {"duration_s": None, "transport": "", "attempts": None}

    def test_empty_node_id_is_allowed_for_run_level_calls(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        # Unlike requirement_state events, usage without a node is valid: it
        # attributes model calls made outside any node's context to the run.
        events.record_llm_usage(model="gpt-4o", input_tokens=1, output_tokens=1, total_tokens=2)
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["node_id"] == ""
        assert lines[0]["phase"] == ""

    def test_defaults_report_zero_usage_without_cost(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_llm_usage(node_id="REQ-1")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["source"] == "reported"
        assert lines[0]["usage"] == {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cache_write_1h": None,
            "reasoning": None,
            "total": 0,
        }
        assert lines[0]["cost"] is None

    def test_negative_and_invalid_values_are_clamped(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_llm_usage(
            node_id="REQ-1",
            input_tokens=-5,
            output_tokens="7",
            reasoning_tokens=-1,
            total_tokens=None,
        )
        lines = read_jsonl(event_paths.runner_events_path)
        usage = lines[0]["usage"]
        assert usage["input"] == 0
        assert usage["output"] == 7
        assert usage["reasoning"] is None  # negative breakdown is not reportable
        assert usage["total"] == 0

    def test_non_dict_cost_is_normalized_to_null(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_llm_usage(node_id="REQ-1", cost="not-a-dict")  # type: ignore[arg-type]
        assert read_jsonl(event_paths.runner_events_path)[0]["cost"] is None


class TestToolUsageEvents:
    """Pin the ``tool_usage`` schema: one event per agent tool round-trip."""

    def test_record_tool_usage_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_tool_usage(
            node_id=" REQ-1 ",
            phase="IMPLEMENT",
            tool="read_file",
            status="ok",
            path="/workspace/src/app.tsx",
            offset=0,
            limit=None,
            result_chars=12345,
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "tool_usage",
            "node_id": "REQ-1",
            "phase": "IMPLEMENT",
            "tool": "read_file",
            "status": "ok",
            "detail": {
                "path": "/workspace/src/app.tsx",
                "offset": 0,
                "limit": None,
                "result_chars": 12345,
                "result_empty": False,
            },
            "timestamp": lines[0]["timestamp"],
        }

    def test_defaults_report_ok_status_without_file_detail(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_tool_usage(tool="grep")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["status"] == "ok"
        assert lines[0]["detail"] == {
            "path": None,
            "offset": None,
            "limit": None,
            "result_chars": 0,
            "result_empty": True,
        }

    def test_blocked_and_error_statuses_are_preserved(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_tool_usage(node_id="REQ-1", tool="read_file", status="blocked")
        events.record_tool_usage(node_id="REQ-1", tool="run_tests", status="error", result_chars=42)
        lines = read_jsonl(event_paths.runner_events_path)
        assert [line["status"] for line in lines] == ["blocked", "error"]
        assert lines[1]["detail"]["result_empty"] is False

    def test_negative_and_invalid_values_are_clamped(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_tool_usage(
            node_id="REQ-1",
            tool="read_file",
            offset=-5,
            limit="7",
            result_chars=-1,
        )
        lines = read_jsonl(event_paths.runner_events_path)
        detail = lines[0]["detail"]
        assert detail["offset"] is None  # negative breakdown is not reportable
        assert detail["limit"] == 7
        assert detail["result_chars"] == 0
        assert detail["result_empty"] is True

    def test_empty_node_id_is_allowed_for_run_level_calls(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_tool_usage(tool="grep", status="error")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["node_id"] == ""
        assert lines[0]["phase"] == ""


class TestLayerReverifyEvents:
    """Pin the ``layer_reverify`` schema: trigger + outcome of the TDD
    late-fix re-verification (issue #116)."""

    @pytest.mark.parametrize("status", ["triggered", "passed", "failed"])
    def test_record_layer_reverify_writes_canonical_schema(
        self,
        events: EventClient,
        event_paths: RuntimePaths,
        status: str,
    ) -> None:
        events.record_layer_reverify(
            node_id=" REQ-2 ",
            layer=" Integration ",
            status=status,
            files=["tests/integration/test_flow.py", "  "],
            used=10,
            message="AssertionError: boom" if status == "failed" else None,
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "layer_reverify",
            "node_id": "REQ-2",
            "layer": "Integration",
            "status": status,
            "files": ["tests/integration/test_flow.py"],
            "used": 10,
            "message": "AssertionError: boom" if status == "failed" else None,
            "timestamp": lines[0]["timestamp"],
        }

    def test_defaults_and_invalid_values_are_normalized(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_layer_reverify(node_id="REQ-2", layer="Unit", status="triggered", used=-3)
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["files"] == []
        assert lines[0]["used"] == 0
        assert lines[0]["message"] is None

    def test_string_used_is_coerced(self, events: EventClient, event_paths: RuntimePaths) -> None:
        events.record_layer_reverify(node_id="REQ-2", layer="Unit", status="passed", used="7")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["used"] == 7


class TestTddStallEvents:
    """Pin the deterministic fingerprint-stall stop event (issue #264)."""

    def test_record_tdd_stall_writes_stop_evidence(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_tdd_stall(
            node_id=" REQ-2 ",
            layer=" Integration ",
            reason="repeated failure fingerprint with no effective source/environment change",
            fingerprint="1|Error: Cannot find module 'missing-parser'",
            repetitions=3,
            threshold=3,
            used=3,
            budget=10,
            suggested_action="repair the environment or choose a different implementation hypothesis",
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "tdd_stall",
            "node_id": "REQ-2",
            "layer": "Integration",
            "reason": "repeated failure fingerprint with no effective source/environment change",
            "fingerprint": "1|Error: Cannot find module 'missing-parser'",
            "repetitions": 3,
            "threshold": 3,
            "used": 3,
            "budget": 10,
            "suggested_action": "repair the environment or choose a different implementation hypothesis",
            "timestamp": lines[0]["timestamp"],
        }


class TestDemoTestStatus:
    """Demo helpers are documented as no-op with respect to disk state."""

    def test_read_returns_empty_payload(self, events: EventClient) -> None:
        assert events.read_demo_test_status_payload() == {"tests": {}, "requirements": {}}

    def test_write_is_a_noop(self, events: EventClient, event_paths: RuntimePaths) -> None:
        events.write_demo_test_status_payload({"anything": 1})
        # no event written, no file created
        assert not event_paths.runner_events_path.exists()

    def test_set_demo_test_status_emits_signal(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_test_status("T1", "passed")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["type"] == "signal"

    def test_set_demo_test_status_empty_id_is_ignored(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_test_status("", "passed")
        events.set_demo_test_status("   ", "passed")
        assert read_jsonl(event_paths.runner_events_path) == []

    def test_set_demo_test_statuses_skips_when_empty(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_test_statuses({})
        assert read_jsonl(event_paths.runner_events_path) == []

    def test_clear_demo_test_statuses_skips_when_empty(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.clear_demo_test_statuses([])
        assert read_jsonl(event_paths.runner_events_path) == []

    def test_set_demo_requirement_status_emits_signal(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_requirement_status("R1", "PASSED")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["type"] == "signal"

    def test_set_demo_requirement_status_empty_id_is_ignored(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_requirement_status("", "PASSED")
        assert read_jsonl(event_paths.runner_events_path) == []

class TestRebaseReplayEvents:
    """Pin the ``rebase_replay`` schema: the mid-phase replay lifecycle
    (issue #127 / ADR 0003)."""

    @pytest.mark.parametrize("status", ["started", "resolved", "conflicts", "aborted"])
    def test_record_rebase_replay_writes_canonical_schema(
        self,
        events: EventClient,
        event_paths: RuntimePaths,
        status: str,
    ) -> None:
        events.record_rebase_replay(
            node_id=" REQ-2.1 ",
            status=status,
            files=["backend/shared.js", "  "],
            message="rebase onto master conflicted",
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "rebase_replay",
            "node_id": "REQ-2.1",
            "status": status,
            "files": ["backend/shared.js"],
            "message": "rebase onto master conflicted",
            "timestamp": lines[0]["timestamp"],
        }

    def test_defaults_and_invalid_values_are_normalized(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_rebase_replay(node_id="REQ-2.1", status="resolved")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["files"] == []
        assert lines[0]["message"] is None


class TestStraySweepEvents:
    """Pin the ``stray_sweep`` schema: the IMPLEMENT wrap-up cleanup of stray
    duplicate files (issue #159)."""

    def test_record_stray_sweep_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_stray_sweep(
            node_id=" REQ-1 ",
            files=["src/api/auth.ts", "  "],
            message="duplicate of frontend/src/api/auth.ts",
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "stray_sweep",
            "node_id": "REQ-1",
            "files": ["src/api/auth.ts"],
            "message": "duplicate of frontend/src/api/auth.ts",
            "timestamp": lines[0]["timestamp"],
        }

    def test_defaults_and_invalid_values_are_normalized(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_stray_sweep(node_id="REQ-1")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["files"] == []
        assert lines[0]["message"] is None


class TestZeroTestLeafEvents:
    """Pin the ``zero_test_leaf`` schema: the observation-only event for leaf
    nodes that own interface contracts yet registered an empty test manifest
    (issue #187). The event records the shape for later gating decisions —
    no gate, no retry; an empty manifest stays a legal DESIGN result."""

    def test_record_zero_test_leaf_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_zero_test_leaf(
            node_id=" REQ-3 ",
            interface_count=2,
            summary="Node renders a static chart; no local behavior to test.",
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "zero_test_leaf",
            "node_id": "REQ-3",
            "interface_count": 2,
            "summary": "Node renders a static chart; no local behavior to test.",
            "timestamp": lines[0]["timestamp"],
        }

    def test_defaults_and_invalid_values_are_normalized(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_zero_test_leaf(node_id="REQ-3", interface_count=-3)
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["interface_count"] == 0
        assert lines[0]["summary"] is None


class TestDesignConvergenceEvents:
    """Pin the audit event for deterministic DESIGN coverage reuse."""

    def test_record_design_convergence_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_design_convergence(
            node_id=" REQ-3 ",
            status="CONVERGED",
            interface_ids=["IF-A", " "],
            interface_status={"IF-A": "implemented"},
            test_ids=["T-A"],
            coverage=[
                {
                    "test_id": "T-A",
                    "file_path": "tests/unit/test_a.py",
                    "coverage_scope": "dependency",
                    "interface_ids": ["IF-A"],
                    "state": "green",
                }
            ],
            checkpoint_ids=["REQ-OWNER"],
            checkpoint_paths={"IF-A": "src/calc.py"},
            message="complete reused coverage",
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "design_convergence",
            "node_id": "REQ-3",
            "status": "converged",
            "result_state": "CONVERGED",
            "interface_ids": ["IF-A"],
            "interface_status": {"IF-A": "implemented"},
            "test_ids": ["T-A"],
            "coverage": [
                {
                    "test_id": "T-A",
                    "file_path": "tests/unit/test_a.py",
                    "coverage_scope": "dependency",
                    "interface_ids": ["IF-A"],
                    "state": "green",
                }
            ],
            "checkpoint_ids": ["REQ-OWNER"],
            "checkpoint_paths": {"IF-A": "src/calc.py"},
            "message": "complete reused coverage",
            "timestamp": lines[0]["timestamp"],
        }

    def test_defaults_and_invalid_status_are_normalized(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_design_convergence(status="unknown")

        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["node_id"] == ""
        assert lines[0]["status"] == "reused"
        assert lines[0]["result_state"] == "CONVERGED"
        assert lines[0]["interface_ids"] == []
        assert lines[0]["interface_status"] == {}
        assert lines[0]["test_ids"] == []
        assert lines[0]["coverage"] == []
        assert lines[0]["checkpoint_ids"] == []
        assert lines[0]["checkpoint_paths"] == {}
        assert lines[0]["message"] is None


class TestEdgeReconcileEvents:
    """Pin the ``edge_reconcile`` schema: the compile-wrap-up dangling-edge
    sweep over every stored interface's callers/callees (issue #238). The
    event carries the backfilled cross_req edges and the references that
    still resolved to no stored contract at compile end."""

    def test_record_edge_reconcile_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_edge_reconcile(
            backfilled=[
                {
                    "interface_id": "IF-A",
                    "kind": "callees",
                    "ref_id": "IF-B",
                    "edges": [{"source_req_id": "REQ-1", "target_req_id": "REQ-2"}],
                }
            ],
            unresolved=[{"interface_id": "IF-C", "kind": "callers", "ref_id": "IF-GHOST"}],
            message="backfilled 1 edge(s); 1 reference(s) still unresolved",
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "edge_reconcile",
            "backfilled": [
                {
                    "interface_id": "IF-A",
                    "kind": "callees",
                    "ref_id": "IF-B",
                    "edges": [{"source_req_id": "REQ-1", "target_req_id": "REQ-2"}],
                }
            ],
            "unresolved": [{"interface_id": "IF-C", "kind": "callers", "ref_id": "IF-GHOST"}],
            "message": "backfilled 1 edge(s); 1 reference(s) still unresolved",
            "timestamp": lines[0]["timestamp"],
        }

    def test_defaults_and_invalid_values_are_normalized(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_edge_reconcile()
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["backfilled"] == []
        assert lines[0]["unresolved"] == []
        assert lines[0]["message"] is None


class TestTraceabilityRowEvents:
    """Pin the row-event schemas ``TraceabilityStore`` emits through the
    public channel (issue #163): the payload mirrors the persisted row and
    the envelope only adds a trailing timestamp."""

    def test_record_traceability_row_event_stamps_timestamp_at_the_end(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        row = {
            "type": "interface_upsert",
            "interface_id": "IF-1",
            "req_ids": ["REQ-1"],
            "interface_type": "API",
            "content": "GET /a",
        }
        events.record_traceability_row_event(row)

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        event = lines[0]
        assert event["type"] == "interface_upsert"
        assert event["interface_id"] == "IF-1"
        assert list(event.keys())[-1] == "timestamp"
        # The caller's payload dict is not mutated (no envelope leaks back).
        assert "timestamp" not in row

    def test_record_traceability_row_event_preserves_a_caller_timestamp(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_traceability_row_event(
            {"type": "interface_status", "interface_id": "IF-1", "implemented": True, "timestamp": "2026-01-01 00:00:00"}
        )
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["timestamp"] == "2026-01-01 00:00:00"


class TestGitIdentityEvent:
    """Pin the ``git_identity_configured`` signal ``GitClient`` emits once per
    ``ensure_repo`` (issue #163)."""

    def test_record_git_identity_configured_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_git_identity_configured("ARC Bench Agent", "arcbench@example.com")

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "type": "signal",
            "reason": "git_identity_configured",
            "refresh": {
                "submission": False,
                "logs": False,
                "commit_history": True,
                "traceability_selected": False,
                "traceability_all": False,
                "preview": False,
            },
            "message": "ARC Bench Agent <arcbench@example.com>",
            "timestamp": lines[0]["timestamp"],
        }


class TestContractDriftEvents:
    """Pin the ``contract_drift`` schema (DESIGN gate pipelining, issue #83;
    public channel per issue #163)."""

    def test_record_contract_drift_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        drift_payload = {
            "interface_id": "RA-FUNC-Auth",
            "file_path": "backend/src/features/auth.js",
            "registered_first_line": "export const auth",
            "landed_first_line": "export const session",
        }
        events.record_contract_drift(
            node_id=" RA ",
            drift=[drift_payload],
            arbitration=False,
        )

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        assert lines[0] == {
            "timestamp": lines[0]["timestamp"],
            "type": "contract_drift",
            "node_id": "RA",
            "drift": [drift_payload],
            "arbitration": False,
        }

    def test_record_contract_drift_repair_adds_outcome(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_contract_drift(
            node_id="RA",
            drift=[{"interface_id": "IF-1"}],
            arbitration=True,
            outcome="repaired",
        )
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["arbitration"] is True
        assert lines[0]["outcome"] == "repaired"
        assert list(lines[0].keys())[-1] == "outcome"

    def test_record_contract_drift_defaults(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_contract_drift(node_id="RA")
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0]["drift"] == []
        assert lines[0]["arbitration"] is False
        assert "outcome" not in lines[0]


class TestMergeArbitrationEvents:
    """Pin the ``merge_arbitration`` audit envelope (issue #81; public
    channel per issue #163): the arbiter supplies the compact record, the
    EventClient stamps ``type`` and ``timestamp`` first."""

    def test_record_merge_arbitration_writes_canonical_schema(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        record = {
            "node_id": "REQ-3",
            "phase": "IMPLEMENT",
            "trigger": "conflict",
            "outcome": "applied",
            "conflict_files": ["backend/src.js"],
        }
        events.record_merge_arbitration(record)

        lines = read_jsonl(event_paths.runner_events_path)
        assert len(lines) == 1
        event = lines[0]
        assert list(event.keys())[:2] == ["type", "timestamp"]
        assert event["type"] == "merge_arbitration"
        assert event["node_id"] == "REQ-3"
        assert event["outcome"] == "applied"
        assert event["conflict_files"] == ["backend/src.js"]
        # The caller's record dict is not mutated.
        assert "type" not in record and "timestamp" not in record

    def test_timestamp_flag_off_keeps_the_pre_funnel_reverified_shape(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        """The workflow's post-reverify path emitted without a timestamp
        before the funnel; byte compatibility pins that shape (issue #163)."""
        events.record_merge_arbitration(
            {
                "node_id": "REQ-3",
                "phase": "IMPLEMENT",
                "trigger": "health-gate",
                "outcome": "reverified-passed",
                "detail": "",
            },
            timestamp=False,
        )
        lines = read_jsonl(event_paths.runner_events_path)
        assert lines[0] == {
            "type": "merge_arbitration",
            "node_id": "REQ-3",
            "phase": "IMPLEMENT",
            "trigger": "health-gate",
            "outcome": "reverified-passed",
            "detail": "",
        }

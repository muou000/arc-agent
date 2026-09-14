"""Tests for ``arcbench_agent_runtime.events.EventClient``.

These tests pin the on-disk JSONL schema that ARC-Bench frontend reads from
``.arc/runner-events.jsonl``. Any change to event shape will break these tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcbench_agent_runtime.context import RuntimePaths
from arcbench_agent_runtime.events import EventClient


@pytest.fixture
def event_paths(tmp_project_dir: Path) -> RuntimePaths:
    return RuntimePaths.from_env(project_dir=str(tmp_project_dir))


@pytest.fixture
def events(event_paths: RuntimePaths) -> EventClient:
    return EventClient(event_paths)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


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

        lines = _read_jsonl(event_paths.runner_events_path)
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
        assert _read_jsonl(event_paths.runner_events_path) == []

    def test_whitespace_node_id_is_stripped(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.mark_design_done("  REQ-1  ", "msg")
        lines = _read_jsonl(event_paths.runner_events_path)
        assert lines[0]["node_id"] == "REQ-1"

    def test_default_message_is_null(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.mark_design_done("REQ-1")
        lines = _read_jsonl(event_paths.runner_events_path)
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
        lines = _read_jsonl(event_paths.runner_events_path)
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
        lines = _read_jsonl(event_paths.runner_events_path)
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
        lines = _read_jsonl(event_paths.runner_events_path)
        event = lines[0]
        assert event["reason"] == "git_commit"
        assert event["refresh"]["commit_history"] is True
        assert event["refresh"]["preview"] is True

    def test_notify_commit_history_changed_without_preview(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.notify_commit_history_changed("git_initialized")
        lines = _read_jsonl(event_paths.runner_events_path)
        event = lines[0]
        assert event["refresh"]["commit_history"] is True
        assert event["refresh"]["preview"] is False

    def test_reason_defaults_when_blank(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.notify_traceability_changed("   ")
        lines = _read_jsonl(event_paths.runner_events_path)
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
        )

        lines = _read_jsonl(event_paths.runner_events_path)
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
            "cost": cost,
            "timestamp": lines[0]["timestamp"],
        }

    def test_empty_node_id_is_allowed_for_run_level_calls(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        # Unlike requirement_state events, usage without a node is valid: it
        # attributes planning-time model calls to the run as a whole.
        events.record_llm_usage(model="gpt-4o", input_tokens=1, output_tokens=1, total_tokens=2)
        lines = _read_jsonl(event_paths.runner_events_path)
        assert lines[0]["node_id"] == ""
        assert lines[0]["phase"] == ""

    def test_defaults_report_zero_usage_without_cost(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_llm_usage(node_id="REQ-1")
        lines = _read_jsonl(event_paths.runner_events_path)
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
        lines = _read_jsonl(event_paths.runner_events_path)
        usage = lines[0]["usage"]
        assert usage["input"] == 0
        assert usage["output"] == 7
        assert usage["reasoning"] is None  # negative breakdown is not reportable
        assert usage["total"] == 0

    def test_non_dict_cost_is_normalized_to_null(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_llm_usage(node_id="REQ-1", cost="not-a-dict")  # type: ignore[arg-type]
        assert _read_jsonl(event_paths.runner_events_path)[0]["cost"] is None


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

        lines = _read_jsonl(event_paths.runner_events_path)
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
        lines = _read_jsonl(event_paths.runner_events_path)
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
        lines = _read_jsonl(event_paths.runner_events_path)
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
        lines = _read_jsonl(event_paths.runner_events_path)
        detail = lines[0]["detail"]
        assert detail["offset"] is None  # negative breakdown is not reportable
        assert detail["limit"] == 7
        assert detail["result_chars"] == 0
        assert detail["result_empty"] is True

    def test_empty_node_id_is_allowed_for_run_level_calls(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.record_tool_usage(tool="grep", status="error")
        lines = _read_jsonl(event_paths.runner_events_path)
        assert lines[0]["node_id"] == ""
        assert lines[0]["phase"] == ""


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
        lines = _read_jsonl(event_paths.runner_events_path)
        assert lines[0]["type"] == "signal"

    def test_set_demo_test_status_empty_id_is_ignored(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_test_status("", "passed")
        events.set_demo_test_status("   ", "passed")
        assert _read_jsonl(event_paths.runner_events_path) == []

    def test_set_demo_test_statuses_skips_when_empty(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_test_statuses({})
        assert _read_jsonl(event_paths.runner_events_path) == []

    def test_clear_demo_test_statuses_skips_when_empty(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.clear_demo_test_statuses([])
        assert _read_jsonl(event_paths.runner_events_path) == []

    def test_set_demo_requirement_status_emits_signal(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_requirement_status("R1", "PASSED")
        lines = _read_jsonl(event_paths.runner_events_path)
        assert lines[0]["type"] == "signal"

    def test_set_demo_requirement_status_empty_id_is_ignored(
        self, events: EventClient, event_paths: RuntimePaths
    ) -> None:
        events.set_demo_requirement_status("", "PASSED")
        assert _read_jsonl(event_paths.runner_events_path) == []
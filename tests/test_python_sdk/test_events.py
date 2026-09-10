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
"""Run-level provider outage pause, persistence, and resume contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.model.openai_api_adapter import ARCModelAPIError
from core.provider_outage import (
    OUTAGE_OPEN,
    OUTAGE_OBSERVED,
    OUTAGE_RECOVERED,
    RUN_STATUS_PROVIDER_OUTAGE,
    RUN_STATUS_RUNNING,
)
from core.queue_state import (
    NODE_DESIGNED,
    NODE_PASSED,
    NODE_UNSEEN,
    PHASE_DESIGN,
    TASK_COMPLETED,
    TASK_PENDING,
    load_or_create_queue,
    mark_provider_outage_health_check,
    record_provider_outage,
    save_queue,
)
from core.workflow import ARCWorkflowManager


def _details(*, base_url: str = "https://provider.test/v1", fingerprint: str = "fp-a") -> dict[str, Any]:
    return {
        "fingerprint": fingerprint,
        "provider": "provider.test",
        "base_url": base_url,
        "model": "test-model",
        "api_mode": "chat_completions",
        "error_category": "provider_outage",
        "error_type": "EndpointUnreachable",
        "status_code": None,
        "message": "endpoint unavailable",
    }


def test_provider_outage_state_aggregates_by_fingerprint_and_window() -> None:
    queue: dict[str, Any] = {"run_status": RUN_STATUS_RUNNING}
    first = datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)

    opened, state = record_provider_outage(
        queue,
        _details(),
        threshold=2,
        window_seconds=300,
        now=first,
    )
    assert opened is False
    assert state["status"] == OUTAGE_OBSERVED
    assert state["failure_count"] == 1
    assert queue["run_status"] == RUN_STATUS_RUNNING

    # A different endpoint starts a separate fingerprint window.
    opened, state = record_provider_outage(
        queue,
        _details(base_url="https://other.test/v1", fingerprint="fp-b"),
        threshold=2,
        window_seconds=300,
        now=first + timedelta(seconds=40),
    )
    assert opened is False
    assert state["failure_count"] == 1
    assert state["fingerprint"] == "fp-b"
    assert queue["provider_outage_fingerprints"]["fp-a"]["failure_count"] == 1

    # The original fingerprint keeps its own window and reaches the threshold
    # even though another fingerprint was observed in between.
    opened, state = record_provider_outage(
        queue,
        _details(),
        threshold=2,
        window_seconds=300,
        now=first + timedelta(seconds=60),
    )
    assert opened is True
    assert state["failure_count"] == 2
    assert state["status"] == OUTAGE_OPEN
    assert queue["run_status"] == RUN_STATUS_PROVIDER_OUTAGE
    assert state["tripped_at"] is not None

    # A later observation outside A's window starts a fresh count.
    opened, state = record_provider_outage(
        queue,
        _details(),
        threshold=3,
        window_seconds=300,
        now=first + timedelta(seconds=700),
    )
    assert opened is False
    assert state["failure_count"] == 1


def test_provider_outage_state_persists_and_records_health_recovery(tmp_path: Path) -> None:
    tree = {"id": "R", "name": "root", "description": "root", "children": []}
    path = tmp_path / ".arc" / "processing_queue.json"
    queue = load_or_create_queue(str(path), tree)
    record_provider_outage(
        queue,
        _details(),
        threshold=1,
        now=datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc),
    )
    save_queue(queue, str(path))

    restored = load_or_create_queue(str(path), tree, require_compatible_existing_queue=True)
    assert restored["provider_outage"]["status"] == OUTAGE_OPEN
    assert restored["provider_outage"]["base_url"] == "https://provider.test/v1"
    assert restored["provider_outage"]["threshold"] == 1

    recovered = mark_provider_outage_health_check(
        restored,
        healthy=True,
        now=datetime(2026, 9, 24, 1, 3, tzinfo=timezone.utc),
        message="provider health check passed",
    )
    assert recovered["status"] == OUTAGE_RECOVERED
    assert recovered["recovered_at"] is not None
    assert restored["run_status"] == RUN_STATUS_RUNNING


class _Traceability:
    def __init__(self) -> None:
        self.requirements = {
            "R": {"id": "R", "name": "root", "description": "root"},
            "A": {"id": "A", "name": "a", "description": "a"},
            "B": {"id": "B", "name": "b", "description": "b"},
        }
        self.states: dict[str, str] = {}

    def get_requirement(self, node_id: str) -> dict[str, Any] | None:
        return self.requirements.get(node_id)

    def upsert_node_state(self, node_id: str, state: str) -> None:
        self.states[node_id] = state


class _Events:
    def __init__(self) -> None:
        self.paused: list[str] = []
        self.resumed: list[str] = []

    def mark_run_paused(self, message: str) -> None:
        self.paused.append(message)

    def mark_run_resumed(self, message: str) -> None:
        self.resumed.append(message)

    def __getattr__(self, _name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            return None

        return record


class _Git:
    def commit(self, message: str) -> bool:
        return False

    def status_porcelain(self) -> str:
        return ""


def _manager(tmp_path: Path) -> ARCWorkflowManager:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(
        traceability=_Traceability(),
        events=_Events(),
        git=_Git(),
    )
    return manager


def _outage_error(base_url: str = "https://provider.test/v1") -> ARCModelAPIError:
    return ARCModelAPIError(
        "Model API endpoint unreachable",
        api_mode="chat_completions",
        model="test-model",
        base_url=base_url,
        error_type="EndpointUnreachable",
    )


def test_outage_threshold_stops_new_sibling_tasks_without_failing_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_NODE_WORKTREES", "0")
    monkeypatch.setenv("ARC_PROVIDER_OUTAGE_THRESHOLD", "2")
    manager = _manager(tmp_path)
    calls: list[str] = []

    async def fail_with_outage(task: dict[str, Any], ctx: Any = None) -> bool:
        del ctx
        calls.append(str(task["node_id"]))
        raise _outage_error()

    monkeypatch.setattr(manager, "_run_task", fail_with_outage)
    tasks = [
        {"task_id": "A:DESIGN", "node_id": "A", "phase": PHASE_DESIGN, "order": 0, "status": TASK_PENDING},
        {"task_id": "B:DESIGN", "node_id": "B", "phase": PHASE_DESIGN, "order": 1, "status": TASK_PENDING},
    ]
    queue = {
        "root_id": "R",
        "tasks": tasks,
        "node_states": {"A": NODE_UNSEEN, "B": NODE_UNSEEN},
        "node_design_done": {"A": False, "B": False},
        "last_task_id": None,
    }

    asyncio.run(manager._drain_runnable_tasks(queue))

    assert calls == ["A", "B"]
    assert queue["provider_outage"]["status"] == OUTAGE_OPEN
    assert queue["run_status"] == RUN_STATUS_PROVIDER_OUTAGE
    assert queue["node_states"] == {"A": NODE_UNSEEN, "B": NODE_UNSEEN}
    assert all(task["status"] == TASK_PENDING for task in tasks)
    assert manager.runtime.events.paused


def test_resume_health_check_recovers_only_interrupted_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path)
    queue = {
        "run_status": RUN_STATUS_PROVIDER_OUTAGE,
        "provider_outage": {
            **_details(),
            "status": OUTAGE_OPEN,
            "failure_count": 2,
            "threshold": 2,
            "window_seconds": 300,
            "first_seen_at": "2026-09-24T01:00:00+00:00",
            "last_seen_at": "2026-09-24T01:00:01+00:00",
            "tripped_at": "2026-09-24T01:00:01+00:00",
            "recovered_at": None,
        },
        "tasks": [
            {"task_id": "A:DESIGN", "node_id": "A", "phase": PHASE_DESIGN, "order": 0, "status": TASK_COMPLETED},
            {"task_id": "B:DESIGN", "node_id": "B", "phase": PHASE_DESIGN, "order": 1, "status": TASK_PENDING},
        ],
        "node_states": {"A": NODE_PASSED, "B": NODE_UNSEEN},
        "node_design_done": {"A": True, "B": False},
    }
    monkeypatch.setattr("core.workflow.probe_endpoint_reachable", lambda **kwargs: True)

    recovered = asyncio.run(manager._resume_provider_outage(queue))

    assert recovered is True
    assert queue["provider_outage"]["status"] == OUTAGE_RECOVERED
    assert queue["run_status"] == RUN_STATUS_RUNNING
    assert queue["node_states"]["A"] == NODE_PASSED
    assert manager.runtime.events.resumed


def test_resume_keeps_outage_open_when_health_check_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path)
    queue = {
        "run_status": RUN_STATUS_PROVIDER_OUTAGE,
        "provider_outage": {
            **_details(),
            "status": OUTAGE_OPEN,
            "failure_count": 1,
            "threshold": 1,
            "window_seconds": 300,
            "first_seen_at": "2026-09-24T01:00:00+00:00",
            "last_seen_at": "2026-09-24T01:00:00+00:00",
            "tripped_at": "2026-09-24T01:00:00+00:00",
            "recovered_at": None,
        },
        "tasks": [],
        "node_states": {},
    }
    monkeypatch.setattr("core.workflow.probe_endpoint_reachable", lambda **kwargs: False)

    recovered = asyncio.run(manager._resume_provider_outage(queue))

    assert recovered is False
    assert queue["provider_outage"]["status"] == OUTAGE_OPEN
    assert queue["run_status"] == RUN_STATUS_PROVIDER_OUTAGE
    assert not manager.runtime.events.resumed

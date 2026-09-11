"""Verify that an escaped agent/model exception marks the node FAILED.

Before the guard in ``ARCWorkflowManager._drain_runnable_tasks``, an exception
raised inside a phase (e.g. an ``ARCModelAPIError`` that survived model-layer
retries) propagated uncaught and crashed the whole compilation run. The queue
must instead degrade to the same FAILED path used for ordinary task failures.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from agents.model.openai_api_adapter import ARCModelAPIError
from core.workflow import (
    ARCWorkflowManager,
    NODE_FAILED,
    PHASE_DESIGN,
    TASK_FAILED,
    TASK_PENDING,
)


class _Traceability:
    def __init__(self) -> None:
        self.requirements: dict[str, dict[str, Any]] = {
            "R1": {"id": "R1", "name": "root", "description": "test requirement"},
        }
        self.states: dict[str, str] = {}

    def get_requirement(self, node_id: str) -> dict[str, Any] | None:
        return self.requirements.get(node_id)

    def upsert_node_state(self, node_id: str, state: str) -> None:
        self.states[node_id] = state


class _Events:
    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            return None

        return record


class _Git:
    def commit(self, message: str) -> bool:
        return False


def _make_manager(tmp_path) -> ARCWorkflowManager:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(traceability=_Traceability(), events=_Events(), git=_Git())
    return manager


def _single_design_task() -> dict[str, Any]:
    return {
        "task_id": "R1:DESIGN",
        "node_id": "R1",
        "phase": PHASE_DESIGN,
        "order": 0,
        "status": TASK_PENDING,
    }


def test_crashed_design_task_marks_node_failed(tmp_path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)

    async def crash(node_id: str, requirement_data: dict[str, Any]) -> bool:
        raise ARCModelAPIError(
            "Model API request failed",
            api_mode="chat_completions",
            model="test-model",
            status_code=429,
        )

    monkeypatch.setattr(manager.phase_runner, "run_design_phase", crash)

    task = _single_design_task()
    queue_state = {"root_id": "R1", "tasks": [task], "node_states": {"R1": "UNSEEN"}, "last_task_id": None}

    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert task["status"] == TASK_FAILED
    assert queue_state["node_states"]["R1"] == NODE_FAILED


def test_crash_in_one_task_does_not_block_remaining_tasks(tmp_path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)

    async def crash(node_id: str, requirement_data: dict[str, Any]) -> bool:
        raise RuntimeError("unexpected phase error")

    async def succeed(node_id: str, requirement_data: dict[str, Any]) -> bool:
        return True

    manager.runtime.traceability.requirements["R2"] = {"id": "R2", "name": "child", "description": "d"}

    # R1's design phase crashes; R2 still runs afterwards.
    async def run_design(node_id: str, requirement_data: dict[str, Any]) -> bool:
        if node_id == "R1":
            return await crash(node_id, requirement_data)
        return await succeed(node_id, requirement_data)

    monkeypatch.setattr(manager.phase_runner, "run_design_phase", run_design)

    tasks = [
        {"task_id": "R1:DESIGN", "node_id": "R1", "phase": PHASE_DESIGN, "order": 0, "status": TASK_PENDING},
        {"task_id": "R2:DESIGN", "node_id": "R2", "phase": PHASE_DESIGN, "order": 1, "status": TASK_PENDING},
    ]
    queue_state = {"root_id": "R1", "tasks": tasks, "node_states": {"R1": "UNSEEN", "R2": "UNSEEN"}, "last_task_id": None}

    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert tasks[0]["status"] == TASK_FAILED
    assert tasks[1]["status"] == "COMPLETED"
    assert queue_state["node_states"]["R1"] == NODE_FAILED
    assert queue_state["node_states"]["R2"] == "DESIGNED"

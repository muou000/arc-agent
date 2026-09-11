"""Bounded concurrency for the compilation queue.

The queue used to drain strictly one task at a time. It now runs up to
``ARC_MAX_CONCURRENT_TASKS`` tasks at once (default 1, i.e. the old behaviour)
while still honouring the ordering the queue relies on: a node never runs two
tasks simultaneously and an IMPLEMENT task waits for its node's DESIGN plus
every earlier IMPLEMENT.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from core.workflow import (
    ARCWorkflowManager,
    DEFAULT_MAX_CONCURRENT_TASKS,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_PENDING,
)


class _Traceability:
    def __init__(self, node_ids: list[str]) -> None:
        self.requirements: dict[str, dict[str, Any]] = {
            node_id: {"id": node_id, "name": node_id, "description": "d"} for node_id in node_ids
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


class _Probe:
    """Tracks how many phases overlap and the order in which they start."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.started: list[str] = []

    async def run(self, label: str) -> bool:
        self.started.append(label)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return True


def _make_manager(tmp_path, node_ids: list[str]) -> ARCWorkflowManager:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path),
        requirement_path="",
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(
        traceability=_Traceability(node_ids),
        events=_Events(),
        git=_Git(),
    )
    return manager


def _task(node_id: str, phase: str, order: int, status: str = TASK_PENDING) -> dict[str, Any]:
    return {
        "task_id": f"{node_id}:{phase}",
        "node_id": node_id,
        "phase": phase,
        "order": order,
        "status": status,
    }


def _queue(root_id: str, tasks: list[dict[str, Any]], node_ids: list[str]) -> dict[str, Any]:
    return {
        "root_id": root_id,
        "tasks": tasks,
        "node_states": {node_id: "UNSEEN" for node_id in node_ids},
        "last_task_id": None,
    }


def test_default_concurrency_runs_tasks_serially(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ARC_MAX_CONCURRENT_TASKS", raising=False)
    manager = _make_manager(tmp_path, ["R1", "R2"])
    probe = _Probe()

    async def run_design(node_id: str, requirement_data: dict[str, Any]) -> bool:
        return await probe.run(f"{node_id}:DESIGN")

    monkeypatch.setattr(manager.phase_runner, "run_design_phase", run_design)

    tasks = [_task("R1", PHASE_DESIGN, 0), _task("R2", PHASE_DESIGN, 1)]
    queue_state = _queue("R1", tasks, ["R1", "R2"])

    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert probe.peak == 1
    assert probe.started == ["R1:DESIGN", "R2:DESIGN"]
    assert [task["status"] for task in tasks] == [TASK_COMPLETED, TASK_COMPLETED]


def test_opt_in_concurrency_overlaps_independent_nodes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_manager(tmp_path, ["R1", "R2"])
    probe = _Probe()

    async def run_design(node_id: str, requirement_data: dict[str, Any]) -> bool:
        return await probe.run(f"{node_id}:DESIGN")

    monkeypatch.setattr(manager.phase_runner, "run_design_phase", run_design)

    tasks = [_task("R1", PHASE_DESIGN, 0), _task("R2", PHASE_DESIGN, 1)]
    queue_state = _queue("R1", tasks, ["R1", "R2"])

    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert probe.peak == 2
    assert sorted(probe.started) == ["R1:DESIGN", "R2:DESIGN"]
    assert [task["status"] for task in tasks] == [TASK_COMPLETED, TASK_COMPLETED]


def test_implement_waits_for_its_design(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_manager(tmp_path, ["R1"])
    probe = _Probe()

    async def run_design(node_id: str, requirement_data: dict[str, Any]) -> bool:
        return await probe.run(f"{node_id}:DESIGN")

    async def run_implement(node_id: str, requirement_data: dict[str, Any]) -> bool:
        return await probe.run(f"{node_id}:IMPLEMENT")

    monkeypatch.setattr(manager.phase_runner, "run_design_phase", run_design)
    monkeypatch.setattr(manager.phase_runner, "run_implement_phase", run_implement)

    design = _task("R1", PHASE_DESIGN, 0)
    implement = _task("R1", PHASE_IMPLEMENT, 1)
    queue_state = _queue("R1", [design, implement], ["R1"])

    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert probe.started == ["R1:DESIGN", "R1:IMPLEMENT"]
    assert probe.peak == 1
    assert design["status"] == TASK_COMPLETED
    assert implement["status"] == TASK_COMPLETED


def test_implement_tasks_keep_their_generated_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "3")
    manager = _make_manager(tmp_path, ["R1", "R2", "R3"])
    probe = _Probe(delay=0.01)

    async def run_implement(node_id: str, requirement_data: dict[str, Any]) -> bool:
        return await probe.run(f"{node_id}:IMPLEMENT")

    monkeypatch.setattr(manager.phase_runner, "run_implement_phase", run_implement)

    # Mirrors _build_processing_tasks: each child's IMPLEMENT precedes its parent's.
    tasks = [
        _task("R1", PHASE_DESIGN, 0, TASK_COMPLETED),
        _task("R2", PHASE_DESIGN, 1, TASK_COMPLETED),
        _task("R3", PHASE_DESIGN, 2, TASK_COMPLETED),
        _task("R2", PHASE_IMPLEMENT, 3),
        _task("R3", PHASE_IMPLEMENT, 4),
        _task("R1", PHASE_IMPLEMENT, 5),
    ]
    queue_state = _queue("R1", tasks, ["R1", "R2", "R3"])

    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert probe.started == ["R2:IMPLEMENT", "R3:IMPLEMENT", "R1:IMPLEMENT"]
    assert probe.peak == 1


def test_concurrency_setting_is_parsed_defensively(monkeypatch) -> None:
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "not-a-number")
    assert ARCWorkflowManager._max_concurrent_tasks() == DEFAULT_MAX_CONCURRENT_TASKS

    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "0")
    assert ARCWorkflowManager._max_concurrent_tasks() == 1

    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "4")
    assert ARCWorkflowManager._max_concurrent_tasks() == 4

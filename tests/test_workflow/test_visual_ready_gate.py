from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import workflow
from core.queue_state import (
    PHASE_DESIGN,
    STAGE_FAILED,
    STAGE_PUBLISHED,
    STAGE_READY,
    STAGE_RETRY_WAIT,
    STAGE_VISUAL_ANALYSIS,
    load_or_create_queue,
    node_state,
    stage_status_of,
    transition_stage_task,
)
from core.visual_analysis import VisualAnalysisError


class _Traceability:
    def __init__(self, requirements: dict[str, dict[str, Any]]) -> None:
        self.requirements = requirements
        self.states: dict[str, str] = {}

    def get_requirement(self, node_id: str) -> dict[str, Any] | None:
        return self.requirements.get(node_id)

    def upsert_node_state(self, node_id: str, state: str) -> None:
        self.states[node_id] = state


class _Events:
    def __init__(self) -> None:
        self.visual: list[dict[str, Any]] = []

    def record_visual_analysis(self, **payload: Any) -> None:
        self.visual.append(payload)

    def __getattr__(self, _name: str):
        def record(*_args: Any, **_kwargs: Any) -> None:
            return None

        return record


class _Git:
    def commit(self, _message: str) -> bool:
        return False


def _tree() -> dict[str, Any]:
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [
            {
                "id": "A",
                "name": "image A",
                "description": "![a](a.png)",
                "visual_reference": [{"image_path": "a.png"}],
                "children": [],
            },
            {
                "id": "B",
                "name": "image B",
                "description": "![b](b.png)",
                "visual_reference": [{"image_path": "b.png"}],
                "children": [],
            },
        ],
    }


def _manager(tmp_path: Path, tree: dict[str, Any]) -> tuple[workflow.ARCWorkflowManager, dict[str, Any]]:
    requirements_path = tmp_path / "requirements" / "requirements.yaml"
    requirements_path.parent.mkdir(parents=True)
    manager = workflow.ARCWorkflowManager(
        workspace_path=str(tmp_path / "workspace"),
        requirement_path=str(requirements_path),
        log_cb=lambda *_args, **_kwargs: None,
    )
    requirements = {}

    def walk(node: dict[str, Any]) -> None:
        requirements[node["id"]] = node
        for child in node.get("children", []) or []:
            walk(child)

    walk(tree)
    events = _Events()
    manager.runtime = SimpleNamespace(
        traceability=_Traceability(requirements),
        events=events,
        git=_Git(),
    )
    queue = load_or_create_queue(manager.queue_path, tree)
    return manager, queue


def test_visual_failure_does_not_block_an_independent_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.delenv("ARC_VISUAL_PRECOMPUTE", raising=False)
    tree = _tree()
    manager, queue = _manager(tmp_path, tree)

    async def fake_ready(**kwargs: Any) -> dict[str, Any]:
        requirement = kwargs["requirement_data"]
        if requirement["id"] == "A":
            raise VisualAnalysisError("invalid image", transient=False, image_path="a.png")
        return {**requirement, "visual_reference": [{"image_path": "b.png", "analysis": "ready"}]}

    monkeypatch.setattr(workflow, "analyze_visual_ready_references", fake_ready)

    async def run() -> None:
        await manager._prepare_visual_ready_tasks(tree, queue)
        await manager._finish_visual_ready_tasks(queue)

    asyncio.run(run())

    assert stage_status_of(queue, "A", STAGE_VISUAL_ANALYSIS) == STAGE_FAILED
    assert stage_status_of(queue, "B", STAGE_VISUAL_ANALYSIS) == STAGE_READY
    assert node_state(queue, "A") == "FAILED"
    assert node_state(queue, "B") == "UNSEEN"
    assert [event["status"] for event in manager.runtime.events.visual if event["node_id"] == "B"] == [
        "started",
        "ready",
    ]


def test_transient_visual_failure_retries_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.setenv("ARC_VISUAL_PRECOMPUTE", "1")
    monkeypatch.setattr(workflow, "VISUAL_STAGE_RETRY_BACKOFF_SECONDS", 0.0)
    tree = {"id": "A", "name": "image A", "description": "![a](a.png)", "visual_reference": [{"image_path": "a.png"}], "children": []}
    manager, queue = _manager(tmp_path, tree)
    calls = 0

    async def fake_ready(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise VisualAnalysisError("provider unavailable", transient=True, image_path="a.png")
        return {**kwargs["requirement_data"], "visual_reference": [{"image_path": "a.png", "analysis": "ready"}]}

    monkeypatch.setattr(workflow, "analyze_visual_ready_references", fake_ready)

    async def run() -> None:
        await manager._prepare_visual_ready_tasks(tree, queue)
        await manager._finish_visual_ready_tasks(queue)

    asyncio.run(run())

    assert calls == 3
    assert stage_status_of(queue, "A", STAGE_VISUAL_ANALYSIS) == STAGE_READY
    statuses = [event["status"] for event in manager.runtime.events.visual]
    assert statuses == ["started", "retry_wait", "started", "retry_wait", "started", "ready"]
    assert queue["stage_tasks"][0]["attempt_count"] == 3


def test_design_waits_for_its_own_visual_ready_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    tree = {"id": "A", "name": "image A", "description": "![a](a.png)", "visual_reference": [{"image_path": "a.png"}], "children": []}
    manager, queue = _manager(tmp_path, tree)
    order: list[str] = []

    async def fake_ready(**kwargs: Any) -> dict[str, Any]:
        order.append("visual-start")
        await asyncio.sleep(0)
        order.append("visual-ready")
        return {**kwargs["requirement_data"], "visual_reference": [{"image_path": "a.png", "analysis": "ready"}]}

    async def fake_design(_node_id: str, _requirement: dict[str, Any]) -> bool:
        order.append("design")
        return True

    monkeypatch.setattr(workflow, "analyze_visual_ready_references", fake_ready)
    manager.phase_runner = SimpleNamespace(run_design_phase=fake_design)
    task = next(item for item in queue["tasks"] if item["phase"] == PHASE_DESIGN)

    async def run() -> None:
        manager._begin_task(task, queue)
        await manager._execute_task(task, queue)

    asyncio.run(run())

    assert order == ["visual-start", "visual-ready", "design"]
    assert stage_status_of(queue, "A", STAGE_VISUAL_ANALYSIS) == STAGE_PUBLISHED
    assert queue["node_states"]["A"] == "DESIGNED"


def test_eager_visual_jobs_share_a_node_level_concurrency_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.setenv("ARC_VISUAL_PRECOMPUTE", "1")
    monkeypatch.setenv("ARC_VISUAL_PRECOMPUTE_CONCURRENCY", "1")
    tree = _tree()
    manager, queue = _manager(tmp_path, tree)
    in_flight = 0
    peak = 0

    async def fake_ready(**kwargs: Any) -> dict[str, Any]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0)
            requirement = kwargs["requirement_data"]
            return {
                **requirement,
                "visual_reference": [
                    {"image_path": f"{requirement['id']}.png", "analysis": "ready"}
                ],
            }
        finally:
            in_flight -= 1

    monkeypatch.setattr(workflow, "analyze_visual_ready_references", fake_ready)

    async def run() -> None:
        await manager._prepare_visual_ready_tasks(tree, queue)
        await manager._finish_visual_ready_tasks(queue)

    asyncio.run(run())

    assert peak == 1


def test_resume_republishes_visual_recovery_in_runner_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_STAGE_PIPELINE", "1")
    monkeypatch.setenv("ARC_VISUAL_PRECOMPUTE", "1")
    tree = {"id": "A", "name": "image A", "description": "![a](a.png)", "visual_reference": [{"image_path": "a.png"}], "children": []}
    manager, queue = _manager(tmp_path, tree)
    transition_stage_task(queue, "A", STAGE_VISUAL_ANALYSIS, "RUNNING")
    transition_stage_task(
        queue,
        "A",
        STAGE_VISUAL_ANALYSIS,
        STAGE_RETRY_WAIT,
        error="interrupted",
    )
    queue["recovered_interrupted_tasks"] = [{"node_id": "A", "phase": PHASE_DESIGN}]

    async def fake_ready(**kwargs: Any) -> dict[str, Any]:
        return {**kwargs["requirement_data"], "visual_reference": [{"image_path": "a.png", "analysis": "ready"}]}

    monkeypatch.setattr(workflow, "analyze_visual_ready_references", fake_ready)

    async def run() -> None:
        await manager._prepare_visual_ready_tasks(tree, queue)
        await manager._finish_visual_ready_tasks(queue)

    asyncio.run(run())

    statuses = [event["status"] for event in manager.runtime.events.visual]
    assert statuses == ["recovered", "started", "ready"]

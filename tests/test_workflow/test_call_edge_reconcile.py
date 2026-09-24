"""Compile-wrap-up call-edge reconciliation mount (issue #238).

DESIGN registration derives a ``cross_req`` edge only when both endpoint
contracts are already stored, so a forward reference — node A declares a
callee whose contract is designed later — leaves the edge permanently
missing unless the later side declares the reverse. The wrap-up sweep
(``ARCWorkflowManager`` → ``DesignArtifactRegistry.reconcile_call_edges``)
re-derives every stored interface's callers/callees against the final store
state. These tests pin the mount: the sweep runs on every completion point
(a fresh compile and the ``--resume`` queue-load path both funnel through
``compile_requirement_tree``), the backfill lands in the traceability store
through the public SDK API, and the action leaves an ``edge_reconcile``
runner event plus an operator-visible log line.

The drain is a no-op: task execution is covered by the faux-compile e2e and
the drain-level suites; what is under test here is the completion point
wiring, seeded with the store state a real compile's DESIGN passes leave
behind.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.workflow import ARCWorkflowManager
from tests.test_agents.conftest import arc_runtime  # noqa: F401

_TREE = {
    "id": "R",
    "name": "root",
    "description": "root",
    "children": [
        {"id": "REQ-1", "name": "caller side", "description": "declares the forward reference", "children": []},
        {"id": "REQ-2", "name": "callee side", "description": "registers its contract later", "children": []},
    ],
}


def _seed_forward_reference(runtime: Any) -> None:
    """REQ-1 designed first and declared IF-B as a callee; REQ-2's contract
    registered later and never listed IF-A back — the store state the
    registration path leaves with no cross_req edge."""
    runtime.traceability.upsert_interface(
        interface_id="IF-A",
        req_ids=["REQ-1"],
        type="FUNC",
        content="{}",
        file_path="src/a.py",
        callers=[],
        callees=["IF-B"],
    )
    runtime.traceability.upsert_interface(
        interface_id="IF-B",
        req_ids=["REQ-2"],
        type="FUNC",
        content="{}",
        file_path="src/b.py",
    )


def _make_manager(tmp_project_dir: Path, runtime: Any, logs: list[tuple]) -> ARCWorkflowManager:
    (tmp_project_dir / ".arc").mkdir(parents=True, exist_ok=True)
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_project_dir),
        requirement_path="",
        web_port=4100,
        log_cb=lambda agent, message, status=None, node_id=None: logs.append((agent, message, status)),
    )
    manager.runtime = runtime
    return manager


async def _no_drain(queue_state: dict[str, Any]) -> None:
    return None


def _edge_reconcile_events(runtime: Any) -> list[dict[str, Any]]:
    events_path = Path(runtime.paths.runner_events_path)
    if not events_path.exists():
        return []
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("type") == "edge_reconcile"
    ]


def _stored_edge(runtime: Any) -> dict[str, Any] | None:
    for edge in runtime.traceability.list_call_edges():
        if (
            edge["source_req_id"],
            edge["target_req_id"],
            edge["from_interface_id"],
            edge["to_interface_id"],
        ) == ("REQ-1", "REQ-2", "IF-A", "IF-B"):
            return edge
    return None


def test_fresh_compile_completion_backfills_forward_reference(
    tmp_project_dir, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = arc_runtime
    _seed_forward_reference(runtime)
    logs: list[tuple] = []
    manager = _make_manager(tmp_project_dir, runtime, logs)
    monkeypatch.setattr(manager, "_drain_runnable_tasks", _no_drain)

    result = asyncio.run(manager.compile_requirement_tree(dict(_TREE)))

    stored = _stored_edge(runtime)
    assert stored is not None
    assert stored["edge_type"] == "cross_req"
    events = _edge_reconcile_events(runtime)
    assert len(events) == 1
    assert events[0]["backfilled"] == [
        {
            "interface_id": "IF-A",
            "kind": "callees",
            "ref_id": "IF-B",
            "edges": [{"source_req_id": "REQ-1", "target_req_id": "REQ-2"}],
        }
    ]
    assert events[0]["unresolved"] == []
    assert any(
        "Call-edge reconcile backfilled" in message and status == "warning"
        for _agent, message, status in logs
    )
    # The reconcile is observability/repair only: it does not flip the
    # drain's verdict for the still-pending tasks.
    assert result["ok"] is False


def test_resume_completion_backfills_edges_a_crashed_run_left_behind(
    tmp_project_dir, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run killed before its wrap-up leaves both contracts registered but
    the edge missing; the ``--resume`` completion point sweeps them."""
    runtime = arc_runtime
    _seed_forward_reference(runtime)
    logs: list[tuple] = []
    manager = _make_manager(tmp_project_dir, runtime, logs)
    # What the previous (crashed) run persisted: a compatible queue file.
    queue_state = manager._load_or_create_processing_queue(dict(_TREE))
    manager._save_processing_queue(queue_state)
    monkeypatch.setattr(manager, "_drain_runnable_tasks", _no_drain)

    asyncio.run(manager.compile_requirement_tree(dict(_TREE), resume_from_queue=True))

    assert _stored_edge(runtime) is not None
    events = _edge_reconcile_events(runtime)
    assert len(events) == 1
    assert events[0]["backfilled"]

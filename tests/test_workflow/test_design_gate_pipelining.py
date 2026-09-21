"""DESIGN gate pipelining and contract drift validation (issue #83, ADR 0001 lever 2).

Two behaviours live here:

1. **Gate pipelining** (``ARC_DESIGN_GATE_PIPELINE``, default off): a
   dependent's DESIGN waits for its declared dependencies' *DESIGN* (merged,
   so the registered contracts are readable) instead of their IMPLEMENT. The
   dependent designs against the dependency's registered interface cards -
   which carry the ``implemented`` flag - and only its IMPLEMENT keeps
   waiting for the dependency's IMPLEMENT (the scenarios still read runtime
   state only the landed implementation creates). With the gate closed the
   scheduling decisions are byte-for-byte main's.

2. **Contract drift validation**: when a node's IMPLEMENT merges, the landed
   tree is compared against the contracts the node registered at DESIGN
   time. A dependency-facing contract whose registered anchor is no longer
   findable (the implementation moved or reshaped the surface a dependent
   may already be designing against) is drift. With arbitration enabled the
   drift escalates through the health-gate arbitration path (one budget per
   node, same as #81); with arbitration off - or the budget spent - the drift
   is recorded as a runner event and a warning without blocking the merge,
   and downstream TDD red lights remain the final backstop.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.contract_drift import ContractDrift, detect_contract_drift
from core.workflow import (
    ARCWorkflowManager,
    NODE_PASSED,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    _design_pipelining_enabled,
)


def _task(node_id: str, phase: str, status: str = TASK_PENDING, order: int = 0) -> dict:
    return {"task_id": f"{node_id}:{phase}", "node_id": node_id, "phase": phase, "order": order, "status": status}


def _queue(tasks: list[dict], dependencies: dict[str, list[str]] | None = None) -> dict:
    queue: dict = {"tasks": tasks, "descendants": {}}
    if dependencies is not None:
        queue["dependencies"] = dependencies
    return queue


# ---------------------------------------------------------------------------
# env gate
# ---------------------------------------------------------------------------


def test_design_pipelining_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_DESIGN_GATE_PIPELINE", raising=False)
    assert _design_pipelining_enabled() is False

    for raw in ("", "0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", raw)
        assert _design_pipelining_enabled() is False, raw

    for raw in ("1", "true", "yes", "on", "ON", "True"):
        monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", raw)
        assert _design_pipelining_enabled() is True, raw


# ---------------------------------------------------------------------------
# gate closed: scheduling is byte-for-byte main
# ---------------------------------------------------------------------------


def test_gate_closed_design_still_waits_for_dependency_implement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin for the default: with the pipeline gate closed, a dependent's
    DESIGN stays blocked until the dependency's IMPLEMENT completes (the
    run8 serial semantics PR #38 installed)."""
    monkeypatch.delenv("ARC_DESIGN_GATE_PIPELINE", raising=False)
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 1),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 2),
        ],
        dependencies={"RB": ["RA"]},
    )

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_gate_closed_implement_gate_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_DESIGN_GATE_PIPELINE", raising=False)
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_COMPLETED, 1),
            _task("RB", PHASE_DESIGN, TASK_COMPLETED, 2),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 3),
        ],
        dependencies={"RB": ["RA"]},
    )
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][3]) is True

    queue["tasks"][1]["status"] = TASK_FAILED
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][3]) is False


# ---------------------------------------------------------------------------
# gate open: DESIGN waits for dependency DESIGN only
# ---------------------------------------------------------------------------


def test_gate_open_design_starts_while_dependency_implement_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deliverable of this ticket: in pipeline mode B's DESIGN starts
    while A's IMPLEMENT is still in flight (A's DESIGN has completed and
    merged, so its registered contracts are already readable)."""
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 1),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 2),
        ],
        dependencies={"RB": ["RA"]},
    )

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is True


def test_gate_open_design_still_waits_for_dependency_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_RUNNING, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 2),
        ],
        dependencies={"RB": ["RA"]},
    )

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_gate_open_design_blocked_by_failed_dependency_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dependency whose DESIGN failed never exposed a usable contract
    baseline, so the dependent must not design against nothing."""
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_FAILED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 2),
        ],
        dependencies={"RB": ["RA"]},
    )

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False


def test_gate_open_design_blocked_when_dependency_design_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unknown-dependency rule survives the relaxation: a dependency
    without a DESIGN task means the queue is inconsistent with its tree, so
    block rather than design against an unknown baseline."""
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    queue = _queue(
        [_task("RB", PHASE_DESIGN, TASK_PENDING, 0)],
        dependencies={"RB": ["RA"]},
    )

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][0]) is False


def test_gate_open_implement_still_waits_for_dependency_implement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the DESIGN gate relaxes: the dependent's scenarios still read
    runtime state (accounts, routes, orders) that only the dependency's
    landed implementation creates, so its IMPLEMENT keeps waiting."""
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 1),
            _task("RB", PHASE_DESIGN, TASK_COMPLETED, 2),
            _task("RB", PHASE_IMPLEMENT, TASK_PENDING, 3),
        ],
        dependencies={"RB": ["RA"]},
    )

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][3]) is False


def test_gate_open_multiple_dependencies_all_designs_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    queue = _queue(
        [
            _task("RA", PHASE_DESIGN, TASK_COMPLETED, 0),
            _task("RA", PHASE_IMPLEMENT, TASK_RUNNING, 1),
            _task("RC", PHASE_DESIGN, TASK_RUNNING, 2),
            _task("RC", PHASE_IMPLEMENT, TASK_PENDING, 3),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 4),
        ],
        dependencies={"RB": ["RA", "RC"]},
    )

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][4]) is False

    queue["tasks"][2]["status"] = TASK_COMPLETED
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][4]) is True


def test_gate_open_parent_design_rule_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent-serial DESIGN rule (children design against the parent's
    merged shell) is independent of the dependency gate and stays as-is in
    both modes."""
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    queue = _queue(
        [
            _task("R", PHASE_DESIGN, TASK_RUNNING, 0),
            _task("R", PHASE_IMPLEMENT, TASK_PENDING, 1),
            _task("RB", PHASE_DESIGN, TASK_PENDING, 2),
        ],
    )
    queue["parents"] = {"RB": "R"}

    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is False

    queue["tasks"][0]["status"] = TASK_COMPLETED
    assert ARCWorkflowManager._task_dependencies_met(queue, queue["tasks"][2]) is True


# ---------------------------------------------------------------------------
# cycle health under the relaxed edge
# ---------------------------------------------------------------------------


def test_cycle_breaking_uses_the_pipelined_edge_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cycle check must reflect the edge the gate will actually enforce:
    pipelined mode schedules ``D:dependency -> D:dependent``, so an edge that
    closes a cycle only through DESIGN vertices must be dropped there too.
    Main's IMPLEMENT-vertex edge would miss a DESIGN-only cycle and the drain
    would deadlock."""
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    # B depends on C; C depends on A; A is B's parent's... no - keep it
    # sibling-shaped: B -> C and C -> B close a plain declared cycle.
    kept, dropped = ARCWorkflowManager._break_dependency_cycles({"RB": ["RC"], "RC": ["RB"]})

    assert len(kept) == 1 and len(dropped) == 1
    assert _is_acyclic(kept)


def test_cycle_breaking_catches_the_pipelined_design_vertex_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadlock shape pipelining re-exposes: A depends on C, C depends on
    B, B is A's child. The enforced DESIGN edges close a pure DESIGN-vertex
    cycle (D:A waits D:C waits D:B waits D:A structurally), which only exists
    because the pipelined DESIGN gate sequences declared dependencies. The
    checker must still drop an edge of that cycle so the drain ends with
    reported task states instead of stalling.

    Soundness argument (why the historical ``I:dep -> D:dependent`` checker
    edge still covers the pipelined ``D:dep -> D:dependent`` edge): the
    checker edge implies both pipelined edges through the structural
    design-before-implement edges, so every cycle among enforced pipelined
    edges is also a cycle among checker edges; dropping edges until the
    checker graph is acyclic therefore leaves the enforced graph acyclic in
    both modes."""
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    parents = {"B": "A", "C": "R", "A": "R"}
    structural = ARCWorkflowManager._structural_precedence_edges(parents, ["R", "A", "B", "C"])
    # A depends on C (kept first: D:C -> D:A is not yet reachable), then
    # C depends on B - whose source D:C now reaches D:B through the kept
    # edge plus the structural parent edge D:A -> D:B.
    kept, dropped = ARCWorkflowManager._break_dependency_cycles(
        {"A": ["C"], "C": ["B"]}, structural
    )

    assert kept == {"A": ["C"]}
    assert dropped == [("C", "B")]
    assert _is_acyclic(kept)


def _reaches(graph: dict, start: str, goal: str, seen: frozenset = frozenset()) -> bool:
    if start == goal:
        return True
    if start in seen:
        return False
    return any(_reaches(graph, nxt, goal, seen | {start}) for nxt in graph.get(start, ()))


def _is_acyclic(graph: dict) -> bool:
    return not any(
        _reaches(graph, dependency_id, dependent_id)
        for dependent_id, dependency_ids in graph.items()
        for dependency_id in dependency_ids
    )


# ---------------------------------------------------------------------------
# contract drift validation (pure function)
# ---------------------------------------------------------------------------


def _registered(interface_id: str = "RA-FUNC-Auth", file_path: str = "backend/src/features/auth.js") -> dict:
    return {
        "interface_id": interface_id,
        "type": "FUNC",
        "file_path": file_path,
        "first_line": "export const auth",
        "content": json.dumps(
            {"responsibility": "Owns the auth surface.", "specification": "register/login/logout"}
        ),
    }


def test_drift_undetected_when_registered_anchor_lands(tmp_path: Path) -> None:
    """The green path: the implementation kept the registered anchor (same
    file, the first_line text still present), so nothing is drift."""
    registered = [_registered()]
    landed = tmp_path / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True)
    landed.write_text("// implementation\nexport const auth = { done: true };\n", encoding="utf-8")

    assert detect_contract_drift(registered, workspace_root=str(tmp_path)) == []


def test_drift_detected_when_implementation_moved_the_surface(tmp_path: Path) -> None:
    """A dependency-facing contract whose registered anchor is gone from the
    merged tree: the implementation moved the surface (different path), so
    dependents that designed against the registered card are now pointing at
    nothing. This is the drift this ticket must catch."""
    registered = [_registered()]
    moved = tmp_path / "backend" / "src" / "features" / "authService.js"
    moved.parent.mkdir(parents=True)
    moved.write_text("// implementation elsewhere\nexport const auth = { done: true };\n", encoding="utf-8")

    drift = detect_contract_drift(registered, workspace_root=str(tmp_path))

    assert len(drift) == 1
    assert drift[0].interface_id == "RA-FUNC-Auth"
    assert drift[0].reason == "anchor-file-missing"


def test_drift_detected_when_anchor_line_disappeared(tmp_path: Path) -> None:
    """Same file, but the registered first_line anchor text is gone: the
    implementation reshaped the surface (renamed the export a dependent's
    design references)."""
    registered = [_registered()]
    landed = tmp_path / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True)
    landed.write_text("// implementation reshaped\nexport const session = {};\n", encoding="utf-8")

    drift = detect_contract_drift(registered, workspace_root=str(tmp_path))

    assert len(drift) == 1
    assert drift[0].reason == "anchor-line-missing"


def test_drift_skips_implemented_contracts_without_anchors(tmp_path: Path) -> None:
    """Contracts registered without a file anchor (reused foreign rows) carry
    no mechanical expectation; only anchored rows can drift."""
    registered = [_registered()]
    registered[0]["file_path"] = ""
    (tmp_path / "empty").mkdir()
    assert detect_contract_drift(registered, workspace_root=str(tmp_path)) == []


def test_drift_tolerates_unreadable_workspace(tmp_path: Path) -> None:
    """A missing workspace root reads as an empty tree: every anchored
    contract drifts, never raises - the check is a guard, not a gate."""
    registered = [_registered()]
    drift = detect_contract_drift(registered, workspace_root=str(tmp_path / "nope"))

    assert len(drift) == 1
    assert drift[0].reason == "anchor-file-missing"


def test_drift_payload_is_json_serializable_for_runner_events(tmp_path: Path) -> None:
    registered = [_registered()]
    drift = detect_contract_drift(registered, workspace_root=str(tmp_path))

    payload = [item.to_payload() for item in drift]
    assert json.loads(json.dumps(payload))[0]["interface_id"] == "RA-FUNC-Auth"
    assert "file_path" in payload[0]


def test_drift_checks_each_contract_independently(tmp_path: Path) -> None:
    """Multiple anchored contracts: one landed, one moved - only the moved
    one reports drift, with its own interface_id."""
    landed = tmp_path / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True)
    landed.write_text("export const auth = {};\n", encoding="utf-8")
    registered = [
        _registered(),
        _registered(interface_id="RA-FUNC-Label", file_path="backend/src/features/label.js"),
    ]

    drift = detect_contract_drift(registered, workspace_root=str(tmp_path))

    assert [item.interface_id for item in drift] == ["RA-FUNC-Label"]


# ---------------------------------------------------------------------------
# drain-level scheduling order (real git worktrees, stubbed phase execution)
# ---------------------------------------------------------------------------


class _Traceability:
    def __init__(self, node_ids: list[str]) -> None:
        self.requirements = {
            node_id: {"id": node_id, "name": node_id, "description": "req"}
            for node_id in node_ids
        }
        self.states: dict[str, str] = {}
        self.cleared_design_artifacts: list[str] = []
        self.reset_test_statuses: list[str] = []
        self.interfaces: list[dict[str, Any]] = []

    def get_requirement(self, node_id: str) -> dict[str, Any] | None:
        return self.requirements.get(node_id)

    def upsert_node_state(self, node_id: str, state: str) -> None:
        self.states[node_id] = state

    def clear_node_design_artifacts(self, node_id: str) -> None:
        self.cleared_design_artifacts.append(node_id)

    def reset_test_pass_statuses_for_requirement(self, node_id: str) -> None:
        self.reset_test_statuses.append(node_id)

    def list_interfaces(self, req_id: str | None = None) -> list[dict[str, Any]]:
        if req_id is None:
            return list(self.interfaces)
        return [row for row in self.interfaces if req_id in row.get("req_ids", [])]


class _Events:
    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            return None

        return record


def _make_drain_manager(tmp_path: Path, node_ids: list[str], with_events_file: bool = False) -> ARCWorkflowManager:
    """Manager over a real git workspace with a stubbed runtime.

    Stub boundary (same shape as test_parallel_worktree_drain's helper): the
    drain, scheduler, worktrees and merges are real; ``runtime.traceability``
    and ``runtime.events`` are stubs, so anything reading through them (drift
    checks, event emission) needs either the stub to carry that surface
    (``_Traceability.interfaces``) or ``with_events_file=True`` for a real
    ``runner_events.jsonl`` at ``runtime.paths``. The full real-runtime path
    is covered by the keep-req2 faux e2e; anything beyond these two surfaces
    belongs there, not here.
    """
    from types import SimpleNamespace

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    import subprocess

    for args in (["init", "-q"], ["config", "user.email", "test@example.com"], ["config", "user.name", "test"]):
        subprocess.run(["git", *args], cwd=str(workspace), check=True, capture_output=True)
    (workspace / ".gitignore").write_text(
        "# >>> arcbench-agent-runtime >>>\n.arc/*\n!.arc/traceability/\n!.arc/traceability/**\n# <<< arcbench-agent-runtime <<<\n",
        encoding="utf-8",
    )
    (workspace / "backend").mkdir()
    (workspace / "frontend").mkdir()
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(workspace), check=True, capture_output=True)

    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *args, **kwargs: None,
    )
    runtime: Any = SimpleNamespace(
        traceability=_Traceability(node_ids),
        events=_Events(),
    )
    if with_events_file:
        events_path = workspace / ".arc" / "runner-events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.touch()
        runtime.paths = SimpleNamespace(runner_events_path=events_path)
    manager.runtime = runtime
    return manager


def _dependency_tree() -> dict[str, Any]:
    return {
        "id": "R",
        "name": "root",
        "description": "root",
        "children": [
            {"id": "RA", "name": "dependency", "description": "a", "children": []},
            {"id": "RB", "name": "dependent", "description": "b", "dependencies": ["RA"], "children": []},
        ],
    }


def test_pipeline_drain_starts_dependent_design_before_dependency_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket's deliverable at the drain level: with the gate open, RB's
    DESIGN starts while RA's IMPLEMENT is still executing (its DESIGN already
    completed and merged, so the registered cards are readable); RA's
    IMPLEMENT keeps gating RB's IMPLEMENT."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"])
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())

    events: list[tuple[str, str]] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        events.append(("start", task["task_id"]))
        await asyncio.sleep(0.05)
        events.append(("end", task["task_id"]))
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert events.index(("start", "RB:DESIGN")) < events.index(("end", "RA:IMPLEMENT")), (
        "pipelined mode: the dependent's DESIGN must not wait for the dependency's IMPLEMENT"
    )
    assert events.index(("start", "RB:IMPLEMENT")) > events.index(("end", "RA:IMPLEMENT")), (
        "the dependent's IMPLEMENT still waits for the dependency's IMPLEMENT"
    )


def test_closed_gate_drain_keeps_the_serial_design_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default pin at the drain level: with the gate closed, RB's DESIGN
    starts only after RA's IMPLEMENT finished and merged (the run8 semantics
    the existing parallel-drain tests were written against)."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.delenv("ARC_DESIGN_GATE_PIPELINE", raising=False)
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "2")
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"])
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())

    events: list[tuple[str, str]] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        events.append(("start", task["task_id"]))
        await asyncio.sleep(0.02)
        events.append(("end", task["task_id"]))
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert events.index(("start", "RB:DESIGN")) > events.index(("end", "RA:IMPLEMENT"))


def test_pipeline_mode_implementation_drift_records_event_and_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drift contract on the closed path: after an IMPLEMENT merges, the
    registered anchor check runs; drift is recorded as a runner event and a
    warning without failing the node (downstream TDD is the backstop)."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.delenv("ARC_MERGE_ARBITRATION", raising=False)
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"], with_events_file=True)
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())
    # RA registered one anchored contract at DESIGN time; its IMPLEMENT then
    # landed the file without the anchor line (drift).
    manager.runtime.traceability.interfaces = [
        {
            "interface_id": "RA-FUNC-Auth",
            "req_ids": ["RA"],
            "type": "FUNC",
            "file_path": "backend/src/features/auth.js",
            "first_line": "export const auth",
            "implemented": False,
        }
    ]
    landed = Path(manager.workspace_path) / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True, exist_ok=True)
    landed.write_text("// implementation without the registered anchor\nexport const session = {};\n", encoding="utf-8")

    warnings: list[str] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        # RA's IMPLEMENT lands the drifted file through the merge (the stub
        # commit already carries it via the workspace write above).
        return True

    async def fake_log(source: str, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if status == "warning":
            warnings.append(message)

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    monkeypatch.setattr(manager, "_log", fake_log)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    # The audit record landed in the real runner-events stream (the drain's
    # event emitter is the runtime's, not a stub here).
    events_path = Path(manager.workspace_path) / ".arc" / "runner-events.jsonl"
    drift_events = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "contract_drift":
                drift_events.append(payload)
    assert len(drift_events) == 1, "one drift record per drifted IMPLEMENT merge"
    assert drift_events[0]["node_id"] == "RA"
    assert drift_events[0]["drift"][0]["interface_id"] == "RA-FUNC-Auth"
    assert drift_events[0]["arbitration"] is False
    assert any("Contract drift" in message for message in warnings)
    # The guard is not a gate: the node still passed.
    assert queue_state["node_states"]["RA"] == NODE_PASSED


def test_pipeline_mode_arbitration_on_drift_runs_when_budget_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ARC_MERGE_ARBITRATION on and the node's arbitration budget
    unspent, contract drift escalates through the arbitration path: the
    arbiter runs once, the drift file set is its only edit surface, and the
    repaired tree re-verifies against the registered anchors."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.setenv("ARC_MERGE_ARBITRATION", "1")
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"], with_events_file=True)
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())
    manager.runtime.traceability.interfaces = [
        {
            "interface_id": "RA-FUNC-Auth",
            "req_ids": ["RA"],
            "type": "FUNC",
            "file_path": "backend/src/features/auth.js",
            "first_line": "export const auth",
            "implemented": False,
        }
    ]
    landed = Path(manager.workspace_path) / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True, exist_ok=True)
    landed.write_text("export const session = {};\n", encoding="utf-8")

    arbiter_calls: list[list[str]] = []

    async def fake_arbitrate(
        node_id: str, drift: list[ContractDrift], drift_paths: list[str]
    ) -> bool:
        arbiter_calls.append(drift_paths)
        # The repair restores the registered anchor in the drifted file.
        for path in drift_paths:
            target = Path(manager.workspace_path) / path
            target.write_text("export const auth = { done: true };\n", encoding="utf-8")
        return True

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    monkeypatch.setattr(manager, "_arbitrate_contract_drift", fake_arbitrate)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert arbiter_calls == [["backend/src/features/auth.js"]], (
        "drift escalates to exactly one arbitration over the drift file set"
    )
    assert queue_state["node_states"]["RA"] == NODE_PASSED
    # The budget bookkeeping itself is exercised by the spent-budget test
    # below (it runs the real gate path); mocking the model call here means
    # the budget stamp inside it cannot be asserted from this test.


def test_pipeline_mode_arbitration_skipped_when_budget_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #81 budget contract applied to drift: a node whose single
    arbitration budget is already spent (an earlier merge arbitration) gets
    the warning-and-continue path, never a second model call."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.setenv("ARC_MERGE_ARBITRATION", "1")
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"], with_events_file=True)
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())
    manager.runtime.traceability.interfaces = [
        {
            "interface_id": "RA-FUNC-Auth",
            "req_ids": ["RA"],
            "type": "FUNC",
            "file_path": "backend/src/features/auth.js",
            "first_line": "export const auth",
            "implemented": False,
        }
    ]
    landed = Path(manager.workspace_path) / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True, exist_ok=True)
    landed.write_text("export const session = {};\n", encoding="utf-8")

    from core import sessions
    from core.merge_arbitration import merge_arbitration_budget_key

    sessions.merge_node_session("RA", {merge_arbitration_budget_key(): True})

    arbiter_calls: list[Any] = []

    async def fake_arbitrate(node_id: str, drift: list[ContractDrift], drift_paths: list[str]) -> bool:
        arbiter_calls.append(drift_paths)
        return True

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        return True

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    monkeypatch.setattr(manager, "_arbitrate_contract_drift", fake_arbitrate)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert arbiter_calls == [], "a spent budget never buys a second arbitration"
    assert queue_state["node_states"]["RA"] == NODE_PASSED


def test_pipeline_mode_arbitration_repair_commits_restored_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real arbitration chain on drift: detect -> escalate -> the arbiter
    (model faked at the adapter seam) rewrites the drifted file -> the repair
    is committed on the integration branch -> the budget is spent in the node
    session. This exercises _arbitrate_contract_drift itself, not a stub."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.setenv("ARC_MERGE_ARBITRATION", "1")
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"], with_events_file=True)
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())
    manager.runtime.traceability.interfaces = [
        {
            "interface_id": "RA-FUNC-Auth",
            "req_ids": ["RA"],
            "type": "FUNC",
            "file_path": "backend/src/features/auth.js",
            "first_line": "export const auth",
            "implemented": False,
        }
    ]
    landed = Path(manager.workspace_path) / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True, exist_ok=True)
    landed.write_text("export const session = {};\n", encoding="utf-8")

    from core import sessions
    from core.config import get_workspace_root, set_workspace_root
    from core.merge_arbitration import merge_arbitration_budget_key

    # The session store (arbitration budget) is keyed by the process-wide
    # workspace root; point it at this test's workspace for the drain and
    # restore it after, so later tests in the same process are unaffected.
    original_root = get_workspace_root()
    set_workspace_root(manager.workspace_path)
    try:

        class _RepairModel:
            """Stands in for the arbitration model: returns the anchor-restoring
            content for every drift file (the response shape MergeArbiter parses
            is a JSON object mapping path -> full content)."""

            async def ainvoke(self, messages: Any) -> Any:
                from langchain_core.messages import AIMessage

                return AIMessage(
                    content=json.dumps(
                        {"backend/src/features/auth.js": "export const auth = { done: true };\n"}
                    )
                )

        monkeypatch.setattr(manager, "_build_arbitration_model", lambda: _RepairModel())

        async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
            return True

        monkeypatch.setattr(manager, "_run_task", fake_run_task)
        asyncio.run(manager._drain_runnable_tasks(queue_state))
    finally:
        set_workspace_root(original_root)

    # The repair landed and committed: the file honors the anchor, the budget
    # is spent, and the node still passed.
    assert "export const auth" in landed.read_text(encoding="utf-8")
    assert sessions.load_node_session("RA").get(merge_arbitration_budget_key()) is True
    assert queue_state["node_states"]["RA"] == NODE_PASSED


def test_pipeline_mode_arbitration_idempotent_repair_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted arbitration whose rewrite is byte-identical to the tree
    (the anchors were already honored, e.g. a sibling's merge restored them)
    has nothing to commit: that is a repaired outcome, not a failure - the
    review follow-up for the empty-commit/no-change misreport."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.setenv("ARC_MERGE_ARBITRATION", "1")
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"], with_events_file=True)
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())
    # The registered anchor IS on disk already (no actual drift), but the
    # drift detector fired before the sibling's content landed through the
    # merge - simulate by pointing the detector at a contract whose anchor
    # exists, then having the arbiter rewrite it with identical bytes.
    manager.runtime.traceability.interfaces = [
        {
            "interface_id": "RA-FUNC-Auth",
            "req_ids": ["RA"],
            "type": "FUNC",
            "file_path": "backend/src/features/auth.js",
            "first_line": "export const auth",
            "implemented": False,
        }
    ]
    landed = Path(manager.workspace_path) / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True, exist_ok=True)
    landed.write_text("export const auth = { done: true };\n", encoding="utf-8")

    # The anchor check passes on disk, so _check_contract_drift would not
    # escalate; exercise the commit tail of _arbitrate_contract_drift
    # directly with an accepted-but-identical rewrite.
    from core.contract_drift import ContractDrift as _Drift

    drift_item = _Drift(
        interface_id="RA-FUNC-Auth",
        file_path="backend/src/features/auth.js",
        first_line="export const auth",
        reason="anchor-line-missing",
    )

    class _IdentityModel:
        async def ainvoke(self, messages: Any) -> Any:
            from langchain_core.messages import AIMessage

            return AIMessage(
                content=json.dumps(
                    {"backend/src/features/auth.js": "export const auth = { done: true };\n"}
                )
            )

    monkeypatch.setattr(manager, "_build_arbitration_model", lambda: _IdentityModel())

    repaired = asyncio.run(
        manager._arbitrate_contract_drift(
            "RA", [drift_item], ["backend/src/features/auth.js"]
        )
    )

    assert repaired is True, (
        "an accepted arbitration with nothing left to change is a successful repair"
    )


def test_drift_check_skips_without_failing_the_merge_when_the_store_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's best-effort contract: a traceability store that cannot be
    read (an older queue resumed, a store without the interfaces surface)
    skips the drift check with a warning and never fails the merged node -
    the review follow-up for the unreadable-store branch, which had no
    direct test."""
    monkeypatch.setenv("ARC_NODE_WORKTREES", "1")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    monkeypatch.setenv("ARC_MAX_CONCURRENT_TASKS", "1")
    monkeypatch.delenv("ARC_MERGE_ARBITRATION", raising=False)
    manager = _make_drain_manager(tmp_path, ["R", "RA", "RB"], with_events_file=True)
    queue_state = manager._load_or_create_processing_queue(_dependency_tree())
    # The anchored contract is on disk and healthy, but the store raises on
    # every read: the check must skip, not break the merge.
    landed = Path(manager.workspace_path) / "backend" / "src" / "features" / "auth.js"
    landed.parent.mkdir(parents=True, exist_ok=True)
    landed.write_text("export const auth = { done: true };\n", encoding="utf-8")

    def broken_list_interfaces(req_id: str | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("simulated unreadable store")

    manager.runtime.traceability.list_interfaces = broken_list_interfaces

    warnings: list[str] = []

    async def fake_run_task(task: dict[str, Any], ctx: Any = None) -> bool:
        return True

    async def fake_log(source: str, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if status == "warning":
            warnings.append(message)

    monkeypatch.setattr(manager, "_run_task", fake_run_task)
    monkeypatch.setattr(manager, "_log", fake_log)
    asyncio.run(manager._drain_runnable_tasks(queue_state))

    assert any("Contract drift check" in message and "skipped" in message for message in warnings)
    assert queue_state["node_states"]["RA"] == NODE_PASSED, (
        "an unreadable store must never turn a merged IMPLEMENT into a failure"
    )


def test_pipeline_mode_implemented_flags_reach_the_dependent_context(
    tmp_path: Path,
) -> None:
    """The context half of the contract: interface cards read by a dependent's
    DESIGN carry the ``implemented`` flag, so a pipelined dependent can tell a
    designed-but-not-landed surface from a landed one. The flag is threaded by
    the context pipeline (main since the cross-node card mechanism landed);
    this test pins the store round-trip it reads from."""
    from arcbench_agent_runtime.context import RuntimePaths
    from arcbench_agent_runtime.events import EventClient
    from arcbench_agent_runtime.traceability import TraceabilityStore

    paths = RuntimePaths.from_env(project_dir=str(tmp_path))
    store = TraceabilityStore(paths, EventClient(paths))
    store.init_db(reset=True)
    store.upsert_interface(
        interface_id="RA-FUNC-Auth",
        req_ids=["RA"],
        type="FUNC",
        content="{}",
        file_path="backend/src/features/auth.js",
        first_line="export const auth",
        implemented=False,
    )
    cards = store.list_interfaces(req_id="RA")
    assert cards[0]["implemented"] is False, "a designed-but-unimplemented contract is distinguishable"
    store.set_interface_implemented("RA-FUNC-Auth", True)
    cards = store.list_interfaces(req_id="RA")
    assert cards[0]["implemented"] is True

"""DESIGN phase survives typeless interface records via the backfill ladder.

Issue #230 (serial-5): a non-leaf shell pass returned 11 interface records
with no `type` field; the registry raised on the first entry with no backfill
source, REQ-2:IMPLEMENT never ran, and the whole round was judged failed.
The phase-level contract now reads: the registry resolves `type` through its
backfill ladder before raising, the DESIGN adapter gets exactly one targeted
repair ask for what the ladder cannot resolve, and id-less records — always
dropped — surface as an observable warning instead of a silent hole.

The two full-chain tests drive the REAL InterfaceDesigner (faux model) through
``run_design_phase`` so the acceptance shape is pinned end to end: 11 typeless
records -> repair ask -> ladder backfill -> contracts landed in the
traceability tables; and repair-fail -> registry judgment -> phase False.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.interface_designer import InterfaceDesigner
from core.phases import WorkflowPhaseRunner
from tests.helpers.design_type_backfill import (
    STORED_PARENT_CONTRACTS,
    seed_stored_parent_contracts,
    typed_incident_rows,
)
from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call

# Reuse the process-wide runtime fixture so WorkflowPhaseRunner.traceability,
# core.sessions and context_pipeline all resolve inside tmp_project_dir.
from tests.test_agents.conftest import arc_runtime  # noqa: F401


class _StubDesigner:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def run(self, node_id: str, requirement_data: dict) -> dict:
        return self.payload


class _StubGenerator:
    def __init__(self) -> None:
        self.app_handler = None

    async def run(self, node_id: str, requirement_data: dict) -> tuple:
        return (None, "")


class _StubTDD:
    def __init__(self) -> None:
        self.app_handler = None


def _make_runner(tmp_project_dir: Path, payload: dict):
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    requirements_dir = tmp_project_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=_StubDesigner(payload),
        test_generator=_StubGenerator(),
        test_driven_developer=_StubTDD(),
        log_cb=log_cb,
    )
    return runner, logs


def _make_real_designer_runner(tmp_project_dir: Path, model: FauxChatModel):
    """A runner with the REAL InterfaceDesigner driven by a scripted model —
    the full serial-5 chain (repair ask inside designer.run, ladder judgment
    in prepare_interfaces) in one test."""

    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    requirements_dir = tmp_project_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=InterfaceDesigner(
            log_cb,
            model=model,
            workspace_root=str(tmp_project_dir),
            requirement_path=str(requirements_dir / "req.md"),
            app_type="web",
        ),
        test_generator=_StubGenerator(),
        test_driven_developer=_StubTDD(),
        log_cb=log_cb,
    )
    return runner, logs


def _seed_shell_requirement(runtime, node_id: str) -> None:
    """A non-leaf WITH visual references, so DESIGN runs instead of skipping —
    the exact node shape the serial-5 incident fired on."""
    runtime.traceability.store_requirement_tree(
        {
            "id": node_id,
            "name": "Auth Shell",
            "description": "Authentication composition node",
            "visual_reference": ["screens/login.png"],
            "children": [
                {"id": f"{node_id}-CHILD", "name": "Child feature", "description": "Leaf"}
            ],
        }
    )


def _typeless_row(interface_id: str, path: str, **extra) -> dict:
    row = {"interface_id": interface_id, "file_path": path}
    row.update(extra)
    return row


def _backfillable_rows(node_id: str) -> list[dict]:
    """Every serial-5 record EXCEPT the one unbackfillable orphan: reused ids
    with stored rows, plus prefixed new ids the ladder infers."""
    rows = [
        _typeless_row(interface_id, path, relation="update")
        for interface_id, path, _ in STORED_PARENT_CONTRACTS
    ]
    rows.append(_typeless_row(f"{node_id}-UI-LoginPage", "frontend/src/pages/LoginPage.tsx"))
    rows.append(_typeless_row(f"{node_id}-API-AuthApi", "frontend/src/features/auth/authApi.ts"))
    return rows


def _typeless_payload(node_id: str) -> dict:
    return {"summary": "Shell wired.", "interfaces": _backfillable_rows(node_id), "files_written": []}


def _full_incident_rows(node_id: str) -> list[dict]:
    """The complete serial-5 shape: 11 typeless records, the last one with no
    backfill source at all (no stored row, no id type segment)."""
    return [
        *_backfillable_rows(node_id),
        _typeless_row(f"{node_id}-SeedService", "backend/src/database/seed_accounts.js"),
    ]


def test_design_phase_backfills_missing_types_and_lands_contracts(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-S5-A"
    _seed_shell_requirement(arc_runtime, node_id)
    seed_stored_parent_contracts(arc_runtime.traceability)
    runner, logs = _make_runner(tmp_project_dir, _typeless_payload(node_id))

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Auth Shell", "description": "Shell"})
    )

    assert ok is True
    errors = [entry[1] for entry in logs if entry[2] == "error"]
    assert not any("invalid `type`" in message for message in errors)
    # The contracts landed in the traceability tables with backfilled types.
    store = arc_runtime.traceability
    assert store.get_interface(f"{node_id}-UI-LoginPage")["type"] == "UI"
    assert store.get_interface(f"{node_id}-API-AuthApi")["type"] == "API"
    stored_shell = store.get_interface("ROOT-UI-AppShell")
    assert stored_shell["type"] == "UI"
    assert node_id in stored_shell["req_ids"]


def test_design_phase_typeless_tool_call_is_fixed_in_session_and_lands_all_records(
    tmp_project_dir, arc_runtime
) -> None:
    """The full-chain replay under the tightened schema (#233): the typeless
    structured tool call is rejected at decode, the in-session retry delivers
    every record typed, and all eleven contracts land without spending a
    repair ask."""

    node_id = "REQ-S5-D"
    _seed_shell_requirement(arc_runtime, node_id)
    seed_stored_parent_contracts(arc_runtime.traceability)
    orphan_id = f"{node_id}-SeedService"
    typed_rows = typed_incident_rows(_full_incident_rows(node_id))
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Shell wired.", "interfaces": _full_incident_rows(node_id), "files_written": []},
                call_id="typeless",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Shell wired.", "interfaces": typed_rows, "files_written": []},
                call_id="typed-retry",
            ),
        ]
    )
    runner, logs = _make_real_designer_runner(tmp_project_dir, model)

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Auth Shell", "description": "Shell"})
    )

    assert ok is True
    assert model.call_count == 2  # typeless turn + the decode-retry turn
    store = arc_runtime.traceability
    # All 11 records carry the node and landed with resolved types.
    assert len(store.list_interfaces(req_id=node_id)) == 11
    assert store.get_interface("ROOT-UI-AppShell")["type"] == "UI"
    assert store.get_interface(f"{node_id}-UI-LoginPage")["type"] == "UI"
    assert store.get_interface(f"{node_id}-API-AuthApi")["type"] == "API"
    assert store.get_interface(orphan_id)["type"] == "FUNC"
    errors = [entry[1] for entry in logs if entry[2] == "error"]
    assert not any("invalid `type`" in message for message in errors)


def test_design_phase_recovery_channel_rows_still_reach_the_repair_ask_and_judgment(
    tmp_project_dir, arc_runtime
) -> None:
    """The second direction of the repair bargain, via the decode-bypassing
    recovery channel: a bare-JSON final message hands typeless rows to the
    ladder, the orphan gets its one repair ask, a typeless-decode answer
    exhausts the queue, and the registry's judgment fails DESIGN."""

    node_id = "REQ-S5-E"
    _seed_shell_requirement(arc_runtime, node_id)
    seed_stored_parent_contracts(arc_runtime.traceability)
    orphan_id = f"{node_id}-SeedService"
    raw_json_answer = json.dumps(
        {
            "summary": "Shell wired.",
            "interfaces": _full_incident_rows(node_id),
            "files_written": ["frontend/src/pages/LoginPage.tsx"],
        },
        ensure_ascii=False,
    )
    model = FauxChatModel(
        responses=[
            faux_text(raw_json_answer),
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Tried.", "interfaces": [{"interface_id": orphan_id, "type": "SCHEDULE"}], "files_written": []},
                call_id="repair",
            ),
        ]
    )
    runner, logs = _make_real_designer_runner(tmp_project_dir, model)

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Auth Shell", "description": "Shell"})
    )

    assert ok is False
    # main(1) + the repair ask's stream attempt: tool call(2), decode-retry(3)
    # exhausting the queue. A stream-side failure after that work is terminal;
    # the wrapper must not replay the full agent session.
    assert model.call_count == 3
    errors = [entry[1] for entry in logs if entry[2] == "error"]
    assert any("invalid `type`" in message and orphan_id in message for message in errors)
    assert any("agent stream failed" in message for message in errors)


def test_design_phase_still_fails_when_no_backfill_source(tmp_project_dir, arc_runtime) -> None:
    """Ladder exhausted with nothing resolved: the registry's judgment stands
    (the adapter's repair ask is the only channel before this, and it cannot
    help an id the model never typed and no source covers)."""

    node_id = "REQ-S5-B"
    _seed_shell_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Shell wired.",
            "interfaces": [{"interface_id": "IF-ORPHAN", "file_path": "src/orphan.py"}],
            "files_written": [],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Auth Shell", "description": "Shell"})
    )

    assert ok is False
    errors = [entry[1] for entry in logs if entry[2] == "error"]
    assert any("invalid `type`" in message for message in errors)


def test_design_phase_warns_on_dropped_idless_interface_entries(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-S5-C"
    _seed_shell_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Shell wired.",
            "interfaces": [
                {"type": "UI", "file_path": "frontend/src/pages/Ghost.tsx", "name": "Ghost"},
                {"interface_id": f"{node_id}-UI-LoginPage", "file_path": "frontend/src/pages/LoginPage.tsx"},
            ],
            "files_written": [],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Auth Shell", "description": "Shell"})
    )

    assert ok is True
    warnings = [entry[1] for entry in logs if entry[2] == "warning"]
    assert any("without an `interface_id`" in message and "Ghost.tsx" in message for message in warnings)
    # The well-formed sibling is unaffected.
    assert arc_runtime.traceability.get_interface(f"{node_id}-UI-LoginPage")["type"] == "UI"


def test_design_phase_dropped_warning_survives_a_type_judgment_failure(
    tmp_project_dir, arc_runtime
) -> None:
    """A payload can carry BOTH shapes: an id-less record and an unresolvable
    type. The registry raises on the latter, but the dropped-record warning
    must still be emitted before the phase is judged (PR #237 review round 1)."""

    node_id = "REQ-S5-F"
    _seed_shell_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Shell wired.",
            "interfaces": [
                {"type": "UI", "file_path": "frontend/src/pages/Ghost.tsx", "name": "Ghost"},
                {"interface_id": "IF-ORPHAN", "file_path": "src/orphan.py"},
            ],
            "files_written": [],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Auth Shell", "description": "Shell"})
    )

    assert ok is False
    errors = [entry[1] for entry in logs if entry[2] == "error"]
    assert any("invalid `type`" in message and "IF-ORPHAN" in message for message in errors)
    warnings = [entry[1] for entry in logs if entry[2] == "warning"]
    assert any("without an `interface_id`" in message and "Ghost.tsx" in message for message in warnings)


# ---------------------------------------------------------------------------
# Issue #233: the remaining silent holes become observable warnings
# ---------------------------------------------------------------------------


def test_design_phase_warns_on_unresolved_edge_references(tmp_project_dir, arc_runtime) -> None:
    """A caller/callee id that resolves to no stored contract used to skip
    edge creation silently; the registration now reports it."""

    node_id = "REQ-S5-G"
    _seed_shell_requirement(arc_runtime, node_id)
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Shell wired.",
            "interfaces": [
                {
                    "interface_id": f"{node_id}-UI-Shell",
                    "type": "UI",
                    "file_path": "frontend/src/App.tsx",
                    "callees": ["IF-GHOST-CALLED"],
                },
            ],
            "files_written": [],
        },
    )

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Auth Shell", "description": "Shell"})
    )

    assert ok is True
    warnings = [entry[1] for entry in logs if entry[2] == "warning"]
    assert any(
        "no cross-requirement edge was created" in message and "IF-GHOST-CALLED" in message
        for message in warnings
    )


def test_design_phase_warns_on_dropped_and_backfilled_test_rows(tmp_project_dir, arc_runtime) -> None:
    """Manifest rows that cannot be registered are dropped with a warning;
    rows with a derivable test_id or no coverage_scope are backfilled with a
    warning (issue #233 audit wiring). The manifest then trips the
    foreign-owned check, so the phase verdict is False — irrelevant to the
    warnings under pin, which fire in prepare_tests before it."""

    node_id = "REQ-S5-H"
    arc_runtime.traceability.store_requirement_tree(
        {
            "id": node_id,
            "name": "Login Leaf",
            "description": "Leaf feature node",
        }
    )
    runner, logs = _make_runner(
        tmp_project_dir,
        {
            "summary": "Login designed.",
            "interfaces": [
                {
                    "interface_id": f"{node_id}-UI-LoginPage",
                    "type": "UI",
                    "file_path": "frontend/src/pages/LoginPage.tsx",
                },
            ],
            "files_written": [],
        },
    )

    class _ManifestGenerator:
        app_handler = None

        async def run(self, node_id: str, requirement_data: dict) -> tuple:
            return (
                [
                    # Unregistrable: no file path at all.
                    {"test_id": "T-NOPATH", "type": "Unit"},
                    # Derivable identity: no test_id, no coverage_scope.
                    {"type": "Unit", "file_path": "frontend/tests/unit/login.test.ts"},
                ],
                "summary",
            )

    runner.test_generator = _ManifestGenerator()

    ok = asyncio.run(
        runner.run_design_phase(node_id, {"name": "Login Leaf", "description": "Leaf"})
    )

    warnings = [entry[1] for entry in logs if entry[2] == "warning"]
    assert any(
        "cannot be registered" in message and "T-NOPATH" in message for message in warnings
    )
    assert any(
        "Backfilled manifest row field(s)" in message and "test_id" in message
        and "coverage_scope" in message
        for message in warnings
    )

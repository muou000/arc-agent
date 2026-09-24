"""DESIGN phase survives typeless interface records via the backfill ladder.

Issue #230 (serial-5): a non-leaf shell pass returned 11 interface records
with no `type` field; the registry raised on the first entry with no backfill
source, REQ-2:IMPLEMENT never ran, and the whole round was judged failed.
The phase-level contract now reads: the registry resolves `type` through its
backfill ladder before raising, the DESIGN adapter gets exactly one targeted
repair ask for what the ladder cannot resolve, and id-less records — always
dropped — surface as an observable warning instead of a silent hole.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.phases import WorkflowPhaseRunner

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


def _seed_stored_contracts(runtime, req_id: str) -> None:
    for interface_id, path, interface_type in [
        ("ROOT-UI-AppShell", "frontend/src/App.tsx", "UI"),
        ("ROOT-API-ExpressApp", "backend/src/app.js", "API"),
        ("ROOT-UI-AppHeader", "frontend/src/components/layout/AppHeader.tsx", "UI"),
        ("ROOT-FUNC-SeedDb", "backend/src/database/seed_db.js", "FUNC"),
        ("ROOT-API-AuthRoutes", "backend/src/routes/auth_routes.js", "API"),
        ("ROOT-FUNC-AuthService", "backend/src/services/auth_service.js", "FUNC"),
        ("ROOT-DB-UsersTable", "backend/src/database/init_db.js", "DB"),
    ]:
        runtime.traceability.upsert_interface(
            interface_id=interface_id,
            req_ids=["ROOT"],
            type=interface_type,
            content="{}",
            file_path=path,
        )


def _typeless_payload(node_id: str) -> dict:
    """Serial-5's shape: every record omits `type` (the id prefix made the
    model treat it as redundant). Reused ids, prefixed new ids, all resolvable."""
    rows = [
        {"interface_id": "ROOT-UI-AppShell", "file_path": "frontend/src/App.tsx", "relation": "update"},
        {"interface_id": "ROOT-API-ExpressApp", "file_path": "backend/src/app.js", "relation": "update"},
        {"interface_id": "ROOT-UI-AppHeader", "file_path": "frontend/src/components/layout/AppHeader.tsx", "relation": "update"},
        {"interface_id": "ROOT-FUNC-SeedDb", "file_path": "backend/src/database/seed_db.js", "relation": "update"},
        {"interface_id": "ROOT-API-AuthRoutes", "file_path": "backend/src/routes/auth_routes.js", "relation": "update"},
        {"interface_id": "ROOT-FUNC-AuthService", "file_path": "backend/src/services/auth_service.js", "relation": "update"},
        {"interface_id": "ROOT-DB-UsersTable", "file_path": "backend/src/database/init_db.js", "relation": "update"},
        {"interface_id": f"{node_id}-UI-LoginPage", "file_path": "frontend/src/pages/LoginPage.tsx"},
        {"interface_id": f"{node_id}-API-AuthApi", "file_path": "frontend/src/features/auth/authApi.ts"},
    ]
    return {"summary": "Shell wired.", "interfaces": rows, "files_written": []}


def test_design_phase_backfills_missing_types_and_lands_contracts(
    tmp_project_dir, arc_runtime
) -> None:
    node_id = "REQ-S5-A"
    _seed_shell_requirement(arc_runtime, node_id)
    _seed_stored_contracts(arc_runtime, "ROOT")
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

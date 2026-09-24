"""Shared fixtures for the #230 type-backfill tests.

Both the designer-level replay (``test_agents/test_design_type_backfill_e2e``)
and the phase-level replay (``test_workflow/test_design_type_backfill_phase``)
rebuild the serial-5 incident: a shell node whose DESIGN response omits
`type` from every interface record. The stored parent/dependency contracts
those records reuse are seeded from this one table so the two layers pin the
same incident data.
"""

from __future__ import annotations

from typing import Any

#: Parent-owned contracts already in the traceability store when the shell
#: node's DESIGN pass runs (serial-5 shape: reused rows, ids carrying their
#: type segment, stored rows carrying the authoritative type).
STORED_PARENT_CONTRACTS: list[tuple[str, str, str]] = [
    ("ROOT-UI-AppShell", "frontend/src/App.tsx", "UI"),
    ("ROOT-API-ExpressApp", "backend/src/app.js", "API"),
    ("ROOT-UI-AppHeader", "frontend/src/components/layout/AppHeader.tsx", "UI"),
    ("ROOT-UI-NavBar", "frontend/src/components/layout/NavBar.tsx", "UI"),
    ("ROOT-FUNC-SeedDb", "backend/src/database/seed_db.js", "FUNC"),
    ("ROOT-API-AuthRoutes", "backend/src/routes/auth_routes.js", "API"),
    ("ROOT-FUNC-AuthService", "backend/src/services/auth_service.js", "FUNC"),
    ("ROOT-DB-UsersTable", "backend/src/database/init_db.js", "DB"),
]


def seed_stored_parent_contracts(traceability: Any, req_id: str = "ROOT") -> None:
    for interface_id, path, interface_type in STORED_PARENT_CONTRACTS:
        traceability.upsert_interface(
            interface_id=interface_id,
            req_ids=[req_id],
            type=interface_type,
            content="{}",
            file_path=path,
        )

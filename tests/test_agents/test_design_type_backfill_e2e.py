"""Serial-5 replay (issue #230): 11 typeless interface records must not kill DESIGN.

The 2026-09-24 arc-output-serial-5 run lost REQ-2+ROOT (31 minutes, ~230k
input tokens) to one deterministic defect: every interface record in the
DESIGN structured response omitted `type` (the model read the `-UI-`/`-API-`
segment in the interface_id as making the field redundant), and the registry
validated on the first entry with no backfill source — no repair channel.
This replay pins the recovered behaviour on the incident shape: reused
contracts resolvable from stored rows, new prefixed ids resolvable from the
id segment, and the remainder rescued by exactly one targeted repair ask.
"""

from __future__ import annotations

import asyncio

from agents.interface_designer import InterfaceDesigner
from tests.helpers.design_type_backfill import STORED_PARENT_CONTRACTS, seed_stored_parent_contracts
from tests.helpers.faux import FauxChatModel, faux_tool_call
from tests.test_agents.conftest import arc_runtime  # noqa: F401

NEW_OWNED_ROWS = [
    {"interface_id": "REQ-2-UI-LoginPage", "file_path": "frontend/src/pages/LoginPage.tsx"},
    {"interface_id": "REQ-2-API-AuthApi", "file_path": "frontend/src/features/auth/authApi.ts"},
]


def _typeless_rows() -> list[dict]:
    rows = [
        {"interface_id": interface_id, "file_path": path, "responsibility": "Reused from parent."}
        for interface_id, path, _ in STORED_PARENT_CONTRACTS
    ]
    rows.extend(dict(row) for row in NEW_OWNED_ROWS)
    # The one entry with no backfill source at all: no stored row, and an id
    # without a -UI-/-API-/-FUNC-/-DB- segment (serial-5's fatal record).
    rows.append({"interface_id": "REQ-2-SeedService", "file_path": "backend/src/database/seed_accounts.js"})
    return rows


def test_serial5_typeless_response_backfills_and_repairs(tmp_project_dir, arc_runtime) -> None:
    seed_stored_parent_contracts(arc_runtime.traceability)
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    model = FauxChatModel(
        responses=[
            # Main pass: serial-5's fatal shape — 11 records, none with `type`.
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Login chain designed.", "interfaces": _typeless_rows(), "files_written": []},
                call_id="main",
            ),
            # The targeted repair ask: only the unbackfillable id, now typed.
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Typed the unresolvable record.",
                    "interfaces": [
                        {
                            "interface_id": "REQ-2-SeedService",
                            "type": "FUNC",
                            "responsibility": "Seed bootstrap.",
                        }
                    ],
                    "files_written": [],
                },
                call_id="repair",
            ),
        ]
    )
    designer = InterfaceDesigner(
        log_cb=log_cb,
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    bundle = asyncio.run(
        designer.run(node_id="REQ-2", requirement_data={"name": "登录", "description": "账号密码登录"})
    )

    by_id = {item["interface_id"]: item for item in bundle["interfaces"]}
    assert len(bundle["interfaces"]) == 11
    # The ten records with a backfill source (stored row / id segment) pass
    # through untouched — the registry's ladder owns their deterministic
    # backfill at registration time (pinned in the phase-level tests).
    assert not by_id["ROOT-UI-AppShell"].get("type")
    assert by_id["ROOT-UI-AppShell"]["responsibility"] == "Reused from parent."
    assert not by_id["REQ-2-UI-LoginPage"].get("type")
    # The unbackfillable record is the only one the repair ask touches, and
    # it took exactly one re-invocation to get there.
    assert by_id["REQ-2-SeedService"]["type"] == "FUNC"
    assert by_id["REQ-2-SeedService"]["file_path"] == "backend/src/database/seed_accounts.js"
    assert model.call_count == 2
    warnings = [message for _, message, status, _ in logs if status == "warning"]
    assert any("no resolvable `type`" in message for message in warnings)


def test_type_repair_answer_without_type_is_not_invented(tmp_project_dir, arc_runtime) -> None:
    """The repair patch must not mint a type the model did not supply: a
    garbage answer leaves the record untouched for the registry to judge."""

    seed_stored_parent_contracts(arc_runtime.traceability)
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Login chain designed.", "interfaces": _typeless_rows(), "files_written": []},
                call_id="main",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Tried.",
                    "interfaces": [{"interface_id": "REQ-2-SeedService", "type": "SCHEDULE"}],
                    "files_written": [],
                },
                call_id="repair",
            ),
        ]
    )
    designer = InterfaceDesigner(
        log_cb=log_cb,
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    bundle = asyncio.run(
        designer.run(node_id="REQ-2", requirement_data={"name": "登录", "description": "账号密码登录"})
    )

    by_id = {item["interface_id"]: item for item in bundle["interfaces"]}
    # The backfillable ten pass through for the registry's ladder; the orphan
    # record gets no invented `type` and falls through to its judgment.
    assert not by_id["ROOT-UI-AppShell"].get("type")
    assert not by_id["REQ-2-UI-LoginPage"].get("type")
    assert not by_id["REQ-2-SeedService"].get("type")
    assert model.call_count == 2
    warnings = [message for _, message, status, _ in logs if status == "warning"]
    assert any("still missing a resolvable `type`" in message for message in warnings)

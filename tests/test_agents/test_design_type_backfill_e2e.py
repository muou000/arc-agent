"""Serial-5 replay (issues #230/#233): 11 typeless interface records must not kill DESIGN.

The 2026-09-24 arc-output-serial-5 run lost REQ-2+ROOT (31 minutes, ~230k
input tokens) to one deterministic defect: every interface record in the
DESIGN structured response omitted `type` (the model read the `-UI-`/`-API-`
segment in the interface_id as making the field redundant), and the registry
validated on the first entry with no backfill source — no repair channel.

Two defenses now cover the incident shape, pinned here together:

- decode-time (#233): the tightened response schema rejects a typeless row
  when the structured tool call is parsed, and the in-session retry asks the
  model to fix it before any registration happens;
- registration-time (#230): rows that reach the registry anyway — through the
  decode-bypassing recovery channels (raw JSON final message, fenced prose)
  — resolve `type` through the backfill ladder (stored row, id segment), and
  the remainder get exactly one targeted repair ask.
"""

from __future__ import annotations

import asyncio
import json

from agents.interface_designer import InterfaceDesigner
from tests.helpers.design_type_backfill import (
    STORED_PARENT_CONTRACTS,
    seed_stored_parent_contracts,
    typed_incident_rows,
)
from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call
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


def test_serial5_typeless_tool_call_is_rejected_at_decode_and_fixed(tmp_project_dir, arc_runtime) -> None:
    """The structured-path replay: a typeless tool call cannot reach the
    registry anymore — decode rejects it, the in-session retry returns typed
    rows, and no repair nudge is spent."""

    seed_stored_parent_contracts(arc_runtime.traceability)
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    model = FauxChatModel(
        responses=[
            # Main pass, first turn: serial-5's fatal shape — 11 records, none with `type`.
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Login chain designed.", "interfaces": _typeless_rows(), "files_written": []},
                call_id="typeless",
            ),
            # The decode-retry turn: the same records, now typed.
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Login chain designed.", "interfaces": typed_incident_rows(_typeless_rows()), "files_written": []},
                call_id="typed",
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

    assert len(bundle["interfaces"]) == 11
    by_id = {item["interface_id"]: item for item in bundle["interfaces"]}
    assert by_id["ROOT-UI-AppShell"]["type"] == "UI"
    assert by_id["REQ-2-UI-LoginPage"]["type"] == "UI"
    assert by_id["REQ-2-SeedService"]["type"] == "FUNC"
    assert model.call_count == 2
    # The second model turn carries the decode rejection: the retry message
    # names the violated `type` field, which is what steer the model to fix.
    second_turn = model.calls[1]
    assert any(
        getattr(message, "type", None) == "tool" and "type" in str(getattr(message, "content", ""))
        for message in second_turn
    )
    warnings = [message for _, message, status, _ in logs if status == "warning"]
    assert not any("no resolvable `type`" in message for message in warnings)


def test_typeless_rows_via_recovery_channel_reach_the_type_repair(tmp_project_dir, arc_runtime) -> None:
    """Rows that bypass decode (raw JSON final message instead of the
    structured tool call) still hit the #230 ladder: backfill resolves the
    ten sourced records, and the one unbackfillable id gets its single
    targeted repair ask. A repair answer without a usable `type` invents
    nothing — the record is left for the registration layer to judge."""

    seed_stored_parent_contracts(arc_runtime.traceability)
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    raw_json_answer = json.dumps(
        {
            "summary": "Login chain designed.",
            "interfaces": _typeless_rows(),
            "files_written": ["frontend/src/pages/LoginPage.tsx"],
        },
        ensure_ascii=False,
    )
    model = FauxChatModel(
        responses=[
            # Main pass answers with a bare JSON final message (no tool
            # call): the recovery channel hands the typeless rows on as-is.
            faux_text(raw_json_answer),
            # The targeted repair ask: the answer via the structured tool
            # call carries no usable `type`, so decode rejects it and the
            # in-session retry finds an empty queue (the pass aborts).
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
    # main(1) + the repair ask's stream attempt: structured tool call(2),
    # decode-retry turn(3) exhausting the queue, then the stream wrapper's
    # ainvoke fallback failing again(4) before the adapter catches.
    assert model.call_count == 4
    warnings = [message for _, message, status, _ in logs if status == "warning"]
    assert any("no resolvable `type`" in message for message in warnings)
    assert any("Type re-serialization pass failed" in message for message in warnings)

"""Fenced-JSON fallback for stage payloads that skip the structured tool call.

Regression coverage for the ticket-booking benchmark failure where both
parallel leaf nodes' InterfaceDesigner turns answered with prose plus a
```json``` block instead of calling the ``InterfaceDesignResponse`` tool:
the strict parse kept only ``summary`` + ``_raw_final_message`` and the
workflow stored 0 interfaces, so TestGenerator had to reverse-engineer every
contract from the workspace files.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.interface_designer import InterfaceDesigner
from agents.runtime.runners import parse_json_payload, salvage_json_objects
from tests.helpers.faux import FauxChatModel, faux_text


PROSE = "The traceability DB has no records yet. I'll return the complete interface set.\n\n"

# A string value containing a raw newline (model wrote a multi-line string
# inside the fenced blob) plus a trailing comma: json.loads rejects both.
DAMAGED_FENCED = (
    PROSE
    + "```json\n"
    + "{\n"
    + '  "summary": "login chain",\n'
    + '  "interfaces": [\n'
    + "    {\n"
    + '      "interface_id": "REQ-2-UI-login",\n'
    + '      "type": "UI",\n'
    + '      "name": "LoginPage",\n'
    + '      "specification": "Form fields:\nnatively labeled 用户名或邮箱",\n'
    + '      "file_path": "frontend/src/pages/LoginPage.tsx"\n'
    + "    },\n"
    + "    {\n"
    + '      "interface_id": "REQ-2-API-auth",\n'
    + '      "type": "API",\n'
    + '      "file_path": "frontend/src/api/auth.ts",\n'
    + "    }\n"
    + "  ]\n"
    + "}\n"
    + "```"
)

# Output-token truncation: no closing fence, the root object and the second
# interface never close. parse_json_payload must still fail; the salvage
# scanner must recover the one complete interface object.
TRUNCATED_FENCED = (
    PROSE
    + "```json\n"
    + "{\n"
    + '  "summary": "login chain",\n'
    + '  "interfaces": [\n'
    + '    {"interface_id": "IF-COMPLETE", "type": "UI", "file_path": "a.tsx"},\n'
    + '    {"interface_id": "IF-CUT", "type": "API", "file_path": "b.ts"'
)


def test_parse_json_payload_reads_wellformed_fenced_block() -> None:
    payload = parse_json_payload(PROSE + '```json\n{"summary": "s", "interfaces": []}\n```')
    assert payload == {"summary": "s", "interfaces": []}


def test_parse_json_payload_repairs_control_chars_and_trailing_commas() -> None:
    payload = parse_json_payload(DAMAGED_FENCED)
    assert payload is not None
    assert payload["summary"] == "login chain"
    assert [item["interface_id"] for item in payload["interfaces"]] == [
        "REQ-2-UI-login",
        "REQ-2-API-auth",
    ]


def test_parse_json_payload_still_fails_on_truncated_json() -> None:
    # Whole-document loads must not fabricate a payload from a cut-off blob.
    assert parse_json_payload(TRUNCATED_FENCED) is None


def test_parse_json_payload_returns_none_for_plain_prose() -> None:
    assert parse_json_payload("No JSON in this message at all.") is None


def test_salvage_json_objects_recovers_complete_objects_from_truncation() -> None:
    recovered = salvage_json_objects(TRUNCATED_FENCED)
    assert [item["interface_id"] for item in recovered] == ["IF-COMPLETE"]


def test_salvage_json_objects_ignores_braces_inside_strings_and_prose() -> None:
    payload = salvage_json_objects('{"a": "brace { inside } string", "b": 1} after {not json} text')
    assert payload == [{"a": "brace { inside } string", "b": 1}]


def test_recover_interfaces_requires_raw_final_message_marker() -> None:
    # A structured tool call that deliberately returned 0 interfaces must not
    # be second-guessed from prose.
    payload = {"summary": DAMAGED_FENCED}
    assert InterfaceDesigner._recover_interfaces_from_raw(payload) == []


def test_recover_interfaces_filters_non_contract_objects() -> None:
    payload = {
        "summary": 'note {"unrelated": true} then ```json\n{"interfaces": [{"interface_id": "IF-X", "type": "FUNC"}]}\n```',
        "_raw_final_message": '{"content": "..."}',
    }
    recovered = InterfaceDesigner._recover_interfaces_from_raw(payload)
    # The wrapper root has none of the contract keys and is dropped; the
    # inner interface object survives.
    assert recovered == [{"interface_id": "IF-X", "type": "FUNC"}]


def test_interface_designer_recovers_fenced_interfaces_without_tool_call(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-FALLBACK-1"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )
    model = FauxChatModel(responses=[faux_text(DAMAGED_FENCED)])
    designer = InterfaceDesigner(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    bundle = asyncio.run(
        designer.run(node_id=node_id, requirement_data={"name": "Calculator", "description": "Add two numbers"})
    )

    assert model.call_count == 1
    assert [item["interface_id"] for item in bundle["interfaces"]] == [
        "REQ-2-UI-login",
        "REQ-2-API-auth",
    ]


def test_interface_designer_salvages_truncated_final_message(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-FALLBACK-2"
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )
    model = FauxChatModel(responses=[faux_text(TRUNCATED_FENCED)])
    designer = InterfaceDesigner(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    bundle = asyncio.run(
        designer.run(node_id=node_id, requirement_data={"name": "Calculator", "description": "Add two numbers"})
    )

    assert [item["interface_id"] for item in bundle["interfaces"]] == ["IF-COMPLETE"]

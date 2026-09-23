"""Faux-model e2e tests for the InterfaceDesigner and TestGenerator agents.

Both adapters build their stage agent with a pydantic ``response_format``, so
LangChain routes structured output through an ordinary tool call named after
the schema (``ToolStrategy``). The faux model scripts exactly that tool call,
which lets the *real* deep-agents loop execute file writes and produce the
structured payload — including ``StageDisciplineMiddleware`` enforcement for
the ``interface_design`` and ``test_generation`` stages.
"""

from __future__ import annotations

import asyncio

import pytest
from pathlib import Path

from core import sessions
from agents.interface_designer import InterfaceDesigner
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from agents.tools.test_manifest import DeclaredTestFile, TestManifestLock
from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call


def seed_requirement(runtime, node_id: str) -> None:
    runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )


def make_designer(tmp_project_dir: Path, model: FauxChatModel, log_cb=None) -> InterfaceDesigner:
    return InterfaceDesigner(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
        log_cb=log_cb,
    )


def make_generator(tmp_project_dir: Path, model: FauxChatModel, log_cb=None) -> TestGenerator:
    return TestGenerator(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
        log_cb=log_cb,
    )


def test_interface_designer_writes_skeleton_and_returns_structured_bundle(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-DESIGN-1"
    seed_requirement(arc_runtime, node_id)

    skeleton = "from dataclasses import dataclass\n\n\n@dataclass\nclass CalcContract:\n    value: int\n"
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/contracts/calc.py", "content": skeleton},
                call_id="c1",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Designed the calculator contract.",
                    "interfaces": [
                        {
                            "interface_id": "IF-CALC",
                            "type": "FUNC",
                            "name": "add",
                            "responsibility": "Add two integers",
                            "file_path": "/workspace/src/calc.py",
                            "first_line": "def add(a, b):",
                            "callers": [],
                            "callees": [],
                        }
                    ],
                    "files_written": ["/workspace/src/contracts/calc.py"],
                },
                call_id="c2",
            ),
        ]
    )

    logs: list[str] = []

    def collect_log(agent_name: str, message: str, status: str | None, node_id: str | None) -> None:
        logs.append(message)

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model, log_cb=collect_log).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 2
    assert bundle["summary"] == "Designed the calculator contract."
    assert bundle["files_written"] == ["/workspace/src/contracts/calc.py"]
    assert len(bundle["interfaces"]) == 1
    interface = bundle["interfaces"][0]
    assert interface["interface_id"] == "IF-CALC"
    assert interface["type"] == "FUNC"
    # The scripted skeleton write really landed in the workspace.
    assert (tmp_project_dir / "src" / "contracts" / "calc.py").read_text(encoding="utf-8") == skeleton
    # Write-time registration wiring (factory -> discipline -> registry): the
    # contract-embodied write was derived into a pending contract id, whose
    # notice rode along on the write's tool result into the conversation.
    assert any(
        "Pending contract registration: 1" in message and "REQ-DESIGN-1-FUNC-calc" in message
        for message in logs
    ), logs

def test_interface_designer_repairs_empty_interfaces_after_materializing_files(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A prose-only final answer must not lose the designed contracts.

    The 2026-09-14 arc-bench run deadlocked downstream stages because the
    InterfaceDesigner wrote 9 skeleton files but returned `"interfaces": []`.
    The adapter re-asks on the same thread when the discipline observed real
    writes but the response recorded no interface records; since 2026-09-16
    the re-ask is skeleton-guided — the contract identities are derived
    mechanically from the materialized files and the model fills the semantic
    fields per row.
    """
    node_id = "REQ-DESIGN-REPAIR"
    seed_requirement(arc_runtime, node_id)

    skeleton = "from dataclasses import dataclass\n\n\n@dataclass\nclass CalcContract:\n    value: int\n"
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/contracts/calc.py", "content": skeleton},
                call_id="c1",
            ),
            # First pass: prose-only response, arrays left empty.
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Both contracts are fully specified below in prose.",
                    "interfaces": [],
                    "files_written": [],
                },
                call_id="c2",
            ),
    # Skeleton-guided fill pass on the same thread (the row id matches the
    # mechanically derived skeleton for this file). The repair agent is rebuilt
    # with the minItems-constrained schema, so the structured tool is named
    # after that schema.
    faux_tool_call(
        "InterfaceDesignRepairResponse",
        {
            "summary": "Filled the derived skeleton rows.",
            "interfaces": [
                {
                    "interface_id": "REQ-DESIGN-REPAIR-FUNC-calc",
                    "responsibility": "Owns the calculator value contract.",
                    "specification": "CalcContract dataclass with an int value field.",
                }
            ],
            "files_written": [],
        },
        call_id="c3",
    ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 3
    # Ground truth from StageDisciplineMiddleware, not the model's claim.
    assert bundle["materialized_paths"] == ["/workspace/src/contracts/calc.py"]
    # The mechanical skeleton identity wins; the model contributed semantics only.
    assert [item["interface_id"] for item in bundle["interfaces"]] == [
        "REQ-DESIGN-REPAIR-FUNC-calc"
    ]
    interface = bundle["interfaces"][0]
    assert interface["type"] == "FUNC"
    assert interface["file_path"] == "src/contracts/calc.py"
    assert interface["responsibility"] == "Owns the calculator value contract."
    assert "skeleton_derived" not in interface
    # files_written stays empty on this path (no new writes happened); the
    # discipline's materialized marker below is the write ground truth.
    assert bundle["files_written"] == []


def test_interface_designer_falls_back_to_mechanical_records_when_fill_stays_empty(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """Model never fills the skeleton: conservative records must still land.

    The 2026-09-16 flash-class failure shape (deepseek-v4-flash, REQ-2 with
    12 materialized files): both the main response and every re-ask return a
    schema-valid empty ``interfaces`` array. The repair must then materialize
    conservative mechanical records from the skeletons — never a silent empty
    bundle that would let the workflow hard-gate the DESIGN phase.
    """
    node_id = "REQ-DESIGN-REPAIR-MECHANICAL"
    seed_requirement(arc_runtime, node_id)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/contracts/calc.py", "content": "VALUE = 1\n"},
                call_id="c1",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Designed in prose.", "interfaces": [], "files_written": []},
                call_id="c2",
            ),
            # Skeleton fill pass (constrained repair schema): still empty. The
            # minItems floor rejects the blank sheet inside the agent loop and
            # re-asks the model on the same session, so each scripted refusal
            # is consumed twice (violation + re-ask) before the repair pass
            # gives up and hands an empty payload back.
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {"summary": "Still nothing structured.", "interfaces": [], "files_written": []},
                call_id="c3",
            ),
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {"summary": "Still nothing, second ask.", "interfaces": [], "files_written": []},
                call_id="c4",
            ),
            # Batched retry: still empty (again asked twice by the floor).
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {"summary": "Batch retry also empty.", "interfaces": [], "files_written": []},
                call_id="c5",
            ),
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {"summary": "Batch retry also empty, second ask.", "interfaces": [], "files_written": []},
                call_id="c6",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 4
    assert [item["interface_id"] for item in bundle["interfaces"]] == [
        f"{node_id}-FUNC-calc"
    ]
    interface = bundle["interfaces"][0]
    assert interface["type"] == "FUNC"
    assert interface["file_path"] == "src/contracts/calc.py"
    assert interface["first_line"] == "VALUE = 1"
    # Mechanical records are explicitly marked so downstream consumers can
    # tell them apart from model-serialized contracts.
    assert interface["skeleton_derived"] is True
    assert interface["responsibility"]
    assert interface["specification"]
    # The workflow-level hard gate reads this marker.
    assert bundle["materialized_paths"] == ["/workspace/src/contracts/calc.py"]


def test_interface_designer_batches_partial_fill_gaps_before_fallback(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A half-filled list triggers one batched retry, then mechanical fill.

    The whole-list fill pass may succeed on part of the skeletons (small
    outputs work, large ones collapse — REQ-1's 2026-09-16 self-rescue
    proved compact aggregation is achievable). The gaps are re-asked in one
    batched round; whatever is still missing is covered by conservative
    mechanical records, keyed on the model-filled records already collected.
    """
    node_id = "REQ-DESIGN-PARTIAL"
    seed_requirement(arc_runtime, node_id)

    def _write(path: str, content: str, call_id: str):
        return faux_tool_call("write_file", {"file_path": f"/workspace/{path}", "content": content}, call_id=call_id)

    model = FauxChatModel(
        responses=[
            _write("backend/src/services/calc_service.js", "module.exports = { add };\n", "c1"),
            _write("frontend/src/pages/CalcPage.tsx", "export default function CalcPage() {}\n", "c2"),
            # Main pass: prose-only, arrays empty.
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Designed in prose.", "interfaces": [], "files_written": []},
                call_id="c3",
            ),
            # Whole-list fill (constrained repair schema): only the service row answered.
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {
                    "summary": "Filled one row.",
                    "interfaces": [
                        {
                            "interface_id": f"{node_id}-FUNC-CalcService",
                            "responsibility": "Adds two numbers.",
                            "specification": "add(a, b) returns a + b.",
                        }
                    ],
                    "files_written": [],
                },
                call_id="c4",
            ),
            # Batched retry over the gap: the page row.
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {
                    "summary": "Filled the gap.",
                    "interfaces": [
                        {
                            "interface_id": f"{node_id}-UI-CalcPage",
                            "responsibility": "Calculator page surface.",
                            "specification": "Renders the calculator UI.",
                        }
                    ],
                    "files_written": [],
                },
                call_id="c5",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 5
    by_id = {item["interface_id"]: item for item in bundle["interfaces"]}
    # The model-filled rows carry their semantics and no mechanical marker.
    assert by_id[f"{node_id}-FUNC-CalcService"]["responsibility"] == "Adds two numbers."
    assert by_id[f"{node_id}-UI-CalcPage"]["responsibility"] == "Calculator page surface."
    assert "skeleton_derived" not in by_id[f"{node_id}-FUNC-CalcService"]
    assert "skeleton_derived" not in by_id[f"{node_id}-UI-CalcPage"]
    assert by_id[f"{node_id}-UI-CalcPage"]["type"] == "UI"
    assert by_id[f"{node_id}-UI-CalcPage"]["file_path"] == "frontend/src/pages/CalcPage.tsx"


def test_interface_designer_reused_row_cannot_mask_a_skeleton_gap(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A reused interface with a colliding file_path must not hide a gap.

    PR #33 review: the fill pass matched model rows to skeletons by
    ``file_path`` alone, so a reused parent/dependency row whose stale path
    happened to equal a materialized file satisfied that skeleton and the
    mechanical fallback silently skipped it — an undercount the workflow
    hard gate could not see. The reused row must stay its own record while
    the skeleton still gets its mechanical row.
    """
    node_id = "REQ-DESIGN-REUSED-PATH"
    seed_requirement(arc_runtime, node_id)

    def _write(path: str, content: str, call_id: str):
        return faux_tool_call("write_file", {"file_path": f"/workspace/{path}", "content": content}, call_id=call_id)

    model = FauxChatModel(
        responses=[
            _write("backend/src/services/calc_service.js", "module.exports = { add };\n", "c1"),
            # Main pass: prose-only, arrays empty.
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Designed in prose.", "interfaces": [], "files_written": []},
                call_id="c2",
            ),
            # Fill pass: only a REUSED row whose file_path collides with the
            # service skeleton's path — no valid fill for the skeleton itself.
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {
                    "summary": "Reused only.",
                    "interfaces": [
                        {
                            "interface_id": "ROOT-UI-AppHeader",
                            "req_id": "ROOT",
                            "type": "UI",
                            "file_path": "backend/src/services/calc_service.js",
                            "responsibility": "Reused parent header.",
                        }
                    ],
                    "files_written": [],
                },
                call_id="c3",
            ),
            # Batched retry over the gap: model stays blank.
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {"summary": "Still nothing.", "interfaces": [], "files_written": []},
                call_id="c4",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    by_id = {item["interface_id"]: item for item in bundle["interfaces"]}
    # The skeleton was NOT satisfied by the colliding reused row: its
    # mechanical record landed (marked), so the hard gate sees a full count.
    assert by_id[f"{node_id}-FUNC-CalcService"]["skeleton_derived"] is True
    assert by_id[f"{node_id}-FUNC-CalcService"]["type"] == "FUNC"
    # The reused row survives as its own record, semantics intact.
    assert by_id["ROOT-UI-AppHeader"]["responsibility"] == "Reused parent header."
    assert by_id["ROOT-UI-AppHeader"]["type"] == "UI"


def test_interface_designer_recovers_interfaces_from_fenced_json_in_summary(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """Contracts buried as a fenced JSON block in `summary` must be lifted.

    Observed live on 2026-09-15: deepseek-v4-flash answered the contract
    re-serialization pass by nesting the entire response JSON inside the
    `summary` string. The records are recoverable without another model call.
    """
    import json

    node_id = "REQ-DESIGN-FENCED"
    seed_requirement(arc_runtime, node_id)

    skeleton = "from dataclasses import dataclass\n\n\n@dataclass\nclass CalcContract:\n    value: int\n"
    buried = {
        "summary": "Nested response that must never be consumed as the real summary.",
        "interfaces": [
            {
                "interface_id": "IF-CALC",
                "type": "FUNC",
                "name": "add",
                "responsibility": "Add two integers",
                "file_path": "src/contracts/calc.py",
                "first_line": "@dataclass",
                "callers": [],
                "callees": [],
            }
        ],
        "files_written": ["src/contracts/calc.py"],
    }
    prose_summary = "Both contracts are fully specified below.\n" "```json\n" + json.dumps(buried, indent=2) + "\n```"
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/contracts/calc.py", "content": skeleton},
                call_id="c1",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": prose_summary, "interfaces": [], "files_written": []},
                call_id="c2",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    # No repair call needed: the recovery lifted the records from the prose.
    assert model.call_count == 2
    assert [item["interface_id"] for item in bundle["interfaces"]] == ["IF-CALC"]
    # The inner summary replaces the raw JSON blob parked in `summary`.
    assert bundle["summary"] == "Nested response that must never be consumed as the real summary."
    assert bundle["files_written"] == ["src/contracts/calc.py"]
    assert bundle["materialized_paths"] == ["/workspace/src/contracts/calc.py"]


def test_interface_designer_recovers_interfaces_from_bare_json_summary(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A bare JSON object answered as plain text must also be lifted.

    Observed live on 2026-09-15 (REQ-1 retry): the model skipped the
    structured tool call entirely and answered with a raw JSON object, which
    the runner parks in the `summary` field of the normalized payload.
    """
    import json

    node_id = "REQ-DESIGN-BARE-JSON"
    seed_requirement(arc_runtime, node_id)

    skeleton = "VALUE = 1\n"
    record = {
        "interface_id": "IF-CALC",
        "type": "FUNC",
        "name": "add",
        "responsibility": "Add two integers",
        "file_path": "src/contracts/calc.py",
        "first_line": "VALUE = 1",
        "callers": [],
        "callees": [],
    }
    bare_json_summary = json.dumps(
        {"summary": "inner", "interfaces": [record], "files_written": ["src/contracts/calc.py"]},
        indent=2,
    )
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/contracts/calc.py", "content": skeleton},
                call_id="c1",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": bare_json_summary, "interfaces": [], "files_written": []},
                call_id="c2",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 2
    assert [item["interface_id"] for item in bundle["interfaces"]] == ["IF-CALC"]
    assert bundle["summary"] == "inner"
    assert bundle["files_written"] == ["src/contracts/calc.py"]


def test_interface_designer_repairs_claimed_files_without_writes(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A write-less pass claiming files_written must still re-serialize.

    A leaf whose only contracts are reused parent/dependency interfaces can
    legitimately record no files; if it then returns an empty interfaces
    array while claiming files_written, the self-reported evidence is enough
    to trigger the one-shot repair re-ask. The workflow hard gate stays
    keyed on the discipline's ground truth, so no fabricated failure here.
    """
    node_id = "REQ-DESIGN-CLAIMED"
    seed_requirement(arc_runtime, node_id)

    reused_record = {
        "interface_id": "ROOT-UI-APPHEADER",
        "req_id": "ROOT",
        "type": "UI",
        "name": "AppHeader",
        "file_path": "frontend/src/components/AppHeader.tsx",
        "first_line": "1",
        "responsibility": "Reused parent-owned header surface.",
        "callers": [],
        "callees": [],
    }
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Only reuses the parent header surface.",
                    "interfaces": [],
                    "files_written": ["frontend/src/components/AppHeader.tsx"],
                },
                call_id="c1",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Reused parent header surface recorded.",
                    "interfaces": [reused_record],
                    "files_written": [],
                },
                call_id="c2",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    # No writes happened, yet the repair pass fired on the claimed evidence.
    assert model.call_count == 2
    assert [item["interface_id"] for item in bundle["interfaces"]] == ["ROOT-UI-APPHEADER"]
    assert bundle["materialized_paths"] == []


def _seed_parent_shell_with_leaf(runtime) -> tuple[str, str]:
    """Parent shell node owning one stored UI contract, plus a leaf child."""
    runtime.traceability.store_requirement_tree(
        {
            "id": "REQ-SHELL",
            "name": "Homepage",
            "description": "Homepage shell",
            "children": [
                {
                    "id": "REQ-OPEN-HOME",
                    "name": "Open Homepage",
                    "description": "Opening the application URL shows the homepage",
                }
            ],
        }
    )
    runtime.traceability.upsert_interface(
        interface_id="REQ-SHELL-UI-HOMESHELL",
        req_ids=["REQ-SHELL"],
        type="UI",
        content=(
            '{"interface_id": "REQ-SHELL-UI-HOMESHELL", "type": "UI", "name": "HomeShell", '
            '"responsibility": "Homepage shell composed of header and content grid."}'
        ),
        file_path="frontend/src/pages/HomePage.tsx",
        first_line="import GlobalHeader from '../components/GlobalHeader';",
    )
    return "REQ-SHELL", "REQ-OPEN-HOME"


def test_interface_designer_backfills_reuse_from_registry_for_zero_write_leaf(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A write-less leaf serializing reuse only in prose gets one backfill ask.

    Second live shape of the empty-interfaces failure (submission
    dd3f85b71c2e, 2026-09-19, bookstack REQ-1.1): the leaf decides its
    scenarios are covered by the parent shell, writes nothing, and returns a
    schema-valid empty ``interfaces`` array while describing the reused
    contracts in ``summary``. The repair anchors on the parent's stored
    interfaces — real contracts in the registry — and asks for one structured
    record per reused id.
    """
    parent_id, node_id = _seed_parent_shell_with_leaf(arc_runtime)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Only reuses the parent homepage shell; no owned contract.",
                    "interfaces": [],
                    "files_written": [],
                },
                call_id="c1",
            ),
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {
                    "summary": "Reuse recorded against the parent shell.",
                    "interfaces": [
                        {
                            "interface_id": "REQ-SHELL-UI-HOMESHELL",
                            "relation": "reused",
                            "responsibility": "Homepage shell rendered for the default route.",
                        }
                    ],
                    "files_written": [],
                },
                call_id="c2",
            ),
        ]
    )

    logs: list[str] = []

    def collect_log(agent_name: str, message: str, status: str | None, node_id: str | None) -> None:
        logs.append(message)

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model, log_cb=collect_log).run(
            node_id=node_id,
            requirement_data={"name": "Open Homepage", "description": "Opening the URL shows the homepage"},
        )
    )

    assert model.call_count == 2
    assert [item["interface_id"] for item in bundle["interfaces"]] == ["REQ-SHELL-UI-HOMESHELL"]
    # Pure reuse: no write happened, and the repair must not fabricate one.
    assert bundle["files_written"] == []
    assert bundle["materialized_paths"] == []
    # The backfill ask carried the registry anchor into the conversation.
    repair_prompt = "\n".join(str(message.content) for message in model.calls[-1])
    assert "REQ-SHELL-UI-HOMESHELL" in repair_prompt
    assert f"owned by {parent_id}" in repair_prompt
    assert any("reuse backfill" in message for message in logs)


def test_interface_designer_reuse_backfill_coming_back_empty_stays_gated(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """When the backfill also returns nothing, the bundle stays empty.

    The adapter must not invent contracts to rescue the pass: an empty
    backfill result flows to the workflow leaf gate unchanged.
    """
    _, node_id = _seed_parent_shell_with_leaf(arc_runtime)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Only reuses the parent shell.", "interfaces": [], "files_written": []},
                call_id="c1",
            ),
            # Constrained repair schema: the minItems floor raises the blank
            # sheet out of the pass on the first refusal; salvage finds no
            # rows in the offending tool call.
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {"summary": "Still nothing structured.", "interfaces": [], "files_written": []},
                call_id="c2",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Open Homepage", "description": "Opening the URL shows the homepage"},
        )
    )

    # The backfill fired (main pass + one refused repair ask) and came back
    # empty: the empty bundle is the gate's input, not a rescue.
    assert model.call_count == 2
    assert bundle["interfaces"] == []
    assert bundle["files_written"] == []


def test_interface_designer_zero_write_backfill_skips_non_leaf(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A non-leaf's legal empty response must not trigger a backfill ask.

    Non-leaf nodes may record nothing (the workflow warns and proceeds);
    asking them to serialize reused parent contracts would silently change
    that contract.
    """
    node_id, _leaf_id = _seed_parent_shell_with_leaf(arc_runtime)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Nothing to record on the shell.", "interfaces": [], "files_written": []},
                call_id="c1",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Homepage", "description": "Homepage shell"},
        )
    )

    assert model.call_count == 1
    assert bundle["interfaces"] == []


def test_interface_designer_zero_write_leaf_without_candidates_skips_backfill(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """No stored parent/dependency contract means nothing to anchor on.

    The backfill must not fire an ask with an empty candidate list (it could
    only invite invented contracts); the node flows to the workflow leaf gate.
    """
    node_id = "REQ-LONELY-LEAF"
    seed_requirement(arc_runtime, node_id)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Nothing owned here.", "interfaces": [], "files_written": []},
                call_id="c1",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 1
    assert bundle["interfaces"] == []


def test_interface_designer_backfill_enumeration_failure_is_logged(
    tmp_project_dir: Path, arc_runtime, monkeypatch
) -> None:
    """A registry read failure must be visible, not silently degrade to the gate.

    The backfill helper fails safe to the workflow leaf gate either way, but
    an operator reading the log must be able to tell "enumeration crashed"
    apart from "legally no candidates" — otherwise a registry bug masquerades
    as a design failure.
    """
    node_id = "REQ-BROKEN-REGISTRY"
    seed_requirement(arc_runtime, node_id)

    def raise_runtime_error():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("core.service.get_runtime", raise_runtime_error)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Nothing owned here.", "interfaces": [], "files_written": []},
                call_id="c1",
            ),
        ]
    )

    logs: list[str] = []

    def collect_log(agent_name: str, message: str, status: str | None, node_id: str | None) -> None:
        logs.append(message)

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model, log_cb=collect_log).run(
            node_id=node_id,
            requirement_data={"name": "Calculator", "description": "Add two numbers"},
        )
    )

    # The backfill never fired; the failure reason is on the log instead.
    assert model.call_count == 1
    assert bundle["interfaces"] == []
    assert any("Reuse candidate enumeration failed" in message for message in logs)


def test_interface_designer_backfill_reports_unlanded_dependency_anchor(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A declared dependency with no landed contracts must not shrink silently.

    Declared dependencies are scheduling gates, not landed-contract
    guarantees (a failed dependency releases its dependents), so the
    backfill proceeds on the parent anchor but logs the dependency as
    contributing nothing — the operator can see why the candidate list is
    parent-only.
    """
    arc_runtime.traceability.store_requirement_tree(
        {
            "id": "REQ-SHELL-DEP",
            "name": "Homepage",
            "description": "Homepage shell",
            "children": [
                {
                    "id": "REQ-OPEN-DEP",
                    "name": "Open Homepage",
                    "description": "Opening the application URL shows the homepage",
                    "dependencies": ["REQ-UNLANDED"],
                },
                {"id": "REQ-UNLANDED", "name": "Dependency feature", "description": "Not landed yet"},
            ],
        }
    )
    arc_runtime.traceability.upsert_interface(
        interface_id="REQ-SHELL-DEP-UI-HOMESHELL",
        req_ids=["REQ-SHELL-DEP"],
        type="UI",
        content=(
            '{"interface_id": "REQ-SHELL-DEP-UI-HOMESHELL", "type": "UI", "name": "HomeShell", '
            '"responsibility": "Homepage shell."}'
        ),
        file_path="frontend/src/pages/HomePage.tsx",
    )

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Only reuses the parent homepage shell.", "interfaces": [], "files_written": []},
                call_id="c1",
            ),
            faux_tool_call(
                "InterfaceDesignRepairResponse",
                {
                    "summary": "Reuse recorded against the parent shell.",
                    "interfaces": [
                        {
                            "interface_id": "REQ-SHELL-DEP-UI-HOMESHELL",
                            "relation": "reused",
                            "responsibility": "Homepage shell rendered for the default route.",
                        }
                    ],
                    "files_written": [],
                },
                call_id="c2",
            ),
        ]
    )

    logs: list[str] = []

    def collect_log(agent_name: str, message: str, status: str | None, node_id: str | None) -> None:
        logs.append(message)

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model, log_cb=collect_log).run(
            node_id="REQ-OPEN-DEP",
            requirement_data={"name": "Open Homepage", "description": "Opening the URL shows the homepage"},
        )
    )

    # The backfill still fires on the parent anchor; the unlanded dependency
    # is called out on the log instead of silently shrinking the anchor set.
    assert model.call_count == 2
    assert [item["interface_id"] for item in bundle["interfaces"]] == [
        "REQ-SHELL-DEP-UI-HOMESHELL"
    ]
    assert any("REQ-UNLANDED" in message and "no registered interface" in message for message in logs)


def test_test_generator_writes_test_asset_and_returns_manifest(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-GEN-1"
    seed_requirement(arc_runtime, node_id)

    test_code = "test('add', () => { expect(add(1, 1)).toBe(2); });\n"
    model = FauxChatModel(
        responses=[
            # Manifest-first: the declaration locks the test-file paths
            # before the first write.
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {
                            "file_path": "backend/tests/unit/calc.test.js",
                            "type": "Unit",
                            "interface_ids": [],
                        }
                    ]
                },
                call_id="c0",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/calc.test.js", "content": test_code},
                call_id="c1",
            ),
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "One unit test for add().",
                    "tests": [
                        {
                            "test_id": "T-ADD",
                            "req_id": node_id,
                            "interface_ids": ["IF-CALC"],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/calc.test.js",
                            "first_line": "test('add', () => {",
                        }
                    ],
                    "files_written": ["backend/tests/unit/calc.test.js"],
                },
                call_id="c2",
            ),
        ]
    )

    tests, output_text = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 3
    assert tests is not None and len(tests) == 1
    assert tests[0]["test_id"] == "T-ADD"
    assert tests[0]["type"] == "Unit"
    assert tests[0]["file_path"] == "backend/tests/unit/calc.test.js"
    assert "T-ADD" in output_text
    # The scripted test asset really landed in the workspace.
    assert (tmp_project_dir / "backend" / "tests" / "unit" / "calc.test.js").read_text(encoding="utf-8") == test_code


def test_test_generator_uses_staged_current_interfaces_before_db_commit(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-GEN-STAGED"
    seed_requirement(arc_runtime, node_id)
    sessions.merge_node_session(
        node_id,
        {
            "interfaces": [
                {
                    "interface_id": "REQ-GEN-STAGED-FUNC-CALC",
                    "req_id": node_id,
                    "type": "FUNC",
                    "name": "add",
                    "file_path": "backend/src/services/calc.js",
                    "first_line": "function add(a, b)",
                    "responsibility": "Add two integers.",
                    "specification": "Returns a + b.",
                }
            ],
            "phase_status": {"design": "prepared"},
        },
    )

    test_code = "test('add', () => { expect(add(1, 1)).toBe(2); });\n"
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "get_interfaces_for_requirement",
                {"req_id": node_id},
                call_id="c0",
            ),
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {
                            "file_path": "backend/tests/unit/calc.test.js",
                            "type": "Unit",
                            "interface_ids": ["REQ-GEN-STAGED-FUNC-CALC"],
                        }
                    ]
                },
                call_id="c1",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/calc.test.js", "content": test_code},
                call_id="c2",
            ),
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "One unit test for the staged calculator contract.",
                    "tests": [
                        {
                            "test_id": "T-STAGED-ADD",
                            "req_id": node_id,
                            "interface_ids": ["REQ-GEN-STAGED-FUNC-CALC"],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/calc.test.js",
                            "first_line": "test('add', () => {",
                        }
                    ],
                    "files_written": ["backend/tests/unit/calc.test.js"],
                },
                call_id="c3",
            ),
        ]
    )

    tests, _ = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert model.call_count == 4
    assert tests is not None
    assert tests[0]["interface_ids"] == ["REQ-GEN-STAGED-FUNC-CALC"]


def test_test_generator_delete_cannot_escape_the_workspace_root(tmp_project_dir: Path, arc_runtime) -> None:
    """Green-baseline rejection deletions stay inside the agent's root.

    The discipline now permits `delete` for test assets in the
    test_generation stage; the filesystem backend's virtual-mode resolution
    (root containment, traversal rejection) is what keeps that permission
    from touching files outside the workspace. This locks the guarantee in
    through the real deep-agents backend.
    """
    node_id = "REQ-GEN-DELETE"
    seed_requirement(arc_runtime, node_id)

    test_code = "test('add', () => { expect(add(1, 1)).toBe(2); });\n"
    protected = tmp_project_dir / "protected.txt"
    protected.write_text("must survive\n", encoding="utf-8")

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {
                            "file_path": "backend/tests/unit/calc.test.js",
                            "type": "Unit",
                            "interface_ids": [],
                        }
                    ]
                },
                call_id="c0",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/calc.test.js", "content": test_code},
                call_id="c1",
            ),
            # Traversal attempt: must be rejected by the backend, not delete
            # anything outside the root.
            faux_tool_call(
                "delete",
                {"file_path": "/workspace/tests/../../protected.txt"},
                call_id="c2",
            ),
            faux_tool_call(
                "delete",
                {"file_path": "/workspace/backend/tests/unit/calc.test.js"},
                call_id="c3",
            ),
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "Deleted the tautological test.",
                    "tests": [],
                    "files_written": [],
                },
                call_id="c4",
            ),
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Calculator", "description": "Add two numbers"},
        )
    )

    # The in-root test asset was really deleted; the traversal attempt was
    # rejected and the protected file survived.
    assert tests == []
    assert not (tmp_project_dir / "backend" / "tests" / "unit" / "calc.test.js").exists()
    assert protected.read_text(encoding="utf-8") == "must survive\n"


def test_test_generator_delete_cannot_escape_via_symlink(tmp_project_dir: Path, arc_runtime) -> None:
    """A symlink inside the test tree cannot point the delete outside the root.

    The backend resolves virtual paths through ``Path.resolve()`` and then
    enforces ``relative_to(root_dir)``; a symlink under ``tests/`` pointing
    at a file outside the workspace fails that containment (verified
    empirically: ValueError "outside root directory", target untouched).
    This locks the behavior through the deep-agents filesystem backend.
    """
    import shutil as _shutil

    node_id = "REQ-GEN-SYMLINK"
    seed_requirement(arc_runtime, node_id)

    test_code = "test('add', () => { expect(add(1, 1)).toBe(2); });\n"
    outside_dir = tmp_project_dir.parent / "pr35-symlink-outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    protected = outside_dir / "protected.txt"
    protected.write_text("must survive\n", encoding="utf-8")
    link = tmp_project_dir / "backend" / "tests" / "unit" / "escape.spec.ts"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(protected)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires elevated privileges on this platform")

    try:
        model = FauxChatModel(
            responses=[
                faux_tool_call(
                    "declare_test_manifest",
                    {
                        "files": [
                            {
                                "file_path": "backend/tests/unit/calc.test.js",
                                "type": "Unit",
                                "interface_ids": [],
                            }
                        ]
                    },
                    call_id="c0",
                ),
                faux_tool_call(
                    "write_file",
                    {"file_path": "/workspace/backend/tests/unit/calc.test.js", "content": test_code},
                    call_id="c1",
                ),
                # Symlink escape attempt: deleting the link must be refused
                # (resolved target lies outside the workspace root).
                faux_tool_call(
                    "delete",
                    {"file_path": "/workspace/tests/unit/escape.spec.ts"},
                    call_id="c2",
                ),
                faux_tool_call(
                    "delete",
                    {"file_path": "/workspace/backend/tests/unit/calc.test.js"},
                    call_id="c3",
                ),
                faux_tool_call(
                    "TestGenerationResponse",
                    {
                        "summary": "Deleted the tautological test.",
                        "tests": [],
                        "files_written": [],
                    },
                    call_id="c4",
                ),
            ]
        )

        tests, _output = asyncio.run(
            make_generator(tmp_project_dir, model).run(
                node_id,
                {"name": "Calculator", "description": "Add two numbers"},
            )
        )

        assert tests == []
        assert not (tmp_project_dir / "backend" / "tests" / "unit" / "calc.test.js").exists()
        # The out-of-root target survived; the refused delete never ran.
        assert protected.read_text(encoding="utf-8") == "must survive\n"
    finally:
        _shutil.rmtree(outside_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# manifest-first test generation (declare_test_manifest lock)
# ---------------------------------------------------------------------------


def test_test_generator_blocks_writes_before_declaration(tmp_project_dir: Path, arc_runtime) -> None:
    """A test-file write before the manifest declaration is hard-blocked.

    This is the core manifest-first invariant: the model cannot create a test
    file whose path was never declared, so path churn (renames, duplicate
    formats) fails at write time instead of surfacing as manifest drift.
    """
    node_id = "REQ-GEN-LOCK-1"
    seed_requirement(arc_runtime, node_id)

    model = FauxChatModel(
        responses=[
            # Undeclared write attempt: blocked by the discipline.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/calc.test.js", "content": "export {};\n"},
                call_id="c1",
            ),
            # The model then declares and writes the declared path.
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {"file_path": "backend/tests/unit/calc.test.js", "type": "Unit", "interface_ids": []}
                    ]
                },
                call_id="c2",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/calc.test.js", "content": "test('add', () => { expect(add(1, 1)).toBe(2); });\n"},
                call_id="c3",
            ),
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "One unit test for add().",
                    "tests": [
                        {
                            "test_id": "T-ADD",
                            "req_id": node_id,
                            "interface_ids": [],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/calc.test.js",
                            "first_line": "test('add', () => {",
                        }
                    ],
                    "files_written": ["backend/tests/unit/calc.test.js"],
                },
                call_id="c4",
            ),
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert tests is not None and len(tests) == 1
    assert (tmp_project_dir / "backend" / "tests" / "unit" / "calc.test.js").read_text(encoding="utf-8").startswith("test('add'")


def test_test_generator_blocks_writes_outside_the_declared_manifest(tmp_project_dir: Path, arc_runtime) -> None:
    """Renames and duplicate paths are dead ends once the manifest is locked.

    REQ-1's sessionHeader rename chain and the passwordStrength double-format
    attempt were both "path not settled" churn; with the lock, the second
    path cannot be created at all, and the model must return to the declared
    file.
    """
    node_id = "REQ-GEN-LOCK-2"
    seed_requirement(arc_runtime, node_id)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {"file_path": "backend/tests/unit/auth.test.js", "type": "Unit", "interface_ids": []}
                    ]
                },
                call_id="c1",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/auth.test.js", "content": "test('login', () => { expect(login()).toBe(false); });\n"},
                call_id="c2",
            ),
            # Rename attempt: blocked (not declared).
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/sessionHeader.test.js", "content": "test('login', () => { expect(login()).toBe(false); });\n"},
                call_id="c3",
            ),
            # Back on the declared path: the final manifest is returned.
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "Auth test reworked in place.",
                    "tests": [
                        {
                            "test_id": "T-AUTH",
                            "req_id": node_id,
                            "interface_ids": [],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/auth.test.js",
                            "first_line": "test('login', () => {",
                        }
                    ],
                    "files_written": ["backend/tests/unit/auth.test.js"],
                },
                call_id="c4",
            ),
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Auth", "description": "Login flow"},
        )
    )

    assert tests is not None and len(tests) == 1
    assert tests[0]["file_path"] == "backend/tests/unit/auth.test.js"
    # The rename target was never created.
    assert not (tmp_project_dir / "backend" / "tests" / "unit" / "sessionHeader.test.js").exists()


def test_test_generator_undeclared_manifest_entry_fails_the_pass(tmp_project_dir: Path, arc_runtime) -> None:
    """Returning manifest rows for never-declared paths fails the pass.

    The model cannot register coverage it never declared (phantom rows), even
    if the file exists on disk from an earlier node.
    """
    node_id = "REQ-GEN-LOCK-3"
    seed_requirement(arc_runtime, node_id)

    stale = tmp_project_dir / "backend" / "tests" / "unit" / "stale.test.js"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("test('old', () => { expect(true).toBe(true); });\n", encoding="utf-8")

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {"file_path": "backend/tests/unit/auth.test.js", "type": "Unit", "interface_ids": []}
                    ]
                },
                call_id="c1",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/auth.test.js", "content": "test('login', () => { expect(login()).toBe(false); });\n"},
                call_id="c2",
            ),
            # Phantom row: the stale file exists on disk but was never declared.
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "Auth tests.",
                    "tests": [
                        {
                            "test_id": "T-STALE",
                            "req_id": node_id,
                            "interface_ids": [],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/stale.test.js",
                            "first_line": "test('old', () => {",
                        }
                    ],
                    "files_written": ["backend/tests/unit/auth.test.js"],
                },
                call_id="c3",
            ),
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Auth", "description": "Login flow"},
        )
    )

    assert tests is None


def test_test_generator_written_file_dropped_from_manifest_is_reattached(tmp_project_dir: Path, arc_runtime) -> None:
    """A declared, written file whose row was dropped from the answer keeps
    its registration: the row is re-attached from the declaration."""
    node_id = "REQ-GEN-LOCK-4"
    seed_requirement(arc_runtime, node_id)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {"file_path": "backend/tests/unit/auth.test.js", "type": "Unit", "interface_ids": []}
                    ]
                },
                call_id="c1",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/auth.test.js", "content": "test('login', () => { expect(login()).toBe(false); });\n"},
                call_id="c2",
            ),
            # The answer forgets the manifest row for the written file.
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "Auth tests.",
                    "tests": [],
                    "files_written": ["backend/tests/unit/auth.test.js"],
                },
                call_id="c3",
            ),
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Auth", "description": "Login flow"},
        )
    )

    assert tests is not None and len(tests) == 1
    assert tests[0]["file_path"] == "backend/tests/unit/auth.test.js"
    assert tests[0]["type"] == "Unit"
    assert tests[0].get("manifest_reattached") is True
    # The re-attached row stays node-scoped: its mechanical test_id carries
    # the node prefix and req_id names the owning node (manifest contract).
    assert tests[0]["test_id"].startswith(node_id)
    assert tests[0]["req_id"] == node_id


def test_test_generator_helpers_stay_writable_without_declaration(tmp_project_dir: Path, arc_runtime) -> None:
    """Test helpers and runner configs are not manifest entries: they stay
    writable before and after the declaration (no lock on non-test assets)."""
    node_id = "REQ-GEN-LOCK-5"
    seed_requirement(arc_runtime, node_id)

    model = FauxChatModel(
        responses=[
            # Helper written BEFORE any declaration: allowed.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/tests/setup-tests.ts", "content": "export {}\n"},
                call_id="c1",
            ),
            faux_tool_call(
                "declare_test_manifest",
                {
                    "files": [
                        {"file_path": "backend/tests/unit/auth.test.js", "type": "Unit", "interface_ids": []}
                    ]
                },
                call_id="c2",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/auth.test.js", "content": "test('login', () => { expect(login()).toBe(false); });\n"},
                call_id="c3",
            ),
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "Auth tests with helper.",
                    "tests": [
                        {
                            "test_id": "T-AUTH",
                            "req_id": node_id,
                            "interface_ids": [],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/auth.test.js",
                            "first_line": "test('login', () => {",
                        }
                    ],
                    "files_written": ["tests/setup-tests.ts", "backend/tests/unit/auth.test.js"],
                },
                call_id="c4",
            ),
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).run(
            node_id,
            {"name": "Auth", "description": "Login flow"},
        )
    )

    assert tests is not None and len(tests) == 1
    assert (tmp_project_dir / "tests" / "setup-tests.ts").exists()


def test_test_generator_repair_pass_cannot_introduce_new_test_paths(
    tmp_project_dir: Path, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The green-baseline repair lock is pre-seeded with the previous
    manifest: deleting a green file and re-adding the coverage under a fresh
    name (the rename escape) is blocked at write time."""
    node_id = "REQ-GEN-LOCK-6"
    seed_requirement(arc_runtime, node_id)

    previous_manifest = [
        {
            "test_id": "T-GREEN",
            "req_id": node_id,
            "interface_ids": [],
            "type": "Unit",
            "file_path": "backend/tests/unit/green.test.js",
            "first_line": "test('tautology', () => {",
        }
    ]
    green_file = tmp_project_dir / "backend" / "tests" / "unit" / "green.test.js"
    green_file.parent.mkdir(parents=True, exist_ok=True)
    green_file.write_text("test('tautology', () => { expect(true).toBe(true); });\n", encoding="utf-8")

    original_current_interface_ids = TestGenerator._current_interface_ids
    current_interface_id_calls = 0

    def count_current_interface_id_reads(
        requested_node_id: str,
        interfaces: list[dict[str, object]] | None = None,
    ) -> list[str]:
        nonlocal current_interface_id_calls
        current_interface_id_calls += 1
        return original_current_interface_ids(requested_node_id, interfaces)

    monkeypatch.setattr(
        TestGenerator,
        "_current_interface_ids",
        staticmethod(count_current_interface_id_reads),
    )

    model = FauxChatModel(
        responses=[
            # Rename escape attempt: the fresh path is not in the pre-seeded
            # lock, so the write is blocked.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/greenV2.test.js", "content": "test('tautology', () => { expect(true).toBe(true); });\n"},
                call_id="c1",
            ),
            # Compliant repair: rewrite the declared (pre-seeded) path.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/green.test.js", "content": "test('login', () => { expect(login()).toBe(false); });\n"},
                call_id="c2",
            ),
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "Reworked the tautological test.",
                    "tests": [
                        {
                            "test_id": "T-GREEN",
                            "req_id": node_id,
                            "interface_ids": [],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/green.test.js",
                            "first_line": "test('login', () => {",
                        }
                    ],
                    "files_written": ["backend/tests/unit/green.test.js"],
                },
                call_id="c3",
            ),
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).repair_green_baseline(
            node_id,
            {"name": "", "description": ""},
            green_evidence=[
                {
                    "file_path": "backend/tests/unit/green.test.js",
                    "type": "Unit",
                    "output_summary": "1 passed",
                }
            ],
            previous_manifest=previous_manifest,
        )
    )

    assert tests is not None and len(tests) == 1
    assert tests[0]["file_path"] == "backend/tests/unit/green.test.js"
    assert not (tmp_project_dir / "backend" / "tests" / "unit" / "greenV2.test.js").exists()
    assert current_interface_id_calls == 1


def test_test_generator_repair_prose_answer_is_none_not_empty_manifest(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A repair session that ends in prose (no parseable manifest structure)
    returns ``None`` — distinct from a declared-empty manifest — so the caller
    spends rejection budget retrying instead of reading parse damage as "the
    model deleted every test" (issue #172)."""
    node_id = "REQ-GEN-REPAIR-PROSE"
    seed_requirement(arc_runtime, node_id)

    previous_manifest = [
        {
            "test_id": "T-GREEN",
            "req_id": node_id,
            "interface_ids": [],
            "type": "Unit",
            "file_path": "backend/tests/unit/green.test.js",
            "first_line": "test('tautology', () => {",
        }
    ]
    green_file = tmp_project_dir / "backend" / "tests" / "unit" / "green.test.js"
    green_file.parent.mkdir(parents=True, exist_ok=True)
    green_file.write_text("test('tautology', () => { expect(true).toBe(true); });\n", encoding="utf-8")

    # Prose ending: no structured response and no JSON the runner could
    # parse, so the normalized payload carries no tests structure at all.
    model = FauxChatModel(responses=[faux_text("I removed the tautological test file.")])

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).repair_green_baseline(
            node_id,
            {"name": "", "description": ""},
            green_evidence=[
                {"file_path": "backend/tests/unit/green.test.js", "type": "Unit", "output_summary": "1 passed"}
            ],
            previous_manifest=previous_manifest,
        )
    )

    assert tests is None


def test_test_generator_repair_structured_empty_manifest_is_declared_empty(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A structured ``tests: []`` answer IS a manifest decision: repair
    returns ``[]`` (legitimate empty), not ``None`` (unparseable)."""
    node_id = "REQ-GEN-REPAIR-DECLARED-EMPTY"
    seed_requirement(arc_runtime, node_id)

    previous_manifest = [
        {
            "test_id": "T-GREEN",
            "req_id": node_id,
            "interface_ids": [],
            "type": "Unit",
            "file_path": "backend/tests/unit/green.test.js",
            "first_line": "test('tautology', () => {",
        }
    ]
    green_file = tmp_project_dir / "backend" / "tests" / "unit" / "green.test.js"
    green_file.parent.mkdir(parents=True, exist_ok=True)
    green_file.write_text("test('tautology', () => { expect(true).toBe(true); });\n", encoding="utf-8")

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "TestGenerationResponse",
                {"summary": "Every test was tautological; deleted them all.", "tests": [], "files_written": []},
                call_id="c1",
            )
        ]
    )

    tests, _output = asyncio.run(
        make_generator(tmp_project_dir, model).repair_green_baseline(
            node_id,
            {"name": "", "description": ""},
            green_evidence=[
                {"file_path": "backend/tests/unit/green.test.js", "type": "Unit", "output_summary": "1 passed"}
            ],
            previous_manifest=previous_manifest,
        )
    )

    assert tests == []


# ---------------------------------------------------------------------------
# step-budget salvage (arc-output3 regression)
# ---------------------------------------------------------------------------


def test_test_generator_salvages_step_budget_when_all_declared_files_written(
    tmp_project_dir: Path, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """arc-output3 (2026-09-19): the model wrote every declared test file,
    then looped on delete-rewrite cycles until the session step budget
    raised GraphRecursionError — failing the whole DESIGN task although the
    complete test suite was on disk and untouched. With the same shape (a
    session crash after the declared files are materialized), the stage must
    complete via the mechanical manifest re-attachment instead.

    The crash is injected at the ``ainvoke_stage_agent`` seam — the real
    session runs to normal completion first (declare, write, structured
    answer), then the wrapper raises exactly what LangGraph raises on a
    step-budget trip. Tripping the real limit would make the test depend on
    opaque graph step accounting; the budget trip itself is covered by
    ``test_agent_step_budget.py``.
    """

    import agents.runtime.stage_session as stage_session_module
    from langgraph.errors import GraphRecursionError

    node_id = "REQ-STEP-BUDGET-1"
    seed_requirement(arc_runtime, node_id)

    test_code = "test('add', () => { expect(add(1, 1)).toBe(2); });\n"
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "declare_test_manifest",
                {"files": [{"file_path": "backend/tests/unit/calc.test.js", "type": "Unit", "interface_ids": []}]},
                call_id="c0",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/backend/tests/unit/calc.test.js", "content": test_code},
                call_id="c1",
            ),
            faux_tool_call(
                "TestGenerationResponse",
                {
                    "summary": "One unit test for add().",
                    "tests": [
                        {
                            "test_id": "T-ADD",
                            "req_id": node_id,
                            "interface_ids": [],
                            "type": "Unit",
                            "file_path": "backend/tests/unit/calc.test.js",
                            "first_line": "test('add', () => {",
                        }
                    ],
                    "files_written": ["backend/tests/unit/calc.test.js"],
                },
                call_id="c2",
            ),
        ]
    )

    real_ainvoke = stage_session_module.ainvoke_stage_agent

    async def crash_after_full_session(agent, **kwargs):
        await real_ainvoke(agent, **kwargs)
        raise GraphRecursionError("Recursion limit of 300 reached without hitting a stop condition.")

    monkeypatch.setattr(stage_session_module, "ainvoke_stage_agent", crash_after_full_session)

    logs: list[str] = []

    def collect_log(agent_name: str, message: str, status: str | None, node_id: str | None) -> None:
        logs.append(message)

    tests, output_text = asyncio.run(
        make_generator(tmp_project_dir, model, log_cb=collect_log).run(
            node_id,
            {"name": "Calculator", "description": "Add two numbers"},
        )
    )

    assert tests is not None and len(tests) == 1
    row = tests[0]
    assert row["file_path"] == "backend/tests/unit/calc.test.js"
    assert row["manifest_reattached"] is True
    # Mechanical id shape: <NODE>-T-<file stem with dots dashed> (the .test
    # marker is part of the stem for `calc.test.js`).
    assert row["test_id"] == "REQ-STEP-BUDGET-1-T-CALC-TEST"
    assert row["type"] == "Unit"
    assert "REQ-STEP-BUDGET-1-T-CALC-TEST" in output_text
    # The salvaged suite is the real on-disk artifact.
    assert (tmp_project_dir / "backend" / "tests" / "unit" / "calc.test.js").read_text(encoding="utf-8") == test_code
    # The salvage is observable, not silent.
    assert any("step budget" in message and "salvaging" in message for message in logs), logs


def test_step_budget_salvage_declines_when_a_declared_file_is_missing(tmp_project_dir: Path) -> None:
    """A crashed session that never finished the declared work must keep
    failing the node: salvaging a partial suite is a silent quality cut."""

    generator = _salvage_probe_generator(tmp_project_dir)
    manifest_lock = TestManifestLock(
        declared_files={
            "backend/tests/unit/a.test.js": DeclaredTestFile(
                file_path="backend/tests/unit/a.test.js", test_type="Unit"
            ),
            "backend/tests/unit/b.test.js": DeclaredTestFile(
                file_path="backend/tests/unit/b.test.js", test_type="Unit"
            ),
        }
    )
    built = _stub_build_with_written_paths(["/workspace/backend/tests/unit/a.test.js"])

    import asyncio as _asyncio
    from langgraph.errors import GraphRecursionError

    payload = _asyncio.run(
        generator._salvage_step_budget(
            node_id="REQ-SALVAGE-1",
            manifest_lock=manifest_lock,
            built=built,
            exc=GraphRecursionError("Recursion limit of 300 reached"),
        )
    )
    assert payload is None


def test_step_budget_salvage_declines_without_a_locked_manifest(tmp_project_dir: Path) -> None:
    generator = _salvage_probe_generator(tmp_project_dir)
    built = _stub_build_with_written_paths(["/workspace/backend/tests/unit/a.test.js"])

    import asyncio as _asyncio
    from langgraph.errors import GraphRecursionError

    payload = _asyncio.run(
        generator._salvage_step_budget(
            node_id="REQ-SALVAGE-1",
            manifest_lock=TestManifestLock(),
            built=built,
            exc=GraphRecursionError("Recursion limit of 300 reached"),
        )
    )
    assert payload is None


def test_step_budget_salvage_declines_without_materialized_paths(tmp_project_dir: Path) -> None:
    generator = _salvage_probe_generator(tmp_project_dir)
    manifest_lock = TestManifestLock(
        declared_files={
            "backend/tests/unit/a.test.js": DeclaredTestFile(
                file_path="backend/tests/unit/a.test.js", test_type="Unit"
            )
        }
    )

    import asyncio as _asyncio
    from langgraph.errors import GraphRecursionError

    payload = _asyncio.run(
        generator._salvage_step_budget(
            node_id="REQ-SALVAGE-1",
            manifest_lock=manifest_lock,
            built=_stub_build_with_written_paths([]),
            exc=GraphRecursionError("Recursion limit of 300 reached"),
        )
    )
    assert payload is None


def test_step_budget_salvage_rows_flow_through_the_normal_reconciliation(tmp_project_dir: Path) -> None:
    """The salvaged payload must satisfy the same post-pass contract as a
    model-authored one: normalize keeps every row and the first-pass
    reconciliation re-attaches nothing new and rejects nothing."""

    import asyncio as _asyncio
    from langgraph.errors import GraphRecursionError

    from agents.results import normalize_test_manifest_payload

    node_id = "REQ-SALVAGE-2"
    generator = _salvage_probe_generator(tmp_project_dir)
    manifest_lock = TestManifestLock(
        declared_files={
            "backend/tests/unit/a.test.js": DeclaredTestFile(
                file_path="backend/tests/unit/a.test.js",
                test_type="Unit",
                interface_ids=["IF-A"],
            )
        }
    )
    written = ["/workspace/backend/tests/unit/a.test.js"]
    payload = _asyncio.run(
        generator._salvage_step_budget(
            node_id=node_id,
            manifest_lock=manifest_lock,
            built=_stub_build_with_written_paths(written),
            exc=GraphRecursionError("Recursion limit of 300 reached"),
        )
    )
    assert payload is not None

    tests = normalize_test_manifest_payload(payload)
    reconciled, output_text = _asyncio.run(
        generator._reconcile_first_pass(
            node_id=node_id,
            tests=tests,
            raw_payload=payload,
            manifest_lock=manifest_lock,
            built=_stub_build_with_written_paths(written),
        )
    )
    assert reconciled is not None and len(reconciled) == 1
    assert reconciled[0]["test_id"] == "REQ-SALVAGE-2-T-A-TEST"
    assert reconciled[0]["interface_ids"] == ["IF-A"]
    assert payload["files_written"] == ["backend/tests/unit/a.test.js"]
    assert "REQ-SALVAGE-2-T-A-TEST" in output_text


def _salvage_probe_generator(tmp_project_dir: Path) -> TestGenerator:
    return TestGenerator(
        model="faux:probe",
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )


def _stub_build_with_written_paths(paths: list[str]):
    from types import SimpleNamespace

    from agents.runtime.factory import StageAgentBuild

    return StageAgentBuild(
        agent=SimpleNamespace(),
        stage_discipline=SimpleNamespace(materialized_paths=lambda: list(paths)),
    )


# ---------------------------------------------------------------------------
# merge-conflict retry context (prompt-side guard)
# ---------------------------------------------------------------------------


def test_merge_conflict_context_requires_the_retry_flag(tmp_project_dir: Path, arc_runtime) -> None:
    """The DESIGN prompt only receives conflict paths when the one-shot
    retry flag is set: a fresh DESIGN pass must not be steered around a
    previous run's stale paths."""
    from core import sessions

    node_id = "REQ-CONFLICT-1"
    sessions.merge_node_session(
        node_id,
        {"merge_conflict_context": {"paths": ["shared.js"], "phase": "design"}},
    )
    assert InterfaceDesigner._load_merge_conflict_context(node_id) is None

    sessions.merge_node_session(node_id, {"merge_conflict_retry_used": True})
    assert InterfaceDesigner._load_merge_conflict_context(node_id) == {
        "paths": ["shared.js"],
        "phase": "design",
    }


def test_merge_conflict_context_validates_the_paths_shape(tmp_project_dir: Path, arc_runtime) -> None:
    from core import sessions

    node_id = "REQ-CONFLICT-2"
    sessions.merge_node_session(
        node_id,
        {"merge_conflict_retry_used": True, "merge_conflict_context": {"paths": "shared.js"}},
    )
    assert InterfaceDesigner._load_merge_conflict_context(node_id) is None

    sessions.merge_node_session(
        node_id,
        {"merge_conflict_retry_used": True, "merge_conflict_context": {"paths": ["ok.ts", 3]}},
    )
    assert InterfaceDesigner._load_merge_conflict_context(node_id) is None

    sessions.merge_node_session(node_id, {"merge_conflict_context": "not-a-dict"})
    assert InterfaceDesigner._load_merge_conflict_context(node_id) is None

    assert InterfaceDesigner._load_merge_conflict_context("REQ-CONFLICT-NEVER") is None


def test_tdd_merge_conflict_context_requires_the_retry_flag_and_phase(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """The TDD prompt only receives conflict paths for an IMPLEMENT-phase
    one-shot retry: a fresh IMPLEMENT pass must not be steered around stale
    paths, and a DESIGN-conflict record must not leak into the TDD prompt."""
    from core import sessions

    node_id = "REQ-CONFLICT-3"
    sessions.merge_node_session(
        node_id,
        {"merge_conflict_context": {"paths": ["shared.js"], "phase": "implement"}},
    )
    assert TestDrivenDeveloper._load_merge_conflict_context(node_id) is None

    sessions.merge_node_session(node_id, {"merge_conflict_retry_used": True})
    assert TestDrivenDeveloper._load_merge_conflict_context(node_id) == {
        "paths": ["shared.js"],
        "phase": "implement",
    }

    # A design-phase record belongs to the DESIGN prompt, not the TDD prompt.
    sessions.merge_node_session(
        node_id,
        {"merge_conflict_context": {"paths": ["shared.js"], "phase": "design"}},
    )
    assert TestDrivenDeveloper._load_merge_conflict_context(node_id) is None


def test_tdd_merge_conflict_context_validates_the_paths_shape(
    tmp_project_dir: Path, arc_runtime
) -> None:
    from core import sessions

    node_id = "REQ-CONFLICT-4"
    sessions.merge_node_session(
        node_id,
        {"merge_conflict_retry_used": True, "merge_conflict_context": {"paths": "shared.js", "phase": "implement"}},
    )
    assert TestDrivenDeveloper._load_merge_conflict_context(node_id) is None

    sessions.merge_node_session(
        node_id,
        {"merge_conflict_retry_used": True, "merge_conflict_context": {"paths": ["ok.ts", 3], "phase": "implement"}},
    )
    assert TestDrivenDeveloper._load_merge_conflict_context(node_id) is None

    sessions.merge_node_session(node_id, {"merge_conflict_context": "not-a-dict"})
    assert TestDrivenDeveloper._load_merge_conflict_context(node_id) is None

    assert TestDrivenDeveloper._load_merge_conflict_context("REQ-CONFLICT-NEVER") is None

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

from agents.interface_designer import InterfaceDesigner
from agents.test_generator import TestGenerator
from tests.helpers.faux import FauxChatModel, faux_tool_call


def seed_requirement(runtime, node_id: str) -> None:
    runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )


def make_designer(tmp_project_dir: Path, model: FauxChatModel) -> InterfaceDesigner:
    return InterfaceDesigner(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )


def make_generator(tmp_project_dir: Path, model: FauxChatModel) -> TestGenerator:
    return TestGenerator(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
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

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
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


def test_test_generator_writes_test_asset_and_returns_manifest(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-GEN-1"
    seed_requirement(arc_runtime, node_id)

    test_code = "def test_add():\n    assert add(1, 1) == 2\n"
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/tests/unit/test_calc.py", "content": test_code},
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
                            "file_path": "tests/unit/test_calc.py",
                            "first_line": "def test_add():",
                        }
                    ],
                    "files_written": ["tests/unit/test_calc.py"],
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

    assert model.call_count == 2
    assert tests is not None and len(tests) == 1
    assert tests[0]["test_id"] == "T-ADD"
    assert tests[0]["type"] == "Unit"
    assert tests[0]["file_path"] == "tests/unit/test_calc.py"
    assert "T-ADD" in output_text
    # The scripted test asset really landed in the workspace.
    assert (tmp_project_dir / "tests" / "unit" / "test_calc.py").read_text(encoding="utf-8") == test_code


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

    test_code = "def test_add():\n    assert add(1, 1) == 2\n"
    protected = tmp_project_dir / "protected.txt"
    protected.write_text("must survive\n", encoding="utf-8")

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/tests/unit/test_calc.py", "content": test_code},
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
                {"file_path": "/workspace/tests/unit/test_calc.py"},
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
    assert not (tmp_project_dir / "tests" / "unit" / "test_calc.py").exists()
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

    test_code = "def test_add():\n    assert add(1, 1) == 2\n"
    outside_dir = tmp_project_dir.parent / "pr35-symlink-outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    protected = outside_dir / "protected.txt"
    protected.write_text("must survive\n", encoding="utf-8")
    link = tmp_project_dir / "tests" / "unit" / "escape.spec.ts"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(protected)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires elevated privileges on this platform")

    try:
        model = FauxChatModel(
            responses=[
                faux_tool_call(
                    "write_file",
                    {"file_path": "/workspace/tests/unit/test_calc.py", "content": test_code},
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
                    {"file_path": "/workspace/tests/unit/test_calc.py"},
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
        assert not (tmp_project_dir / "tests" / "unit" / "test_calc.py").exists()
        # The out-of-root target survived; the refused delete never ran.
        assert protected.read_text(encoding="utf-8") == "must survive\n"
    finally:
        _shutil.rmtree(outside_dir, ignore_errors=True)


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

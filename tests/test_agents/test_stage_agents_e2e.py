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
    The adapter must re-ask once on the same thread when the discipline
    observed real writes but the response recorded no interface records.
    """
    node_id = "REQ-DESIGN-REPAIR"
    seed_requirement(arc_runtime, node_id)

    skeleton = "from dataclasses import dataclass\n\n\n@dataclass\nclass CalcContract:\n    value: int\n"
    interface_record = {
        "interface_id": "IF-CALC",
        "type": "FUNC",
        "name": "add",
        "responsibility": "Add two integers",
        "file_path": "src/contracts/calc.py",
        "first_line": "@dataclass",
        "callers": [],
        "callees": [],
    }
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
            # Repair pass on the same thread.
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "Designed the calculator contract.",
                    "interfaces": [interface_record],
                    "files_written": ["/workspace/src/contracts/calc.py"],
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
    assert [item["interface_id"] for item in bundle["interfaces"]] == ["IF-CALC"]
    assert bundle["files_written"] == ["/workspace/src/contracts/calc.py"]


def test_interface_designer_keeps_materialized_marker_when_repair_stays_empty(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-DESIGN-REPAIR-FAIL"
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
            faux_tool_call(
                "InterfaceDesignResponse",
                {"summary": "Still nothing structured.", "interfaces": [], "files_written": []},
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
    assert bundle["interfaces"] == []
    # The workflow-level hard gate reads this marker and fails the DESIGN phase.
    assert bundle["materialized_paths"] == ["/workspace/src/contracts/calc.py"]


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
    assert bundle["files_written"] == ["src/contracts/calc.py"]


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

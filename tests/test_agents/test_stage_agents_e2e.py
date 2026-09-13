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


def test_interface_designer_glue_write_is_blocked_and_node_private_write_lands(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """The registration contract is enforced mechanically: a DESIGN write to a
    shared glue file is rejected by stage discipline, the designer recovers by
    materializing the contract into a node-private registration module, and the
    glue file stays untouched in the workspace.
    """

    node_id = "REQ-DESIGN-GLUE"
    seed_requirement(arc_runtime, node_id)

    section = "function NavSection() {\n  return <nav />;\n}\n\nexport default NavSection;\n"
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/frontend/src/App.tsx", "content": "export {}"},
                call_id="c1",
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/frontend/src/sections/home/NavSection.tsx", "content": section},
                call_id="c2",
            ),
            faux_tool_call(
                "InterfaceDesignResponse",
                {
                    "summary": "App.tsx write was rejected as shared glue; NavSection landed as a section module.",
                    "interfaces": [
                        {
                            "interface_id": "IF-NAV",
                            "type": "UI",
                            "name": "NavSection",
                            "responsibility": "Home navigation section",
                            "file_path": "/workspace/frontend/src/sections/home/NavSection.tsx",
                            "first_line": "function NavSection() {",
                            "callers": [],
                            "callees": [],
                        }
                    ],
                    "files_written": ["/workspace/frontend/src/sections/home/NavSection.tsx"],
                },
                call_id="c3",
            ),
        ]
    )

    bundle = asyncio.run(
        make_designer(tmp_project_dir, model).run(
            node_id=node_id,
            requirement_data={"name": "Navigation", "description": "Home navigation"},
        )
    )

    assert model.call_count == 3
    assert bundle["interfaces"][0]["interface_id"] == "IF-NAV"
    # The node-private section module really landed.
    assert (tmp_project_dir / "frontend" / "src" / "sections" / "home" / "NavSection.tsx").read_text(
        encoding="utf-8"
    ) == section
    # The template-owned glue file was never created or modified.
    assert not (tmp_project_dir / "frontend" / "src" / "App.tsx").exists()

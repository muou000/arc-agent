"""Tests for the DESIGN append-only continuation tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from agents.tools.file_append import APPEND_FILE_TOOL_DESCRIPTION, build_append_file_tool
from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call


def _invoke(tool, **arguments: str) -> str:
    return str(asyncio.run(tool.ainvoke(arguments)))


def test_append_requires_an_initial_skeleton(tmp_path: Path) -> None:
    tool = build_append_file_tool(workspace_root=str(tmp_path))

    result = _invoke(tool, file_path="/workspace/src/missing.ts", content="export {};")

    assert "does not exist" in result
    assert not (tmp_path / "src" / "missing.ts").exists()


def test_append_adds_lines_without_a_per_file_ceiling(tmp_path: Path) -> None:
    target = tmp_path / "src" / "page.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("export function Page() {\n", encoding="utf-8")
    tool = build_append_file_tool(workspace_root=str(tmp_path))

    result = _invoke(tool, file_path="/workspace/src/page.tsx", content="  return null;\n}")

    assert "Appended 2 line(s)" in result
    assert target.read_text(encoding="utf-8") == "export function Page() {\n  return null;\n}\n"


def test_append_allows_files_over_the_removed_skeleton_ceiling(tmp_path: Path) -> None:
    """Issue #161: the per-file 160-line skeleton ceiling is removed (same ADR
    0005 rationale as the removed per-write gate) — a legal shape-only skeleton
    written in one compact write may exceed 160 lines and must stay appendable."""

    target = tmp_path / "src" / "page.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("x\n" * 165, encoding="utf-8")
    tool = build_append_file_tool(workspace_root=str(tmp_path))

    result = _invoke(tool, file_path="/workspace/src/page.tsx", content="y\n")

    assert "Appended 1 line(s)" in result
    assert target.read_text(encoding="utf-8") == "x\n" * 165 + "y\n"


def test_append_rejects_oversized_chunks_and_path_escape(tmp_path: Path) -> None:
    target = tmp_path / "src" / "page.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("x\n", encoding="utf-8")
    tool = build_append_file_tool(workspace_root=str(tmp_path))

    oversized = _invoke(tool, file_path="/workspace/src/page.tsx", content="x\n" * 81)
    safe_name = tmp_path / "src" / "..bar" / "safe.ts"
    safe_name.parent.mkdir(parents=True)
    safe_name.write_text("x\n", encoding="utf-8")
    safe_name_result = _invoke(tool, file_path="/workspace/src/..bar/safe.ts", content="y")
    escaped = _invoke(tool, file_path="/workspace/../outside.ts", content="x")

    assert "Appended" not in oversized
    assert "at most 80" in oversized
    # The rejection points at the #158 escape hatch, not at chunk-and-append.
    assert "not skeleton material" in oversized
    assert "Split the next cohesive" not in oversized
    assert "Appended 1 line(s)" in safe_name_result
    assert "traversal" in escaped or "outside the project root" in escaped


def test_tool_description_matches_design_only_boundary() -> None:
    """Issue #161: the tool description must not teach chunked skeleton
    building — the #158 rule is one compact write per skeleton, with the
    escape hatch in the stage response."""

    assert "each remaining section" not in APPEND_FILE_TOOL_DESCRIPTION
    assert "cohesive" not in APPEND_FILE_TOOL_DESCRIPTION
    assert "not a skeleton" in APPEND_FILE_TOOL_DESCRIPTION
    assert "stage response" in APPEND_FILE_TOOL_DESCRIPTION
    assert "80 lines per call" in APPEND_FILE_TOOL_DESCRIPTION
    assert "3 appends per file" in APPEND_FILE_TOOL_DESCRIPTION


def test_interface_design_agent_exposes_append_file(tmp_path: Path) -> None:
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/page.tsx", "content": "export function Page() {\n"},
                call_id="write-skeleton",
            ),
            faux_tool_call(
                "append_file",
                {"file_path": "/workspace/src/page.tsx", "content": "  return null;\n}\n"},
                call_id="append-section",
            ),
            faux_text("DONE"),
        ]
    )
    built = build_stage_agent(
        name="append-test-designer",
        stage="interface_design",
        model=model,
        system_prompt="Use the file tools.",
        response_format=None,
        workspace_root=str(tmp_path),
        writable_roots=[str(tmp_path)],
        skills=[],
        memory=[],
        tools=[],
    )

    payload = asyncio.run(
        ainvoke_stage_agent(
            built.agent,
            message="materialize the page skeleton",
            context=AgentRuntimeContext(
                node_id="REQ-APPEND",
                phase="DESIGN",
                app_type="web",
                workspace_root=str(tmp_path),
                requirement_path="",
            ),
            thread_id="REQ-APPEND:test",
            label="AppendTest",
        )
    )

    assert payload["summary"] == "DONE"
    assert (tmp_path / "src" / "page.tsx").read_text(encoding="utf-8") == (
        "export function Page() {\n  return null;\n}\n"
    )

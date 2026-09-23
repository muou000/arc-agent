"""Mount-surface pins: the tool set each stage's built agent really offers.

The capability table answers "may stage X call tool Y?" and ``build_stage_agent``
filters the adapters' mounted tools through it — but the model-visible surface
is assembled from several independent pieces: deepagents' default filesystem
suite, the harness-profile tool exclusion, ``DisableToolsMiddleware``, and the
mount-time capability filter. A deepagents upgrade that adds or removes a
default tool used to be visible only as silent runtime interception (or not at
all); issue #182 (grill Q8) pins the assembled surface instead.

Each test drives one scripted model turn through a real ``build_stage_agent``
agent per stage — the same build path the stage adapters use — and asserts the
exact tool-name set the model receives: capability-allowed mounted tools ∪
``append_file`` where the table allows it ∪ the deepagents filesystem suite −
the disabled builtins. The filesystem suite is deliberately hardcoded: an
upstream change turns these pins red and forces a conscious decision about the
table and prompts instead of a silent fallback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agents.runtime.capabilities import DISABLED_BUILTIN_TOOLS, capability_for
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from agents.tools.traceability import build_traceability_tools

from tests.helpers.faux import FauxChatModel, faux_text, tool_display_name

# deepagents' default filesystem suite (the FilesystemMiddleware tool set).
# Deliberately hardcoded, not derived: the pin exists to catch deepagents
# upgrades that change the default suite. The suite may also carry `execute`
# (CompositeBackend implements SandboxBackendProtocol); it never reaches
# bind_tools because DISABLED_BUILTIN_TOOLS excludes it, and the exact-set
# equality below keeps that exclusion load-bearing.
FILESYSTEM_BUILTIN_TOOLS = frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"})

_STAGE_PHASES = {
    "interface_design": "DESIGN",
    "test_generation": "DESIGN",
    "implementation": "IMPLEMENT",
}

# Tools the real stage adapters mount per stage (agents/interface_designer.py,
# agents/test_generator.py, agents/test_driven_developer.py). The traceability
# builders produce real closures here; the stage-specific system tools are
# name-bearing stubs — the pin judges the mount surface, not the tool bodies.
# The trailing *_NOT_MOUNTED names are tools other stages mount but this stage
# must never see: the mount-time capability filter (factory) has to drop them
# for the exact-set equality to hold, so a filter regression turns the pin red.
_STAGE_MOUNTED_TOOLS: dict[str, list[str]] = {
    "interface_design": [],
    "test_generation": ["declare_test_manifest", "append_file_NOT_MOUNTED"],
    "implementation": ["run_tests", "run_build", "install_dependencies", "append_file_NOT_MOUNTED"],
}


def _stub_tool(name: str):
    def tool(text: str) -> str:
        """Name-bearing stub for a stage system tool."""

        return text

    # Both name carriers: `__name__` for the callable path and `name` for any
    # BaseTool-style introspection, so the stub's identity survives either
    # reading of `tool_display_name`.
    tool.__name__ = name
    tool.name = name
    return tool


def _expected_tool_names(stage: str, mounted_names: list[str]) -> set[str]:
    expected = {name for name in mounted_names if capability_for(stage, name).allowed}
    if capability_for(stage, "append_file").allowed:
        expected.add("append_file")
    return expected | (FILESYSTEM_BUILTIN_TOOLS - DISABLED_BUILTIN_TOOLS)


@pytest.mark.parametrize("stage", ["interface_design", "test_generation", "implementation"])
def test_bound_tool_surface_matches_capability_table(stage: str, tmp_project_dir: Path) -> None:
    mounted: list[Any] = list(build_traceability_tools(node_id="REQ-MOUNT"))
    mounted.extend(_stub_tool(name) for name in _STAGE_MOUNTED_TOOLS[stage])

    model = FauxChatModel(responses=[faux_text("DONE")])
    built = build_stage_agent(
        name="mount_surface_probe",
        stage=stage,
        model=model,
        system_prompt="You are a test agent.",
        response_format=None,
        workspace_root=str(tmp_project_dir),
        writable_roots=[str(tmp_project_dir)],
        skills=[],
        memory=[],
        tools=mounted,
        checkpointer=None,
    )
    asyncio.run(
        ainvoke_stage_agent(
            built.agent,
            message="end the pass",
            context=AgentRuntimeContext(
                node_id="REQ-MOUNT",
                phase=_STAGE_PHASES[stage],
                app_type="web",
                workspace_root=str(tmp_project_dir),
                requirement_path="",
            ),
            thread_id=f"REQ-MOUNT:{stage}",
            label="MountSurfaceProbe",
        )
    )

    bound_sets = model.bound_tool_name_sets
    assert bound_sets, "the probe run must reach at least one model call"
    assert model.call_count == len(bound_sets), "every model call must have bound a tool set"
    # The surface is stable across turns; the first binding is the contract.
    for names in bound_sets[1:]:
        assert set(names) == set(bound_sets[0])

    bound = set(bound_sets[0])
    assert bound == _expected_tool_names(stage, [tool_display_name(tool) for tool in mounted])

    # The always-disabled builtins must be absent from every stage's surface —
    # exact-set equality above already implies it; spelled out so a failure
    # reads as the contract it is.
    assert "execute" not in bound
    assert "write_todos" not in bound
    assert "task" not in bound

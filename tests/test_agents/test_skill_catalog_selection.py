"""Generic skill selection: catalog injection, floor, and adapter wiring.

Skill choice is generic (pi-style progressive disclosure): every stage agent
attaches the ``/skills/`` source, the runtime skills middleware lists the whole
catalog in the system prompt, and the stage agent itself ``read_file``s whichever
``SKILL.md`` files match its task. This file pins:

- ``agents/skills/selection.py``: the deterministic safety floor (auth,
  failure repair) — still the only deterministic injection, with no keyword
  fallback for optional skills;
- ``agents/runtime/factory.py``: every declared ``SKILL.md`` stays readable
  once the skills source is attached;
- the three stage adapters: the skills source is attached unconditionally and
  no per-skill read whitelist exists anymore;
- ``agents/skills/planning.py``: the per-node planning agent is gone.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest

import agents.runtime.stage_session as stage_session_module
from agents.context.prompts.common import stage_skill_activation_policy
from agents.interface_designer import InterfaceDesigner
from agents.runtime.factory import _resolve_skill_instruction_paths, StageAgentBuild
from agents.runtime.stage_session import StageSession
from agents.skills.selection import (
    SKILLS_SOURCE,
    available_skill_names,
    implementation_skills,
    interface_design_skills,
    test_generation_skills as select_test_generation_skills,
)
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from tests.helpers.faux import FauxChatModel

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
SKILLS_PREFIX = "/skills"


def declared_skill_paths() -> list[str]:
    return [
        f"{SKILLS_PREFIX}/{skill_dir.name}/SKILL.md"
        for skill_dir in sorted(SKILLS_ROOT.iterdir())
        if (skill_dir / "SKILL.md").is_file()
    ]


# -- selection: deterministic safety floor -------------------------------------


def test_selection_floor_requires_safety_triggers_only():
    # No auth trigger -> empty floor; optional skills are the model's job now.
    assert interface_design_skills({"name": "Counter"}) == []
    assert select_test_generation_skills({"name": "Counter"}) == []
    assert implementation_skills(interface_contract="plain UI", previous_failure_summary="") == []


def test_selection_auth_floor():
    auth_req = {"name": "Login", "description": "user can log in and see their session"}
    assert interface_design_skills(auth_req) == ["auth-session-consistency"]
    assert select_test_generation_skills(auth_req) == ["auth-session-consistency"]
    # Non-leaf design nodes carry no auth floor (per-node children own auth UI).
    assert interface_design_skills({**auth_req, "children_ids": ["c1"]}) == []
    assert implementation_skills(
        interface_contract="UI interface for the login page session handling",
        previous_failure_summary="",
    ) == ["auth-session-consistency"]


def test_selection_test_generation_auth_floor_is_leaf_agnostic():
    # Deliberate asymmetry with interface_design_skills: non-leaf nodes that
    # carry a visual reference still run test generation (only non-leaf nodes
    # *without* one skip the phase), and their snapshot's auth context stays
    # safety-floor relevant. Unchanged from the planner era.
    auth_req = {"name": "Login shell", "description": "register and log in users", "children_ids": ["c1", "c2"]}
    assert select_test_generation_skills(auth_req) == ["auth-session-consistency"]
    assert interface_design_skills(auth_req) == []


def test_selection_failure_repair_floor():
    assert implementation_skills(
        interface_contract="",
        previous_failure_summary="Exit Code: 1; 2 tests failed",
    ) == ["tdd-test-failure-repair"]
    # Both triggers compose.
    assert implementation_skills(
        interface_contract="registration form session",
        previous_failure_summary="Exit Code: 1",
    ) == ["tdd-test-failure-repair", "auth-session-consistency"]


def test_selection_drops_skills_without_instruction_file():
    assert available_skill_names(["auth-session-consistency", "missing-skill"]) == [
        "auth-session-consistency"
    ]
    assert available_skill_names(["missing-skill"]) == []


# -- factory: every declared skill file stays readable -------------------------


def test_resolve_skill_instruction_paths_allows_every_declared_skill():
    paths = _resolve_skill_instruction_paths([SKILLS_SOURCE], SKILLS_ROOT)

    assert paths == declared_skill_paths()


def test_resolve_skill_instruction_paths_ignores_non_skill_sources():
    assert _resolve_skill_instruction_paths(["/workspace/"], SKILLS_ROOT) == []


def test_resolve_skill_instruction_paths_accepts_single_skill_dir():
    paths = _resolve_skill_instruction_paths(["/skills/frontend-design"], SKILLS_ROOT)

    assert paths == ["/skills/frontend-design/SKILL.md"]


# -- prompts: required floor + optional catalog reads --------------------------


def test_activation_policy_empty_without_floor_skills():
    assert stage_skill_activation_policy([]) == ""


def test_activation_policy_lists_required_skills_and_optional_selection():
    policy = stage_skill_activation_policy(["tdd-test-failure-repair"])

    assert "`/skills/tdd-test-failure-repair/SKILL.md`" in policy
    assert "`read_file` every listed" in policy
    # Catalog-driven discretionary selection is allowed and encouraged, but
    # never by listing/searching the skills mount.
    assert "optional" in policy
    assert "Only the listed skill files are readable" not in policy
    assert "ls" in policy and "grep" in policy


# -- adapters: skills source attached unconditionally --------------------------


def _recording_build(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the stage session's build seam and capture its kwargs.

    All three adapters construct their agents through ``StageSession``, so a
    single patch target intercepts every stage build.
    """

    captured: dict[str, Any] = {}

    def fake_build_stage_agent(**kwargs: Any) -> StageAgentBuild:
        captured.update(kwargs)
        return StageAgentBuild(agent=object(), stage_discipline=None)

    monkeypatch.setattr(stage_session_module, "build_stage_agent", fake_build_stage_agent)
    return captured


def _recording_invoke(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> dict[str, Any]:
    """Patch the stage session's invoke seam and capture its kwargs."""

    captured: dict[str, Any] = {}

    async def fake_ainvoke(agent: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(stage_session_module, "ainvoke_stage_agent", fake_ainvoke)
    return captured


def test_designer_attaches_skills_source_without_floor_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured = _recording_build(monkeypatch)
    designer = InterfaceDesigner()
    session = StageSession(
        agent_name="InterfaceDesigner",
        node_id="REQ-SKILL-1",
        phase="DESIGN",
        workspace_root=str(tmp_path),
        app_type="web",
    )

    designer._build_agent(session, required_skill_names=[], response_format=None)

    assert captured["skills"] == [SKILLS_SOURCE]
    assert "permitted_skill_names" not in captured
    # No floor trigger -> no activation section, but the catalog still rides
    # in via the runtime skills middleware.
    assert "Stage Skill Activation" not in captured["system_prompt"]
    assert "InterfaceDesigner" in captured["system_prompt"]


def test_designer_activation_policy_included_with_floor_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured = _recording_build(monkeypatch)
    designer = InterfaceDesigner()
    session = StageSession(
        agent_name="InterfaceDesigner",
        node_id="REQ-SKILL-1",
        phase="DESIGN",
        workspace_root=str(tmp_path),
        app_type="web",
    )

    designer._build_agent(
        session, required_skill_names=["auth-session-consistency"], response_format=None
    )

    assert "Stage Skill Activation" in captured["system_prompt"]
    assert "/skills/auth-session-consistency/SKILL.md" in captured["system_prompt"]


def test_generator_run_attaches_skills_source(
    tmp_project_dir: Path, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _recording_build(monkeypatch)
    invoked = _recording_invoke(
        monkeypatch, {"summary": "ok", "tests": [], "files_written": []}
    )
    arc_runtime.traceability.store_requirement_tree(
        {"id": "REQ-SKILL-1", "name": "Counter", "description": "Add two numbers"}
    )
    generator = TestGenerator(
        model=FauxChatModel(responses=[]),
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    tests, _output = asyncio.run(
        generator.run(node_id="REQ-SKILL-1", requirement_data={"name": "Counter"})
    )

    assert tests == []
    assert captured["skills"] == [SKILLS_SOURCE]
    assert "permitted_skill_names" not in captured
    assert "Stage Skill Activation" not in captured["system_prompt"]
    # The session owns thread identity: the run invoked on the canonical
    # DESIGN thread with a DESIGN-phase runtime context.
    assert invoked["thread_id"].endswith(":REQ-SKILL-1:DESIGN:TestGenerator")
    assert invoked["context"].phase == "DESIGN"
    assert invoked["context"].app_type == "web"


def test_tdd_run_attaches_skills_source(
    tmp_project_dir: Path, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _recording_build(monkeypatch)
    invoked = _recording_invoke(
        monkeypatch, {"summary": "Wired the counter module to the store."}
    )
    arc_runtime.traceability.store_requirement_tree(
        {"id": "REQ-SKILL-1", "name": "Counter", "description": "Add two numbers"}
    )
    developer = TestDrivenDeveloper(
        model=FauxChatModel(responses=[]),
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    final_text = asyncio.run(
        developer.run(
            node_id="REQ-SKILL-1",
            test_files=["tests/test_counter.py"],
            test_type="Unit",
            node_tests=[],
        )
    )

    assert final_text == "Wired the counter module to the store."
    assert captured["skills"] == [SKILLS_SOURCE]
    assert "permitted_skill_names" not in captured
    assert "Stage Skill Activation" not in captured["system_prompt"]
    # IMPLEMENT-phase thread carrying the test-layer suffix.
    assert invoked["thread_id"].endswith(":REQ-SKILL-1:IMPLEMENT:TestDrivenDeveloper:Unit")
    assert invoked["context"].phase == "IMPLEMENT"
    assert invoked["context"].test_type == "Unit"


def test_tdd_run_requires_repair_skill_after_failure(
    tmp_project_dir: Path, arc_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _recording_build(monkeypatch)
    _recording_invoke(monkeypatch, {"summary": "still failing."})
    arc_runtime.traceability.store_requirement_tree(
        {"id": "REQ-SKILL-1", "name": "Counter", "description": "Add two numbers"}
    )
    developer = TestDrivenDeveloper(
        model=FauxChatModel(responses=[]),
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    asyncio.run(
        developer.run(
            node_id="REQ-SKILL-1",
            test_files=["tests/test_counter.py"],
            test_type="Unit",
            node_tests=[],
            previous_failure_summary="Exit Code: 1; 2 tests failed",
        )
    )

    assert "Stage Skill Activation" in captured["system_prompt"]
    assert "/skills/tdd-test-failure-repair/SKILL.md" in captured["system_prompt"]
    assert captured["skills"] == [SKILLS_SOURCE]


# -- planner removal ------------------------------------------------------------


def test_per_node_planning_agent_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agents.skills.planning")

    import core.phases

    assert not hasattr(core.phases, "plan_and_store_stage_skills")

"""Model-driven per-node skill planning: planner, session persistence, selection floor.

Covers:

- ``agents/skills/planning.py``: catalog construction, prompt layout
  (byte-stable system prefix + requirement-first user message), JSON parsing,
  unknown-name/cap validation, failure -> None, session store/reuse.
- ``agents/skills/selection.py``: deterministic safety floor (auth, failure
  repair) union model-planned extras; no keyword fallback.

All model interactions run through ``FauxChatModel`` — no network, no real
provider credentials.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.skills.planning import (
    MAX_SKILLS_PER_STAGE,
    build_skill_catalog,
    load_skill_plan,
    load_skill_plan_extras,
    plan_and_store_stage_skills,
    plan_stage_skills,
)
from agents.skills.selection import (
    implementation_skills,
    interface_design_skills,
    test_generation_skills as select_test_generation_skills,
)
from core.sessions import load_node_session, merge_node_session
from tests.helpers.faux import FauxChatModel, faux_text

ALL_SKILL_NAMES = (
    "auth-session-consistency",
    "frontend-design",
    "leaf-full-design",
    "leaf-test-layer-selection",
    "non-leaf-ui-only-design",
    "tdd-test-failure-repair",
    "test-driven-development",
    "vercel-composition-patterns",
    "vercel-react-best-practices",
    "web-design-guidelines",
    "web-test-harness-skill",
)


# -- catalog -----------------------------------------------------------------


def test_build_skill_catalog_lists_all_skills_deterministically():
    first = build_skill_catalog()
    assert first == build_skill_catalog()

    lines = first.splitlines()
    assert len(lines) == len(ALL_SKILL_NAMES)
    for name in ALL_SKILL_NAMES:
        assert any(line.startswith(f"- {name}: ") for name in [name] for line in lines)
    # Descriptions ride along so the planner can judge relevance.
    assert "Guidance" in first


# -- plan_stage_skills --------------------------------------------------------


def _planner_model(payload: dict | str | None) -> FauxChatModel:
    if payload is None:
        return FauxChatModel(responses=[])
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return FauxChatModel(responses=[faux_text(text)])


def test_plan_stage_skills_returns_valid_plan(arc_runtime):
    model = _planner_model(
        {
            "summary": "plain counter node",
            "design": ["leaf-full-design", "frontend-design"],
            "test_generation": ["leaf-test-layer-selection"],
            "implementation": ["test-driven-development"],
        }
    )
    requirement_data = {"req_id": "smoke--counter", "name": "Counter", "children_ids": []}

    plan = asyncio.run(
        plan_stage_skills(
            node_id="n1",
            requirement_data=requirement_data,
            model=model,
        )
    )

    assert plan == {
        "summary": "plain counter node",
        "design": ["leaf-full-design", "frontend-design"],
        "test_generation": ["leaf-test-layer-selection"],
        "implementation": ["test-driven-development"],
    }
    assert load_skill_plan("n1") is None  # plan_stage_skills does not persist; the wrapper does
    assert model.call_count == 1


def test_plan_prompt_is_cache_friendly(arc_runtime):
    """System prefix must be byte-stable across nodes; requirement opens the user message."""

    model = FauxChatModel(
        responses=[
            faux_text(json.dumps({"design": [], "test_generation": [], "implementation": []})),
            faux_text(json.dumps({"design": [], "test_generation": [], "implementation": []})),
        ]
    )
    asyncio.run(plan_stage_skills(node_id="n1", requirement_data={"name": "Counter"}, model=model))
    asyncio.run(plan_stage_skills(node_id="n2", requirement_data={"name": "Dice"}, model=model))

    first_system, first_user = model.calls[0][0].content, model.calls[0][1].content
    second_system, second_user = model.calls[1][0].content, model.calls[1][1].content

    assert first_system == second_system  # identical catalog prefix -> provider cache hit
    for name in ALL_SKILL_NAMES:
        assert name in first_system
    assert first_user.startswith("Requirement snapshot:\n{")
    assert json.loads(first_user.split("\n\nNode id:")[0].removeprefix("Requirement snapshot:\n")) == {"name": "Counter"}
    assert first_user.endswith("Node id: n1")
    assert second_user.endswith("Node id: n2")


def test_plan_stage_skills_drops_unknown_names_and_caps_per_stage(arc_runtime):
    model = _planner_model(
        {
            "design": [
                "frontend-design",
                "missing-skill",
                "frontend-design",
                "web-design-guidelines",
                "leaf-full-design",
                "non-leaf-ui-only-design",
            ],
            "test_generation": [],
            "implementation": [],
        }
    )

    plan = asyncio.run(plan_stage_skills(node_id="n1", requirement_data={}, model=model))

    # dedupe -> availability filter -> cap
    assert plan["design"] == ["frontend-design", "web-design-guidelines", "leaf-full-design"]
    assert len(plan["design"]) == MAX_SKILLS_PER_STAGE


@pytest.mark.parametrize("payload", ["not json at all", '{"design": "oops"', None])
def test_plan_stage_skills_returns_none_on_failure(arc_runtime, payload):
    model = _planner_model(payload)

    plan = asyncio.run(plan_stage_skills(node_id="n1", requirement_data={}, model=model))

    assert plan is None
    assert load_node_session("n1") == {}


def test_parse_plan_json_tolerates_prose_and_broken_candidates():
    from agents.skills.planning import _parse_plan_json

    good = {"design": ["frontend-design"], "test_generation": [], "implementation": []}
    # Prose before/after the object.
    assert _parse_plan_json('Sure, here is the plan: {"design": []} Done.') == {"design": []}
    # First "{" opens invalid JSON; the later object still parses.
    assert _parse_plan_json('intro {not json} {"design": ["frontend-design"]}') == {
        "design": ["frontend-design"]
    }
    # Code fence + prose combination.
    assert _parse_plan_json('```json\nPlan: {"design": []}\n```') == {"design": []}

    with pytest.raises(ValueError, match="no JSON object"):
        _parse_plan_json("totally not json")
    # Parse failures carry a response excerpt for diagnosis.
    with pytest.raises(ValueError, match="utterly unparseable drivel"):
        _parse_plan_json("utterly unparseable drivel")


def test_plan_stage_skills_skips_when_model_missing(arc_runtime, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MODEL", raising=False)

    plan = asyncio.run(plan_stage_skills(node_id="n1", requirement_data={}))

    assert plan is None


# -- plan_and_store_stage_skills / session reuse -------------------------------


def test_plan_and_store_persists_and_reuses(arc_runtime, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL", "openai:faux-1")

    def _fake_model(model_name, *, api_mode=None):
        return _planner_model(
            {
                "summary": "",
                "design": ["leaf-full-design"],
                "test_generation": ["leaf-test-layer-selection"],
                "implementation": ["test-driven-development"],
            }
        )

    monkeypatch.setattr(planning_module(), "create_arc_chat_model", _fake_model)
    requirement_data = {"req_id": "r1", "name": "Counter"}

    plan = asyncio.run(
        plan_and_store_stage_skills(node_id="n1", requirement_data=requirement_data)
    )
    assert plan["design"] == ["leaf-full-design"]
    stored = load_skill_plan("n1")
    assert stored is not None and stored["implementation"] == ["test-driven-development"]

    # Second invocation reuses the stored plan without another model call.
    reused = asyncio.run(
        plan_and_store_stage_skills(node_id="n1", requirement_data=requirement_data)
    )
    assert reused == plan


def test_plan_and_store_without_model_returns_none(arc_runtime, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MODEL", raising=False)

    result = asyncio.run(plan_and_store_stage_skills(node_id="n1", requirement_data={}))

    assert result is None
    assert load_skill_plan("n1") is None


def test_load_skill_plan_extras_semantics(arc_runtime):
    assert load_skill_plan_extras("n1", "design") is None  # no plan stored

    merge_node_session(
        "n1",
        {"skill_plan": {"design": ["frontend-design"], "implementation": []}},
    )
    assert load_skill_plan_extras("n1", "design") == ["frontend-design"]
    assert load_skill_plan_extras("n1", "implementation") == []  # planned empty
    assert load_skill_plan_extras("n1", "test_generation") == []  # stage missing from plan


# -- selection: safety floor union extras --------------------------------------


def test_selection_floor_requires_safety_triggers_only():
    # Keyword guidance is gone: no auth trigger -> empty floor.
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


def test_selection_unions_extras_with_floor_and_filters():
    merged = interface_design_skills(
        {"name": "Counter"},
        extra_skills=["auth-session-consistency", "frontend-design", "frontend-design"],
    )
    assert merged == ["auth-session-consistency", "frontend-design"]

    # Extras referencing missing SKILL.md files are dropped.
    assert interface_design_skills({}, extra_skills=["missing-skill"]) == []

    # extras=None (planning failed/absent) behaves like no extras: floor only.
    assert interface_design_skills({"name": "Counter"}, extra_skills=None) == []


def planning_module():
    from agents.skills import planning as module

    return module

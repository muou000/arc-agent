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
import json
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
    has_auth_context,
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


def test_selection_chinese_auth_floor():
    # Chinese-first requirement trees must hit the same deterministic floor
    # (issue #178: _AUTH_TERMS used to be English-only, a static miss).
    auth_req = {"name": "登录", "description": "用户可以登录并查看自己的会话"}
    assert interface_design_skills(auth_req) == ["auth-session-consistency"]
    assert select_test_generation_skills(auth_req) == ["auth-session-consistency"]
    assert implementation_skills(
        interface_contract="登录页接口：处理会话保持与当前用户状态",
        previous_failure_summary="",
    ) == ["auth-session-consistency"]


@pytest.mark.parametrize(
    "term",
    [
        "登录",
        "登陆",
        "注册",
        "登出",
        "注销",
        "会话",
        "认证",
        "授权",
        "当前用户",
        "账户",
        "账号",
    ],
)
def test_auth_terms_cover_chinese_variants(term):
    assert has_auth_context({"description": f"支持{term}功能"}) is True


def test_selection_chinese_non_auth_no_trigger():
    non_auth = {"name": "商品列表", "description": "展示商品列表，支持按价格排序并加入购物车"}
    assert interface_design_skills(non_auth) == []
    assert select_test_generation_skills(non_auth) == []
    assert implementation_skills(
        interface_contract="商品列表接口：返回分页数据与筛选结果",
        previous_failure_summary="",
    ) == []


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
    paths = _resolve_skill_instruction_paths(["/skills/auth-session-consistency"], SKILLS_ROOT)

    assert paths == ["/skills/auth-session-consistency/SKILL.md"]


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


# -- repair pass shares the first pass's agent build (issue #173) ----------------


def _capture_generator_build(
    tmp_project_dir: Path,
    arc_runtime,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node_id: str,
    requirement_data: dict[str, Any],
    invoke_payload: dict[str, Any],
    run_repair: bool,
) -> tuple[dict[str, Any], str]:
    """Run one TestGenerator pass with the build and invoke seams recorded.

    Returns the kwargs captured from that pass's single ``build_stage_agent``
    call (each pass builds exactly one agent) and the user message handed to
    ``ainvoke_stage_agent``.
    """

    builds: list[dict[str, Any]] = []
    messages: list[str] = []

    def fake_build_stage_agent(**kwargs: Any) -> StageAgentBuild:
        builds.append(kwargs)
        return StageAgentBuild(agent=object(), stage_discipline=None)

    async def fake_ainvoke(agent: Any, **kwargs: Any) -> dict[str, Any]:
        messages.append(str(kwargs.get("message", "")))
        return invoke_payload

    monkeypatch.setattr(stage_session_module, "build_stage_agent", fake_build_stage_agent)
    monkeypatch.setattr(stage_session_module, "ainvoke_stage_agent", fake_ainvoke)
    arc_runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": requirement_data.get("name", ""), "description": ""}
    )
    generator = TestGenerator(
        model=FauxChatModel(responses=[]),
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    if run_repair:
        asyncio.run(
            generator.repair_green_baseline(
                node_id,
                requirement_data,
                green_evidence=[
                    {"file_path": "backend/tests/unit/green.test.js", "type": "Unit", "output_summary": "1 passed"}
                ],
                previous_manifest=[
                    {
                        "test_id": "T-GREEN",
                        "req_id": node_id,
                        "interface_ids": [],
                        "type": "Unit",
                        "file_path": "backend/tests/unit/green.test.js",
                        "first_line": "test('tautology', () => {",
                    }
                ],
            )
        )
    else:
        asyncio.run(generator.run(node_id=node_id, requirement_data=requirement_data))

    assert len(builds) == 1
    return builds[0], messages[0]


@pytest.mark.parametrize(
    "requirement_data",
    [
        # No auth trigger: activation policy is empty, the joined prompt is the
        # bare system prompt with a trailing joiner.
        {"name": "Counter", "description": "Add two numbers"},
        # Auth trigger: the activation section must ride along on the repair
        # build too, or the shared thread's prefix cache is lost.
        {"name": "Login", "description": "user can log in and see their session"},
    ],
    ids=["no-auth-floor", "auth-floor"],
)
def test_generator_repair_build_matches_first_pass(
    tmp_project_dir: Path,
    arc_runtime,
    monkeypatch: pytest.MonkeyPatch,
    requirement_data: dict[str, Any],
) -> None:
    """The green-baseline repair round re-asks the first pass's thread: its
    ``build_agent`` system prompt and skills must be byte-identical to the
    first pass's, or the provider-side prefix cache is forfeited for the whole
    repair round (issue #173: in=98,008 / cache_read=128 on the repair call
    while the first pass cached normally)."""
    payload = {"summary": "ok", "tests": [], "files_written": []}
    first_pass, _ = _capture_generator_build(
        tmp_project_dir,
        arc_runtime,
        monkeypatch,
        node_id="REQ-SKILL-2",
        requirement_data=requirement_data,
        invoke_payload=payload,
        run_repair=False,
    )
    repair_pass, _ = _capture_generator_build(
        tmp_project_dir,
        arc_runtime,
        monkeypatch,
        node_id="REQ-SKILL-2",
        requirement_data=requirement_data,
        invoke_payload=payload,
        run_repair=True,
    )

    assert repair_pass["system_prompt"] == first_pass["system_prompt"]
    assert repair_pass["skills"] == first_pass["skills"] == [SKILLS_SOURCE]
    # The parity is real, not vacuous: with an auth-triggering requirement the
    # activation section is present in both prompts; without one it is absent
    # from both.
    has_auth_floor = bool(select_test_generation_skills(requirement_data))
    assert ("Stage Skill Activation" in repair_pass["system_prompt"]) is has_auth_floor


def test_generator_repair_message_carries_requirement_snapshot(
    tmp_project_dir: Path,
    arc_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #184: the repair round's prompt must carry a non-empty
    Requirement Snapshot. The rejection message previously named only the
    green evidence and the old manifest; on a cold thread (checkpointer
    disabled, or a repair without the first pass's history) the model had no
    requirement text at all to judge which assertions are node-owned."""
    requirement_data = {"name": "Login", "description": "user can log in and see their session"}
    _build, message = _capture_generator_build(
        tmp_project_dir,
        arc_runtime,
        monkeypatch,
        node_id="REQ-SKILL-2",
        requirement_data=requirement_data,
        invoke_payload={"summary": "ok", "tests": [], "files_written": []},
        run_repair=True,
    )

    assert "### Requirement Snapshot" in message
    assert '"Login"' in message
    assert "user can log in and see their session" in message
    # #211: the repair snapshot is the same compact block the first pass
    # embeds (repair/first-pass parity), not a pretty-printed variant.
    json_text = message.split("### Requirement Snapshot\n```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(json_text) == requirement_data
    assert "\n" not in json_text
    assert ", " not in json_text
    assert ": " not in json_text


# -- planner removal ------------------------------------------------------------


def test_per_node_planning_agent_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agents.skills.planning")

    import core.phases

    assert not hasattr(core.phases, "plan_and_store_stage_skills")

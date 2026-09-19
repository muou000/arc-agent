"""Pin the template shared-surface write guard (stage discipline + app types).

The 0aca31c5 online run lost the web template's static serving to a DESIGN
skeleton that replaced ``backend/src/app.js`` wholesale; TDD then rebuilt the
serving from scratch and the worktree-relative dist path 404'd every asset,
producing a 47-minute blank-page E2E debug loop (REQ-1's TDD alone consumed
58% of the run's 192-minute wall clock). Stage discipline now rejects
whole-file ``write_file`` on the app type's template shared surfaces while
``edit_file``/``append_file`` stay allowed — extending those files additively
is exactly the intended integration pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.runtime.factory import build_stage_agent
from agents.runtime.stage_discipline import StageDisciplineMiddleware
from app_type_handler import template_shared_surfaces as surfaces_for
from app_type_handler.web import WebAppType
from tests.helpers.faux import FauxChatModel

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = REPO_ROOT / "arc-template" / "templates" / "web-react-express"

APP_JS = "/workspace/backend/src/app.js"


def make_request(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    call_id: str = "call-1",
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id},
        tool=None,
        state={},
        runtime=None,
    )


def ok_tool(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="ok", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


def make(stage: str, surfaces: frozenset[str] | None) -> StageDisciplineMiddleware:
    return StageDisciplineMiddleware(stage=stage, template_shared_surfaces=surfaces)


def blocked_content(result: Any) -> str:
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    return str(result.content)


# ---------------------------------------------------------------------------
# Guard behavior
# ---------------------------------------------------------------------------


def test_write_file_on_shared_surface_blocked_in_design_and_implementation() -> None:
    surfaces = surfaces_for("web")
    assert "backend/src/app.js" in surfaces
    for stage in ("interface_design", "implementation"):
        result = make(stage, surfaces).wrap_tool_call(
            make_request("write_file", {"file_path": APP_JS, "content": "const app = 1;\n"}),
            ok_tool,
        )
        content = blocked_content(result)
        assert "Template shared surface blocked" in content
        assert APP_JS in content
        assert "edit_file" in content


def test_guard_uses_the_surface_list_not_a_hardcoded_path() -> None:
    # A path outside the configured list is unaffected even in the same tree.
    middleware = make("interface_design", frozenset({"backend/src/database/init_db.js"}))
    result = middleware.wrap_tool_call(
        make_request(
            "write_file",
            {"file_path": APP_JS, "content": "const x = 1;\n"},
        ),
        ok_tool,
    )
    assert not isinstance(result, ToolMessage) or result.status != "error"


def test_edit_file_and_append_file_stay_allowed_on_shared_surfaces() -> None:
    surfaces = surfaces_for("web")
    edit = make("implementation", surfaces).wrap_tool_call(
        make_request(
            "edit_file",
            {"file_path": APP_JS, "old_string": "// register routes", "new_string": "// register routes\n"},
        ),
        ok_tool,
    )
    assert not isinstance(edit, ToolMessage) or edit.status != "error"

    append = make("interface_design", surfaces).wrap_tool_call(
        make_request(
            "append_file",
            {"file_path": APP_JS, "content": "\n// appended mount point\n"},
        ),
        ok_tool,
    )
    assert not isinstance(append, ToolMessage) or append.status != "error"


def test_guard_not_unlocked_by_validation_failure() -> None:
    # A failing validation unlocks repeated-write churn, never the
    # shared-surface ban: a red test must not make destroying runtime wiring
    # the right repair.
    middleware = make("implementation", surfaces_for("web"))
    failing_validation = ToolMessage(
        content="Exit Code: 1\nSTDERR:\nboom\n",
        name="run_tests",
        tool_call_id="val-1",
    )
    middleware.wrap_tool_call(
        make_request("run_tests", {"test_type": "E2E"}, call_id="val-1"),
        lambda _request: failing_validation,
    )
    result = middleware.wrap_tool_call(
        make_request("write_file", {"file_path": APP_JS, "content": "const app = 1;\n"}),
        ok_tool,
    )
    assert "Template shared surface blocked" in blocked_content(result)


def test_relative_and_prefixed_paths_both_blocked() -> None:
    middleware = make("interface_design", surfaces_for("web"))
    for path in ("backend/src/app.js", "/workspace/backend/src/app.js", "/workspace\\backend/src/app.js"):
        result = middleware.wrap_tool_call(
            make_request("write_file", {"file_path": path, "content": "const app = 1;\n"}),
            ok_tool,
        )
        assert "Template shared surface blocked" in blocked_content(result)


def test_middlewares_without_surfaces_keep_classic_behavior() -> None:
    middleware = make("interface_design", None)
    result = middleware.wrap_tool_call(
        make_request("write_file", {"file_path": APP_JS, "content": "const app = 1;\n"}),
        ok_tool,
    )
    assert not isinstance(result, ToolMessage) or result.status != "error"


# ---------------------------------------------------------------------------
# App-type surface lists
# ---------------------------------------------------------------------------


def test_web_surfaces_cover_the_template_runtime_wiring() -> None:
    surfaces = WebAppType.template_shared_surfaces
    assert {
        "backend/src/app.js",
        "backend/src/index.js",
        "backend/src/database/index.js",
        "backend/src/database/init_db.js",
        "backend/src/database/db_runtime.js",
        "backend/src/database/seed_db.js",
        "backend/src/database/prepare_e2e.js",
        "backend/src/database/test_harness.js",
        "frontend/src/main.tsx",
        "frontend/src/App.tsx",
        "frontend/src/api/index.ts",
    } == surfaces


def test_every_web_surface_exists_in_the_template() -> None:
    # The mirror-drift lesson (PR #45): a protection list naming files the
    # template does not ship would silently guard nothing. Pin each path
    # against the in-repo template copy.
    for relative in surfaces_for("web"):
        assert (TEMPLATE_ROOT / relative).is_file(), relative


def test_non_web_app_types_protect_nothing_yet() -> None:
    assert surfaces_for("android") == frozenset()
    assert surfaces_for("cli") == frozenset()


def test_factory_maps_app_type_to_surfaces(tmp_path: Path) -> None:
    wired = build_stage_agent(
        name="probe",
        stage="interface_design",
        model=FauxChatModel(),
        system_prompt="probe",
        response_format=None,
        workspace_root=str(tmp_path),
        writable_roots=[str(tmp_path)],
        skills=[],
        memory=[],
        tools=[],
        app_type="web",
    )
    assert wired.arc_stage_discipline._template_shared_surfaces == surfaces_for("web")

    unwired = build_stage_agent(
        name="probe",
        stage="interface_design",
        model=FauxChatModel(),
        system_prompt="probe",
        response_format=None,
        workspace_root=str(tmp_path),
        writable_roots=[str(tmp_path)],
        skills=[],
        memory=[],
        tools=[],
    )
    assert unwired.arc_stage_discipline._template_shared_surfaces == frozenset()


# ---------------------------------------------------------------------------
# Prompt / skill pins
# ---------------------------------------------------------------------------


def _skill(name: str) -> str:
    return (REPO_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_tdd_prompt_pins_the_blank_page_diagnosis() -> None:
    from agents.context.prompts.test_driven_developer import get_user_prompt

    prompt = get_user_prompt(
        node_id="REQ-1",
        dynamic_context="ctx",
        test_files=["tests/a.test.js"],
        test_type="Unit",
        node_tests=[],
    )
    assert "lost template runtime wiring" in prompt
    assert "express.static" in prompt
    assert "temp-copy snapshot" in prompt


def test_design_prompt_pins_the_mechanical_enforcement() -> None:
    from agents.context.prompts.interface_designer import get_user_prompt

    prompt = get_user_prompt(
        node_id="REQ-1",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
    )
    assert "The file tools enforce that rule mechanically" in prompt
    assert "reused interface" in prompt


def test_repair_skill_pins_the_runtime_wiring_rule() -> None:
    skill = _skill("tdd-test-failure-repair")
    assert "23. If Playwright E2E fails with blank pages" in skill
    assert "worktree CWD" in skill
    assert "45-minute blank-page loop" in skill

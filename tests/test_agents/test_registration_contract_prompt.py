"""The Registration Contract must reach the agents that write app code.

InterfaceDesigner and TestDrivenDeveloper are the stages that materialize and
wire skeleton/implementation files; both must carry the app-type registration
contract so agents extend shared glue through per-feature registration modules
instead of editing shared assembly files. App types without a registration
contract must not get an empty section.
"""

from __future__ import annotations

import pytest

from agents.context.prompts import common
from agents.context.prompts.interface_designer import get_system_prompt as get_designer_prompt
from agents.context.prompts.test_driven_developer import get_system_prompt as get_tdd_prompt

_WEB_CONTRACT_MARKERS = (
    "Registration Contract",
    "backend/src/routes/<feature>.routes.js",
    "backend/src/database/schema/<feature>.schema.js",
    "frontend/src/pages/<Page>.tsx",
    "frontend/src/sections/home/<Section>.tsx",
    "frontend/src/providers/<Name>Provider.tsx",
)


@pytest.mark.parametrize("get_prompt", [get_designer_prompt, get_tdd_prompt])
def test_web_prompts_carry_registration_contract(get_prompt, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_APP_TYPE", "web")
    prompt = get_prompt()
    for marker in _WEB_CONTRACT_MARKERS:
        assert marker in prompt, f"prompt is missing registration contract marker: {marker}"


@pytest.mark.parametrize("get_prompt", [get_designer_prompt, get_tdd_prompt])
def test_prompts_without_registration_contract_omit_the_section(
    get_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_APP_TYPE", "cli")
    assert "### Registration Contract" not in get_prompt()


def test_designer_prompt_points_design_writes_at_registration_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_APP_TYPE", "web")
    prompt = get_designer_prompt()
    assert "node-private files" in prompt
    assert "app.js, App.tsx, main.tsx, page containers, or database bootstrap files" in prompt


def test_registration_contract_section_uses_the_app_type_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_APP_TYPE", "web")
    section_text = common.registration_contract()
    assert section_text.startswith("### Registration Contract")
    assert "- Backend API:" in section_text

    monkeypatch.setenv("ARC_APP_TYPE", "android")
    assert common.registration_contract() == ""

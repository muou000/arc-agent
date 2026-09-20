"""Pin the E2E scenario-isolation guidance added after the 2026-09-20 arc-output4 run.

That run finished green, but register.e2e.spec.js burned 4 of its 8 attempts
(attempts 5-7 plus the final repair) on pure test-authoring defects: Scenario 2
stuffed six invalid-input sub-cases into one ``test()`` sharing one form, so a
checked 服务条款 checkbox leaked into the "Terms not accepted" sub-case and a
previously rejected 邮箱 value leaked into later sub-cases. The product had
been correct since attempt 4 - every remaining failure was spec-side state
pollution that TestDrivenDeveloper had to diagnose as if it were a product
defect.

The guidance now makes scenario isolation a generation-time requirement in the
TestGenerator prompt and the leaf-test-layer-selection skill: fresh browser
state per ``test()``, and explicit resets (``fill('')`` / ``uncheck()``) or a
split into separate tests whenever one ``test()`` chains multiple form submits.

Coupling convention: the exact strings pinned below (for example ``fill('')``
and ``uncheck()``) are the example phrases the guidance teaches. Changing an
example in a prompt or skill is a conscious edit that must update the matching
pin in the same commit - the pins fail on purpose when examples drift, so
reviewers see prose and tests move together.
"""
from __future__ import annotations

from pathlib import Path

from agents.context.prompts import test_generator as testgen_prompt_module

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills"


def test_system_prompt_requires_per_scenario_browser_isolation() -> None:
    prompt = testgen_prompt_module.get_system_prompt()
    # Fresh context per test plus a known entry state (the arc-output4 spec's
    # beforeEach already did this for scenarios; the rule now demands it).
    assert "fresh context" in prompt
    assert "clears cookies and navigates to the entry URL" in prompt
    assert "Do not reuse a logged-in or form-filled state across tests" in prompt


def test_system_prompt_requires_subcase_input_resets() -> None:
    prompt = testgen_prompt_module.get_system_prompt()
    # The exact defect class from attempts 5-7: chained sub-cases inheriting
    # leftover checkbox/field state inside one Scenario-2 test body.
    assert "never chain multiple form submissions that reuse leftover input state" in prompt
    assert "fill('')" in prompt
    assert "uncheck()" in prompt
    assert "Do not rely on `fill()` to replace a checkbox state" in prompt


def test_user_prompt_states_isolation_is_generation_time() -> None:
    prompt = testgen_prompt_module.get_user_prompt(
        node_id="REQ-1",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
    )
    assert "E2E scenario isolation is a generation-time requirement" in prompt
    assert "not something TDD repairs later" in prompt
    assert "A sub-case that silently inherits a previous sub-case's checkbox or field value is a test defect" in prompt


def test_testgen_skill_isolation_rules_pinned() -> None:
    skill = (SKILL_ROOT / "leaf-test-layer-selection" / "SKILL.md").read_text(encoding="utf-8")
    assert "21a." in skill
    assert "Scenario isolation is a generation-time requirement" in skill
    assert "fresh browser context" in skill
    assert "clearCookies()" in skill
    assert "21b." in skill
    assert "fill('')" in skill
    assert "uncheck()" in skill
    # The concrete loss must stay attached to the rule so future edits keep
    # the "why": 4 of 8 E2E attempts burned while the product was correct.
    assert "cost 4 of 8 E2E attempts" in skill
    assert "the product was already correct" in skill

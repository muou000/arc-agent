"""Pin the contract-literal-anchor guidance added after the 2026-09-19 train-ticket run.

That online submission passed 6/7 of its own internal test batches but failed
6/6 external REQ-1 registration tests: the implementation translated
requirement-quoted accessible names (``Register`` home link, ``Sign out``
link) into Chinese so strict external locators found nothing, register
validation errors rendered as bare ``<p>`` while the login form used
``role="alert"`` (its own external rejection tests all passed), and a backend
unit test disagreed with the service's aggregated duplicate-error taxonomy.
These tests pin the prompt and skill wording that guards each mode.
"""
from __future__ import annotations

from pathlib import Path

from agents.context.prompts import test_generator as testgen_prompt_module
from agents.context.prompts.interface_designer import get_user_prompt as design_user_prompt
from agents.context.prompts.test_driven_developer import get_system_prompt as tdd_system_prompt
from agents.context.prompts.test_driven_developer import get_user_prompt as tdd_user_prompt

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills"


def _tdd_prompt() -> str:
    return tdd_user_prompt(
        node_id="REQ-1",
        dynamic_context="ctx",
        test_files=["tests/a.test.js"],
        test_type="Unit",
        node_tests=[],
    ) + "\n" + tdd_system_prompt()


def test_tdd_prompt_requires_quoted_anchors_verbatim() -> None:
    prompt = _tdd_prompt()
    assert "Quoted requirement anchors are implementation obligations" in prompt
    assert "must appear verbatim in the shipped UI" in prompt
    # The anchor rule also covers bare capitalized UI names, not only
    # backticked/quoted literals (review round 1, suggestion 1).
    assert "distinctly capitalized UI name" in prompt
    # The bilingual escape hatch keeps the anchor rule satisfiable without
    # forcing English-only copy on a Chinese-locale UI.
    assert "Bilingual visible text" in prompt
    assert "do not rewrite the test's selector to the translation" in prompt
    # Requirement-stated anchors outrank test-defined selectors; a generated
    # selector contradicting the requirement is a test defect (review round 2).
    assert "takes precedence over aligning to a test-defined selector" in prompt
    assert "repair the test to the requirement's literal" in prompt


def test_tdd_prompt_requires_alert_region_and_native_labels() -> None:
    prompt = _tdd_prompt()
    assert "role=alert" in prompt
    assert "aria-describedby" in prompt
    assert "<label htmlFor>" in prompt
    assert "never rely on placeholder text" in prompt


def test_testgen_prompt_asserts_contract_literals_verbatim() -> None:
    prompt = testgen_prompt_module.get_system_prompt()
    assert "target that literal verbatim" in prompt
    assert "distinctly capitalized UI name" in prompt
    assert "do not read the implementation to pick a selector" in prompt
    assert "getByRole('alert')" in prompt


def test_design_prompt_fixes_error_taxonomy_once() -> None:
    prompt = design_user_prompt(
        node_id="REQ-1",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
    )
    assert "fix the error taxonomy once" in prompt
    # Per-field codes are the default with a concrete attribution criterion;
    # an aggregated code needs a non-field-attributable failure or explicit
    # requirement backing (review round 2).
    assert "Prefer per-field codes" in prompt
    assert "give each violating field its own code/key" in prompt
    assert "not attributable to a single field" in prompt
    assert "DUPLICATE_USERNAME" in prompt
    assert "DUPLICATE_FIELDS" in prompt


def test_testgen_skill_literal_rules_pinned() -> None:
    skill = (SKILL_ROOT / "leaf-test-layer-selection" / "SKILL.md").read_text(encoding="utf-8")
    assert "16a." in skill
    assert "target that literal verbatim" in skill
    assert "distinctly capitalized UI name" in skill
    assert "16b." in skill
    assert "must carry every quoted literal" in skill
    assert "16c." in skill
    assert "getByRole('alert')" in skill


def test_repair_skill_literal_rules_pinned() -> None:
    skill = (SKILL_ROOT / "tdd-test-failure-repair" / "SKILL.md").read_text(encoding="utf-8")
    assert "21a." in skill
    assert "the requirement text wins" in skill
    assert "21b." in skill
    assert "repair the error presentation path, not the assertion" in skill


def test_design_skills_shared_shell_literal_rules_pinned() -> None:
    leaf_full = (SKILL_ROOT / "leaf-full-design" / "SKILL.md").read_text(encoding="utf-8")
    assert "Carry requirement-stated UI anchors into the interface contract verbatim" in leaf_full
    assert "distinctly capitalized UI name" in leaf_full
    assert "union of every referencing requirement's quoted literals" in leaf_full
    assert "<label htmlFor>" in leaf_full

    ui_only = (SKILL_ROOT / "non-leaf-ui-only-design" / "SKILL.md").read_text(encoding="utf-8")
    assert "Shell navigation and auth slots are assertion targets" in ui_only
    assert "do not substitute a translation" in ui_only

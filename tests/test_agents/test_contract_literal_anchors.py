"""Pin the contract-literal-anchor guidance added after the 2026-09-19 train-ticket runs.

The first online submission passed 6/7 of its own internal test batches but
failed 6/6 external REQ-1 registration tests: the implementation translated
requirement-quoted accessible names (``Register`` home link, ``Sign out``
link) into Chinese so strict external locators found nothing, register
validation errors rendered as bare ``<p>`` while the login form used
``role="alert"`` (its own external rejection tests all passed), and a backend
unit test disagreed with the service's aggregated duplicate-error taxonomy.

A second submission the same day (0aca31c5) proved the first revision's
bilingual escape hatch wrong in the other direction: the concatenated label
``密码 Password`` matched neither branch of the external anchored locator
``getByLabel(/^密码$|^password$/i)``, failing 4/4 REQ-1.2 login tests. The
guidance now pins each control to exactly one requirement-quoted literal
(with an intersection rule for shared controls), requires anchored or exact
matchers in generated tests so concatenated labels turn red inside TDD, and
treats visual-reference section titles as structural anchors (the same run
asserted the register page's 账户信息 heading, which no requirement text
mentions).

Coupling convention: the locators pinned below (for example
``getByLabel(/^密码$|^password$/i)``) are the exact example strings the
guidance teaches. Changing an example in a prompt or skill is a conscious
edit that must update the matching pin in the same commit - the pins fail
on purpose when examples drift, so reviewers see prose and tests move
together.
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
    # Each control ships exactly one quoted literal: the second online run
    # failed 4/4 login tests because the concatenated label `密码 Password`
    # matched neither branch of the external anchored locator
    # getByLabel(/^密码$|^password$/i).
    assert "exactly ONE requirement-quoted literal" in prompt
    assert "never merge several quoted literals into one visible string" in prompt
    assert "getByLabel(/^密码$|^password$/i)" in prompt
    # The shared-control exception is phrased as a sanctioned exception, not
    # a trailing "concatenate only when" that contradicted the absolute
    # prohibition in the same bullet (review round 2).
    assert "intersection of the quoted-literal sets" in prompt
    assert "sanctioned exception" in prompt
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
    # Run-2 failure class: internal tests mirrored the implementation's
    # substring selectors, so a concatenated label passed internally and
    # only failed externally. Anchored/exact pins force it red during TDD.
    assert "exact or anchored matcher" in prompt
    assert "{ exact: true }" in prompt


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
    assert "{ exact: true }" in skill
    assert "16b." in skill
    assert "intersection of the quoted-literal sets" in skill
    assert "16c." in skill
    assert "getByRole('alert')" in skill


def test_repair_skill_literal_rules_pinned() -> None:
    skill = (SKILL_ROOT / "tdd-test-failure-repair" / "SKILL.md").read_text(encoding="utf-8")
    assert "21a." in skill
    assert "the requirement text wins" in skill
    assert "exactly ONE requirement-quoted literal" in skill
    assert "21b." in skill
    assert "repair the error presentation path, not the assertion" in skill


def test_repair_skill_batch_timeout_rules_pinned() -> None:
    """The E2E batch-timeout playbook must survive skill edits verbatim.

    The 2026-09-26 easy-ticketbooking run burned five run_tests calls (three
    blind 120s runner-cap kills) on failures whose evidence survived in
    `backend/test-results/` and whose root cause was a trailing-label-colon
    accessible-name mismatch against `exact: true` locators. These rules are
    the codified playbook; the pins turn an accidental rewording into a local
    test failure.
    """

    skill = (SKILL_ROOT / "tdd-test-failure-repair" / "SKILL.md").read_text(encoding="utf-8")
    assert "24." in skill
    assert "Command timed out after N seconds." in skill
    assert "not a test verdict" in skill
    assert "discards partial output on timeout" in skill
    assert "backend/test-results/<failed-test>/" in skill
    assert "diagnostic probe" in skill
    assert "25." in skill
    assert "test.describe.configure({ mode: 'serial' })" in skill
    assert "the layer still closes on a green full run" in skill
    assert "26." in skill
    assert "frozen in its initial state" in skill
    assert "a trailing colon" in skill
    assert "Repair direction follows 21a" in skill


def test_design_skills_shared_shell_literal_rules_pinned() -> None:
    leaf_full = (SKILL_ROOT / "leaf-full-design" / "SKILL.md").read_text(encoding="utf-8")
    assert "Carry requirement-stated UI anchors into the interface contract verbatim" in leaf_full
    assert "distinctly capitalized UI name" in leaf_full
    assert "exactly ONE requirement-quoted literal" in leaf_full
    assert "intersection of the quoted-literal sets" in leaf_full
    assert "<label htmlFor>" in leaf_full
    # Visual-reference section titles are structural anchors: run 2 asserted
    # the register page's 账户信息 heading, which no requirement text mentions.
    assert "treat those section titles as structural anchors" in leaf_full

    ui_only = (SKILL_ROOT / "non-leaf-ui-only-design" / "SKILL.md").read_text(encoding="utf-8")
    assert "Shell navigation and auth slots are assertion targets" in ui_only
    assert "do not substitute a translation" in ui_only
    assert "anchored or exact matchers fail on concatenated text" in ui_only

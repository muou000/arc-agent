"""Pin the TDD-stage guidance added after the 2026-09-19 test1 failure analysis.

The REQ-1 failure loop on that run showed three prompt-level gaps: the agent
kept editing tests after a green run (and turned them red again), it silenced
a multiple-match ``getByText`` failure with a zero-width-space hack in the
label, and it burned a full budget on an exact-call-count assertion that
StrictMode double-invocation made unfixable. These tests pin the prompt and
skill wording that guards each mode.
"""
from __future__ import annotations

from pathlib import Path

from agents.context.prompts.test_driven_developer import get_system_prompt, get_user_prompt

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills"


def _prompt() -> str:
    return get_user_prompt(
        node_id="REQ-1",
        dynamic_context="ctx",
        test_files=["tests/a.test.js"],
        test_type="Unit",
        node_tests=[],
    ) + "\n" + get_system_prompt()


def test_prompt_forbids_post_green_edits() -> None:
    prompt = _prompt()
    assert "layer is DONE" in prompt
    assert "do not edit its tests" in prompt
    # The full constraint: green closes both the tests AND the code they
    # cover (the REQ-1 failure mode was re-editing covered code, not tests).
    assert "do not edit its tests or the code they cover anymore" in prompt


def test_prompt_forbids_adversarial_test_silencing() -> None:
    prompt = _prompt()
    assert "zero-width" in prompt
    # Anchor the full prohibition sentence, not just topic words: a weaker
    # rewording ("avoid zero-width...") must fail this pin, because the
    # prohibition is the contract, not the vocabulary.
    assert (
        "Never silence a test by adversarial means: no zero-width or invisible "
        "characters in labels, no deleting assertions" in prompt
    )
    assert "distinct accessible names" in prompt


def test_prompt_requires_strictmode_check_before_exact_counts() -> None:
    prompt = _prompt()
    assert "StrictMode" in prompt
    assert "double-invokes effects" in prompt
    assert "Do not blindly re-run the same count assertion" in prompt


def test_repair_skill_rules_pinned() -> None:
    skill = (SKILL_ROOT / "tdd-test-failure-repair" / "SKILL.md").read_text(encoding="utf-8")
    assert "20. When a full-layer run passes, the layer is done" in skill
    assert "21. Never silence a failing test by adversarial means" in skill
    assert "22. When a React test asserts an exact count" in skill


def test_harness_skill_strictmode_section_pinned() -> None:
    skill = (SKILL_ROOT / "web-test-harness-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "## StrictMode and exact-call-count assertions" in skill
    assert "double-invokes" in skill
    assert "zero-width" in skill


def test_prompt_omits_merge_conflict_guidance_by_default() -> None:
    prompt = _prompt()
    assert "Merge Conflict Retry" not in prompt


def test_prompt_renders_merge_conflict_retry_guidance() -> None:
    """The one-shot IMPLEMENT conflict retry injects the sibling-owned paths
    and the steer-away contract into the TDD user prompt."""
    prompt = get_user_prompt(
        node_id="REQ-1",
        dynamic_context="ctx",
        test_files=["tests/a.test.js"],
        test_type="Unit",
        node_tests=[],
        merge_conflict={"paths": ["shared.js", "glue/app.js"], "phase": "implement"},
    )
    assert "Merge Conflict Retry" in prompt
    assert "one-shot retry" in prompt
    assert "shared.js, glue/app.js" in prompt
    assert "Do not create, rewrite, or reorganize those files" in prompt
    assert "node-owned file paths" in prompt
    # Blank-string paths are dropped rather than rendered as empty entries.
    prompt_sparse = get_user_prompt(
        node_id="REQ-1",
        dynamic_context="ctx",
        test_files=["tests/a.test.js"],
        test_type="Unit",
        node_tests=[],
        merge_conflict={"paths": ["", "shared.js"], "phase": "implement"},
    )
    assert "shared.js" in prompt_sparse
    assert ", ," not in prompt_sparse

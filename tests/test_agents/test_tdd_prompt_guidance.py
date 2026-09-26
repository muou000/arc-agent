"""Pin the TDD-stage guidance added after the 2026-09-19 test1 failure analysis.

The REQ-1 failure loop on that run showed three prompt-level gaps: the agent
kept editing tests after a green run (and turned them red again), it silenced
a multiple-match ``getByText`` failure with a zero-width-space hack in the
label, and it burned a full budget on an exact-call-count assertion that
StrictMode double-invocation made unfixable. These tests pin the prompt and
skill wording that guards each mode.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from agents.context.prompts.test_driven_developer import get_system_prompt, get_user_prompt
from agents.test_driven_developer import TestDrivenDeveloper
from tests.helpers.faux import FauxChatModel, faux_tool_call, faux_text

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


def test_repair_skill_read_only_forbidden_zones_pinned() -> None:
    """The 2026-09-25 easy-ticketbooking run burned its last 15 minutes
    probing ``node_modules`` (110 no-hit globs) because no model-facing
    surface said the dependency/build subtrees are off-limits and that an
    empty/denied result is not proof of absence (issue #298). Rule 10b is
    the canonical statement; a weaker rewording must fail this pin."""
    skill = (SKILL_ROOT / "tdd-test-failure-repair" / "SKILL.md").read_text(encoding="utf-8")
    assert "10b. Read-only forbidden zones:" in skill
    # The zone enumeration follows the mechanical deny list, lockfiles included.
    assert "`node_modules`, `dist`, `dist-ssr`, `build`, `coverage`, `.vite`" in skill
    assert "`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`" in skill
    assert "`.arc` (except `.arc/tdd_runs`)" in skill
    # Empty/denied is not absence — the load-bearing fact the run lacked.
    assert "does not mean the file does not exist" in skill
    assert "matches under denied subtrees are withheld, not listed" in skill
    # run_tests is the only verification channel, and the gate vocabulary is
    # quoted verbatim: the executor's ARC_TDD_HARD_STOP copy is pinned on the
    # other side (tests/test_workflow/test_tdd_executor.py), so skill and
    # gate cannot drift apart silently.
    assert "The only valid verification of an environment repair is `run_tests`" in skill
    assert "If the layer is already closed (`ARC_TDD_HARD_STOP`)" in skill


def test_arc_tdd_skill_description_targets_implement_stage() -> None:
    """The optional TDD catalog entry must not invite an unsupported workflow."""

    skill = (SKILL_ROOT / "test-driven-development" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(skill.split("---", 2)[1])
    description = str(frontmatter["description"])

    assert "TestDrivenDeveloper" in description
    assert "IMPLEMENT" in description
    assert "run_tests" in description
    assert "any feature or bugfix" not in description
    assert "before writing implementation code" not in description


def test_arc_tdd_skill_obeys_arc_baseline_and_tool_contract() -> None:
    """The full optional skill must align with staged RED evidence and tools."""

    skill = (SKILL_ROOT / "test-driven-development" / "SKILL.md").read_text(encoding="utf-8")

    assert "Baseline RED Evidence" in skill
    assert "TestGenerator" in skill
    assert "run_tests" in skill
    assert "run_build" in skill
    assert "execute" not in skill
    assert "npm test" not in skill
    assert "delete product" not in skill.lower()
    assert "registered test" in skill.lower()


def test_arc_tdd_catalog_is_safe_on_the_real_implement_surface(
    tmp_project_dir: Path,
    arc_runtime,
) -> None:
    """Faux model sees the rewritten catalog on the real TDD adapter surface."""

    arc_runtime.traceability.store_requirement_tree(
        {"id": "REQ-TDD-CATALOG", "name": "Counter", "description": "Add two numbers"}
    )
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "read_file",
                {"file_path": "/skills/test-driven-development/SKILL.md"},
            ),
            faux_text("DONE"),
        ]
    )
    developer = TestDrivenDeveloper(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
    )

    asyncio.run(
        developer.run(
            node_id="REQ-TDD-CATALOG",
            test_files=["tests/test_counter.py"],
            test_type="Unit",
            node_tests=[],
        )
    )

    assert model.bound_tool_name_sets
    bound = set(model.bound_tool_name_sets[0])
    assert {"run_tests", "run_build", "install_dependencies"} <= bound
    assert "execute" not in bound

    system_messages = [
        message
        for turn in model.calls
        for message in turn
        if getattr(message, "type", "") == "system"
    ]
    assert system_messages
    system_prompt = "\n".join(str(message.content) for message in system_messages)
    assert "test-driven-development" in system_prompt
    assert "TestDrivenDeveloper" in system_prompt
    assert "npm test" not in system_prompt

    skill_results = [
        message
        for turn in model.calls
        for message in turn
        if getattr(message, "type", "") == "tool"
        and getattr(message, "tool_call_id", "")
    ]
    assert skill_results
    skill_content = "\n".join(str(message.content) for message in skill_results)
    assert "Baseline RED Evidence" in skill_content
    assert "run_tests" in skill_content
    assert "npm test" not in skill_content
    assert "never remove product files" in skill_content.lower()


def test_hard_stop_prompt_closes_environment_repair_path() -> None:
    prompt = _prompt()
    assert "record the repair as unverified in the failure report" in prompt
    assert "Do not inspect node_modules or dist" in prompt


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

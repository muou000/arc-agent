"""Prompt contract tests for the declaration-first DESIGN workflow."""

from agents.context.prompts.interface_designer import get_system_prompt, get_user_prompt
from agents.interface_designer import InterfaceDesigner
from agents.runtime.stage_discipline import MAX_DESIGN_WRITES, MAX_NON_LEAF_DESIGN_WRITES


def test_interface_designer_prompt_puts_declaration_before_file_materialization() -> None:
    prompt = get_system_prompt()

    declaration = "Declaration-first order is mandatory"
    materialization = "materialize each declared skeleton at most once"

    assert declaration in prompt
    assert materialization in prompt
    assert prompt.index(declaration) < prompt.index(materialization)
    assert "A blocked rewrite is a stop signal" in prompt


def test_interface_designer_prompt_states_the_write_budget_counting_rule() -> None:
    """The budget semantics must be stated up front (arc-output4 section 4.2).

    The pass spent 3-4 turns reverse-engineering whether "8" counted new
    skeletons or total modifications and whether edit_file was charged. The
    prompt now pins the counting rule and both tier ceilings before any
    materialization happens.
    """

    prompt = get_system_prompt()

    assert f"at most {MAX_DESIGN_WRITES} distinct files as a leaf node" in prompt
    assert f"or {MAX_NON_LEAF_DESIGN_WRITES} as a non-leaf shell node" in prompt
    assert "re-touching a file you already wrote costs nothing" in prompt
    assert "reserved the moment a call is validated" in prompt
    # The budget rule follows the declaration-first rule it budgets for.
    budget_rule = "Design write budget, decided before you start"
    materialization = "materialize each declared skeleton at most once"
    assert prompt.index(budget_rule) > prompt.index(materialization)


def test_interface_designer_prompt_pins_mechanical_read_lock() -> None:
    """arc-output4 DESIGN wasted 11 rounds on read blocks: the designer wrote
    skeletons and then read them back "to verify the final state". The rule
    existed in the shared reflection policy but was buried; pin the
    stage-level mechanical statement (locked paths + blocked retry shapes).
    """

    prompt = get_system_prompt()

    assert "Read discipline is enforced mechanically" in prompt
    assert "every later `read_file` on it is rejected" in prompt
    assert "including retries with a smaller `limit`, a shifted `offset`" in prompt
    assert "Never read a file back to verify the final state of your own work" in prompt


def test_interface_designer_prompt_pins_shape_only_skeleton_boundary() -> None:
    """Issue #158 (ADR 0005): the skeleton definition must be mechanical
    (shape, not length), the Hard boundary must lead the Role section, and
    the prompt must teach the escape hatch - behavior goes to the stage
    response - instead of teaching chunked writes."""

    prompt = get_system_prompt()

    # Hard boundary leads the Role section (PR #74 prominence-fronting pattern).
    assert "Hard boundary: DESIGN designs, it does not implement" in prompt
    assert prompt.index("Hard boundary: DESIGN designs, it does not implement") < prompt.index(
        "Position: first agent stage"
    )

    # Mechanical shape definition: no function bodies, shape checklist, and
    # the implementation tripwires.
    assert "shape-only" in prompt
    assert "`// TODO(TDD): <behavior>` markers" in prompt
    assert "beyond a single return statement" in prompt
    assert "Any `if`/loop, SQL, validation logic" in prompt

    # Economic motivation: implementation in DESIGN is thrown-away work.
    assert "discarded work" in prompt

    # The escape hatch replaces the chunking teaching.
    assert "it is not a skeleton" in prompt
    assert "stage response for TestDrivenDeveloper" in prompt


def test_interface_designer_prompt_drops_chunking_teaching() -> None:
    """The three chunk-teaching sites (compact first chunk + append_file
    continuations + 'overcome the DESIGN skeleton limit') taught the exact
    bypass that burned the arc-output-serial run; they must be gone."""

    prompt = get_system_prompt()
    user_prompt = get_user_prompt(
        node_id="REQ-LEAF-1",
        requirement_data={"id": "REQ-LEAF-1", "name": "Registration", "description": "Register an account.", "children_ids": []},
        dynamic_context="",
    )

    assert "cohesive continuation" not in prompt
    assert "chunk small enough" not in prompt
    assert "DESIGN skeleton limit" not in prompt
    assert "feature-complete business flow" not in prompt
    assert "DESIGN skeleton limit" not in user_prompt
    # The user prompt carries the same one-compact-write escape hatch.
    assert "is not a skeleton" in user_prompt
    assert "do not split it into chunks or append continuations" in user_prompt
    assert "put the complete behavior description in your stage response for TestDrivenDeveloper" in user_prompt
    # The response contract must not send behavior detail back into skeleton
    # files - that contradiction is what made the escape hatch unreachable.
    assert "put implementation detail into the skeleton files, not into the response" not in prompt


def test_interface_designer_user_prompt_pins_the_node_budget_number() -> None:
    leaf = get_user_prompt(
        node_id="REQ-LEAF-1",
        requirement_data={"id": "REQ-LEAF-1", "name": "Registration", "description": "Register an account.", "children_ids": []},
        dynamic_context="",
    )
    assert f"design write budget for this pass is {MAX_DESIGN_WRITES} distinct files" in leaf

    shell = get_user_prompt(
        node_id="ROOT",
        requirement_data={"id": "ROOT", "name": "Shell", "description": "Site shell.", "children_ids": ["REQ-LEAF-1"]},
        dynamic_context="",
        max_design_writes=MAX_NON_LEAF_DESIGN_WRITES,
    )
    assert f"design write budget for this pass is {MAX_NON_LEAF_DESIGN_WRITES} distinct files" in shell


def test_interface_designer_write_budget_tiers_on_children_ids() -> None:
    """Leaf-ness comes from the traceability record's children_ids, the same
    source the workflow non-leaf skip gate reads (arc-output4: the ROOT shell
    pass legally needed 12 files, so its tier is the non-leaf ceiling)."""

    assert InterfaceDesigner._max_design_writes({"children_ids": []}) == MAX_DESIGN_WRITES
    assert InterfaceDesigner._max_design_writes({}) == MAX_DESIGN_WRITES
    assert (
        InterfaceDesigner._max_design_writes({"children_ids": ["REQ-1", "REQ-2"]})
        == MAX_NON_LEAF_DESIGN_WRITES
    )


def test_interface_designer_user_prompt_pins_leaf_interfaces_non_empty() -> None:
    """A leaf may not answer the reuse shortcut with an empty interfaces array.

    Submission 77bdef8ce610: the pass claimed the parent-designed shell was
    reused in `summary` prose and returned `"interfaces": []`; the workflow
    now fails that shape, so the prompt must pin the same rule.
    """

    prompt = get_user_prompt(
        node_id="REQ-LEAF-1",
        requirement_data={
            "id": "REQ-LEAF-1",
            "name": "Registration",
            "description": "Register an account.",
            "children_ids": [],
        },
        dynamic_context="",
    )

    assert "its `interfaces` array must be non-empty" in prompt
    assert "original `interface_id`" in prompt
    assert "Only a non-leaf node without visual references may return an empty list" in prompt


def test_interface_designer_user_prompt_leads_serialization_rules_with_type_mandatory() -> None:
    """Issue #230 (serial-5): all 11 interface records omitted `type` because
    the interface_id already encodes it and the mandatory-field rule was
    buried mid-list. The rule must lead the serialization rules and name the
    exact trap: the id segment does not substitute for the field."""

    prompt = get_user_prompt(
        node_id="REQ-LEAF-1",
        requirement_data={
            "id": "REQ-LEAF-1",
            "name": "Registration",
            "description": "Register an account.",
            "children_ids": [],
        },
        dynamic_context="",
    )

    assert "Every interface record must carry a `type` field" in prompt
    assert "even when the `interface_id` already contains the type segment" in prompt
    # Prominence: the rule is the first serialization rule of the response
    # contract, ahead of the response-shape and field-inventory statements
    # it used to be buried under.
    assert prompt.index("Every interface record must carry a `type` field") < prompt.index(
        "Return `summary`, `interfaces`, and `files_written`"
    )
    assert prompt.index("Every interface record must carry a `type` field") < prompt.index(
        "Each interface should include"
    )
    # The old buried one-liner is gone; the promoted rule is the only statement.
    assert "The `type` field must be exactly one of" not in prompt

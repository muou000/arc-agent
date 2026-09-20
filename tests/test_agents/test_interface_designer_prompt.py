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

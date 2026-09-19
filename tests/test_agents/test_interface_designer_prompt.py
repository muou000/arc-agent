"""Prompt contract tests for the declaration-first DESIGN workflow."""

from agents.context.prompts.interface_designer import get_system_prompt, get_user_prompt


def test_interface_designer_prompt_puts_declaration_before_file_materialization() -> None:
    prompt = get_system_prompt()

    declaration = "Declaration-first order is mandatory"
    materialization = "materialize each declared skeleton at most once"

    assert declaration in prompt
    assert materialization in prompt
    assert prompt.index(declaration) < prompt.index(materialization)
    assert "A blocked rewrite is a stop signal" in prompt


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

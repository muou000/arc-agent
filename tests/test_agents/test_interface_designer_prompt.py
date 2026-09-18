"""Prompt contract tests for the declaration-first DESIGN workflow."""

from agents.context.prompts.interface_designer import get_system_prompt


def test_interface_designer_prompt_puts_declaration_before_file_materialization() -> None:
    prompt = get_system_prompt()

    declaration = "Declaration-first order is mandatory"
    materialization = "materialize each declared skeleton at most once"

    assert declaration in prompt
    assert materialization in prompt
    assert prompt.index(declaration) < prompt.index(materialization)
    assert "A blocked rewrite is a stop signal" in prompt

from __future__ import annotations

from agents.context.prompts.test_generator import get_system_prompt, get_user_prompt


def test_system_prompt_forbids_empty_interface_coverage_fallback() -> None:
    prompt = get_system_prompt()

    assert "empty `interface_ids` list is forbidden" in prompt
    assert "Do not search ROOT, invent ids, or submit `[]` to bypass validation." in prompt
    assert "never replace the id with `[]` just to make the declaration pass" in prompt


def test_system_prompt_pins_read_lock_and_no_probe_retries() -> None:
    """arc-output4: after a read block the TestGenerator retried the same
    path with `limit: 5` (once in REQ-1, four files in a row in REQ-2) -
    narrow probes to fetch `first_line` or "verify" a written test. Pin both
    the mechanical lock statement and the explicit no-retry-shape rule.
    """

    prompt = get_system_prompt()

    assert "Read discipline is enforced mechanically" in prompt
    assert "every test file you write in this pass is read-locked immediately" in prompt
    assert "retrying with a smaller `limit` (for example `limit: 5`), a shifted `offset`" in prompt
    assert "not to fetch its `first_line`" in prompt


def test_user_prompt_requires_exact_current_contract_ids() -> None:
    prompt = get_user_prompt(
        node_id="REQ-X",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
        interface_contract='{"interface_id":"REQ-X-FUNC-EXAMPLE"}',
    )

    assert "`interface_ids` must be non-empty" in prompt
    assert "exact ids from the Current Interface Contract" in prompt
    assert "Do not replace them with an empty list." in prompt

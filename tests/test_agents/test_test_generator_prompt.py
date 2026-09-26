from __future__ import annotations

from agents.context.prompts import common as common_prompts
from agents.context.prompts.test_generator import get_system_prompt, get_user_prompt
from agents.tools.test_manifest import TestManifestLock, build_declare_test_manifest_tool


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

    assert "That boundary's read rule is enforced mechanically" in prompt
    assert "every test file you write in this pass is read-locked immediately" in prompt
    assert "retrying with a smaller `limit` (for example `limit: 5`), a shifted `offset`" in prompt
    assert "not to fetch its `first_line`" in prompt
    # The mechanical statement must read as an enforcement note on the Hard
    # boundary line directly above it, not as a second, ambiguous rule of
    # its own (review finding 6f448e98fbf1 on PR #74).
    assert prompt.index("Hard boundary: write verification assets") < prompt.index(
        "That boundary's read rule is enforced mechanically"
    )


def test_system_prompt_states_redeclaration_merge_protocol() -> None:
    """Issue #183: "exactly once" contradicted the tool's re-declaration
    merge protocol — the lock merges a later declaration, adding only paths
    whose earlier attempt failed validation (mechanical contract pinned by
    test_second_declaration_extends_the_lock_without_reset). After a
    rejection the model must know that fixing and re-declaring is the
    recovery path, not a protocol violation.
    """

    prompt = get_system_prompt()

    assert "exactly once" not in prompt
    assert "If the declaration is rejected, fix the reported issues and re-declare" in prompt
    assert "the lock merges, adding only paths whose earlier declaration failed" in prompt


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


def test_prompts_require_contract_http_statuses_without_a_200_default() -> None:
    system_prompt = get_system_prompt()
    user_prompt = get_user_prompt(
        node_id="REQ-X",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
        interface_contract='{"interface_id":"REQ-X-API-EXAMPLE"}',
    )

    assert "Never invent or default to 200" in system_prompt
    assert "including 201 or another non-default 2xx" in user_prompt
    assert "report `needs-info` instead" in user_prompt


def test_generator_prompts_and_manifest_tool_explain_helper_declarations() -> None:
    system_prompt = get_system_prompt()
    user_prompt = get_user_prompt(
        node_id="REQ-X",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
    )
    manifest_tool = build_declare_test_manifest_tool(
        node_id="REQ-X",
        manifest_lock=TestManifestLock(node_id="REQ-X", enforce_node_namespace=True),
    )

    for visible_text in (system_prompt, user_prompt, manifest_tool.__doc__ or ""):
        normalized = " ".join(visible_text.split())
        assert "stage pipeline is active" in normalized
        assert "declare_stage_write_set" in normalized
        assert "declare_test_manifest" in normalized
        assert "node" in normalized.lower()
        assert "shared runner configuration" in normalized.lower()
        assert "read-only" in normalized
        assert "stay writable without a declaration" not in normalized
        assert "declared nowhere and stay writable" not in normalized

    assert "When the stage pipeline is active, shared runner configuration" in system_prompt


def test_repair_timing_policy_is_consistent_across_test_generator_prompts_and_tools() -> None:
    system_prompt = get_system_prompt()
    user_prompt = get_user_prompt(
        node_id="REQ-X",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
    )
    policy = common_prompts.test_generator_repair_policy()

    assert policy in system_prompt
    assert system_prompt.count(policy) == 1
    assert policy in user_prompt
    assert policy in common_prompts.workspace_tool_policy()
    assert "repair generated tests in the same pass" not in system_prompt
    assert "Do not run, reread, or self-repair files written in this pass" not in user_prompt
    assert "separate later repair pass" in system_prompt
    assert "separate later repair pass" in user_prompt

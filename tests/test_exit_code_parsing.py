"""Exit-code parsing must ignore incidental text from test runners."""

from __future__ import annotations

from app_type_handler.test_results import TestRunResult, parse_test_run


def test_parse_test_run_ignores_embedded_exit_code_text() -> None:
    output = "runner note: Exit Code: 99 was mentioned\nExit Code: 0\nPASS renders home\n"

    parsed = parse_test_run(output)

    assert parsed.exit_code == 0


def test_parse_test_run_skips_malformed_exit_code_line() -> None:
    output = "Exit Code: unavailable\nExit Code: 1\nFAIL renders home\n"

    parsed = parse_test_run(output)

    assert parsed.exit_code == 1


def test_parse_test_run_aggregates_nested_e2e_commands() -> None:
    output = (
        "Runner: Playwright\n"
        "=== Frontend Build ===\nExit Code: 0\n"
        "=== Database Prepare ===\nExit Code: 0\n"
        "=== Playwright Result ===\nExit Code: 1\nFAIL renders home\n"
    )

    parsed = parse_test_run(output)

    assert parsed.exit_code == 1


def test_parse_test_run_prefers_explicit_batch_exit_code() -> None:
    output = (
        "Runner: Vitest\nExit Code: 1\n"
        "=== Backend Vitest Batch ===\nExit Code: 1\n"
        "=== Frontend Vitest Batch ===\nExit Code: 0\n"
    )

    parsed = parse_test_run(output)

    assert parsed.exit_code == 1


def test_parse_test_run_reports_the_first_failing_nested_stage() -> None:
    """Several nested stages failed: the earliest failure in execution order wins."""
    output = (
        "=== Frontend Build ===\nExit Code: 0\n"
        "=== Database Prepare ===\nExit Code: 2\n"
        "=== Playwright Result ===\nExit Code: 1\nFAIL renders home\n"
    )

    parsed = parse_test_run(output)

    assert parsed.exit_code == 2


def test_structural_exit_code_overrides_the_transcription() -> None:
    """Handlers that know the code structurally never re-derive it from text."""

    result = parse_test_run("Exit Code: 0\nSTDOUT:\nnested Exit Code: 3\n", exit_code=3)

    assert result.exit_code == 3
    assert not result.passed_run


def test_partitions_split_reporter_lines_by_outcome() -> None:
    output = (
        "Exit Code: 1\n"
        "✓ renders home\n"
        "PASS backend suite\n"
        "✗ rejects empty payload\n"
        "FAIL frontend suite\n"
        "✘ summary glyph row\n"
        "line mentioning FAILED keyword\n"
    )

    result = parse_test_run(output)

    assert result.passed == ["✓ renders home", "PASS backend suite"]
    assert result.failed == [
        "✗ rejects empty payload",
        "FAIL frontend suite",
        "line mentioning FAILED keyword",
    ]


def test_default_fields_carry_the_run_facts() -> None:
    result = parse_test_run("Exit Code: 1\nCannot find module 'left-pad'\nFAIL a\n")

    assert result.environment_failure == "missing dependency: left-pad"
    assert result.fingerprint.startswith("1|")
    assert result.build_note == ""
    assert result.served_verdict == ""
    assert result.run_log_path == ""
    assert result.passed_run is False


def test_explicit_verdicts_pass_through_untouched() -> None:
    """Handler-known build/served verdicts land on the object verbatim."""

    result = TestRunResult(
        exit_code=0,
        output="Exit Code: 0\n",
        build_note="reused existing frontend/dist (fingerprint abc123def456)",
        served_verdict="frontend/dist/index.html present at result time",
    )

    assert result.build_note.startswith("reused existing frontend/dist")
    assert result.served_verdict.startswith("frontend/dist/index.html present")
    assert result.passed_run

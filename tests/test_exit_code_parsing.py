"""Exit-code parsing must ignore incidental text from test runners."""

from __future__ import annotations

from app_type_handler.test_results import parse_test_results


def test_parse_test_results_ignores_embedded_exit_code_text() -> None:
    output = "runner note: Exit Code: 99 was mentioned\nExit Code: 0\nPASS renders home\n"

    parsed = parse_test_results(output)

    assert parsed["exit_code"] == 0


def test_parse_test_results_skips_malformed_exit_code_line() -> None:
    output = "Exit Code: unavailable\nExit Code: 1\nFAIL renders home\n"

    parsed = parse_test_results(output)

    assert parsed["exit_code"] == 1

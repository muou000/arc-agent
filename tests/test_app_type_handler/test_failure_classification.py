"""Environment failures must stop the TDD loop, assertion failures must not.

A node whose workspace is broken - a missing package, a test runner that was
never installed - fails every attempt for the same reason. The agent has no way
to install packages mid-compile, so retrying burns the whole budget (~10 model
turns per layer, three layers per node, 117 leaves) on an un-fixable failure.

`classify_test_failure` separates that case from an ordinary failure the agent
*can* fix by editing. Unresolved relative specifiers count as fixable: they
point at workspace files the agent can create or import paths it can correct
(observed on the 2026-09-14 ticket-booking run: `Cannot find module
'../src/database/test_harness'` and `Failed to resolve import
"./helpers/auth.fixtures"` were both repairable test defects, but the
environment label closed the layer before the repair could be validated).
"""

from __future__ import annotations

import pytest

from app_type_handler.test_results import classify_test_failure, failure_fingerprint


@pytest.mark.parametrize(
    ("output", "expected_reason"),
    [
        # Dependency / module resolution
        (
            "Exit Code: 1\nSTDERR:\nError: Cannot find module "
            "'/c:/ws/frontend/tests/_shims/testing-library-dom.mjs'",
            "missing dependency",
        ),
        (
            "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'axios'",
            "missing dependency",
        ),
        (
            'Failed to resolve import "react-router-dom" from "src/App.tsx"',
            "unresolved import",
        ),
        (
            'Error [ERR_MODULE_NOT_FOUND]: Cannot find package \'dotenv\'',
            "missing dependency",
        ),
        (
            # No quoted specifier on the line: the bare-code fallback must fire
            # even though the error name itself sits inside quotes.
            "node:internal/modules/run_main:123\n  code: 'ERR_MODULE_NOT_FOUND'",
            "missing dependency",
        ),
        # Test runner never installed
        (
            "Exit Code: 1\nSTDERR:\n'vite' is not recognized as an internal or "
            "external command,\r\noperable program or batch file.",
            "test runner not installed",
        ),
        (
            "Exit Code: 1\nSTDERR:\n'vitest' is not recognized as an internal or "
            "external command",
            "test runner not installed",
        ),
        ("sh: 1: playwright: not found", "test runner not installed"),
        # Broken package.json / empty install
        ('npm error Missing script: "build"', "missing npm script"),
        ("Error: node_modules does not exist", "dependencies not installed"),
        # Playwright runner present but its browser binaries were never downloaded
        (
            "  1) test-e2e\\home.spec.js:22:3 › Display the default home page \n"
            "    Error: browserType.launch: Executable doesn't exist at "
            "C:\\Users\\u\\AppData\\Local\\ms-playwright\\chromium_headless_shell-1200"
            "\\chrome-headless-shell-win64\\chrome-headless-shell.exe",
            "browser binaries not installed",
        ),
        (
            "║ Looks like Playwright Test or Playwright was just installed or updated. ║\n"
            "║ Please run the following command to download new browsers:              ║",
            "browser binaries not installed",
        ),
        # Test runner crashes that no code edit can fix (observed on the 12306
        # benchmark with Node 22.11: jsdom's dependency tree requires require(esm)).
        (
            "Error: [vitest-pool]: Failed to start forks worker for test files "
            "D:/ws/frontend/tests/data/REQ-1.1.homeContent.spec.ts.",
            "test worker failed to start",
        ),
        (
            "Caused by: Error: require() of ES Module "
            "D:/ws/frontend/node_modules/@exodus/bytes/encoding-lite.js from "
            "D:/ws/frontend/node_modules/html-encoding-sniffer/lib/html-encoding-sniffer.js "
            "not supported.",
            "cjs require of esm module",
        ),
        (
            "Error: The module '//sqlite3' was compiled against a different "
            "Node.js version using NODE_MODULE_VERSION 127.",
            "native module built for another node version",
        ),
    ],
)
def test_environment_failures_are_detected(output: str, expected_reason: str) -> None:
    assert expected_reason in classify_test_failure(output)


@pytest.mark.parametrize(
    "output",
    [
        # Ordinary assertion failures: the agent can fix these.
        "Exit Code: 1\nFAIL tests/REQ-1.1-home-page.test.tsx\n"
        "  × renders the hero banner\n  expected data-region to be present",
        "Test Files  1 failed (1)\n     Tests  3 failed (3)",
        "AssertionError: expected 'Login' to equal 'Log in'",
        "Exit Code: 1\nSTDOUT:\n  ✗ REQ-5.2.7 unavailable ticket class",
        # Unresolved relative specifiers are fixable with a file edit: create
        # the missing local module or correct the import path (2026-09-14
        # ticket-booking run, both cases repaired by the agent afterwards).
        "Error: Cannot find module '../src/database/test_harness'\n"
        "Require stack:\n- /ws/backend/tests/features/authService.test.js",
        'Error: Failed to resolve import "./helpers/auth.fixtures" from '
        '"tests/features/registerPage.test.tsx". Does the file exist?',
        'Could not resolve "./pages/HomePage" from "src/App.tsx"',
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find module './lib/env.mjs' "
        "imported from /ws/backend/src/app.js",
        # ERR_MODULE_NOT_FOUND whose specifier is relative: the capture branch
        # sees it (and the relative guard skips it); the bare-code fallback
        # must not fire just because the line contains quotes.
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find module '../src/database/test_harness'",
        "Error: Cannot find module '..\\src\\database\\test_harness'",
        # A passing run is never an environment failure.
        "Exit Code: 0\nSTDOUT:\n  ✓ renders the home page",
        # Empty / missing output tells us nothing.
        "",
    ],
)
def test_assertion_failures_are_not_flagged(output: str) -> None:
    assert classify_test_failure(output) == ""


def test_reason_names_the_offending_module() -> None:
    """The reason is surfaced to the operator, so it must be actionable."""

    reason = classify_test_failure("Error: Cannot find module '@testing-library/dom'")
    assert reason == "missing dependency: @testing-library/dom"


def test_reason_falls_back_to_the_bare_label_without_a_capture() -> None:
    assert classify_test_failure("Error [ERR_MODULE_NOT_FOUND]: ...") == "missing dependency"


def test_long_environment_detail_is_truncated() -> None:
    """The reason is surfaced in the CLI, so a runaway path must not flood it."""

    reason = classify_test_failure(
        "Error: browserType.launch: Executable doesn't exist at "
        + "C:\\very\\long\\segment\\" * 40
        + "chrome-headless-shell.exe"
    )

    assert reason.startswith("browser binaries not installed: ")
    assert len(reason) <= 160


# ---------------------------------------------------------------------------
# failure_fingerprint: stall-governance input
# ---------------------------------------------------------------------------


def test_fingerprint_pairs_exit_code_with_first_error_line() -> None:
    output = (
        "Exit Code: 1\n"
        "STDERR:\n"
        "Some runner noise\n"
        "AssertionError: expected 'Login' to equal 'Log in'\n"
        "more stack\n"
    )
    assert failure_fingerprint(output) == "1|AssertionError: expected 'Login' to equal 'Log in'"


def test_fingerprint_is_stable_for_identical_failures() -> None:
    """The stall detector compares consecutive fingerprints; identical failures
    (including identical trailing stack traces) must produce identical output."""
    output = (
        "Exit Code: 1\n"
        "FAIL tests/unit/test_calc.py\n"
        "AssertionError: add(1, 1) returned 0\n"
        "at Object.<anonymous> (test_calc.js:5:9)\n"
    )
    assert failure_fingerprint(output) == failure_fingerprint(output)


def test_fingerprint_distinguishes_different_failures() -> None:
    """Different assertion messages are different stalls only when they repeat;
    the fingerprint itself must tell them apart."""
    first = failure_fingerprint("Exit Code: 1\nAssertionError: expected 'Login' to equal 'Log in'")
    second = failure_fingerprint("Exit Code: 1\nAssertionError: add(1, 1) returned 0")
    assert first != second


def test_fingerprint_handles_output_without_error_lines() -> None:
    """A failing run with no recognizable error line still yields a fingerprint
    (exit code plus empty key line) instead of raising."""
    assert failure_fingerprint("Exit Code: 1\nSTDERR:\n(none)") == "1|"


def test_fingerprint_handles_empty_output() -> None:
    assert failure_fingerprint("") == "-1|"


def test_fingerprint_truncates_runaway_key_lines() -> None:
    """The fingerprint is echoed into tool results and sessions; keep it short."""
    long_line = "AssertionError: " + "x" * 500
    fingerprint = failure_fingerprint(f"Exit Code: 1\n{long_line}")
    assert fingerprint.startswith("1|AssertionError: ")
    assert len(fingerprint) <= 162  # "1|" + 160-char cap


def test_fingerprint_skips_success_and_diff_lines() -> None:
    """Success markers, jest/vitest diff rows and zero-failure summaries
    mention error-ish keywords without being the failure; selecting one would
    give every distinct failure in a run the same generic fingerprint."""
    output = (
        "Exit Code: 1\n"
        "  ✓ renders the cart summary\n"
        "  Expected: 2\n"
        "  Received: 3\n"
        "  0 failed, 3 passed\n"
        "AssertionError: cart total mismatch\n"
    )
    assert failure_fingerprint(output) == "1|AssertionError: cart total mismatch"


def test_fingerprint_masks_ports_and_line_numbers() -> None:
    """The same connection failure on a restarted server (different port) or a
    shifted stack frame must keep the same fingerprint so the stall detector
    still sees the repeat."""
    first = failure_fingerprint("Exit Code: 1\nError: connect ECONNREFUSED 127.0.0.1:3001")
    second = failure_fingerprint("Exit Code: 1\nError: connect ECONNREFUSED 127.0.0.1:5173")
    assert first == second == "1|Error: connect ECONNREFUSED 127.0.0.1:#"

    shifted = failure_fingerprint("Exit Code: 1\nTypeError: cannot read props at App.js:42:17")
    original = failure_fingerprint("Exit Code: 1\nTypeError: cannot read props at App.js:39:11")
    assert shifted == original


def test_fingerprint_keeps_assertion_values_distinct() -> None:
    """Masking must not blur real assertion differences, or every failure in a
    layer looks like a stall of the first one."""
    first = failure_fingerprint("Exit Code: 1\nAssertionError: expected 'Login' to equal 'Log in'")
    second = failure_fingerprint("Exit Code: 1\nAssertionError: add(1, 1) returned 0")
    assert first != second


def test_fingerprint_masks_vitest_header_durations() -> None:
    """Vitest file-header lines embed the run duration; it must be masked.

    Observed on the 2026-09-19 test1 run: the first error-bearing line of a
    failed vitest run is the ``❯ file.test.js (14 tests | 11 failed) 980ms``
    header. The duration differs on every rerun, so every rerun got a distinct
    fingerprint, the stall governor (three identical consecutive fingerprints)
    never fired, and the agent burned 7 Integration calls on one unchanged
    500-failure with zero STALL DETECTED notices.
    """

    header = "❯ tests/routes/authRoutes.test.js (14 tests | 11 failed)"
    runs = [
        f"Exit Code: 1\n{header} 980ms\n",
        f"Exit Code: 1\n{header} 951ms\n",
        f"Exit Code: 1\n{header} 1040ms\n",
        f"Exit Code: 1\n{header} 1.2s\n",
    ]
    fingerprints = {failure_fingerprint(output) for output in runs}
    assert len(fingerprints) == 1
    assert "<dur>" in next(iter(fingerprints))


def test_fingerprint_strips_ansi_color_codes() -> None:
    """Color codes around durations/counts differ run to run and break matching."""

    plain = "Exit Code: 1\nAssertionError: expected 500 to be 200"
    colored = (
        "Exit Code: 1\n"
        "\x1b[31mAssertionError\x1b[39m: expected \x1b[31m500\x1b[39m to be \x1b[31m200\x1b[39m"
    )
    assert failure_fingerprint(plain) == failure_fingerprint(colored)


def test_fingerprint_keeps_assertion_numbers() -> None:
    """Masking must not swallow assertion values - distinct failures stay distinct."""

    first = failure_fingerprint("Exit Code: 1\nAssertionError: expected 2 got 1")
    second = failure_fingerprint("Exit Code: 1\nAssertionError: expected 2 got 3")
    assert first != second

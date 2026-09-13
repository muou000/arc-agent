"""Environment failures must stop the TDD loop, assertion failures must not.

A node whose workspace is broken - a missing dependency, a test runner that was
never installed - fails every attempt for the same reason. The agent has no way
to install packages mid-compile, so retrying burns the whole budget (~10 model
turns per layer, three layers per node, 117 leaves) on an un-fixable failure.

`classify_test_failure` separates that case from an ordinary assertion failure,
which the agent *can* fix by editing the implementation.
"""

from __future__ import annotations

import pytest

from app_type_handler.test_results import classify_test_failure


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
            'Could not resolve "./pages/HomePage" from "src/App.tsx"',
            "unresolved import",
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

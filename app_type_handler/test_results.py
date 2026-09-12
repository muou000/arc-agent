from __future__ import annotations

import re
from typing import Any


def parse_test_results(test_output: str) -> dict[str, Any]:
    """Parse ARC test-run output into a compact status structure."""

    result: dict[str, Any] = {"passed": [], "failed": [], "exit_code": -1, "sub_batches": []}
    output = test_output or ""
    result["exit_code"] = _extract_overall_exit_code(output)

    test_file_sections = re.findall(
        r"Test File:\s*(.+?)\r?\nTest Results:\r?\n(.*?)(?=\r?\nTest File: |\Z)",
        output,
        re.DOTALL,
    )
    for file_path, raw_section in test_file_sections:
        result["sub_batches"].append(
            {
                "requested_files": [file_path.strip().replace("\\", "/")],
                "exit_code": _extract_exit_code(raw_section),
                "raw_output": raw_section.strip(),
            }
        )

    if not result["sub_batches"]:
        requested_files = [
            line.split("-", 1)[1].strip().replace("\\", "/")
            for line in output.splitlines()
            if line.startswith("- ")
        ]
        for label in ("Backend Vitest Batch", "Frontend Vitest Batch", "Playwright E2E Batch"):
            section = _extract_labeled_section(output, label)
            if not section:
                continue
            result["sub_batches"].append(
                {
                    "requested_files": requested_files,
                    "exit_code": _extract_exit_code(section),
                    "raw_output": section.strip(),
                }
            )

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("PASS ", "✓", "√", "✔")):
            result["passed"].append(stripped)
        elif stripped.startswith(("FAIL ", "✗", "×", "✕")) or " FAILED" in stripped:
            result["failed"].append(stripped)
    return result


def _extract_overall_exit_code(output: str) -> int:
    """Prefer an explicit aggregate status, otherwise combine nested commands.

    When several nested stages fail, the first failing stage's code wins so the
    reported cause matches the earliest failure in execution order.
    """

    lines = (output or "").splitlines()
    exit_codes: list[tuple[int, int]] = []
    first_section_index: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if first_section_index is None and (
            stripped.startswith("=== ") or stripped.startswith("Test File:")
        ):
            first_section_index = index
        exit_code = _parse_exit_code_line(line)
        if exit_code is not None:
            exit_codes.append((index, exit_code))

    if not exit_codes:
        return -1

    if first_section_index is None:
        return exit_codes[0][1]

    aggregate_codes = [code for index, code in exit_codes if index < first_section_index]
    if aggregate_codes:
        return aggregate_codes[0]

    nested_codes = [code for _, code in exit_codes]
    return 0 if all(code == 0 for code in nested_codes) else next(
        code for code in nested_codes if code != 0
    )


def _extract_exit_code(output: str) -> int:
    for line in (output or "").splitlines():
        exit_code = _parse_exit_code_line(line)
        if exit_code is not None:
            return exit_code
    return -1


def _parse_exit_code_line(line: str) -> int | None:
    stripped = (line or "").strip()
    if not stripped.startswith("Exit Code:"):
        return None
    try:
        return int(stripped.split("Exit Code:", 1)[1].strip())
    except ValueError:
        return None


def _extract_labeled_section(output: str, label: str) -> str:
    pattern = rf"=== {re.escape(label)} ===\r?\n(.*?)(?=\r?\n=== |\Z)"
    match = re.search(pattern, output or "", re.DOTALL)
    return match.group(1).strip() if match else ""


# --------------------------------------------------------------------------
# environment vs. assertion failures
# --------------------------------------------------------------------------

_TEST_RUNNER_BINARIES = ("vite", "vitest", "playwright", "jest", "tsc", "eslint")

#: Ordered ``(reason, pattern)`` pairs describing failures that come from a
#: broken workspace rather than from the implementation under test.
_ENVIRONMENT_FAILURE_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "missing dependency",
        re.compile(r"(?:Cannot find module|Cannot find package)\s+'([^']+)'"),
    ),
    ("missing dependency", re.compile(r"ERR_MODULE_NOT_FOUND")),
    (
        "unresolved import",
        re.compile(r"(?:Failed to resolve import|Could not resolve)\s+\"?([^\"\s]+)\"?"),
    ),
    (
        "test runner not installed",
        re.compile(
            r"'(" + "|".join(_TEST_RUNNER_BINARIES) + r")' is not recognized",
            re.IGNORECASE,
        ),
    ),
    (
        "test runner not installed",
        re.compile(
            r"(?:(" + "|".join(_TEST_RUNNER_BINARIES) + r"):\s*not found"
            r"|command not found:?\s*(" + "|".join(_TEST_RUNNER_BINARIES) + r"))",
            re.IGNORECASE,
        ),
    ),
    ("missing npm script", re.compile(r"Missing script:\s*\"([^\"]+)\"")),
    (
        "dependencies not installed",
        re.compile(
            r"node_modules[^\n]{0,80}?\b(?:does not exist|not found|missing)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "browser binaries not installed",
        re.compile(r"Executable doesn't exist at\s+([^\r\n]+)"),
    ),
    (
        "browser binaries not installed",
        re.compile(r"Please run the following command to download new browsers"),
    ),
    (
        "browser launch failed",
        re.compile(r"browserType\.launch[^\r\n]*"),
    ),
)


_DETAIL_LIMIT = 120


def classify_test_failure(test_output: str) -> str:
    """Return a short reason when a failed run is environmental, else ``""``.

    Some failures are not the implementation's fault: a dependency is missing,
    the test runner was never installed, or ``node_modules`` is empty. The agent
    cannot fix any of these - it has no way to install packages mid-compile - so
    retrying only burns the TDD budget. Callers use this to stop the loop early
    instead of spending every attempt on an un-fixable failure.
    """

    output = test_output or ""
    for reason, pattern in _ENVIRONMENT_FAILURE_MARKERS:
        match = pattern.search(output)
        if not match:
            continue
        detail = next((group for group in match.groups() if group), "")
        detail = " ".join(detail.split())[:_DETAIL_LIMIT].strip()
        return f"{reason}: {detail}" if detail else reason
    return ""

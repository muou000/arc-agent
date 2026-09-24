from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TestRunResult:
    """Structured outcome of one system-side test run.

    This object is the seam between the app-type handlers (producers) and the
    stage callers (consumers): handlers fill the fields while they still know
    the facts structurally, and callers read fields instead of re-parsing the
    rendered text. ``output`` is the model-facing transcription — rendered
    once by the handler, never re-interpreted downstream.
    """

    #: Overall verdict. ``-1`` means the run never produced a code (timeout,
    #: spawn failure); ``0`` is the only passing value.
    exit_code: int = -1
    #: Model-facing transcription of the run (handler-rendered once).
    output: str = ""
    #: Reporter lines partitioned by outcome (``PASS ...`` vs ``FAIL ...``).
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: Environmental verdict from :func:`classify_test_failure` ("" when the
    #: failure is the implementation's own).
    environment_failure: str = ""
    #: Frontend build verdict in the failure-digest phrasing ("" for runs
    #: without a frontend build). Filled structurally by the web handler.
    build_note: str = ""
    #: Served-artifact verdict in the failure-digest phrasing ("" for runs
    #: without a static host). Filled structurally by the web handler.
    served_verdict: str = ""
    #: Stall-governance fingerprint from :func:`failure_fingerprint`.
    fingerprint: str = ""
    #: Workspace-relative path of the persisted raw output; set by the TDD
    #: phase after persisting (the handler cannot know it).
    run_log_path: str = ""
    #: Deterministic compiler/executor stop evidence when repeated failures
    #: close the active layer before its ordinary run_tests budget is spent.
    stall_stop: dict[str, Any] | None = None

    @property
    def passed_run(self) -> bool:
        return self.exit_code == 0

    @property
    def has_error_fingerprint(self) -> bool:
        """Whether the stall fingerprint carries a real error key line.

        ``failure_fingerprint`` pairs the exit code with the first
        error-bearing line; layer/budget gate rejections render as
        ``<exit>|`` with nothing after the pipe. Only error-bearing failures
        may count as "the agent ran tests and they failed on content" —
        consumers keying consecutive-failure chains on the fingerprint (the
        TDD stall hint) must not let rejections in.
        """

        return bool(self.fingerprint.partition("|")[2].strip())


def parse_test_run(
    test_output: str,
    *,
    exit_code: int | None = None,
    build_note: str = "",
    served_verdict: str = "",
) -> TestRunResult:
    """Build the structured :class:`TestRunResult` for one run.

    The single interpretation point for a run's transcription: exit code
    (unless the producer passes the structural value it already knows),
    outcome partitions, environmental verdict and stall fingerprint. Handlers
    that know facts before rendering (frontend build / served-artifact
    verdicts) pass them in instead of letting anything re-parse the text.
    """

    output = test_output or ""
    passed, failed = _partition_lines(output)
    return TestRunResult(
        exit_code=extract_overall_exit_code(output) if exit_code is None else exit_code,
        output=output,
        passed=passed,
        failed=failed,
        environment_failure=classify_test_failure(output),
        build_note=build_note,
        served_verdict=served_verdict,
        fingerprint=failure_fingerprint(output),
    )


_PASS_LINE_PREFIXES = ("PASS ", "✓", "√", "✔")
_FAIL_LINE_PREFIXES = ("FAIL ", "✗", "×", "✕")


def _partition_lines(output: str) -> tuple[list[str], list[str]]:
    """Split reporter lines into passed/failed partitions."""

    passed: list[str] = []
    failed: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(_PASS_LINE_PREFIXES):
            passed.append(stripped)
        elif stripped.startswith(_FAIL_LINE_PREFIXES) or " FAILED" in stripped:
            failed.append(stripped)
    return passed, failed


def extract_overall_exit_code(output: str) -> int:
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


def _parse_exit_code_line(line: str) -> int | None:
    stripped = (line or "").strip()
    if not stripped.startswith("Exit Code:"):
        return None
    try:
        return int(stripped.split("Exit Code:", 1)[1].strip())
    except ValueError:
        return None


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
    # Bare ERR_MODULE_NOT_FOUND hits without a quoted specifier on the rest of
    # the line (e.g. ``Error [ERR_MODULE_NOT_FOUND]: ...`` or ``code:
    # 'ERR_MODULE_NOT_FOUND'``). Lines whose specifier is quoted are matched by
    # the capture patterns above instead - and judged by the relative-specifier
    # guard in classify_test_failure.
    ("missing dependency", re.compile(r"ERR_MODULE_NOT_FOUND(?![^\r\n]*['\"][^'\"]+['\"])")),
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
    (
        "test worker failed to start",
        re.compile(r"Failed to start [a-z]+ worker for test files?", re.IGNORECASE),
    ),
    (
        "cjs require of esm module",
        re.compile(r"require\(\) of ES Module[^\r\n]*"),
    ),
    (
        "native module built for another node version",
        re.compile(r"NODE_MODULE_VERSION|was compiled against a different Node\.js version", re.IGNORECASE),
    ),
)


_DETAIL_LIMIT = 120

#: Cap for the fingerprint's key line; the fingerprint is echoed into tool
#: results and node sessions, so it stays short on purpose.
_FINGERPRINT_LINE_LIMIT = 160

#: Lines that mention error-ish keywords but are not the failure itself:
#: success markers (mirroring the outcome partitions), jest/vitest diff rows, and
#: zero-failure summary rows. Selecting one of these as the key line would
#: either fabricate a fingerprint for a healthy section of the output or give
#: every distinct failure in a run the same generic fingerprint.
_FINGERPRINT_SKIP_LINE_PREFIXES = ("✓", "√", "✔", "PASS ", "Expected", "Received")
_FINGERPRINT_ZERO_FAILURE_PATTERN = re.compile(r"\b0\s+(?:errors?|failed|failures?)\b", re.IGNORECASE)

#: Informational labels and section headers that can carry keyword substrings
#: without being errors. The launcher-PID note in the E2E result preamble
#: contains "expected" ("This is expected when `npm` ...") and appears in EVERY
#: E2E run on Windows, so before this guard every failed E2E run of the
#: 2026-09-19 test1 run keyed on that note instead of the real Playwright error
#: below it, and any three consecutive E2E failures looked like one stalled
#: hypothesis to the stall governor. Section headers ("=== Backend Runtime
#: Error ===") are skipped for the mirror-image reason: a header is generic, so
#: keying on it would give every distinct failure in a run the same
#: fingerprint while the specific error text sits on the line right below it.
_FINGERPRINT_INFORMATIONAL_PATTERN = re.compile(
    r"^(?:===.*===|(?:Note|Info|Warning)\s*:)",
    re.IGNORECASE,
)

#: Ports, line:column references and similar colon-number pairs drift between
#: runs of the same failure (restarted dev server, shifted stack frames).
#: Masking them keeps the fingerprint stable across reruns of one failure while
#: leaving assertion values (``add(1, 1) returned 0``) intact, so distinct
#: failures keep distinct fingerprints.
_FINGERPRINT_NOISE_PATTERN = re.compile(r":\d+(?::\d+)?")

#: Wall-clock durations attached to reporter lines (``980ms``, ``1.2s``) are
#: pure run-to-run noise. They matter on the vitest/jest file-header line
#: (``❯ file.test.js (14 tests | 11 failed) 980ms``), which is often the first
#: error-bearing line of a failed run: without masking, every rerun of the
#: same failure got a distinct fingerprint and the stall governor (three
#: identical consecutive fingerprints) never fired - observed on the
#: 2026-09-19 test1 run, where Integration burned 7 calls on one unchanged
#: failure with zero STALL DETECTED notices.
#: The mask also applies to durations inside assertion text ("expected
#: response within 500ms"): a timeout that drifts 500ms -> 1200ms under load
#: is the same failing assertion, and the stall governor asks "did the
#: failure change?", not "did the timing change?". Stability outweighs
#: duration precision here by design.
_FINGERPRINT_DURATION_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|m)\b")

#: ANSI color codes wrap the duration (and shift between color/no-color runs),
#: so they are stripped before line scanning.
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def classify_test_failure(test_output: str) -> str:
    """Return a short reason when a failed run is environmental, else ``""``.

    Some failures are not the implementation's fault: a dependency is missing,
    the test runner was never installed, or ``node_modules`` is empty. Most of
    these have no in-run repair, so retrying only burns the TDD budget and
    callers use this to stop the loop early. The exception: reasons of the
    exact form ``missing dependency: <pkg>`` (a bare package name surfaced by
    ``Cannot find module`` / ``Cannot find package``) name something the
    TDD-stage ``install_dependencies`` tool can install, so ``core.phases``
    grants those one extra install-and-revalidate cycle before closing the
    layer; every other reason, including relative-specifier ``Cannot find
    module './helper'`` forms, keeps the close-the-layer behavior.

    Unresolved *relative* specifiers (``./helper``, ``../src/module``) are the
    exception: they point at workspace files the agent can create or import
    paths it can correct with an edit, so they are treated as ordinary
    fixable failures. Only bare package names (or absolute paths outside the
    agent's reach) count as environmental.
    """

    output = test_output or ""
    for reason, pattern in _ENVIRONMENT_FAILURE_MARKERS:
        for match in pattern.finditer(output):
            detail = next((group for group in match.groups() if group), "")
            if _is_relative_specifier(detail):
                continue
            detail = " ".join(detail.split())[:_DETAIL_LIMIT].strip()
            return f"{reason}: {detail}" if detail else reason
    return ""


def failure_fingerprint(test_output: str) -> str:
    """Build a short, repeatable fingerprint of *why* a test run failed.

    The TDD loop uses this to detect stalled repairs: when several consecutive
    ``run_tests`` failures carry the same fingerprint, the agent is patching
    neighbors of the failure instead of changing its hypothesis. The
    fingerprint pairs the exit code with the first error-bearing line, masked
    against run-to-run noise (ANSI color codes, ports, ``line:column``
    references, wall-clock durations) and skipping lines that merely *mention*
    failure keywords (success markers, diff rows, zero-failure summaries).
    Truncated to a bounded length because it is echoed into tool results and
    node sessions.
    """

    output = _ANSI_PATTERN.sub("", test_output or "")
    exit_code = extract_overall_exit_code(output)
    key_line = ""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_FINGERPRINT_SKIP_LINE_PREFIXES):
            continue
        if _FINGERPRINT_ZERO_FAILURE_PATTERN.search(stripped):
            continue
        if _FINGERPRINT_INFORMATIONAL_PATTERN.match(stripped):
            continue
        lowered = stripped.lower()
        if "error" in lowered or "failed" in lowered or "expect" in lowered or "assert" in lowered:
            key_line = _FINGERPRINT_NOISE_PATTERN.sub(":#", stripped)
            key_line = _FINGERPRINT_DURATION_PATTERN.sub("<dur>", key_line)
            break
    return f"{exit_code}|{key_line[:_FINGERPRINT_LINE_LIMIT]}"


def _is_relative_specifier(specifier: str) -> bool:
    """True when a module specifier resolves inside the editable workspace."""

    spec = (specifier or "").strip().replace("\\", "/")
    return spec.startswith(("./", "../"))

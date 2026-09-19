"""Structured failure digest for TDD run_tests outputs.

The cross-session TDD handoff used to be a tail of the last 30-40 output
lines. For E2E layers the per-test error detail (Playwright's
``Expected/Received`` blocks, the failing selector) sits scattered in a long
output, so a follow-up session spent its first minutes re-running tests or
probing truncated tool results to re-localize a failure the system had
already seen. This module turns the raw output into a deterministic digest
and persists the raw output itself so the next session starts from the
failure, not from a search for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from contextlib import suppress
from pathlib import Path
from typing import Any

#: Cap on the excerpt lines kept per failed test in the digest text.
_PER_TEST_EXCERPT_LINES = 8
#: Cap on retained raw-output files per node before the oldest are pruned.
_MAX_RETAINED_RUN_LOGS = 20


@dataclass
class FailedTestDigest:
    """One failed test: where it lives and the first error-bearing lines."""

    name: str
    location: str = ""
    error_lines: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [f"- {self.name}"]
        if self.location:
            lines.append(f"  at {self.location}")
        for line in self.error_lines[:_PER_TEST_EXCERPT_LINES]:
            lines.append(f"  {line}")
        return "\n".join(lines)


# Vitest/jest list-report lines ("FAIL path/spec.ts > suite > case") and the
# standalone FAIL header form ("FAIL path/spec.ts"). Reporter output is often
# indented, so the markers may carry leading whitespace.
_FAIL_LIST_LINE = re.compile(r"^\s*FAIL\s+(.+?)\s+>\s+(.+)$")
_FAIL_FILE_LINE = re.compile(r"^\s*FAIL\s+(.+)$")

# Vitest per-file run header: " ❯ tests/routes/authRoutes.test.js (14 tests | 11 failed) 980ms".
# It is the only place vitest names the FILE for the × lines that follow it.
_VITEST_FILE_HEADER = re.compile(r"^\s*❯\s+(\S+.*?)\s+\(\d+\s+tests?\s*\|\s*\d+\s+failed\)")

# Vitest failed-test list row: "     × POST /auth/register rejects ... 47ms".
# Vitest prints one of these per failed test but only ONE detail block (the
# "FAIL file > suite > test" marker below); the × rows are how the digest
# reaches every failed test, not just the detail-block one.
_VITEST_X_LINE = re.compile(r"^\s*[×✘x]\s+(.+?)\s+\d+(?:\.\d+)?\s*m?s\s*$")

# Playwright detail block head (the per-failure section that carries the
# error, locator and expected/received):
#   "  1) test-e2e\spec.js:53:3 › suite › case ─────"
#   "  1) [chromium] › e2e/spec.ts:18:5 › suite › case"
# The trailing ─ run is optional. Paths keep their native separators.
_PLAYWRIGHT_FAIL_LINE = re.compile(
    r"^\s*\d+\)\s+(?:\[(\w+)\]\s*›\s*)?(.+?):(\d+):(\d+)\s*›\s*(.+?)\s*─*\s*$"
)

# Playwright run-summary line: "  ✘  2 path:line:col › suite › case (5.1s)"
_PLAYWRIGHT_SUMMARY_FAIL = re.compile(
    r"^\s*[✘×x]+\s+\d+\s+(.+?):(\d+):\d+\s*›\s*(.+?)\s*\(\d+(\.\d+)?(m?s)\)\s*$"
)

# Playwright per-failure detail lines worth excerpting:
#   "    Error: expect(locator).toBeVisible() failed"
#   "    Locator: getByLabel('用户名')"
#   "    Expected: visible" / "    Received: ..."
#   "    Timeout: 5000ms" / "    Error: element(s) not found"
_PW_DETAIL_KEYS = ("Locator:", "Expected:", "Received:", "Timeout:")

# Vitest/jest assertion details:
#   "Expected: ..." / "Received: ..." or "- Expected" / "+ Received" diff rows,
#   and the error head "AssertionError: ..." / "Error: ..." lines.
_VITEST_EXPECTED = re.compile(r"^\s*(?:- )?Expected(?![A-Za-z])\s*:?\s*(.*)$")
_VITEST_RECEIVED = re.compile(r"^\s*(?:\+ )?Received(?![A-Za-z])\s*:?\s*(.*)$")
_ERROR_HEAD = re.compile(r"^\s*(?:[A-Za-z]*Error|TimeoutError)\s*:\s*(.*)$")


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")


def build_failure_digest(test_output: str) -> dict[str, Any]:
    """Parse a failed run output into per-test error digests.

    Best-effort and format-driven: it targets the Vitest and Playwright
    reporter shapes ARC's web handler emits. When no per-test structure is
    recognized, ``tests`` stays empty and callers fall back to the
    key-line/tail summary — the digest must never fabricate entries.
    """

    output = _strip_ansi(test_output or "")
    lines = output.splitlines()
    failed: list[FailedTestDigest] = []
    marker_lines: dict[int, FailedTestDigest] = {}
    seen: set[str] = set()
    # File context for vitest × rows (reset to "" when no ❯ header has been
    # seen, so stray × glyphs outside a vitest run block are ignored).
    vitest_current_file = ""

    def add(name: str, location: str, line_index: int) -> None:
        key = name if not location else f"{location}::{name}"
        if not name:
            return
        if key in seen:
            # Same test reported again (Playwright prints a ✘ run-summary
            # line first and a numbered detail block later). Re-anchor to the
            # later marker: the detail block that follows it carries the
            # error lines the excerpt wants.
            for existing in failed:
                if (existing.name if not existing.location else f"{existing.location}::{existing.name}") == key:
                    for old_index in [i for i, e in marker_lines.items() if e is existing]:
                        del marker_lines[old_index]
                    marker_lines[line_index] = existing
                    break
            return
        # Vitest names the same test twice: a bare "× case name" list row and
        # a "FAIL file > suite > case" detail marker. Both match on file;
        # replace the earlier bare row with the fuller detail marker (it
        # carries the suite prefix and the error block anchor) instead of
        # listing the failure twice.
        for position, existing in enumerate(failed):
            if not existing.location or not location:
                continue
            same_file = existing.location.split(":")[0] == location.split(":")[0]
            if same_file and (
                existing.name.endswith(f"> {name}")
                or name.endswith(f"> {existing.name}")
                or f"> {existing.name}" in name
            ):
                if existing.name == name:
                    return
                # The incoming marker is the "suite > case" form.
                failed[position] = FailedTestDigest(name=name, location=location)
                for old_index in [i for i, e in marker_lines.items() if e is existing]:
                    del marker_lines[old_index]
                marker_lines[line_index] = failed[position]
                return
        seen.add(key)
        entry = FailedTestDigest(name=name, location=location)
        failed.append(entry)
        marker_lines[line_index] = entry

    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        header_match = _VITEST_FILE_HEADER.match(line)
        if header_match:
            # Track the file context for the × rows that follow; the header
            # itself is not a failed test.
            vitest_current_file = header_match.group(1).strip()
            continue
        if line.lstrip().startswith(("Test Files", "Tests ")):
            # The vitest run block ended: its per-file summary lines close the
            # section the × rows belong to, so drop the file context. A later
            # "× row" outside a run block is unrelated output (custom loggers,
            # CI summaries) and must not be attributed to the last file.
            vitest_current_file = ""
            continue
        match = _FAIL_LIST_LINE.match(line)
        if match:
            add(match.group(2).strip(), match.group(1).strip(), index)
            continue
        match = _PLAYWRIGHT_FAIL_LINE.match(line)
        if match:
            path, line_no, name = match.group(2).strip(), match.group(3), match.group(5).strip()
            add(name, f"{path}:{line_no}", index)
            continue
        match = _PLAYWRIGHT_SUMMARY_FAIL.match(line)
        if match:
            path, line_no, name = match.group(1).strip(), match.group(2), match.group(3).strip()
            add(name, f"{path}:{line_no}", index)
            continue
        match = _VITEST_X_LINE.match(line)
        if match:
            # Only inside a vitest run block (a ❯ header was seen); the same
            # glyph could appear in unrelated output.
            if vitest_current_file:
                add(match.group(1).strip(), vitest_current_file, index)
            continue
        match = _FAIL_FILE_LINE.match(line)
        if match:
            add(match.group(1).strip(), "", index)

    # Attach the error detail that follows each marker: for Playwright detail
    # blocks the useful lines are the Locator/Expected/Received/Error keys;
    # for Vitest the first error head plus the Expected/Received pair.
    for marker_index in sorted(marker_lines):
        entry = marker_lines[marker_index]
        detail: list[str] = []
        expected = received = ""
        for follow in lines[marker_index + 1 : marker_index + 80]:
            text = follow.strip()
            if not text:
                continue
            if (
                _FAIL_LIST_LINE.match(text)
                or _FAIL_FILE_LINE.match(text)
                or _PLAYWRIGHT_FAIL_LINE.match(text)
                or _PLAYWRIGHT_SUMMARY_FAIL.match(text)
            ):
                break
            if text.startswith(("✓", "√", "✔", "PASS ", "✘", "Test Files", "Tests ", "Running ")):
                if text.startswith("✘"):
                    break
                continue
            # Playwright detail keys: "Locator: ...", "Expected: ...",
            # "Received: ...". Expected/Received fold into the synthesized
            # pair line below instead of their own excerpt rows.
            key_hit = next((key for key in _PW_DETAIL_KEYS if text.startswith(key)), "")
            if key_hit:
                if key_hit == "Expected:":
                    expected = text[len("Expected:"):].strip()[:200]
                elif key_hit == "Received:":
                    received = text[len("Received:"):].strip()[:200]
                else:
                    detail.append(text[:240])
                continue
            vit_expect = _VITEST_EXPECTED.match(text)
            if vit_expect:
                expected = vit_expect.group(1).strip()[:200]
                continue
            vit_received = _VITEST_RECEIVED.match(text)
            if vit_received:
                received = vit_received.group(1).strip()[:200]
                continue
            error_head = _ERROR_HEAD.match(text)
            if error_head and error_head.group(1).strip():
                detail.append(text[:240])
                continue
            if detail and text.startswith("at ") and len(detail) >= 2:
                detail.append(text[:240])
                break
        if expected or received:
            detail.insert(0, f"Expected: {expected or '(empty)'} / Received: {received or '(empty)'}")
        entry.error_lines = detail[:_PER_TEST_EXCERPT_LINES]

    return {
        "failed_tests": [
            {"name": entry.name, "location": entry.location, "error_lines": entry.error_lines}
            for entry in failed
        ],
    }


def format_failure_digest(
    digest: dict[str, Any],
    *,
    test_type: str,
    raw_output_path: str | None = None,
    fingerprint: str = "",
    environment_failure: str = "",
) -> str:
    """Render a digest dict into the handoff text block for the next session."""

    failed_tests = digest.get("failed_tests") or []
    blocks: list[str] = [
        "### Structured Failure Digest (system-parsed from the latest failed run)",
        f"- test_type: {test_type}",
    ]
    if fingerprint:
        blocks.append(f"- fingerprint: {fingerprint}")
    if environment_failure:
        blocks.append(f"- environment_failure: {environment_failure}")
    if failed_tests:
        blocks.append(f"- failed tests ({len(failed_tests)}):")
        for item in failed_tests:
            entry = FailedTestDigest(
                name=str(item.get("name", "") or ""),
                location=str(item.get("location", "") or ""),
                error_lines=[str(line) for line in item.get("error_lines") or []],
            )
            blocks.append(entry.to_text())
    else:
        blocks.append(
            "- failed tests: no per-test structure recognized in the output; "
            "treat the key line below as the failure evidence."
        )
    if raw_output_path:
        blocks.append(
            f"- full raw output of this run: `{raw_output_path}` — read this file "
            "for the complete output instead of re-running tests."
        )
    return "\n".join(blocks)


def persist_run_output(
    workspace_root: str | Path,
    node_id: str,
    test_type: str,
    sequence: int,
    output: str,
) -> str:
    """Write a raw run output under ``.arc/tdd_runs`` and return its workspace-relative path.

    The directory lives under the runtime-ignored ``.arc/`` tree, so it never
    reaches Git checkpoints or merges. Only the most recent
    ``_MAX_RETAINED_RUN_LOGS`` files per node are kept.
    """

    safe_node = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "node"
    safe_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(test_type or "").strip()) or "layer"
    directory = Path(workspace_root) / ".arc" / "tdd_runs" / safe_node
    directory.mkdir(parents=True, exist_ok=True)
    file_name = f"{safe_type}-{max(0, int(sequence)):03d}.log"
    path = directory / file_name
    path.write_text((output or "") + "\n", encoding="utf-8", errors="replace")
    _prune_run_logs(directory, keep=_MAX_RETAINED_RUN_LOGS)
    return f".arc/tdd_runs/{safe_node}/{file_name}"


def _prune_run_logs(directory: Path, keep: int) -> None:
    try:
        logs = sorted(
            (item for item in directory.iterdir() if item.is_file() and item.suffix == ".log"),
            key=lambda item: item.name,
        )
    except OSError:
        return
    for stale in logs[:-keep] if len(logs) > keep else []:
        with suppress(OSError):
            stale.unlink()

"""Unit tests for the TDD failure digest (``agents/tools/test_failure_digest.py``).

The digest turns a failed run_tests output into per-test entries (name,
location, first error lines, expected/received) plus a persisted raw-output
pointer. These tests pin the parser against the reporter shapes ARC's web
handler actually emits — Vitest ``FAIL path > suite > case`` list lines and
Playwright numbered detail blocks — including fixtures distilled from real
benchmark runs (ANSI-colored markers, Windows separators, ✘ summary lines
ahead of the detail blocks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.tools.test_failure_digest import (
    build_failure_digest,
    format_failure_digest,
    persist_run_output,
)
from app_type_handler.web import (
    SERVED_VERDICT_FINGERPRINT_CHARS,
    _frontend_serving_note,
    _frontend_serving_verdict,
)


VITEST_FAILURE = """Exit Code: 1

=== Backend Vitest Batch ===
 FAIL tests/authApi.test.js > Auth API > rejects duplicate username with 409
AssertionError: expected [ { …(4) } ] to deeply equal []
Expected: []
Received: [ { id: 1, username: 'a' } ]
 at /workspace/backend/tests/authApi.test.js:88:5
 ✓ tests/authApi.test.js > Auth API > registers a new user
Exit Code: 1
"""

PLAYWRIGHT_FAILURE = """Exit Code: 1

=== Frontend Build ===
Exit Code: 0

Running 3 tests using 1 worker

  ✓  1 test-e2e\\register.e2e.spec.js:13:3 › REQ-1 注册 (E2E) › happy path (944ms)
  ✘  2 test-e2e\\register.e2e.spec.js:53:3 › REQ-1 注册 (E2E) › 拒绝缺失或格式错误的资料 (5.1s)

  1) test-e2e\\register.e2e.spec.js:53:3 › REQ-1 注册 (E2E) › 拒绝缺失或格式错误的资料 ─────

    Error: expect(locator).toBeVisible() failed

    Locator: getByLabel('用户名')
    Expected: visible
    Timeout: 5000ms
    Error: element(s) not found

      58 |     await expect(page.getByLabel('用户名')).toBeVisible();
        at D:\\ws\\test-e2e\\register.e2e.spec.js:58:42

  2) test-e2e\\register.e2e.spec.js:113:3 › REQ-1 注册 (E2E) › 拒绝重复用户名 ─

    TimeoutError: locator.fill: Test timeout of 30000ms exceeded.

Exit Code: 1
"""

ANSI_FAIL_LINE = (
    "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m tests/authApi.test.js"
    "\x1b[2m > \x1b[22mAuth API > registers a new user"
)


def test_vitest_fail_list_line_parsed() -> None:
    digest = build_failure_digest(VITEST_FAILURE)
    names = [item["name"] for item in digest["failed_tests"]]
    assert names == ["Auth API > rejects duplicate username with 409"]
    entry = digest["failed_tests"][0]
    assert entry["location"] == "tests/authApi.test.js"
    joined = "\n".join(entry["error_lines"])
    assert "AssertionError" in joined
    assert "Expected: [] / Received: [ { id: 1, username: 'a' } ]" in joined


def test_vitest_ansi_colored_fail_marker_parsed() -> None:
    output = "Exit Code: 1\n" + ANSI_FAIL_LINE + "\nError: boom\n"
    digest = build_failure_digest(output)
    names = [item["name"] for item in digest["failed_tests"]]
    assert names == ["Auth API > registers a new user"]


def test_playwright_detail_blocks_parsed_with_locator_and_expected() -> None:
    digest = build_failure_digest(PLAYWRIGHT_FAILURE)
    entries = digest["failed_tests"]
    assert len(entries) == 2

    first = entries[0]
    assert first["name"] == "REQ-1 注册 (E2E) › 拒绝缺失或格式错误的资料"
    assert first["location"] == "test-e2e\\register.e2e.spec.js:53"
    joined = "\n".join(first["error_lines"])
    assert "Locator: getByLabel('用户名')" in joined
    assert "Error: element(s) not found" in joined
    assert "Expected: visible" in joined

    second = entries[1]
    assert second["location"] == "test-e2e\\register.e2e.spec.js:113"
    assert any("Test timeout of 30000ms" in line for line in second["error_lines"])


def test_playwright_marker_dedup_anchors_to_detail_block() -> None:
    """The ✘ summary line and the numbered block describe the same test.

    The digest must keep one entry (not two) and anchor the excerpt to the
    detail block — the summary line carries no error detail.
    """

    digest = build_failure_digest(PLAYWRIGHT_FAILURE)
    names = [item["name"] for item in digest["failed_tests"]]
    assert len(names) == len(set(names))
    assert all(item["error_lines"] for item in digest["failed_tests"])


def test_passing_output_yields_no_entries() -> None:
    output = "Exit Code: 0\n ✓ tests/a.test.js > suite > case\n"
    assert build_failure_digest(output)["failed_tests"] == []


def test_unstructured_failure_yields_no_entries() -> None:
    """No recognizable marker means no fabricated entries."""

    output = "Exit Code: 1\nSTDERR:\nsomething exploded\n"
    assert build_failure_digest(output)["failed_tests"] == []


def test_format_includes_pointer_and_fingerprint() -> None:
    digest = build_failure_digest(VITEST_FAILURE)
    text = format_failure_digest(
        digest,
        test_type="Integration",
        raw_output_path=".arc/tdd_runs/REQ-1/Integration-003.log",
        fingerprint="1|AssertionError: expected",
        environment_failure="",
    )
    assert "test_type: Integration" in text
    assert "fingerprint: 1|AssertionError: expected" in text
    assert ".arc/tdd_runs/REQ-1/Integration-003.log" in text
    assert "instead of re-running tests" in text


def test_format_without_failed_tests_explains_fallback() -> None:
    text = format_failure_digest(build_failure_digest("Exit Code: 1\n"), test_type="Unit")
    assert "no per-test structure recognized" in text


# The serving verdict line the web handler emits next to the build verdict
# (`_frontend_serving_verdict`), distilled from the 2026-09-20 arc-output1
# failure shape: builder says Reused, request-time stat says absent.


def test_serving_note_states_present_fact_with_fingerprint(tmp_path) -> None:
    """The structural serving note mirrors the rendered verdict line."""

    frontend = tmp_path / "frontend"
    dist_dir = frontend / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")

    present_line = _frontend_serving_verdict(str(tmp_path))
    note = _frontend_serving_note(str(tmp_path))
    assert note.startswith("frontend/dist/index.html present at result time")
    assert "(fingerprint " in note
    # The truncated hash the handler prints is exactly what the note echoes.
    import re as _re
    printed = _re.search(r"fingerprint ([0-9a-f]+)", present_line)
    assert printed is not None
    assert len(printed.group(1)) == SERVED_VERDICT_FINGERPRINT_CHARS
    assert printed.group(1) in note
    assert str(dist_dir / "index.html") in note


def test_serving_note_states_absent_fact(tmp_path) -> None:
    """Absent dist: the note names the checked path, mirroring the line."""

    (tmp_path / "frontend").mkdir()
    line = _frontend_serving_verdict(str(tmp_path))
    assert line.endswith("(absent)")
    note = _frontend_serving_note(str(tmp_path))
    assert note.startswith("frontend/dist/index.html absent at result time")
    assert "checked" in note


def test_serving_note_handles_unavailable_fingerprint(tmp_path) -> None:
    """An unreadable dist yields the explicit 'unavailable' fingerprint."""

    frontend = tmp_path / "frontend"
    dist_dir = frontend / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")
    note = _frontend_serving_note(str(tmp_path))
    assert "fingerprint" in note


def test_format_includes_served_note_when_present() -> None:
    text = format_failure_digest(
        build_failure_digest("Exit Code: 1\n"),
        test_type="E2E",
        served="frontend/dist/index.html absent at result time (checked D:\\ws\\frontend\\dist\\index.html)",
    )
    assert (
        "- served_artifact: frontend/dist/index.html absent at result time "
        "(checked D:\\ws\\frontend\\dist\\index.html)" in text
    )


def test_format_omits_served_note_when_absent() -> None:
    text = format_failure_digest(build_failure_digest("Exit Code: 1\n"), test_type="Unit")
    assert "served_artifact:" not in text


def test_format_includes_build_note_when_present() -> None:
    text = format_failure_digest(
        build_failure_digest("Exit Code: 1\n"),
        test_type="E2E",
        build="reused existing frontend/dist (fingerprint abc123def456)",
    )
    assert "- build: reused existing frontend/dist (fingerprint abc123def456)" in text


def test_format_omits_build_note_when_absent() -> None:
    text = format_failure_digest(build_failure_digest("Exit Code: 1\n"), test_type="Unit")
    assert "build:" not in text


def test_persist_run_output_writes_and_prunes(tmp_path: Path) -> None:
    for i in range(1, 26):
        persist_run_output(tmp_path, "REQ-1", "E2E", i, f"attempt {i}")
    logs = sorted((tmp_path / ".arc" / "tdd_runs" / "REQ-1").glob("*.log"))
    assert len(logs) == 20
    assert logs[0].name == "E2E-006.log"
    assert logs[-1].name == "E2E-025.log"
    assert "attempt 25" in logs[-1].read_text(encoding="utf-8")


def test_persist_run_output_sanitizes_ids(tmp_path: Path) -> None:
    relative = persist_run_output(tmp_path, "REQ 1/a", "E2E test", 1, "boom")
    assert relative == ".arc/tdd_runs/REQ_1_a/E2E_test-001.log"
    assert (tmp_path / relative).exists()


def test_tdd_runs_readonly_permission_carveout(tmp_path: Path) -> None:
    """``.arc/tdd_runs`` must be readable but not writable for agents.

    The whole ``.arc`` tree is denied read+write; the digest contract needs
    the agent to read the persisted run outputs, so a read-only allow rule is
    carved out ahead of the deny. Writes into it (or reads anywhere else under
    ``.arc``) must still be denied.
    """

    from agents.runtime.factory import _build_filesystem_permissions
    from deepagents.middleware.filesystem import _check_fs_permission

    permissions = _build_filesystem_permissions(
        tmp_path,
        [str(tmp_path)],
        skill_instruction_paths=[],
    )
    log_path = "/workspace/.arc/tdd_runs/REQ-1/E2E-001.log"
    assert _check_fs_permission(permissions, "read", log_path) == "allow"
    assert _check_fs_permission(permissions, "write", log_path) == "deny"
    # The rest of .arc stays denied in both directions.
    assert _check_fs_permission(permissions, "read", "/workspace/.arc/processing_queue.json") == "deny"
    assert _check_fs_permission(permissions, "write", "/workspace/.arc/queue.json") == "deny"
    # Regular workspace files stay writable.
    assert _check_fs_permission(permissions, "write", "/workspace/src/app.js") == "allow"


# ---------------------------------------------------------------------------
# Vitest × list rows: every failed test, not only the detail-block one
# ---------------------------------------------------------------------------


def test_digest_parses_vitest_x_rows_with_file_header() -> None:
    """Every failed test of a vitest run must appear, with the file location.

    Vitest prints one ``× case name Nms`` row per failed test but only ONE
    ``FAIL file > suite > case`` detail block. Before ×-row parsing the digest
    listed a single failure out of eleven (observed on the 2026-09-19 test1
    run's Integration log), and the follow-up session repaired one symptom
    while ten siblings went unseen.
    """

    output = (
        "Exit Code: 1\n"
        "STDERR:\n"
        " ❯ tests/routes/authRoutes.test.js (14 tests | 11 failed) 980ms\n"
        "     × POST /auth/register with valid payload returns 200 71ms\n"
        "     × POST /auth/register rejects duplicate usernames with 409 47ms\n"
        "     × POST /auth/register rejects duplicate emails with 409 48ms\n"
        "     ✓ POST /auth/options lists options 12ms\n"
        " Test Files  1 failed (1)\n"
        " FAIL  tests/routes/authRoutes.test.js > AuthRoutes > POST /auth/register with valid payload returns 200\n"
        " AssertionError: expected 500 to be 200\n"
    )
    digest = build_failure_digest(output)
    names = [item["name"] for item in digest["failed_tests"]]
    assert len(names) == 3
    assert "POST /auth/register rejects duplicate usernames with 409" in names
    locations = {item["location"] for item in digest["failed_tests"]}
    assert locations == {"tests/routes/authRoutes.test.js"}


def test_digest_dedupes_vitest_x_row_against_fail_detail() -> None:
    """A test named by both an × row and a FAIL detail block counts once.

    The FAIL ``suite > case`` marker carries the error lines, so it wins over
    the bare × row of the same test.
    """

    output = (
        "Exit Code: 1\n"
        " ❯ tests/features/SessionContext.test.tsx (5 tests | 1 failed) 230ms\n"
        "     × refresh() re-fetches the current user 230ms\n"
        " FAIL  tests/features/SessionContext.test.tsx > SessionProvider > refresh() re-fetches the current user\n"
        " AssertionError: expected \"vi.fn()\" to be called 2 times, but got 4 times\n"
    )
    digest = build_failure_digest(output)
    assert len(digest["failed_tests"]) == 1
    entry = digest["failed_tests"][0]
    assert entry["name"].startswith("SessionProvider > refresh()")
    assert any("vi.fn()" in line for line in entry["error_lines"])


def test_digest_ignores_x_glyphs_outside_vitest_block() -> None:
    """A bare × line without a preceding ❯ run header is not a failed test."""

    output = (
        "Exit Code: 1\n"
        "AssertionError: expected 500 to be 200\n"
        " × not a vitest row 12ms\n"
    )
    digest = build_failure_digest(output)
    names = [item["name"] for item in digest["failed_tests"]]
    assert "not a vitest row" not in names


def test_digest_two_suites_same_case_name_count_both_failures() -> None:
    """Same case name under two describes yields two distinct failures.

    The x-row dedup consumes the bare row via the first FAIL detail marker;
    the second suite's FAIL marker must survive as its own entry instead of
    being merged into the first (the dedup matches on the case tail, and
    'A > saves' vs 'B > saves' differ there).
    """

    output = (
        "Exit Code: 1\n"
        " ❯ tests/a.test.js (4 tests | 2 failed) 100ms\n"
        "     × saves 30ms\n"
        "     × saves 30ms\n"
        " FAIL  tests/a.test.js > A > saves\n"
        " AssertionError: boom A\n"
        " FAIL  tests/a.test.js > B > saves\n"
        " AssertionError: boom B\n"
    )
    digest = build_failure_digest(output)
    names = [item["name"] for item in digest["failed_tests"]]
    assert names == ["A > saves", "B > saves"]
    errors = [item["error_lines"][0] for item in digest["failed_tests"] if item["error_lines"]]
    assert errors == ["AssertionError: boom A", "AssertionError: boom B"]


def test_digest_resets_vitest_context_at_summary_lines() -> None:
    """x-like rows after the vitest run summary must not be attributed.

    ``Test Files`` / ``Tests`` summary lines close the per-file run block;
    keeping the file context past them let any later "× row 12ms" from
    unrelated output (custom loggers, CI summaries) leak into the digest
    attributed to the last seen file.
    """

    output = (
        "Exit Code: 1\n"
        " ❯ tests/a.test.js (2 tests | 1 failed) 100ms\n"
        "     × real failure 30ms\n"
        " Test Files  1 failed (1)\n"
        "      Tests  1 failed | 1 passed (2)\n"
        " × unrelated summary row 12ms\n"
    )
    digest = build_failure_digest(output)
    names = [item["name"] for item in digest["failed_tests"]]
    assert names == ["real failure"]


def test_persist_run_output_continues_sequence_across_passes(tmp_path: Path) -> None:
    """A second TDD pass must not overwrite the first pass's run logs.

    ``sequence`` is the per-layer counter of the current pass, which restarts
    at 1 on every pass (post-run auto retry, ``--retry-failed``). Trusting it
    verbatim made pass N+1 overwrite pass N's logs (observed on the 2026-09-19
    test1 run: the first round's Integration-003.log was replaced by the
    second round's, so a handoff written by round 1 pointed at round 2's
    content). The on-disk counter must continue past the highest existing log
    of the same layer.
    """

    # Pass 1: three Unit runs.
    for i in (1, 2, 3):
        persist_run_output(tmp_path, "REQ-1", "Unit", i, f"pass1 attempt {i}")
    # Pass 2: the layer counter restarts at 1.
    rel = persist_run_output(tmp_path, "REQ-1", "Unit", 1, "pass2 attempt 1")
    assert rel == ".arc/tdd_runs/REQ-1/Unit-004.log"
    assert (tmp_path / ".arc" / "tdd_runs" / "REQ-1" / "Unit-003.log").read_text(
        encoding="utf-8"
    ).startswith("pass1 attempt 3")
    assert (tmp_path / rel).read_text(encoding="utf-8").startswith("pass2 attempt 1")
    # Layers are independent counters: a first Integration run stays at 001.
    rel_integration = persist_run_output(tmp_path, "REQ-1", "Integration", 1, "integration 1")
    assert rel_integration == ".arc/tdd_runs/REQ-1/Integration-001.log"


def test_persist_run_output_prunes_oldest_across_passes(tmp_path: Path) -> None:
    """Cross-pass continuation still respects the retention cap."""

    for i in range(1, 25):
        persist_run_output(tmp_path, "REQ-1", "Unit", 1, f"attempt {i}")
    kept = sorted(
        item.name
        for item in (tmp_path / ".arc" / "tdd_runs" / "REQ-1").iterdir()
        if item.suffix == ".log"
    )
    assert len(kept) == 20
    assert kept[0] == "Unit-005.log"
    assert kept[-1] == "Unit-024.log"


def test_persist_run_output_scan_failure_propagates(tmp_path: Path, monkeypatch) -> None:
    """A directory-scan failure must not silently fall back to the old numbering.

    Falling back to ``requested`` on OSError would reintroduce the exact
    overwrite this continuation prevents (a second pass clobbering the
    first pass's evidence) with no log line anywhere - the internal
    swallow made the outer OSError handler in ``core.phases`` unreachable.
    The scan failure now propagates so that handler logs and skips the
    pointer instead.
    """

    calls = {"n": 0}

    def broken_iterdir(self):
        calls["n"] += 1
        raise OSError("scan denied")

    monkeypatch.setattr(Path, "iterdir", broken_iterdir)
    with pytest.raises(OSError):
        persist_run_output(tmp_path, "REQ-1", "Unit", 1, "boom")
    assert calls["n"] >= 1


def test_digest_bare_row_never_swallows_suite_named_entry() -> None:
    """Only the fuller "suite > case" form may replace; never the bare row.

    A later bare x-row carrying less identity (just the case name) must not
    replace an existing "suite > case" entry: the old symmetric suffix
    match let "× saves" swallow an earlier "B > saves" row, silently
    dropping the suite identity. The merge direction is now explicit.
    """

    output = (
        "Exit Code: 1\n"
        " ❯ tests/a.test.js (4 tests | 2 failed) 100ms\n"
        "     × B > saves 30ms\n"
        "     × saves 30ms\n"
        " FAIL  tests/a.test.js > A > saves\n"
        " AssertionError: boom A\n"
    )
    digest = build_failure_digest(output)
    names = [item["name"] for item in digest["failed_tests"]]
    assert names == ["B > saves", "A > saves"]


def test_digest_summary_reset_matches_decorated_lines() -> None:
    """Box-drawing decorations around 'Test Files' still close the run block."""

    output = (
        "Exit Code: 1\n"
        " ❯ tests/a.test.js (2 tests | 1 failed) 100ms\n"
        "     × real failure 30ms\n"
        "  ⎯⎯⎯ Test Files  1 failed (1) ⎯⎯⎯\n"
        " × leaked row 12ms\n"
    )
    digest = build_failure_digest(output)
    names = [item["name"] for item in digest["failed_tests"]]
    assert names == ["real failure"]

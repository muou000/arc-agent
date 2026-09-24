"""Verify the post-run auto TDD re-prompt helpers in ``core/tdd_retry.py``.

After the multi-node compilation pass finishes, ``ARCWorkflowManager`` scans
``.arc/runner-events.jsonl`` for any ``test/failed`` requirement_state rows and
re-prompts each unique still-failing node once with a TDD-first follow-up that is
injected into the node session (and therefore reaches ``TestDrivenDeveloper`` as
``previous_failure_summary``).

These tests cover the two pure helpers that drive that flow:
``scan_test_failures`` (reads JSONL, dedupes, preserves order) and
``build_tdd_reprompt`` (formats the follow-up prompt). They import the helpers
directly from the dependency-free ``core.tdd_retry`` module so the heavy
agent/runtime stack is never loaded.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.tdd_retry import build_tdd_reprompt, collect_attempt_facts, scan_test_failures


def _append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _timestamped(**fields) -> dict:
    payload = dict(fields)
    payload.setdefault("timestamp", "2026-09-10 00:00:00")
    return payload


# ---------------------------------------------------------------------------
# scan_test_failures
# ---------------------------------------------------------------------------


def test_scan_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """A fresh workspace with no events file must not raise and must return []."""
    assert scan_test_failures(tmp_path / ".arc" / "runner-events.jsonl") == []


def test_scan_ignores_blank_lines_and_non_json(tmp_path: Path) -> None:
    path = tmp_path / "runner-events.jsonl"
    path.write_text(
        "\n"
        "not-json\n"
        "\n"
        + json.dumps(_timestamped(type="requirement_state", node_id="REQ-A", phase="test", status="failed", message="boom"))
        + "\n",
        encoding="utf-8",
    )
    assert scan_test_failures(path) == [("REQ-A", "boom")]


def test_scan_collects_only_test_failed_requirement_states(tmp_path: Path) -> None:
    """Only ``requirement_state`` rows with ``phase=test`` and ``status=failed`` count."""
    path = tmp_path / "runner-events.jsonl"
    rows = [
        _timestamped(type="runner_state", state="running"),
        _timestamped(type="requirement_state", node_id="REQ-A", phase="design", status="completed"),
        _timestamped(type="requirement_state", node_id="REQ-A", phase="implement", status="failed"),
        _timestamped(type="requirement_state", node_id="REQ-A", phase="test", status="passed"),
        _timestamped(type="requirement_state", node_id="REQ-B", phase="test", status="failed", message="boom"),
        _timestamped(type="requirement_state", node_id="REQ-C", phase="test", status="failed"),
        _timestamped(type="signal", reason="refresh"),
    ]
    for row in rows:
        _append_record(path, row)
    failures = scan_test_failures(path)
    assert failures == [("REQ-B", "boom"), ("REQ-C", None)]


def test_scan_dedupes_repeated_failures_for_same_node_first_message_wins(tmp_path: Path) -> None:
    """The same node emitting test/failed multiple times must collapse to one retry;
    the first message wins so the re-prompt carries the original failure detail."""
    path = tmp_path / "runner-events.jsonl"
    _append_record(path, _timestamped(type="requirement_state", node_id="REQ-X", phase="test", status="failed", message="first"))
    _append_record(path, _timestamped(type="requirement_state", node_id="REQ-X", phase="test", status="failed", message="second"))
    _append_record(path, _timestamped(type="requirement_state", node_id="REQ-X", phase="test", status="failed", message="third"))
    failures = scan_test_failures(path)
    assert failures == [("REQ-X", "first")]


def test_scan_preserves_first_seen_order(tmp_path: Path) -> None:
    """Retries run in the order the failures first appeared in the JSONL."""
    path = tmp_path / "runner-events.jsonl"
    for node in ["REQ-C", "REQ-A", "REQ-B"]:
        _append_record(path, _timestamped(type="requirement_state", node_id=node, phase="test", status="failed", message=node))
    assert scan_test_failures(path) == [("REQ-C", "REQ-C"), ("REQ-A", "REQ-A"), ("REQ-B", "REQ-B")]


def test_scan_skips_empty_or_whitespace_node_id(tmp_path: Path) -> None:
    """Records with an empty / whitespace node_id must not produce retry entries."""
    path = tmp_path / "runner-events.jsonl"
    _append_record(path, _timestamped(type="requirement_state", node_id="   ", phase="test", status="failed", message="blank"))
    _append_record(path, _timestamped(type="requirement_state", node_id="", phase="test", status="failed", message="empty"))
    _append_record(path, _timestamped(type="requirement_state", node_id="REQ-OK", phase="test", status="failed", message="real"))
    assert scan_test_failures(path) == [("REQ-OK", "real")]


def test_scan_treats_non_dict_records_as_garbage(tmp_path: Path) -> None:
    """Top-level scalars / lists must not raise; they are silently skipped."""
    path = tmp_path / "runner-events.jsonl"
    path.write_text(
        "null\n42\n\"string\"\n[]\n"
        + json.dumps(_timestamped(type="requirement_state", node_id="REQ-1", phase="test", status="failed"))
        + "\n",
        encoding="utf-8",
    )
    assert scan_test_failures(path) == [("REQ-1", None)]


def test_scan_zero_byte_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "runner-events.jsonl"
    path.write_text("", encoding="utf-8")
    assert scan_test_failures(path) == []


# ---------------------------------------------------------------------------
# build_tdd_reprompt
# ---------------------------------------------------------------------------


def test_build_reprompt_includes_node_id_and_message() -> None:
    prompt = build_tdd_reprompt("REQ-42", "login form rejected valid credentials")
    assert "REQ-42" in prompt
    assert "login form rejected valid credentials" in prompt
    # Must spell out the TDD sequence so the agent doesn't skip step 1.
    assert "failing test" in prompt.lower()
    # ARC reports progress through run_tests and declares success with IMPLEMENTED.
    assert "run_tests" in prompt
    assert "IMPLEMENTED" in prompt
    assert "REQ-42" in prompt


def test_build_reprompt_uses_placeholder_when_message_is_none() -> None:
    prompt = build_tdd_reprompt("REQ-42", None)
    assert "REQ-42" in prompt
    assert "(no detail provided)" in prompt


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_build_reprompt_uses_placeholder_when_message_is_blank(blank: str) -> None:
    prompt = build_tdd_reprompt("REQ-42", blank)
    assert "(no detail provided)" in prompt


def test_build_reprompt_drops_non_string_message_to_placeholder() -> None:
    """Defensive: if a non-str message sneaks in (e.g. int from JSON), don't crash."""
    prompt = build_tdd_reprompt("REQ-42", 12345)  # type: ignore[arg-type]
    assert "(no detail provided)" in prompt


def test_build_reprompt_forbids_weakening_tests() -> None:
    """The policy must explicitly forbid silencing tests to force a pass."""
    prompt = build_tdd_reprompt("REQ-7", "cart total is wrong")
    lowered = prompt.lower()
    assert "do not weaken the test" in lowered
    assert "do not skip step 1" in lowered


# ---------------------------------------------------------------------------
# build_tdd_reprompt: previous-round handoff evidence
# ---------------------------------------------------------------------------


def test_build_reprompt_appends_handoff_evidence() -> None:
    """Fingerprint history and layer usage from the failed round reach the retry.

    A retried session starts with fresh budgets; without the "already tried"
    evidence it repeats hypotheses the previous round burned its budget on
    (observed on the 2026-09-19 test1 run: the retry re-hit the same
    StrictMode failure for another full 10-call budget).
    """

    handoff = {
        "layer_usage": {"Unit": 10, "Integration": 4},
        "fingerprint_history": {"Unit": ["1|x", "1|x", "1|x"]},
    }
    reprompt = build_tdd_reprompt("REQ-1", "Unit: assertion failed", handoff=handoff)
    assert "Unit: 10/10 run_tests calls" in reprompt
    assert "Integration: 4/10 run_tests calls" in reprompt
    assert "Budgets are fresh this round" in reprompt
    assert "Failure fingerprints from the previous round (already tried)" in reprompt
    assert "unchanged across runs - the hypothesis was wrong, do not retry it" in reprompt
    assert "Do not repeat an approach" in reprompt


def test_build_reprompt_handoff_without_fingerprints_omits_block() -> None:
    """A handoff with only usage still shows budgets but no fingerprint block."""

    reprompt = build_tdd_reprompt("REQ-1", "boom", handoff={"layer_usage": {"Unit": 3}})
    assert "Unit: 3/10" in reprompt
    assert "Failure fingerprints" not in reprompt


def test_build_reprompt_ignores_malformed_handoff() -> None:
    """Malformed handoff payloads degrade to the plain reprompt, never raise."""

    for bad in (None, "string", 42, {"layer_usage": "nope"}, {"fingerprint_history": [1, 2]}):
        reprompt = build_tdd_reprompt("REQ-1", "boom", handoff=bad)
        assert "TDD follow-up for REQ-1" in reprompt
        assert "boom" in reprompt


def test_build_reprompt_dedupes_across_layers() -> None:
    """Distinct fingerprints across layers are listed per layer, last one shown."""

    handoff = {
        "fingerprint_history": {
            "Unit": ["1|a", "1|b"],
            "E2E": ["1|c"],
        }
    }
    reprompt = build_tdd_reprompt("REQ-1", "boom", handoff=handoff)
    assert "Unit: 1|b" in reprompt
    assert "E2E: 1|c" in reprompt
    assert "unchanged across runs" not in reprompt


# ---------------------------------------------------------------------------
# collect_attempt_facts: objective numbers from the raw per-call event streams
# ---------------------------------------------------------------------------


def _tool_usage(node_id: str, tool: str, status: str = "ok", phase: str = "IMPLEMENT") -> dict:
    return _timestamped(type="tool_usage", node_id=node_id, phase=phase, tool=tool, status=status)


def _llm_usage(node_id: str, phase: str = "IMPLEMENT") -> dict:
    return _timestamped(type="llm_usage", node_id=node_id, phase=phase)


def test_collect_counts_model_calls_reads_tests_and_writes(tmp_path: Path) -> None:
    """The facts quote the raw per-call streams: model calls (≈ agent steps),
    read-only reads/searches, run_tests calls, and successful file writes.

    Only successful writes count — a write the discipline blocked or that
    errored did not change the workspace, and the zero-writes fact must stay
    truthful (issue #219: the retry must know the previous round produced
    nothing).
    """
    path = tmp_path / "runner-events.jsonl"
    for row in [
        _llm_usage("REQ-1"),
        _llm_usage("REQ-1"),
        _llm_usage("REQ-1"),
        _tool_usage("REQ-1", "read_file"),
        _tool_usage("REQ-1", "grep"),
        _tool_usage("REQ-1", "glob"),
        _tool_usage("REQ-1", "run_tests"),
        _tool_usage("REQ-1", "write_file"),
        _tool_usage("REQ-1", "edit_file", status="error"),
    ]:
        _append_record(path, row)
    facts = collect_attempt_facts(path, "REQ-1")
    assert facts["model_calls"] == 3
    assert facts["tool_calls"] == 6
    assert facts["read_only_calls"] == 3
    assert facts["run_tests_calls"] == 1
    assert facts["successful_writes"] == 1
    assert facts["end_line"] == 9


def test_collect_ignores_other_nodes_and_other_phases(tmp_path: Path) -> None:
    """Only the node's IMPLEMENT-phase calls are the failed attempt's work."""
    path = tmp_path / "runner-events.jsonl"
    for row in [
        _llm_usage("REQ-OTHER"),
        _llm_usage("REQ-1", phase="DESIGN"),
        _tool_usage("REQ-1", "write_file", phase="DESIGN"),
        _tool_usage("REQ-OTHER", "grep"),
        _llm_usage("REQ-1"),
    ]:
        _append_record(path, row)
    facts = collect_attempt_facts(path, "REQ-1")
    assert facts["model_calls"] == 1
    assert facts["tool_calls"] == 0
    assert facts["successful_writes"] == 0


def test_collect_counts_delete_as_write_but_not_blocked_attempts(tmp_path: Path) -> None:
    path = tmp_path / "runner-events.jsonl"
    for row in [
        _tool_usage("REQ-1", "delete"),
        _tool_usage("REQ-1", "write_file", status="blocked"),
        _tool_usage("REQ-1", "append_file"),
    ]:
        _append_record(path, row)
    facts = collect_attempt_facts(path, "REQ-1")
    assert facts["successful_writes"] == 2
    assert facts["read_only_calls"] == 0


def test_collect_honors_start_line_cursor(tmp_path: Path) -> None:
    """A recorded cursor fences finished attempts: with ``start_line`` at the
    previous retry's position only the latest attempt's events count, so a
    repeated auto retry does not double-count earlier ones."""
    path = tmp_path / "runner-events.jsonl"
    for row in [_llm_usage("REQ-1"), _llm_usage("REQ-1"), _tool_usage("REQ-1", "grep")]:
        _append_record(path, row)
    first = collect_attempt_facts(path, "REQ-1")
    assert first["model_calls"] == 2
    assert first["end_line"] == 3

    # The retry attempt appends its own events; the next preparation reads
    # only the new segment.
    _append_record(path, _llm_usage("REQ-1"))
    _append_record(path, _tool_usage("REQ-1", "read_file"))
    second = collect_attempt_facts(path, "REQ-1", start_line=first["end_line"])
    assert second["model_calls"] == 1
    assert second["read_only_calls"] == 1
    assert second["end_line"] == 5


def test_collect_returns_zeros_when_cursor_sits_at_file_end(tmp_path: Path) -> None:
    """A cursor exactly at EOF means nothing new happened; it must scan nothing
    (not restart from zero and double-count the fenced attempt)."""
    path = tmp_path / "runner-events.jsonl"
    _append_record(path, _llm_usage("REQ-1"))
    facts = collect_attempt_facts(path, "REQ-1", start_line=1)
    assert facts["model_calls"] == 0
    assert facts["end_line"] == 1


def test_collect_clamps_stale_cursor_beyond_file(tmp_path: Path) -> None:
    """A cursor past EOF (events file recreated smaller) must not hide the
    attempt behind an impossible slice."""
    path = tmp_path / "runner-events.jsonl"
    _append_record(path, _llm_usage("REQ-1"))
    facts = collect_attempt_facts(path, "REQ-1", start_line=99)
    assert facts["model_calls"] == 1


def test_collect_tolerates_missing_file_and_garbage_lines(tmp_path: Path) -> None:
    missing = collect_attempt_facts(tmp_path / "nope.jsonl", "REQ-1")
    assert missing["model_calls"] == 0
    assert missing["end_line"] == 0

    path = tmp_path / "runner-events.jsonl"
    path.write_text("not-json\nnull\n[]\n", encoding="utf-8")
    facts = collect_attempt_facts(path, "REQ-1")
    assert facts["model_calls"] == 0
    assert facts["end_line"] == 3


# ---------------------------------------------------------------------------
# build_tdd_reprompt: previous-attempt objective facts
# ---------------------------------------------------------------------------


def _attempt_facts(**overrides: int) -> dict:
    facts = {
        "model_calls": 96,
        "tool_calls": 214,
        "read_only_calls": 88,
        "run_tests_calls": 7,
        "successful_writes": 0,
        "end_line": 500,
    }
    facts.update(overrides)
    return facts


def test_build_reprompt_quotes_previous_attempt_facts() -> None:
    """Issue #219: the retry must know what the previous attempt objectively
    spent — model calls, read-only calls, run_tests calls — and that it wrote
    nothing, so the inherited thread's bulk cannot pass for progress."""
    reprompt = build_tdd_reprompt("REQ-1", "Unit: assertion failed", attempt_facts=_attempt_facts())
    assert "Previous attempt record" in reprompt
    assert "96 model calls" in reprompt
    assert "214 tool calls" in reprompt
    assert "88 read-only" in reprompt
    assert "7 run_tests" in reprompt
    lowered = reprompt.lower()
    assert "no successful file edits" in lowered
    assert "change the approach" in lowered


def test_build_reprompt_with_writes_skips_zero_write_callout() -> None:
    reprompt = build_tdd_reprompt("REQ-1", "boom", attempt_facts=_attempt_facts(successful_writes=5))
    assert "5 successful file writes" in reprompt
    assert "NO successful file edits" not in reprompt


def test_build_reprompt_omits_attempt_facts_block_without_signal() -> None:
    """All-zero facts mean the node never reached IMPLEMENT; an empty record
    would only be noise."""
    reprompt = build_tdd_reprompt("REQ-1", "boom", attempt_facts=_attempt_facts(model_calls=0, tool_calls=0))
    assert "Previous attempt record" not in reprompt


def test_build_reprompt_tolerates_malformed_attempt_facts() -> None:
    for bad in (None, "x", 42, {"model_calls": "nope"}):
        reprompt = build_tdd_reprompt("REQ-1", "boom", attempt_facts=bad)  # type: ignore[arg-type]
        assert "TDD follow-up for REQ-1" in reprompt
        assert "boom" in reprompt


def test_build_reprompt_orders_attempt_facts_before_handoff_evidence() -> None:
    """Scale first (what was spent), then what was tried (fingerprints)."""
    reprompt = build_tdd_reprompt(
        "REQ-1",
        "boom",
        handoff={"layer_usage": {"Unit": 10}},
        attempt_facts=_attempt_facts(),
    )
    assert reprompt.index("Previous attempt record") < reprompt.index("Unit: 10/10 run_tests calls")


# ---------------------------------------------------------------------------
# Digest stall-hint compatibility: the injected hint rides in the message
# field and must not disturb the scan
# ---------------------------------------------------------------------------


def test_scan_message_carrying_stall_hint_still_dedupes_in_order(tmp_path: Path) -> None:
    """A failure message embedding the TEST-EDIT STALL digest hint stays scannable.

    The stall hint is appended to the failure digest text, which can reach
    ``runner-events.jsonl`` inside a ``test/failed`` message. The scanner keys
    on the structured fields only (type/phase/status/node_id) and dedupes per
    node with first message winning — a hint-bearing message must neither
    break parsing nor change the dedup order.
    """

    path = tmp_path / ".arc" / "runner-events.jsonl"
    hint_message = (
        "### Structured Failure Digest (system-parsed from the latest failed run)\n"
        "- test_type: Integration\n"
        "- fingerprint: 1|AssertionError: expected 'Login' to equal 'Log in'\n"
        "- TEST-EDIT STALL: the last 8 edits all landed in test files "
        "(`tests/integration/loginPage.test.tsx`) and the failure fingerprint is "
        "unchanged; the failure most likely lives in the implementation or "
        "environment layer. Fix the implementation or repair the environment "
        "instead of adjusting the tests again.\n"
    )
    _append_record(path, _timestamped(type="requirement_state", node_id="REQ-A", phase="test", status="failed", message=hint_message))
    _append_record(path, _timestamped(type="requirement_state", node_id="REQ-A", phase="test", status="failed", message="second failure"))
    _append_record(path, _timestamped(type="requirement_state", node_id="REQ-B", phase="test", status="failed", message="other node"))

    assert scan_test_failures(path) == [
        ("REQ-A", hint_message),
        ("REQ-B", "other node"),
    ]

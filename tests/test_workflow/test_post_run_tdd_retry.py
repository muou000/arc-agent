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

from core.tdd_retry import build_tdd_reprompt, scan_test_failures


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

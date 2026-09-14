"""Tests for ``arcbench_agent_runtime.usage.aggregate_llm_usage``.

The aggregator folds ``llm_usage`` runner events into the per-node, per-phase,
per-model and run-level totals that ARC-Bench token-efficiency work reads.
These tests pin the bucket schema and the robustness rules (skip other event
types, malformed lines, unpriced calls).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcbench_agent_runtime.usage import aggregate_llm_usage, aggregate_tool_usage, empty_tool_bucket, empty_usage_bucket


_UNSET = object()


def _usage_event(
    *,
    node_id: str = "REQ-1",
    phase: str = "DESIGN",
    model: str = "gpt-4o",
    source: str = "reported",
    input: int = 90,
    output: int = 30,
    total: int = 150,
    cost: object = _UNSET,
) -> dict:
    return {
        "type": "llm_usage",
        "node_id": node_id,
        "phase": phase,
        "model": model,
        "api_mode": "chat_completions",
        "source": source,
        "usage": {
            "input": input,
            "output": output,
            "cache_read": 20,
            "cache_write": 10,
            "cache_write_1h": None,
            "reasoning": 5,
            "total": total,
        },
        "cost": cost
        if cost is not _UNSET
        else {
            "input": 0.000225,
            "output": 0.0003,
            "cache_read": 0.000025,
            "cache_write": 0.0,
            "total": 0.00055,
        },
        "timestamp": "2026-09-13 00:00:00",
    }


def _write_events(path: Path, payloads: list) -> Path:
    lines = []
    for payload in payloads:
        lines.append(payload if isinstance(payload, str) else json.dumps(payload))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestAggregateLLMUsage:
    def test_missing_file_yields_empty_totals(self, tmp_path: Path) -> None:
        summary = aggregate_llm_usage(tmp_path / "runner-events.jsonl")
        assert summary["totals"] == empty_usage_bucket()
        assert summary["by_node"] == {}
        assert summary["by_phase"] == {}
        assert summary["by_model"] == {}

    def test_totals_and_sections_accumulate(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [
                _usage_event(node_id="REQ-1", phase="DESIGN", model="gpt-4o"),
                _usage_event(
                    node_id="REQ-1",
                    phase="IMPLEMENT",
                    model="gpt-4o",
                    input=10,
                    output=5,
                    total=45,
                    cost={"input": 0.1, "output": 0.2, "cache_read": 0.0, "cache_write": 0.0, "total": 0.3},
                ),
                _usage_event(
                    node_id="REQ-2",
                    phase="IMPLEMENT",
                    model="deepseek-chat",
                    input=100,
                    output=50,
                    total=180,
                ),
            ],
        )

        summary = aggregate_llm_usage(events_path)

        totals = summary["totals"]
        assert totals["calls"] == 3
        assert totals["input"] == 200
        assert totals["output"] == 85
        assert totals["cache_read"] == 60
        assert totals["cache_write"] == 30
        assert totals["reasoning"] == 15
        assert totals["total"] == 375
        assert totals["estimated_calls"] == 0
        assert totals["unpriced_calls"] == 0
        assert totals["cost"]["total"] == pytest.approx(0.00055 * 2 + 0.3)
        assert summary["by_model"]["deepseek-chat"]["cost"]["total"] == pytest.approx(0.00055)

        assert set(summary["by_node"]) == {"REQ-1", "REQ-2"}
        assert summary["by_node"]["REQ-1"]["calls"] == 2
        assert summary["by_node"]["REQ-2"]["total"] == 180

        assert set(summary["by_phase"]) == {"DESIGN", "IMPLEMENT"}
        assert summary["by_phase"]["DESIGN"]["calls"] == 1

        assert set(summary["by_model"]) == {"gpt-4o", "deepseek-chat"}
        assert summary["by_model"]["gpt-4o"]["calls"] == 2

    def test_empty_node_id_buckets_as_run_level(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [_usage_event(node_id="", phase="", model="gpt-4o")],
        )
        summary = aggregate_llm_usage(events_path)
        assert set(summary["by_node"]) == {""}
        assert set(summary["by_phase"]) == {""}

    def test_ignores_other_event_types_and_malformed_lines(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [
                _usage_event(),
                {"type": "requirement_state", "node_id": "REQ-1", "phase": "test", "status": "failed"},
                "{not json",
                "",
                json.dumps({"type": "llm_usage", "usage": "not-a-dict"}),
            ],
        )
        summary = aggregate_llm_usage(events_path)
        totals = summary["totals"]
        # One fully-formed event plus one malformed llm_usage (still counted as
        # a call, with zero tokens and no cost dict -> unpriced).
        assert totals["calls"] == 2
        assert totals["input"] == 90
        assert totals["unpriced_calls"] == 1

    def test_estimated_and_unpriced_calls_are_flagged(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [
                _usage_event(source="estimated", model="unknown-model", cost=None),
                _usage_event(cost=None),
            ],
        )
        summary = aggregate_llm_usage(events_path)
        totals = summary["totals"]
        assert totals["calls"] == 2
        assert totals["estimated_calls"] == 1
        assert totals["unpriced_calls"] == 2
        assert totals["cost"]["total"] == 0.0


def _tool_event(
    *,
    node_id: str = "REQ-1",
    phase: str = "IMPLEMENT",
    tool: str = "grep",
    status: str = "ok",
    detail: dict | None = None,
) -> dict:
    return {
        "type": "tool_usage",
        "node_id": node_id,
        "phase": phase,
        "tool": tool,
        "status": status,
        "detail": detail
        if detail is not None
        else {"path": None, "offset": None, "limit": None, "result_chars": 0, "result_empty": True},
        "timestamp": "2026-09-14 00:00:00",
    }


class TestAggregateToolUsage:
    def test_missing_file_yields_empty_totals(self, tmp_path: Path) -> None:
        summary = aggregate_tool_usage(tmp_path / "runner-events.jsonl")
        assert summary["totals"] == empty_tool_bucket()
        assert summary["by_node"] == {}
        assert summary["by_tool"] == {}
        assert summary["by_phase"] == {}

    def test_totals_and_sections_accumulate(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [
                # Whole-file read signal: read_file without an explicit limit.
                _tool_event(
                    node_id="REQ-1",
                    tool="read_file",
                    detail={"path": "/workspace/src/big.ts", "offset": 0, "limit": None, "result_chars": 90000, "result_empty": False},
                ),
                # Paginated read: not an unpaged-read signal.
                _tool_event(
                    node_id="REQ-1",
                    tool="read_file",
                    detail={"path": "/workspace/src/big.ts", "offset": 200, "limit": 200, "result_chars": 9000, "result_empty": False},
                ),
                # Ineffective grep: successful but empty.
                _tool_event(node_id="REQ-2", phase="TEST", tool="grep"),
                # Blocked discipline call still counts as a round-trip attempt.
                _tool_event(node_id="REQ-2", phase="TEST", tool="read_file", status="blocked"),
                _tool_event(node_id="REQ-3", tool="run_tests", status="error", detail={"path": None, "offset": None, "limit": None, "result_chars": 42, "result_empty": False}),
            ],
        )

        summary = aggregate_tool_usage(events_path)

        totals = summary["totals"]
        assert totals == {
            "calls": 5,
            "blocked": 1,
            "errors": 1,
            "empty_results": 1,
            "unpaged_reads": 2,  # the unpaged attempt and the blocked unpaged read
        }
        assert set(summary["by_node"]) == {"REQ-1", "REQ-2", "REQ-3"}
        assert summary["by_node"]["REQ-1"]["calls"] == 2
        assert summary["by_node"]["REQ-1"]["unpaged_reads"] == 1
        assert summary["by_node"]["REQ-2"]["empty_results"] == 1
        assert summary["by_node"]["REQ-2"]["blocked"] == 1
        assert summary["by_tool"]["grep"]["empty_results"] == 1
        assert summary["by_tool"]["read_file"]["unpaged_reads"] == 2
        assert summary["by_phase"]["TEST"]["calls"] == 2

    def test_empty_node_id_buckets_as_run_level(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [_tool_event(node_id="", phase="", tool="grep")],
        )
        summary = aggregate_tool_usage(events_path)
        assert set(summary["by_node"]) == {""}
        assert set(summary["by_phase"]) == {""}

    def test_ignores_other_event_types_and_malformed_lines(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [
                _tool_event(),
                {"type": "llm_usage", "node_id": "REQ-1", "usage": {"total": 1}},
                "{not json",
                "",
            ],
        )
        summary = aggregate_tool_usage(events_path)
        assert summary["totals"]["calls"] == 1

    def test_malformed_detail_is_tolerated(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [{"type": "tool_usage", "node_id": "REQ-1", "tool": "grep", "status": "ok", "detail": "not-a-dict"}],
        )
        summary = aggregate_tool_usage(events_path)
        assert summary["totals"]["calls"] == 1
        assert summary["totals"]["empty_results"] == 0
        assert summary["totals"]["unpaged_reads"] == 0

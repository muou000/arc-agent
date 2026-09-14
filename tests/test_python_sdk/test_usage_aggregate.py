"""Tests for ``arcbench_agent_runtime.usage.aggregate_llm_usage``.

The aggregator folds ``llm_usage`` runner events into the per-node, per-phase,
per-model and run-level totals that ARC-Bench token-efficiency work reads.
These tests pin the bucket schema (including the provider prefix-cache hit
rate) and the robustness rules (skip other event types, malformed lines,
unpriced calls).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcbench_agent_runtime.usage import aggregate_llm_usage, empty_usage_bucket


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
    cache_read: int = 20,
    cache_write: int = 10,
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
            "cache_read": cache_read,
            "cache_write": cache_write,
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
        # prompt_tokens = input + cache_read + cache_write over reported calls
        assert totals["prompt_tokens"] == 290
        assert totals["cache_hit_rate"] == pytest.approx(60 / 290)
        assert totals["estimated_calls"] == 0
        assert totals["unpriced_calls"] == 0
        assert totals["cost"]["total"] == pytest.approx(0.00055 * 2 + 0.3)
        assert summary["by_model"]["deepseek-chat"]["cost"]["total"] == pytest.approx(0.00055)

        assert set(summary["by_node"]) == {"REQ-1", "REQ-2"}
        assert summary["by_node"]["REQ-1"]["calls"] == 2
        assert summary["by_node"]["REQ-2"]["total"] == 180
        assert summary["by_node"]["REQ-2"]["prompt_tokens"] == 130
        assert summary["by_node"]["REQ-2"]["cache_hit_rate"] == pytest.approx(20 / 130)

        assert set(summary["by_phase"]) == {"DESIGN", "IMPLEMENT"}
        assert summary["by_phase"]["DESIGN"]["calls"] == 1
        assert summary["by_phase"]["DESIGN"]["cache_hit_rate"] == pytest.approx(20 / 120)

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
        # a call, with zero tokens and no cost dict -> unpriced). The malformed
        # event has no usage dict, so it stays out of the hit-rate denominator.
        assert totals["calls"] == 2
        assert totals["input"] == 90
        assert totals["unpriced_calls"] == 1
        assert totals["prompt_tokens"] == 120
        assert totals["cache_hit_rate"] == pytest.approx(20 / 120)

    def test_estimated_and_unpriced_calls_are_flagged(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [
                _usage_event(source="estimated", model="unknown-model", cache_read=0, cache_write=0, cost=None),
                _usage_event(cost=None),
            ],
        )
        summary = aggregate_llm_usage(events_path)
        totals = summary["totals"]
        assert totals["calls"] == 2
        assert totals["estimated_calls"] == 1
        assert totals["unpriced_calls"] == 2
        assert totals["cost"]["total"] == 0.0

    def test_estimated_calls_are_excluded_from_cache_hit_rate(self, tmp_path: Path) -> None:
        # Estimated usage carries no cache breakdown by construction (the
        # capture path zeroes it), so it must not dilute the hit-rate
        # denominator even though its input tokens still count in the sums.
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [
                _usage_event(node_id="REQ-1", phase="IMPLEMENT"),
                _usage_event(
                    node_id="REQ-1",
                    phase="IMPLEMENT",
                    source="estimated",
                    input=5000,
                    cache_read=0,
                    cache_write=0,
                ),
            ],
        )
        summary = aggregate_llm_usage(events_path)
        totals = summary["totals"]
        assert totals["estimated_calls"] == 1
        assert totals["input"] == 5090
        assert totals["prompt_tokens"] == 120
        assert totals["cache_hit_rate"] == pytest.approx(20 / 120)
        assert summary["by_phase"]["IMPLEMENT"]["cache_hit_rate"] == pytest.approx(20 / 120)

    def test_reported_zero_cache_is_measured_zero(self, tmp_path: Path) -> None:
        # A reported call with zero cache fields is a genuine 0% hit (e.g. a
        # provider without caching), not "unmeasured" — it counts in the
        # denominator and yields rate 0.0.
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [_usage_event(cache_read=0, cache_write=0)],
        )
        summary = aggregate_llm_usage(events_path)
        assert summary["totals"]["prompt_tokens"] == 90
        assert summary["totals"]["cache_hit_rate"] == 0.0

    def test_no_reported_calls_yield_unmeasured_hit_rate(self, tmp_path: Path) -> None:
        events_path = _write_events(
            tmp_path / "runner-events.jsonl",
            [_usage_event(source="estimated", cache_read=0, cache_write=0)],
        )
        summary = aggregate_llm_usage(events_path)
        assert summary["totals"]["prompt_tokens"] == 0
        assert summary["totals"]["cache_hit_rate"] is None

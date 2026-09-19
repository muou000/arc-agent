"""Pure-function tests for the A/B eval harness: metric extraction, comparison
math, and the report rendering layout (mirrors the pi ``Eval Comparisons``
format from ``pi/packages/evals/README.md``)."""

from __future__ import annotations

import json

from core.evals import (
    collect_run_record,
    node_outcome_counts,
    parse_env_overrides,
    render_report_text,
    summarize_runs,
    task_outcome_counts,
)

import pytest


# ---------------------------------------------------------------------------
# node_outcome_counts
# ---------------------------------------------------------------------------
def test_node_outcome_counts_buckets_states():
    counts = node_outcome_counts(
        {
            "n1": "PASSED",
            "n2": "CONVERGED",
            "n3": "FAILED",
            "n4": "DESIGNED",
            "n5": "CONVERGED_WITH_FAILED_CHILDREN",
        }
    )
    assert counts == {"total": 5, "passed": 2, "failed": 1, "blocked": 0, "other": 2}


def test_node_outcome_counts_empty():
    assert node_outcome_counts({}) == {"total": 0, "passed": 0, "failed": 0, "blocked": 0, "other": 0}
    assert node_outcome_counts(None) == {"total": 0, "passed": 0, "failed": 0, "blocked": 0, "other": 0}


def test_task_outcome_counts_buckets_queue_statuses():
    assert task_outcome_counts(
        [
            {"status": "COMPLETED"},
            {"status": "FAILED"},
            {"status": "PENDING"},
            {"status": "RUNNING"},
            {"status": "unknown"},
        ]
    ) == {"total": 5, "completed": 1, "failed": 1, "pending": 1, "running": 1, "other": 1}


# ---------------------------------------------------------------------------
# collect_run_record
# ---------------------------------------------------------------------------
def _write_workspace(
    tmp_path,
    *,
    node_states: dict[str, str] | None,
    usage_events: list[dict] | None,
    tasks: list[dict] | None = None,
    traceability_tests: dict | None = None,
):
    workspace = tmp_path / "ws"
    arc = workspace / ".arc"
    arc.mkdir(parents=True)
    if usage_events is not None:
        lines = [json.dumps(event) for event in usage_events]
        (arc / "runner-events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if node_states is not None:
        (arc / "processing_queue.json").write_text(
            json.dumps({"root_id": "root", "tasks": tasks or [], "node_states": node_states}),
            encoding="utf-8",
        )
    if traceability_tests is not None:
        traceability_dir = arc / "traceability"
        traceability_dir.mkdir()
        (traceability_dir / "tests.json").write_text(json.dumps(traceability_tests), encoding="utf-8")
    return workspace


_USAGE_EVENT = {
    "type": "llm_usage",
    "node_id": "n1",
    "phase": "IMPLEMENT",
    "model": "m",
    "api_mode": "chat_completions",
    "source": "reported",
    "usage": {"input": 10, "output": 20, "cache_read": 0, "cache_write": 0, "cache_write_1h": None, "reasoning": None, "total": 30},
    "cost": {"input": 0.001, "output": 0.002, "cache_read": 0.0, "cache_write": 0.0, "total": 0.003},
    "timestamp": "2026-09-13 10:00:00",
}


def test_collect_run_record_passed_run(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        node_states={"n1": "PASSED", "n2": "CONVERGED"},
        usage_events=[_USAGE_EVENT, _USAGE_EVENT],
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=1500.0)
    assert record["passed"] is True
    assert record["nodes_total"] == 2
    assert record["nodes_passed"] == 2
    assert record["nodes_failed"] == 0
    assert record["node_states"] == {"n1": "PASSED", "n2": "CONVERGED"}
    assert record["usage"] == {
        "calls": 2,
        "estimated_calls": 0,
        "unpriced_calls": 0,
        "total_tokens": 60,
        "cost_total": pytest.approx(0.006),
        "prompt_tokens": 20,
        "cache_hit_rate": 0.0,
    }
    assert record["outcome"] == "passed"
    assert record["diagnostics"]["tasks"]["present"] is False
    assert record["diagnostics"]["llm_usage"]["by_phase"]["IMPLEMENT"]["total"] == 60


def test_collect_run_record_usage_cache_hit_rate(tmp_path):
    cached_event = {
        **_USAGE_EVENT,
        "usage": {"input": 90, "output": 20, "cache_read": 20, "cache_write": 10, "cache_write_1h": None, "reasoning": None, "total": 150},
    }
    # 1 reported cache-bearing call: rate = 20 / (90 + 20 + 10)
    workspace = _write_workspace(tmp_path, node_states={"n1": "PASSED"}, usage_events=[cached_event])
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["usage"]["prompt_tokens"] == 120
    assert record["usage"]["cache_hit_rate"] == pytest.approx(20 / 120)


def test_collect_run_record_cache_hit_rate_unmeasured_without_reported_usage(tmp_path):
    # Only estimated calls (no cache breakdown): the rate is unmeasurable.
    estimated_event = {
        **_USAGE_EVENT,
        "source": "estimated",
        "usage": {"input": 10, "output": 20, "cache_read": 0, "cache_write": 0, "cache_write_1h": None, "reasoning": None, "total": 30},
    }
    workspace = _write_workspace(tmp_path, node_states={"n1": "PASSED"}, usage_events=[estimated_event])
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["usage"]["prompt_tokens"] == 0
    assert record["usage"]["cache_hit_rate"] is None


def test_collect_run_record_failed_node_is_not_passed(tmp_path):
    workspace = _write_workspace(
        tmp_path, node_states={"n1": "PASSED", "n2": "FAILED"}, usage_events=[_USAGE_EVENT]
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["passed"] is False
    assert record["nodes_failed"] == 1


def test_collect_run_record_blocked_node_is_not_passed(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        node_states={"n1": "PASSED", "n2": "BLOCKED_BY_DEPENDENCY"},
        usage_events=[_USAGE_EVENT],
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["passed"] is False
    assert record["outcome"] == "node_blocked"
    assert record["nodes_blocked"] == 1


def test_collect_run_record_nonzero_exit_is_not_passed(tmp_path):
    workspace = _write_workspace(
        tmp_path, node_states={"n1": "PASSED"}, usage_events=[_USAGE_EVENT]
    )
    record = collect_run_record(workspace, exit_code=1, latency_ms=100.0)
    assert record["passed"] is False


def test_collect_run_record_rejects_incomplete_queue(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        node_states={"n1": "DESIGNED"},
        tasks=[{"task_id": "n1:IMPLEMENT", "status": "PENDING"}],
        usage_events=[_USAGE_EVENT],
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["passed"] is False
    assert record["outcome"] == "incomplete_tasks"
    assert record["tasks_pending"] == 1


def test_collect_run_record_distinguishes_task_failure(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        node_states={"n1": "PASSED"},
        tasks=[{"task_id": "n1:TEST", "status": "FAILED"}],
        usage_events=[_USAGE_EVENT],
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["passed"] is False
    assert record["outcome"] == "task_failed"


def test_collect_run_record_rejects_nonterminal_node_state(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        node_states={"n1": "DESIGNING"},
        usage_events=[_USAGE_EVENT],
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["passed"] is False
    assert record["outcome"] == "incomplete_node_state"


def test_collect_run_record_includes_failure_and_test_diagnostics(tmp_path):
    failure_event = {
        "type": "requirement_state",
        "node_id": "n1",
        "phase": "test",
        "status": "failed",
        "message": "Exit Code: 1\nError: assertion failed",
    }
    tool_event = {
        "type": "tool_usage",
        "node_id": "n1",
        "phase": "IMPLEMENT",
        "tool": "run_tests",
        "status": "error",
        "detail": {"result_empty": False, "limit": None},
    }
    workspace = _write_workspace(
        tmp_path,
        node_states={"n1": "FAILED"},
        usage_events=[_USAGE_EVENT, failure_event, tool_event],
        traceability_tests={
            "t1": {"type": "Unit", "passed": True},
            "t2": {"type": "E2E", "passed": False},
            "t3": {"type": "E2E", "passed": None},
        },
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=123.0)
    diagnostics = record["diagnostics"]
    assert diagnostics["events"]["counts"] == {
        "llm_usage": 1,
        "requirement_state": 1,
        "tool_usage": 1,
    }
    assert diagnostics["events"]["failure_count"] == 1
    assert diagnostics["events"]["failures"][0]["node_id"] == "n1"
    assert diagnostics["tool_usage"]["totals"]["errors"] == 1
    assert diagnostics["traceability_tests"] == {
        "total": 3,
        "passed": 1,
        "failed": 1,
        "unmeasured": 1,
        "by_type": {
            "Unit": {"total": 1, "passed": 1, "failed": 0, "unmeasured": 0},
            "E2E": {"total": 2, "passed": 0, "failed": 1, "unmeasured": 1},
        },
    }


def test_collect_run_record_without_artifacts(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    record = collect_run_record(workspace, exit_code=None, latency_ms=0.0, error="runner not executable: boom")
    assert record["passed"] is False
    assert record["nodes_total"] == 0
    assert record["usage"] is None


def test_collect_run_record_without_events_keeps_pass_and_marks_unmeasured(tmp_path):
    workspace = _write_workspace(
        tmp_path,
        node_states={"n1": "PASSED"},
        usage_events=None,
    )
    record = collect_run_record(workspace, exit_code=0, latency_ms=100.0)
    assert record["passed"] is True
    assert record["outcome"] == "passed"
    assert record["usage"] is None
    assert record["diagnostics"]["events_present"] is False
    assert record["diagnostics"]["llm_usage"]["totals"]["calls"] == 0
    assert record["diagnostics"]["tool_usage"]["totals"]["calls"] == 0


# ---------------------------------------------------------------------------
# summarize_runs
# ---------------------------------------------------------------------------
def _run(
    arm: str,
    rep: int,
    *,
    passed: bool,
    latency: float,
    tokens: int | None,
    cost: float | None,
    cache_hit_rate: float | None = None,
):
    usage = None if tokens is None else {"total_tokens": tokens, "cost_total": cost, "cache_hit_rate": cache_hit_rate}
    return {"arm": arm, "repetition": rep, "passed": passed, "latency_ms": latency, "usage": usage}


def test_summarize_paired_deltas():
    runs = [
        _run("baseline", 1, passed=False, latency=20000.0, tokens=10000, cost=0.020, cache_hit_rate=0.4),
        _run("candidate", 1, passed=True, latency=10000.0, tokens=9000, cost=0.018, cache_hit_rate=0.6),
        _run("baseline", 2, passed=True, latency=22000.0, tokens=11000, cost=0.022, cache_hit_rate=0.5),
        _run("candidate", 2, passed=True, latency=11000.0, tokens=9200, cost=0.0184, cache_hit_rate=0.7),
    ]
    comparison = summarize_runs(runs, repetitions=2)
    assert comparison["pairs"] == 2
    assert comparison["pass_rate"]["baseline"] == 50.0
    assert comparison["pass_rate"]["candidate"] == 100.0
    assert comparison["pass_rate"]["delta_pp"] == pytest.approx(50.0)
    assert comparison["tokens"]["baseline"] == 10500.0
    assert comparison["tokens"]["candidate"] == 9100.0
    assert comparison["tokens"]["delta"] == -1400.0
    # stored as a ratio per run, compared in percent
    assert comparison["cache_hit_rate"]["baseline"] == pytest.approx(45.0)
    assert comparison["cache_hit_rate"]["candidate"] == pytest.approx(65.0)
    assert comparison["cache_hit_rate"]["delta"] == pytest.approx(20.0)
    assert comparison["latency_ms"]["delta"] == -10500.0
    assert comparison["est_cost"]["delta"] == pytest.approx(-0.0028)
    assert comparison["latency_distribution"]["baseline"]["p95"] == pytest.approx(21900.0)
    assert comparison["latency_distribution"]["candidate"]["median"] == pytest.approx(10500.0)
    assert comparison["latency_distribution"]["paired_delta"]["max"] == pytest.approx(-10000.0)


def test_summarize_missing_telemetry_keeps_other_side():
    runs = [
        _run("baseline", 1, passed=True, latency=100.0, tokens=5000, cost=0.01, cache_hit_rate=0.5),
        _run("candidate", 1, passed=True, latency=100.0, tokens=None, cost=None),
    ]
    comparison = summarize_runs(runs, repetitions=1)
    assert comparison["tokens"]["baseline"] == 5000.0
    assert comparison["tokens"]["candidate"] is None
    assert comparison["tokens"]["delta"] is None
    assert comparison["tokens"]["n"] == {"baseline": 1, "candidate": 0}
    assert comparison["est_cost"]["delta"] is None
    assert comparison["cache_hit_rate"]["baseline"] == pytest.approx(50.0)
    assert comparison["cache_hit_rate"]["candidate"] is None
    assert comparison["cache_hit_rate"]["delta"] is None
    # latency telemetry is harness-measured and always available
    assert comparison["latency_ms"]["delta"] == 0.0


def test_summarize_unmeasured_cache_hit_rate_is_excluded():
    # cache_hit_rate=None (no reported cache breakdown) on one side drops the
    # delta instead of averaging over a partial denominator.
    runs = [
        _run("baseline", 1, passed=True, latency=100.0, tokens=5000, cost=0.01, cache_hit_rate=0.5),
        _run("candidate", 1, passed=True, latency=100.0, tokens=5000, cost=0.01, cache_hit_rate=None),
    ]
    comparison = summarize_runs(runs, repetitions=1)
    assert comparison["cache_hit_rate"]["baseline"] == pytest.approx(50.0)
    assert comparison["cache_hit_rate"]["candidate"] is None
    assert comparison["cache_hit_rate"]["delta"] is None
    assert comparison["cache_hit_rate"]["n"] == {"baseline": 1, "candidate": 0}


def test_summarize_skips_unpaired_repetitions():
    runs = [
        _run("baseline", 1, passed=True, latency=100.0, tokens=5000, cost=0.01),
        _run("candidate", 1, passed=False, latency=100.0, tokens=5000, cost=0.01),
        _run("baseline", 2, passed=True, latency=100.0, tokens=5000, cost=0.01),
        # candidate rep 2 never ran
    ]
    comparison = summarize_runs(runs, repetitions=2)
    assert comparison["pairs"] == 1
    assert comparison["pass_rate"]["baseline"] == 100.0
    assert comparison["pass_rate"]["candidate"] == 0.0


def test_summarize_no_pairs_reports_none():
    comparison = summarize_runs([], repetitions=3)
    assert comparison["pairs"] == 0
    assert comparison["pass_rate"]["baseline"] is None
    assert comparison["pass_rate"]["delta_pp"] is None
    assert comparison["tokens"]["delta"] is None


# ---------------------------------------------------------------------------
# render_report_text
# ---------------------------------------------------------------------------
def test_render_report_text_matches_reference_layout():
    report = {
        "set_name": "Add model to existing provider",
        "baseline": {"label": "system-prompt-without-docs"},
        "candidate": {"label": "default-system-prompt"},
        "comparison": {
            "pairs": 5,
            "repetitions": 5,
            "pass_rate": {"baseline": 20.0, "candidate": 80.0, "delta_pp": 60.0},
            "tokens": {"baseline": 22800.0, "candidate": 24000.0, "delta": 1200.0},
            "cache_hit_rate": {"baseline": 49.2, "candidate": 61.5, "delta": 12.3},
            "latency_ms": {"baseline": 14850.0, "candidate": 14000.0, "delta": -850.0},
            "est_cost": {"baseline": 0.1100, "candidate": 0.1200, "delta": 0.0100},
        },
    }
    assert render_report_text(report) == (
        "Eval Comparisons\n"
        "  Add model to existing provider\n"
        "     Baseline  system-prompt-without-docs\n"
        "    Candidate  default-system-prompt (5/5 pairs)\n"
        "    Pass rate  +60.0 pp (candidate 80.0%, baseline 20.0%)\n"
        "       Tokens  +1200.0 (candidate 24000.0, baseline 22800.0)\n"
        "    Cache hit  +12.3 pp (candidate 61.5%, baseline 49.2%)\n"
        "      Latency  -850.0ms (candidate 14000.0ms, baseline 14850.0ms)\n"
        "    Est. cost  +¥0.0100 (candidate ¥0.1200, baseline ¥0.1100)"
    )


def test_render_report_text_unavailable_branches():
    report = {
        "set_name": "broken",
        "baseline": {"label": "b"},
        "candidate": {"label": "c"},
        "comparison": {
            "pairs": 0,
            "repetitions": 5,
            "pass_rate": {"baseline": None, "candidate": None, "delta_pp": None},
            "tokens": {"baseline": None, "candidate": None, "delta": None},
            "cache_hit_rate": {"baseline": None, "candidate": None, "delta": None},
            "latency_ms": {"baseline": None, "candidate": None, "delta": None},
            "est_cost": {"baseline": None, "candidate": None, "delta": None},
        },
    }
    text = render_report_text(report)
    assert "    Candidate  c (0/5 pairs)" in text
    assert "unavailable (0/5 pairs)" in text
    assert text.count("unavailable (missing telemetry)") == 4


def test_render_report_text_annotates_latency_p95_sample_counts():
    report = {
        "set_name": "small sample",
        "baseline": {"label": "b"},
        "candidate": {"label": "c"},
        "comparison": {
            "pairs": 1,
            "repetitions": 1,
            "pass_rate": {"baseline": 100.0, "candidate": 100.0, "delta_pp": 0.0},
            "tokens": {"baseline": 1.0, "candidate": 1.0, "delta": 0.0},
            "cache_hit_rate": {"baseline": None, "candidate": None, "delta": None},
            "latency_ms": {"baseline": 100.0, "candidate": 90.0, "delta": -10.0},
            "latency_distribution": {
                "baseline": {"n": 1, "p95": 100.0},
                "candidate": {"n": 1, "p95": 90.0},
            },
            "est_cost": {"baseline": 0.0, "candidate": 0.0, "delta": 0.0},
        },
    }
    assert "Latency p95  candidate 90.0ms (n=1), baseline 100.0ms (n=1)" in render_report_text(report)


# ---------------------------------------------------------------------------
# parse_env_overrides
# ---------------------------------------------------------------------------
def test_parse_env_overrides_splits_on_first_equals():
    assert parse_env_overrides(["A=1", "B=x=y", "C="], flag="--x") == {"A": "1", "B": "x=y", "C": ""}


def test_parse_env_overrides_rejects_bad_entries():
    with pytest.raises(ValueError, match="--x"):
        parse_env_overrides(["A=1", "novalue"], flag="--x")
    with pytest.raises(ValueError, match="--x"):
        parse_env_overrides(["=1"], flag="--x")


# ---------------------------------------------------------------------------
# default artifacts dir naming
# ---------------------------------------------------------------------------
def test_default_artifacts_dir_stamp_is_utc_and_slugified():
    import time as _time

    from core.evals import DEFAULT_ARTIFACTS_ROOT, _next_default_artifacts_dir

    lower_bound = _time.strftime("%Y%m%d-%H%M%S", _time.gmtime())
    path = _next_default_artifacts_dir("Skill Lift 汇报")
    upper_bound = _time.strftime("%Y%m%d-%H%M%S", _time.gmtime())
    stamp, slug = path.name[:15], path.name[16:]  # stamp is the fixed 15-char prefix
    assert path.parent == DEFAULT_ARTIFACTS_ROOT
    assert lower_bound <= stamp <= upper_bound
    assert slug == "skill-lift"  # non-ascii runs collapse to nothing


def test_default_artifacts_dir_skips_existing_names(tmp_path, monkeypatch):
    from core.evals import _next_default_artifacts_dir

    monkeypatch.setattr("core.evals.DEFAULT_ARTIFACTS_ROOT", tmp_path)
    first = _next_default_artifacts_dir("dup")
    first.mkdir(parents=True)
    second = _next_default_artifacts_dir("dup")
    assert second != first
    assert second.name == f"{first.name}-2"

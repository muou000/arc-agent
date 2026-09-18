"""A/B evaluation harness over the ARC compile pipeline.

Ports the comparative eval workflow of the ARC-Bench reference (pi,
``packages/evals``): declare a baseline arm and a candidate arm, run each arm
``repetitions`` times against the same requirement tree, and report the
candidate-minus-baseline lift on five fixed metrics — pass rate (pp), tokens,
cache hit rate (pp), latency (ms) and estimated cost (CNY) — each with both
arms' absolute values.

One invocation writes a self-contained artifacts directory (by default under
``records/evals/``):

- ``report.txt``: the terminal comparison report without color codes.
- ``report.json``: the same aggregate comparison data as structured JSON.
- ``runs.jsonl``: one record for every completed harness run.
- ``sessions/<run_id>/``: the run's ``.arc`` evidence (runner events, queue,
  traceability tables, node sessions, debug log) plus captured console output.

Each run record also contains compact diagnostics: completion/outcome gates,
failure events and fingerprints, traceability test status, and the existing
LLM/tool aggregations split by node, phase, model, and tool.

Run workspaces are throwaway: they live under a work root (system temp by
default) and are deleted after their evidence is snapshotted unless
``keep_workspaces`` is set, and an auto-created temp work root is removed
with them. The harness never calls the model provider itself;
each run is launched as a subprocess so an arm sees the same clean process
environment and module state as a normal ``arc compile`` invocation. The
baseline/candidate difference is expressed purely as environment overrides and
extra compile arguments on :class:`ArmConfig`.

Lift semantics mirror pi: runs are paired by repetition; the pass rate is the
share of paired runs whose compilation succeeded, and token/cache-hit/latency/
cost deltas are candidate-minus-baseline means over those pairs. Missing
telemetry (one side has no ``llm_usage`` events, or none with a
provider-reported cache breakdown) keeps the absolute values of the other
side but reports the delta as unavailable instead of guessing.

Runner contract: ``runner_command`` is the full command prefix of one run; the
harness appends ``<requirement> -o <workspace> -t <app_type> --port <web_port>``
plus the arm's extra argv. The default prefix runs the repository
``arc_main.py`` ``compile`` subcommand; a custom runner (``--runner-script``)
is an arbitrary script that receives those plain run arguments and must not
expect the ``compile`` subcommand.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app_type_handler.test_results import failure_fingerprint
from arcbench_agent_runtime.events import utc_timestamp
from arcbench_agent_runtime.jsonio import append_jsonl, read_json, write_json_atomic
from arcbench_agent_runtime.usage import aggregate_llm_usage, aggregate_tool_usage

REPORT_SCHEMA = "arc.eval.report/1"
RUN_SCHEMA = "arc.eval.run/1"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACTS_ROOT = REPO_ROOT / "records" / "evals"

_QUEUE_FILENAME = "processing_queue.json"  # core.workflow QUEUE_FILENAME

# Node states counted as passed/failed for the node-level breakdown. A
# CONVERGED node passed its own tests; its failed children appear as their own
# FAILED entries, so CONVERGED_WITH_FAILED_CHILDREN is counted as neither.
_NODE_PASSED_STATES = frozenset({"PASSED", "CONVERGED"})
_NODE_FAILED_STATES = frozenset({"FAILED"})
_TASK_COMPLETED_STATES = frozenset({"COMPLETED"})
_TASK_FAILED_STATES = frozenset({"FAILED"})
_ARM_ORDER_VALUES = frozenset({"baseline-first", "candidate-first", "alternate"})
_SECRET_ENV_MARKERS = frozenset(
    {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "PWD",
        "CREDENTIAL",
        "APIKEY",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+\S+"),
)
_MAX_FAILURE_EVENTS = 20

_CNY = "¥"
_ARM_KEYS = ("baseline", "candidate")


@dataclass(slots=True)
class ArmConfig:
    """One comparison arm: an env/argv delta on top of a plain compile run.

    ``env`` entries are applied over the inherited process environment of the
    runner subprocess (they win over the repository ``.env`` because
    ``load_project_env`` does not override existing variables). ``argv`` entries
    are appended after the harness-built compile arguments; argparse keeps the
    last occurrence of a repeated flag, so later entries win.
    """

    label: str
    env: dict[str, str] = field(default_factory=dict)
    argv: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EvalResult:
    report: dict[str, Any]
    runs: list[dict[str, Any]]
    artifacts_dir: Path


def default_runner_command() -> list[str]:
    """Runner prefix for each arm: this interpreter on the repo ``compile`` CLI.

    The harness appends the plain run arguments
    (``<requirement> -o <workspace> -t <app_type> --port <web_port> <arm argv>``)
    after this prefix, so the ``compile`` subcommand belongs to the prefix.
    """

    return [sys.executable, str(REPO_ROOT / "arc_main.py"), "compile"]


def parse_env_overrides(pairs: Sequence[str], *, flag: str) -> dict[str, str]:
    """Parse repeated ``KEY=VALUE`` flags into a dict, rejecting bad entries."""

    overrides: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"{flag} expects KEY=VALUE, got: {pair!r}")
        overrides[key.strip()] = value
    return overrides


def node_outcome_counts(node_states: dict[str, Any]) -> dict[str, int]:
    """Bucket a ``processing_queue.json`` ``node_states`` map."""

    counts = {"total": 0, "passed": 0, "failed": 0, "other": 0}
    for state in (node_states or {}).values():
        counts["total"] += 1
        if state in _NODE_PASSED_STATES:
            counts["passed"] += 1
        elif state in _NODE_FAILED_STATES:
            counts["failed"] += 1
        else:
            counts["other"] += 1
    return counts


def task_outcome_counts(tasks: Sequence[dict[str, Any]] | None) -> dict[str, int]:
    """Bucket processing-queue task statuses for completion diagnostics."""

    counts = {
        "total": 0,
        "completed": 0,
        "failed": 0,
        "pending": 0,
        "running": 0,
        "other": 0,
    }
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        counts["total"] += 1
        status = str(task.get("status") or "").strip().upper()
        if status in _TASK_COMPLETED_STATES:
            counts["completed"] += 1
        elif status in _TASK_FAILED_STATES:
            counts["failed"] += 1
        elif status == "PENDING":
            counts["pending"] += 1
        elif status == "RUNNING":
            counts["running"] += 1
        else:
            counts["other"] += 1
    return counts


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    """Keep eval provenance useful without copying likely credentials into artifacts."""

    redacted: dict[str, str] = {}
    for key, value in env.items():
        normalized = str(key).upper().replace("-", "_")
        value_text = str(value)
        key_is_sensitive = any(token in _SECRET_ENV_MARKERS for token in normalized.split("_"))
        value_is_sensitive = any(pattern.search(value_text) for pattern in _SECRET_VALUE_PATTERNS)
        if key_is_sensitive or value_is_sensitive:
            redacted[str(key)] = "<redacted>"
        else:
            redacted[str(key)] = value_text
    return redacted


def _summarize_traceability_tests(arc_dir: Path) -> dict[str, Any]:
    """Summarize declared/final test statuses without reading test output text."""

    table = read_json(arc_dir / "traceability" / "tests.json", default={})
    summary: dict[str, Any] = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "unmeasured": 0,
        "by_type": {},
    }
    for row in table.values():
        if not isinstance(row, dict):
            continue
        summary["total"] += 1
        passed = row.get("passed")
        if passed is True:
            summary["passed"] += 1
        elif passed is False:
            summary["failed"] += 1
        else:
            summary["unmeasured"] += 1
        test_type = str(row.get("type") or "unknown").strip() or "unknown"
        bucket = summary["by_type"].setdefault(
            test_type,
            {"total": 0, "passed": 0, "failed": 0, "unmeasured": 0},
        )
        bucket["total"] += 1
        bucket["passed" if passed is True else "failed" if passed is False else "unmeasured"] += 1
    return summary


def _summarize_runner_events(events_path: Path) -> dict[str, Any]:
    """Collect stable failure/event summaries while preserving raw session evidence."""

    counts: Counter[str] = Counter()
    requirement_states: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    if not events_path.exists():
        return {
            "counts": {},
            "requirement_states": {},
            "failure_count": 0,
            "failures": [],
        }
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        event_type = str(record.get("type") or "unknown")
        counts[event_type] += 1
        if event_type != "requirement_state":
            continue
        phase = str(record.get("phase") or "unknown").strip() or "unknown"
        status = str(record.get("status") or "unknown").strip() or "unknown"
        requirement_states[f"{phase}/{status}"] += 1
        if status != "failed":
            continue
        if len(failures) >= _MAX_FAILURE_EVENTS:
            continue
        message = str(record.get("message") or "").strip()
        failures.append(
            {
                "node_id": str(record.get("node_id") or "").strip(),
                "phase": phase,
                "message": " ".join(message.split())[:240] or None,
                "fingerprint": failure_fingerprint(message) if message else None,
            }
        )
    return {
        "counts": dict(sorted(counts.items())),
        "requirement_states": dict(sorted(requirement_states.items())),
        "failure_count": sum(
            count for key, count in requirement_states.items() if key.endswith("/failed")
        ),
        "failures": failures,
    }


def _run_outcome(
    *,
    exit_code: int | None,
    error: str | None,
    node_counts: dict[str, int],
    task_counts: dict[str, int],
    node_states_present: bool,
    tasks_present: bool,
) -> tuple[bool, str]:
    """Return a fail-closed outcome based on the same completion contract as workflow."""

    if error:
        return False, "timeout" if error.startswith("timed out after") else "launch_error"
    if exit_code is None:
        return False, "runner_exit_missing"
    if not node_states_present:
        return False, "missing_node_states"
    if node_counts["failed"]:
        return False, "node_failed"
    if task_counts["failed"]:
        return False, "task_failed"
    if tasks_present and (
        task_counts["completed"] != task_counts["total"]
        or task_counts["other"]
        or task_counts["pending"]
        or task_counts["running"]
    ):
        return False, "incomplete_tasks"
    if node_counts["other"]:
        return False, "incomplete_node_state"
    if exit_code != 0:
        return False, "runner_exit_nonzero"
    return True, "passed"


def _metric_distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    """Return robust distribution stats for a small repeated-run sample."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p95": None,
            "min": None,
            "max": None,
        }
    index = (len(ordered) - 1) * 0.95
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {
        "n": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "median": median,
        "p95": p95,
        "min": ordered[0],
        "max": ordered[-1],
    }


def _ordered_arms(
    baseline: ArmConfig,
    candidate: ArmConfig,
    *,
    repetition: int,
    arm_order: str,
) -> list[tuple[str, ArmConfig]]:
    """Return the per-repetition arm order used by the harness."""

    if arm_order == "candidate-first":
        return [("candidate", candidate), ("baseline", baseline)]
    if arm_order == "alternate" and repetition % 2 == 0:
        return [("candidate", candidate), ("baseline", baseline)]
    return [("baseline", baseline), ("candidate", candidate)]


def summarize_run_diagnostics(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate compact outcome, LLM, and tool diagnostics by evaluation arm."""

    by_arm: dict[str, dict[str, Any]] = {}
    for run in runs:
        arm = str(run.get("arm") or "unknown")
        summary = by_arm.setdefault(
            arm,
            {
                "runs": 0,
                "outcomes": Counter(),
                # Event-level count; distinct from one outcome per run.
                "failure_events": 0,
                "llm": {"calls": 0, "total_tokens": 0, "cost_total": 0.0},
                "tools": {
                    "calls": 0,
                    "blocked": 0,
                    "errors": 0,
                    "empty_results": 0,
                    "unpaged_reads": 0,
                },
            },
        )
        summary["runs"] += 1
        summary["outcomes"][str(run.get("outcome") or "unknown")] += 1
        diagnostics = run.get("diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        events = diagnostics.get("events")
        if isinstance(events, dict):
            failure_count = events.get("failure_count")
            if failure_count is not None:
                summary["failure_events"] += int(failure_count)
        usage = run.get("usage")
        if isinstance(usage, dict):
            summary["llm"]["calls"] += int(usage.get("calls") or 0)
            summary["llm"]["total_tokens"] += int(usage.get("total_tokens") or 0)
            summary["llm"]["cost_total"] += float(usage.get("cost_total") or 0.0)
        tool_usage = diagnostics.get("tool_usage")
        totals = tool_usage.get("totals") if isinstance(tool_usage, dict) else None
        if isinstance(totals, dict):
            for key in summary["tools"]:
                summary["tools"][key] += int(totals.get(key) or 0)

    for summary in by_arm.values():
        summary["outcomes"] = dict(sorted(summary["outcomes"].items()))
    return {"by_arm": by_arm}


def collect_run_record(
    workspace: str | Path,
    *,
    exit_code: int | None,
    latency_ms: float,
    error: str | None = None,
) -> dict[str, Any]:
    """Extract the eval metrics of one finished run from its output workspace.

    Reads ``.arc/processing_queue.json`` for node/task outcomes and aggregates
    ``.arc/runner-events.jsonl`` for token/cost/tool/failure diagnostics; all
    evidence is optional so a run that crashed before producing artifacts still
    yields a usable record. A run counts as passed only when the queue is
    complete (when present), every recorded node is terminal-successful, and
    the runner exited 0.
    """

    workspace = Path(workspace)
    usage: dict[str, Any] | None = None
    events_path = workspace / ".arc" / "runner-events.jsonl"
    llm_summary = aggregate_llm_usage(events_path)
    tool_summary = aggregate_tool_usage(events_path)
    event_summary = {
        "counts": {},
        "requirement_states": {},
        "failure_count": 0,
        "failures": [],
    }
    events_present = events_path.exists()
    if events_path.exists():
        event_summary = _summarize_runner_events(events_path)
        totals = llm_summary["totals"]
        usage = {
            "calls": totals["calls"],
            "estimated_calls": totals["estimated_calls"],
            "unpriced_calls": totals["unpriced_calls"],
            "total_tokens": totals["total"],
            "cost_total": totals["cost"]["total"],
            "prompt_tokens": totals["prompt_tokens"],
            "cache_hit_rate": totals["cache_hit_rate"],
        }

    queue = read_json(workspace / ".arc" / _QUEUE_FILENAME, default={})
    node_states_raw = queue.get("node_states")
    node_states = node_states_raw if isinstance(node_states_raw, dict) else {}
    tasks_raw = queue.get("tasks")
    tasks = tasks_raw if isinstance(tasks_raw, list) else []
    counts = node_outcome_counts(node_states)
    task_counts = task_outcome_counts(tasks)
    passed, outcome = _run_outcome(
        exit_code=exit_code,
        error=error,
        node_counts=counts,
        task_counts=task_counts,
        node_states_present=bool(node_states),
        tasks_present=isinstance(tasks_raw, list) and bool(tasks),
    )
    ok = exit_code == 0 and error is None
    passed = bool(ok and counts["total"] > 0 and passed)
    return {
        "nodes_total": counts["total"],
        "nodes_passed": counts["passed"],
        "nodes_failed": counts["failed"],
        "nodes_other": counts["other"],
        "node_states": node_states,
        "tasks_total": task_counts["total"],
        "tasks_completed": task_counts["completed"],
        "tasks_failed": task_counts["failed"],
        "tasks_pending": task_counts["pending"],
        "tasks_running": task_counts["running"],
        "tasks_other": task_counts["other"],
        "passed": passed,
        "outcome": "passed" if passed else outcome,
        "diagnostics": {
            "wall_clock_ms": float(latency_ms),
            "events_present": events_present,
            "tasks": {**task_counts, "present": isinstance(tasks_raw, list) and bool(tasks)},
            "events": event_summary,
            "llm_usage": llm_summary,
            "tool_usage": tool_summary,
            "traceability_tests": _summarize_traceability_tests(workspace / ".arc"),
        },
        "usage": usage,
    }


def summarize_runs(runs: Sequence[dict[str, Any]], *, repetitions: int) -> dict[str, Any]:
    """Fold per-run records into the baseline-vs-candidate comparison.

    Only repetitions where both arms produced a run are paired; each metric's
    absolute values average over the paired runs where that metric is
    available, and the delta is ``None`` when either side lacks data.
    """

    by_arm_rep: dict[tuple[str, int], dict[str, Any]] = {}
    for run in runs:
        by_arm_rep[(str(run.get("arm")), int(run.get("repetition", 0)))] = run
    pairs = [
        (by_arm_rep[("baseline", rep)], by_arm_rep[("candidate", rep)])
        for rep in range(1, repetitions + 1)
        if ("baseline", rep) in by_arm_rep and ("candidate", rep) in by_arm_rep
    ]

    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def _delta(base: float | None, cand: float | None) -> float | None:
        return None if base is None or cand is None else cand - base

    def _metric(getter: Callable[[dict[str, Any]], float | None]) -> dict[str, Any]:
        base_vals = [v for v in (getter(base) for base, _ in pairs) if v is not None]
        cand_vals = [v for v in (getter(cand) for _, cand in pairs) if v is not None]
        base_mean, cand_mean = _mean(base_vals), _mean(cand_vals)
        return {
            "baseline": base_mean,
            "candidate": cand_mean,
            "delta": _delta(base_mean, cand_mean),
            "n": {"baseline": len(base_vals), "candidate": len(cand_vals)},
        }

    # getter returns percent directly so the paired mean is the pass rate in %
    # and its delta is already in percentage points.
    pass_rate = _metric(lambda run: 100.0 if run.get("passed") else 0.0)
    pass_rate["delta_pp"] = pass_rate.pop("delta")

    baseline_latency = [
        float(base["latency_ms"])
        for base, _candidate in pairs
        if base.get("latency_ms") is not None
    ]
    candidate_latency = [
        float(candidate["latency_ms"])
        for _base, candidate in pairs
        if candidate.get("latency_ms") is not None
    ]
    paired_latency_delta = [
        float(candidate["latency_ms"]) - float(base["latency_ms"])
        for base, candidate in pairs
        if base.get("latency_ms") is not None and candidate.get("latency_ms") is not None
    ]

    return {
        "pairs": len(pairs),
        "repetitions": repetitions,
        "pass_rate": pass_rate,
        "tokens": _metric(lambda run: _usage_field(run, "total_tokens")),
        # cache_hit_rate is stored as a ratio (0-1) per run; the comparison
        # metric is in percent so its delta reads in percentage points.
        "cache_hit_rate": _metric(lambda run: _usage_percent_field(run, "cache_hit_rate")),
        "latency_ms": _metric(lambda run: run.get("latency_ms")),
        "latency_distribution": {
            "baseline": _metric_distribution(baseline_latency),
            "candidate": _metric_distribution(candidate_latency),
            "paired_delta": _metric_distribution(paired_latency_delta),
        },
        "est_cost": _metric(lambda run: _usage_field(run, "cost_total")),
    }


def _usage_field(run: dict[str, Any], key: str) -> float | None:
    usage = run.get("usage")
    if not isinstance(usage, dict) or usage.get(key) is None:
        return None
    return float(usage[key])


def _usage_percent_field(run: dict[str, Any], key: str) -> float | None:
    value = _usage_field(run, key)
    return None if value is None else 100.0 * value


def render_report_text(report: dict[str, Any]) -> str:
    """Render the terminal comparison report (pi ``Eval Comparisons`` layout)."""

    comparison = report["comparison"]
    lines = [
        "Eval Comparisons",
        f"  {report['set_name']}",
        f"{'Baseline':>13}  {report['baseline']['label']}",
        f"{'Candidate':>13}  {report['candidate']['label']}"
        f" ({comparison['pairs']}/{comparison['repetitions']} pairs)",
    ]
    lines.append(
        f"{'Pass rate':>13}  {_format_pass_rate(comparison['pass_rate'], comparison['pairs'], comparison['repetitions'])}"
    )
    lines.append(f"{'Tokens':>13}  {_format_float_delta(comparison['tokens'], 'f')}")
    lines.append(f"{'Cache hit':>13}  {_format_pp_delta(comparison['cache_hit_rate'])}")
    lines.append(f"{'Latency':>13}  {_format_float_delta(comparison['latency_ms'], 'ms')}")
    latency_distribution = comparison.get("latency_distribution") or {}
    if latency_distribution.get("baseline") and latency_distribution.get("candidate"):
        baseline_p95 = latency_distribution["baseline"].get("p95")
        candidate_p95 = latency_distribution["candidate"].get("p95")
        if baseline_p95 is not None and candidate_p95 is not None:
            baseline_n = latency_distribution["baseline"].get("n")
            candidate_n = latency_distribution["candidate"].get("n")
            lines.append(
                f"{'Latency p95':>13}  candidate {candidate_p95:.1f}ms (n={candidate_n}), "
                f"baseline {baseline_p95:.1f}ms (n={baseline_n})"
            )
    lines.append(f"{'Est. cost':>13}  {_format_cost_delta(comparison['est_cost'])}")
    return "\n".join(lines)


def _format_pass_rate(metric: dict[str, Any], pairs: int, repetitions: int) -> str:
    if metric["baseline"] is None or metric["candidate"] is None or pairs == 0:
        return f"unavailable (0/{repetitions} pairs)"
    delta_pp = metric["delta_pp"]
    assert delta_pp is not None
    return (
        f"{delta_pp:+.1f} pp"
        f" (candidate {metric['candidate']:.1f}%, baseline {metric['baseline']:.1f}%)"
    )


def _format_pp_delta(metric: dict[str, Any]) -> str:
    if metric["delta"] is None:
        return "unavailable (missing telemetry)"
    return (
        f"{metric['delta']:+.1f} pp"
        f" (candidate {metric['candidate']:.1f}%, baseline {metric['baseline']:.1f}%)"
    )


def _format_float_delta(metric: dict[str, Any], unit: str) -> str:
    if metric["delta"] is None:
        return "unavailable (missing telemetry)"
    suffix = unit if unit == "ms" else ""
    return (
        f"{metric['delta']:+.1f}{suffix}"
        f" (candidate {metric['candidate']:.1f}{suffix}, baseline {metric['baseline']:.1f}{suffix})"
    )


def _format_cost_delta(metric: dict[str, Any]) -> str:
    if metric["delta"] is None:
        return "unavailable (missing telemetry)"

    def signed_cny(value: float) -> str:
        sign = "+" if value >= 0 else "-"
        return f"{sign}{_CNY}{abs(value):.4f}"

    return (
        f"{signed_cny(metric['delta'])}"
        f" (candidate {_CNY}{metric['candidate']:.4f}, baseline {_CNY}{metric['baseline']:.4f})"
    )


def eval_table(
    name: str,
    baseline: ArmConfig,
    candidate: ArmConfig,
    *,
    requirement_path: str | Path,
    repetitions: int = 1,
    app_type: str = "web",
    web_port: int = 3301,
    timeout_seconds: float | None = None,
    runner_command: Sequence[str] | None = None,
    artifacts_dir: str | Path | None = None,
    work_root: str | Path | None = None,
    keep_workspaces: bool = False,
    arm_order: str = "baseline-first",
    log: Callable[[str], None] = print,
) -> EvalResult:
    """Run the A/B comparison and write its artifacts; see module docstring."""

    if repetitions < 1:
        raise ValueError(f"repetitions must be at least 1, got {repetitions}")
    if arm_order not in _ARM_ORDER_VALUES:
        raise ValueError(f"arm_order must be one of {sorted(_ARM_ORDER_VALUES)}, got {arm_order!r}")
    for arm_key, arm in (("baseline", baseline), ("candidate", candidate)):
        if not arm.label.strip():
            raise ValueError(f"{arm_key} arm label must not be empty")
    requirement = Path(requirement_path)
    if not requirement.exists():
        raise FileNotFoundError(f"requirement path not found: {requirement}")

    runner_prefix = list(runner_command) if runner_command else default_runner_command()
    artifacts = _prepare_artifacts_dir(artifacts_dir, name)
    sessions_dir = artifacts / "sessions"
    runs_jsonl = artifacts / "runs.jsonl"
    auto_created_root = False
    if work_root:
        workspace_root = Path(work_root)
    else:
        workspace_root = Path(tempfile.mkdtemp(prefix="arc-eval-"))
        auto_created_root = True
    workspace_root.mkdir(parents=True, exist_ok=True)

    log(f"Eval artifacts: {artifacts}")
    log(f"Workspaces: {workspace_root}")

    runs: list[dict[str, Any]] = []
    for rep in range(1, repetitions + 1):
        for order_index, (arm_key, arm) in enumerate(
            _ordered_arms(baseline, candidate, repetition=rep, arm_order=arm_order),
            start=1,
        ):
            run_id = f"{arm_key}-rep{rep:03d}"
            workspace = workspace_root / run_id
            run = _run_once(
                run_id=run_id,
                arm_key=arm_key,
                arm=arm,
                repetition=rep,
                runner_prefix=runner_prefix,
                requirement=requirement,
                app_type=app_type,
                web_port=web_port,
                timeout_seconds=timeout_seconds,
                workspace=workspace,
                sessions_dir=sessions_dir,
                order_index=order_index,
                arm_order=arm_order,
            )
            append_jsonl(runs_jsonl, run)
            runs.append(run)
            usage = run.get("usage") or {}
            log(
                f"[{run_id}] exit={run['exit_code']} latency={run['latency_ms'] / 1000.0:.1f}s"
                f" tokens={usage.get('total_tokens', '-')} passed={run['passed']}"
                + (f" error={run['error']}" if run["error"] else "")
            )
            if not keep_workspaces:
                shutil.rmtree(workspace, ignore_errors=True)

    comparison = summarize_runs(runs, repetitions=repetitions)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "set_name": name,
        "generated_at": utc_timestamp(),
        "repetitions": repetitions,
        "arm_order": arm_order,
        "requirement_path": str(requirement),
        "app_type": app_type,
        "web_port": web_port,
        "runner_command": runner_prefix,
        "baseline": {
            "label": baseline.label,
            "env": _redact_env(baseline.env),
            "argv": list(baseline.argv),
        },
        "candidate": {
            "label": candidate.label,
            "env": _redact_env(candidate.env),
            "argv": list(candidate.argv),
        },
        "comparison": comparison,
        "diagnostics": summarize_run_diagnostics(runs),
        "artifacts": {"runs_jsonl": str(runs_jsonl), "sessions_dir": str(sessions_dir)},
        "run_ids": [run["run_id"] for run in runs],
    }
    write_json_atomic(artifacts / "report.json", report)
    (artifacts / "report.txt").write_text(render_report_text(report) + "\n", encoding="utf-8")
    # an auto-created work root is harness-owned litter once its run
    # workspaces are gone; an explicit --work-root is the caller's territory
    if auto_created_root and not keep_workspaces:
        shutil.rmtree(workspace_root, ignore_errors=True)
    return EvalResult(report=report, runs=runs, artifacts_dir=artifacts)


def _next_default_artifacts_dir(name: str) -> Path:
    """Unique not-yet-created artifacts path under the default root.

    The stamp is UTC (matching every other machine timestamp in the harness)
    so directory ordering stays stable across timezone or DST changes.
    """

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "eval"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base = DEFAULT_ARTIFACTS_ROOT / f"{stamp}-{slug}"
    candidate = base
    index = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{index}")
        index += 1
    return candidate


def _prepare_artifacts_dir(artifacts_dir: str | Path | None, name: str) -> Path:
    if artifacts_dir:
        base = Path(artifacts_dir)
        base.mkdir(parents=True, exist_ok=True)
        return base
    candidate = _next_default_artifacts_dir(name)
    candidate.mkdir(parents=True)
    return candidate


def _run_once(
    *,
    run_id: str,
    arm_key: str,
    arm: ArmConfig,
    repetition: int,
    runner_prefix: Sequence[str],
    requirement: Path,
    app_type: str,
    web_port: int,
    timeout_seconds: float | None,
    workspace: Path,
    sessions_dir: Path,
    order_index: int,
    arm_order: str,
) -> dict[str, Any]:
    """Launch one compile run, snapshot its evidence, and collect its record."""

    if workspace.exists():
        raise ValueError(f"run workspace already exists: {workspace}")
    argv = [
        *runner_prefix,
        str(requirement),
        "-o",
        str(workspace),
        "-t",
        app_type,
        "--port",
        str(web_port),
        *arm.argv,
    ]
    started_at = utc_timestamp()
    start = time.perf_counter()
    exit_code: int | None = None
    error: str | None = None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            argv,
            env={**os.environ, **arm.env},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        stdout, stderr = completed.stdout or "", completed.stderr or ""
    except FileNotFoundError as exc:
        error = f"runner not executable: {exc}"
    except subprocess.TimeoutExpired:
        error = f"timed out after {timeout_seconds:g}s"
    latency_ms = (time.perf_counter() - start) * 1000.0

    snapshot = sessions_dir / run_id
    _snapshot_run_evidence(workspace, snapshot, stdout, stderr)

    record: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "arm": arm_key,
        "label": arm.label,
        "repetition": repetition,
        "order_index": order_index,
        "arm_order": arm_order,
        "requirement_path": str(requirement),
        "workspace": str(workspace),
        "env": _redact_env(arm.env),
        "argv": list(arm.argv),
        "exit_code": exit_code,
        "latency_ms": latency_ms,
        "started_at": started_at,
        "finished_at": utc_timestamp(),
        "error": error,
        "snapshot": str(snapshot),
    }
    record.update(
        collect_run_record(workspace, exit_code=exit_code, latency_ms=latency_ms, error=error)
    )
    return record


def _snapshot_run_evidence(workspace: Path, snapshot: Path, stdout: str, stderr: str) -> None:
    """Copy the run's ``.arc`` evidence and console output before cleanup."""

    snapshot.mkdir(parents=True, exist_ok=True)
    arc_dir = workspace / ".arc"
    if arc_dir.is_dir():
        shutil.copytree(
            arc_dir,
            snapshot / ".arc",
            ignore=shutil.ignore_patterns("worktrees"),
            dirs_exist_ok=True,
        )
    console = snapshot / "console.log"
    console.write_text(
        f"=== stdout ===\n{stdout}\n=== stderr ===\n{stderr}\n", encoding="utf-8"
    )

"""Aggregation helpers over ``llm_usage`` and ``tool_usage`` runner events.

``EventClient.record_llm_usage`` appends one event per model call to
``.arc/runner-events.jsonl``. This module folds those events into per-node,
per-phase, per-model and run-level token/cost totals so optimization work has
measured numbers to regress against. ``EventClient.record_tool_usage``
likewise appends one event per agent tool round-trip; ``aggregate_tool_usage``
folds those into per-node/per-tool round-trip counts, which is how whole-file
reads (unpaged ``read_file`` calls) and ineffective greps (empty results)
become measurable. Both aggregators only read the JSONL file — no runtime
object is required, so post-run tooling can aggregate a finished workspace.

``reasoning`` tokens are a subset of ``output`` (pi semantics) and are only
included in sums when the provider reported them; unreported breakdowns count
as zero. Costs are CNY sums over priced calls; ``unpriced_calls`` counts calls
whose model has no catalog entry, so an understated cost total is visible
instead of silent.

Every bucket also carries a provider prefix-cache hit rate:
``cache_hit_rate = cache_read / prompt_tokens`` where ``prompt_tokens`` is the
prompt total (``input + cache_read + cache_write``) accumulated over
provider-reported calls only. Estimated calls carry no cache breakdown by
construction (pi semantics), so they are kept out of the denominator instead
of silently diluting the rate. Reported calls whose cache fields are zero
count as genuine zero-hit prompt tokens — a provider that does not track
caching is indistinguishable from a provider that never hits, by construction
of the data. When no reported call exists the rate is ``None`` (unmeasured),
not ``0.0``, so consumers can tell "no data" from "a real 0% run". This rate
measures the provider's prompt cache — a low value with many writes means the
assembled context prefix is jittering (timestamps, random ids, ordering
drift). The in-process ``NodeContextCache`` in ``agents.context.pipeline`` is
memoization of local computation and has no bearing on this number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

_TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")
_COST_KEYS = ("input", "output", "cache_read", "cache_write", "total")


def empty_usage_bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "estimated_calls": 0,
        "unpriced_calls": 0,
        "prompt_tokens": 0,
        "cache_hit_rate": None,
        **{key: 0 for key in _TOKEN_KEYS},
        "cost": {key: 0.0 for key in _COST_KEYS},
        # Latency telemetry. ``duration_s`` is a SUM over ``timed_calls``
        # calls only; events without a latency block (written before the
        # field existed / by external runners) count toward ``calls`` but
        # neither bucket here. ``timed_calls == 0`` therefore means "latency
        # unmeasured", and consumers must read the mean as
        # ``duration_s / timed_calls`` only when ``timed_calls > 0`` — the
        # CLI renders the Latency line under exactly that guard. Attempts is
        # likewise a sum over the calls that reported it.
        "duration_s": 0.0,
        "timed_calls": 0,
        "streamed_calls": 0,
        "plain_calls": 0,
        "attempts": 0,
    }


def aggregate_llm_usage(events_path: str | Path) -> dict[str, Any]:
    """Aggregate ``llm_usage`` events from a runner-events JSONL file.

    Returns ``{"totals", "by_node", "by_phase", "by_model"}``. Empty ``node_id``
    values (run-level calls) are keyed as ``""``; callers choose how to display
    them. Events of other types, unparseable lines and malformed usage records
    are skipped.
    """
    path = Path(events_path)
    totals = empty_usage_bucket()
    by_node: dict[str, dict[str, Any]] = {}
    by_phase: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    for record in _iter_llm_usage_events(path):
        _accumulate(totals, record)
        _accumulate(_bucket(by_node, str(record.get("node_id") or "")), record)
        _accumulate(_bucket(by_phase, str(record.get("phase") or "")), record)
        _accumulate(_bucket(by_model, str(record.get("model") or "")), record)
    for bucket in (totals, *by_node.values(), *by_phase.values(), *by_model.values()):
        bucket["cache_hit_rate"] = _cache_hit_rate(bucket)
    return {"totals": totals, "by_node": by_node, "by_phase": by_phase, "by_model": by_model}


def _bucket(target: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    bucket = target.get(key)
    if bucket is None:
        bucket = empty_usage_bucket()
        target[key] = bucket
    return bucket


def _iter_llm_usage_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield ``llm_usage`` records line by line, never buffering the file.

    Streaming keeps large event logs out of memory; a trailing partial line
    (a concurrent writer mid-append) fails JSON parsing and is skipped like
    any other malformed line.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "llm_usage":
                yield record


def _accumulate(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    bucket["calls"] += 1
    estimated = str(record.get("source") or "") == "estimated"
    if estimated:
        bucket["estimated_calls"] += 1
    usage = record.get("usage")
    usage_dict = usage if isinstance(usage, dict) else None
    for key in _TOKEN_KEYS:
        bucket[key] += _int(usage.get(key) if usage_dict else 0)
    # cache_hit_rate denominator: only provider-reported calls have a known
    # cache breakdown; estimated usage would silently dilute the rate.
    if not estimated and usage_dict is not None:
        bucket["prompt_tokens"] += (
            _int(usage_dict.get("input"))
            + _int(usage_dict.get("cache_read"))
            + _int(usage_dict.get("cache_write"))
        )
    _accumulate_latency(bucket, record)
    cost = record.get("cost")
    if not isinstance(cost, dict):
        bucket["unpriced_calls"] += 1
        return
    for key in _COST_KEYS:
        bucket["cost"][key] += _float(cost.get(key))


def _accumulate_latency(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    """Fold the optional per-call ``latency`` block into the bucket.

    Events written before the telemetry existed (or by external runners) have
    no block; their calls still count toward ``calls`` but not toward
    ``timed_calls``, so ``duration_s / timed_calls`` is a true mean. A block
    whose duration fails to parse does not count as timed either.
    """

    latency = record.get("latency")
    if not isinstance(latency, dict):
        return
    duration = latency.get("duration_s")
    if duration is not None and _is_float(duration):
        bucket["timed_calls"] += 1
        bucket["duration_s"] += _float(duration)
    transport = str(latency.get("transport") or "")
    if transport == "streamed":
        bucket["streamed_calls"] += 1
    elif transport == "plain":
        bucket["plain_calls"] += 1
    attempts = latency.get("attempts")
    if attempts is not None:
        bucket["attempts"] += _int(attempts)


def _is_float(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _cache_hit_rate(bucket: dict[str, Any]) -> float | None:
    prompt_tokens = bucket["prompt_tokens"]
    return bucket["cache_read"] / prompt_tokens if prompt_tokens > 0 else None


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def empty_tool_bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "blocked": 0,
        "errors": 0,
        "empty_results": 0,
        "unpaged_reads": 0,
    }


def aggregate_tool_usage(events_path: str | Path) -> dict[str, Any]:
    """Aggregate ``tool_usage`` events from a runner-events JSONL file.

    Returns ``{"totals", "by_node", "by_tool", "by_phase"}``. Every event
    counts as one tool round-trip; ``blocked`` counts discipline-refused
    calls, ``errors`` tool executions that failed, ``empty_results`` successful
    calls that returned nothing (the ineffective-grep signal), and
    ``unpaged_reads`` ``read_file`` attempts without an explicit ``limit``
    (the whole-file-read signal; the event's ``result_chars`` ranks them by
    size). Events of other types and unparseable lines are skipped.
    """
    path = Path(events_path)
    totals = empty_tool_bucket()
    by_node: dict[str, dict[str, Any]] = {}
    by_tool: dict[str, dict[str, Any]] = {}
    by_phase: dict[str, dict[str, Any]] = {}
    for record in _iter_tool_usage_events(path):
        _accumulate_tool(totals, record)
        _accumulate_tool(_tool_bucket(by_node, str(record.get("node_id") or "")), record)
        _accumulate_tool(_tool_bucket(by_tool, str(record.get("tool") or "")), record)
        _accumulate_tool(_tool_bucket(by_phase, str(record.get("phase") or "")), record)
    return {"totals": totals, "by_node": by_node, "by_tool": by_tool, "by_phase": by_phase}


def _tool_bucket(target: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    bucket = target.get(key)
    if bucket is None:
        bucket = empty_tool_bucket()
        target[key] = bucket
    return bucket


def _iter_tool_usage_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield ``tool_usage`` records line by line, never buffering the file."""

    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "tool_usage":
                _normalize_tool_path_audit(record)
                yield record


def _normalize_tool_path_audit(record: dict[str, Any]) -> None:
    """Normalize optional virtual-path audit fields without changing totals.

    Older ``tool_usage`` events do not carry these fields. Newer events may
    carry the raw request, its classification, and the path passed to the
    filesystem tool; the usage reader keeps those fields typed and available
    to downstream readers while the existing aggregate buckets remain stable.
    """

    detail = record.get("detail")
    if not isinstance(detail, dict):
        return
    if not any(field in detail for field in ("requested_path", "path_classification", "execution_path")):
        return

    for field in ("requested_path", "execution_path"):
        value = detail.get(field)
        detail[field] = str(value).strip() or None if value is not None else None
    classification = detail.get("path_classification")
    detail["path_classification"] = str(classification).strip() if classification is not None else ""


def _accumulate_tool(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    bucket["calls"] += 1
    status = str(record.get("status") or "")
    if status == "blocked":
        bucket["blocked"] += 1
    elif status == "error":
        bucket["errors"] += 1
    detail = record.get("detail")
    if not isinstance(detail, dict):
        return
    if status == "ok" and bool(detail.get("result_empty")):
        bucket["empty_results"] += 1
    if str(record.get("tool") or "") == "read_file" and detail.get("limit") is None:
        bucket["unpaged_reads"] += 1

"""Aggregation helpers over ``llm_usage`` runner events.

``EventClient.record_llm_usage`` appends one event per model call to
``.arc/runner-events.jsonl``. This module folds those events into per-node,
per-phase, per-model and run-level token/cost totals so optimization work has
measured numbers to regress against. It only reads the JSONL file — no runtime
object is required, so post-run tooling can aggregate a finished workspace.

``reasoning`` tokens are a subset of ``output`` (pi semantics) and are only
included in sums when the provider reported them; unreported breakdowns count
as zero. Costs are CNY sums over priced calls; ``unpriced_calls`` counts calls
whose model has no catalog entry, so an understated cost total is visible
instead of silent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")
_COST_KEYS = ("input", "output", "cache_read", "cache_write", "total")


def empty_usage_bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "estimated_calls": 0,
        "unpriced_calls": 0,
        **{key: 0 for key in _TOKEN_KEYS},
        "cost": {key: 0.0 for key in _COST_KEYS},
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
    return {"totals": totals, "by_node": by_node, "by_phase": by_phase, "by_model": by_model}


def _bucket(target: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    bucket = target.get(key)
    if bucket is None:
        bucket = empty_usage_bucket()
        target[key] = bucket
    return bucket


def _iter_llm_usage_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "llm_usage":
            records.append(record)
    return records


def _accumulate(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    bucket["calls"] += 1
    if str(record.get("source") or "") == "estimated":
        bucket["estimated_calls"] += 1
    usage = record.get("usage")
    for key in _TOKEN_KEYS:
        bucket[key] += _int(usage.get(key) if isinstance(usage, dict) else 0)
    cost = record.get("cost")
    if not isinstance(cost, dict):
        bucket["unpriced_calls"] += 1
        return
    for key in _COST_KEYS:
        bucket["cost"][key] += _float(cost.get(key))


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

from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable

from core.service import get_runtime


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


def build_traceability_tools(
    *,
    node_id: str,
    log_cb: LogCallback | None = None,
    current_interfaces: list[dict[str, Any]] | None = None,
) -> list[object]:
    """Build read-only traceability tools for agents.

    ``current_interfaces`` is a DESIGN-stage overlay. The workflow keeps
    prepared interfaces out of the committed traceability tables until the
    baseline gate passes, but TestGenerator still needs to query those exact
    ids while declaring its manifest.
    """

    overlay = _normalize_overlay_records(current_interfaces or [], node_id=node_id)
    overlay_by_id = {
        str(record.get("interface_id") or "").strip(): record
        for record in overlay
        if str(record.get("interface_id") or "").strip()
    }

    def records_for_requirement(requested_req_id: str) -> list[dict[str, Any]]:
        store = get_runtime().traceability
        records = store.list_interfaces(req_id=requested_req_id)
        if requested_req_id == node_id:
            records = _merge_overlay_records(records, overlay)
        return records

    def all_records(requested_req_id: str | None = None) -> list[dict[str, Any]]:
        store = get_runtime().traceability
        records = store.list_interfaces(req_id=requested_req_id or None)
        if requested_req_id is None or requested_req_id == node_id:
            records = _merge_overlay_records(records, overlay, req_id=requested_req_id)
        return records

    async def get_interfaces_for_requirement(req_id: str) -> str:
        """Return raw interface records associated with a requirement id."""

        requested_req_id = str(req_id or "").strip()
        if not requested_req_id:
            return _json_error("req_id is required.")
        await _emit_log(log_cb, "Traceability", f"Query interfaces for requirement `{requested_req_id}`.", node_id=node_id)
        return _json_records(records_for_requirement(requested_req_id))

    async def get_interface(interface_id: str) -> str:
        """Return one raw interface record by interface_id."""

        requested_interface_id = str(interface_id or "").strip()
        if not requested_interface_id:
            return _json_error("interface_id is required.")
        await _emit_log(log_cb, "Traceability", f"Query interface `{requested_interface_id}`.", node_id=node_id)
        record = overlay_by_id.get(requested_interface_id)
        if record is None:
            record = get_runtime().traceability.get_interface(requested_interface_id)
        if record is None:
            return _json_records([])
        return _json_records([record])

    async def search_interfaces(keyword: str, req_id: str | None = None, interface_type: str | None = None, limit: int = 20) -> str:
        """Search raw interface records by keyword, optional requirement id, and optional interface type."""

        query = str(keyword or "").strip().lower()
        if not query:
            return _json_error("keyword is required.")
        requested_req_id = str(req_id or "").strip()
        requested_type = str(interface_type or "").strip().upper()
        max_items = _normalize_limit(limit)
        await _emit_log(
            log_cb,
            "Traceability",
            f"Search interfaces keyword={query!r} req_id={requested_req_id or '*'} type={requested_type or '*'} limit={max_items}.",
            node_id=node_id,
        )
        records = all_records(requested_req_id or None)
        matches: list[dict[str, Any]] = []
        for record in records:
            if requested_type and str(record.get("type", "") or "").strip().upper() != requested_type:
                continue
            haystack = json.dumps(record, ensure_ascii=False, default=str).lower()
            if query in haystack:
                matches.append(record)
            if len(matches) >= max_items:
                break
        return _json_records(matches)

    return [get_interfaces_for_requirement, get_interface, search_interfaces]


def _normalize_overlay_records(
    records: list[dict[str, Any]],
    *,
    node_id: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = {
            key: value
            for key, value in raw.items()
            if not str(key).startswith("_")
        }
        interface_id = str(record.get("interface_id") or "").strip()
        if not interface_id:
            continue
        req_ids = [
            str(value).strip()
            for value in record.get("req_ids") or [record.get("req_id") or node_id]
            if str(value or "").strip()
        ]
        record["req_ids"] = list(dict.fromkeys(req_ids or [node_id]))
        normalized.append(record)
    return normalized


def _merge_overlay_records(
    records: list[dict[str, Any]],
    overlay: list[dict[str, Any]],
    *,
    req_id: str | None = None,
) -> list[dict[str, Any]]:
    by_id = {
        str(record.get("interface_id") or "").strip(): record
        for record in records
        if str(record.get("interface_id") or "").strip()
    }
    for record in overlay:
        req_ids = {
            str(value).strip()
            for value in record.get("req_ids") or []
            if str(value or "").strip()
        }
        interface_id = str(record.get("interface_id") or "").strip()
        if interface_id and (req_id is None or req_id in req_ids):
            by_id[interface_id] = record
    return list(by_id.values())


def _json_records(records: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "count": len(records),
            "interfaces": records,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _json_error(message: str) -> str:
    return json.dumps({"error": message, "count": 0, "interfaces": []}, ensure_ascii=False, indent=2)


def _normalize_limit(value: Any) -> int:
    try:
        parsed = int(value or 20)
    except (TypeError, ValueError):
        parsed = 20
    return max(1, min(parsed, 100))


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result

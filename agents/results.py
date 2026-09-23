from __future__ import annotations

from typing import Any

from agents.runtime.runners import salvage_json_objects, text_from_raw_dump


def normalize_test_manifest_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize structured or fallback agent output into test manifest items."""

    candidates = payload.get("tests")
    if candidates is None:
        candidates = payload.get("items")
    if candidates is None and _looks_like_test_item(payload):
        candidates = [payload]
    if candidates is None:
        # No tests/items key at all: the answer may still carry a complete
        # manifest that every parser rule missed (reasoning-prefix shapes).
        # An explicit ``tests: []`` never reaches this branch — a model that
        # deliberately returned an empty manifest is a valid answer.
        candidates = _salvage_test_rows(payload)
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict) and _looks_like_test_item(item)]


def _salvage_test_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover manifest rows from the final-message text the fallback preserved.

    Only ``tests``/``items`` container objects count as a manifest; the
    quote-aware scanner also emits their depth-1 row objects beside the
    root, and standalone prose-side objects are not manifest evidence.
    """

    for source in _salvage_source_texts(payload):
        rows: list[dict[str, Any]] = []
        for obj in salvage_json_objects(source):
            nested = obj.get("tests")
            if not isinstance(nested, list):
                nested = obj.get("items")
            if isinstance(nested, list):
                rows.extend(
                    item for item in nested if isinstance(item, dict) and _looks_like_test_item(item)
                )
        if rows:
            return rows
    return []


def _salvage_source_texts(payload: dict[str, Any]) -> list[str]:
    """The preserved final-message texts, in scan order, unwrapped as needed.

    ``summary`` carries the plain final text; ``_raw_final_message`` is a
    truncated JSON debug dump whose embedded payload is escaped inside a
    string value, so it goes through ``text_from_raw_dump`` before scanning.
    """

    sources: list[str] = []
    summary = str(payload.get("summary") or "")
    if summary.strip():
        sources.append(summary)
    raw = str(payload.get("_raw_final_message") or "")
    if raw.strip():
        sources.append(text_from_raw_dump(raw))
    return sources


def _looks_like_test_item(value: dict[str, Any]) -> bool:
    return bool(
        str(value.get("test_id", "")).strip()
        or str(value.get("file_path", "")).strip()
        or str(value.get("type", "")).strip()
    )

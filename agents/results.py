from __future__ import annotations

from typing import Any

from agents.runtime.runners import salvage_json_objects, text_from_raw_dump


def _manifest_candidates(payload: dict[str, Any]) -> Any:
    """The payload's declared test-manifest container, if any."""

    candidates = payload.get("tests")
    if candidates is None:
        candidates = payload.get("items")
    return candidates


def normalize_test_manifest_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize structured or fallback agent output into test manifest items."""

    candidates = _manifest_candidates(payload)
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


def payload_declares_test_manifest(payload: dict[str, Any]) -> bool:
    """True when the payload itself carries a parseable test-manifest structure.

    ``normalize_test_manifest_payload`` collapses "the payload carries no
    manifest structure at all" (prose fallback, damaged JSON) and "the model
    returned an explicit empty manifest" into the same ``[]``. Callers that
    must treat those differently — an unparseable answer is a retryable
    defect, a declared-empty manifest is the model's decision — ask this
    first. Undeclared shapes: no ``tests``/``items`` container at all, a
    container that is not a list, and a list none of whose items parse as
    test entries (a declared-one-parsed-zero answer is parse damage, not a
    decision to return zero tests). Salvage rows recovered from the final
    message do NOT count as declared: they are parser recovery, not a
    manifest the model itself answered with.
    """

    candidates = _manifest_candidates(payload)
    if candidates is None:
        return _looks_like_test_item(payload)
    if not isinstance(candidates, list):
        return False
    return not candidates or any(
        isinstance(item, dict) and _looks_like_test_item(item) for item in candidates
    )


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

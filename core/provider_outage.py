"""Run-level provider outage state and recovery helpers.

The model adapter owns one-call retries and classifies provider failures. This
module owns the durable run-level consequence: aggregate one outage fingerprint
inside a bounded time window, pause new work once the threshold is reached,
and retain enough provider metadata for a later ``--resume`` health check.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

ARC_PROVIDER_OUTAGE_THRESHOLD = "ARC_PROVIDER_OUTAGE_THRESHOLD"
ARC_PROVIDER_OUTAGE_WINDOW_SECONDS = "ARC_PROVIDER_OUTAGE_WINDOW_SECONDS"

DEFAULT_PROVIDER_OUTAGE_THRESHOLD = 1
DEFAULT_PROVIDER_OUTAGE_WINDOW_SECONDS = 300

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_PROVIDER_OUTAGE = "PROVIDER_OUTAGE"

OUTAGE_OBSERVED = "OBSERVED"
OUTAGE_OPEN = "OPEN"
OUTAGE_RECOVERED = "RECOVERED"


def provider_outage_threshold() -> int:
    """Return the run-level outage threshold; ``0`` disables the gate."""

    raw = os.environ.get(ARC_PROVIDER_OUTAGE_THRESHOLD, "").strip()
    if not raw:
        return DEFAULT_PROVIDER_OUTAGE_THRESHOLD
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_PROVIDER_OUTAGE_THRESHOLD


def provider_outage_window_seconds() -> int:
    """Return the wall-clock aggregation window for one outage fingerprint."""

    raw = os.environ.get(ARC_PROVIDER_OUTAGE_WINDOW_SECONDS, "").strip()
    if not raw:
        return DEFAULT_PROVIDER_OUTAGE_WINDOW_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_PROVIDER_OUTAGE_WINDOW_SECONDS


def build_provider_outage_state(
    fingerprints: dict[str, Any] | None,
    details: dict[str, Any],
    *,
    threshold: int | None = None,
    window_seconds: int | None = None,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any], dict[str, dict[str, Any]], bool]:
    """Compute one outage transition without mutating queue state.

    A new fingerprint or an observation outside the previous window starts a
    fresh count. The returned map is the durable fingerprint projection, and
    the boolean indicates whether this observation started a new window. The
    state deliberately stores no credential material.
    """

    observed_at = _ensure_utc(now or datetime.now(timezone.utc))
    threshold_value = provider_outage_threshold() if threshold is None else max(0, int(threshold))
    window_value = (
        provider_outage_window_seconds()
        if window_seconds is None
        else max(1, int(window_seconds))
    )
    fingerprint = str(details.get("fingerprint") or "").strip()
    fingerprints = {
        str(key): dict(value)
        for key, value in (fingerprints or {}).items()
        if isinstance(value, dict)
    }
    fingerprint_previous = fingerprints.get(fingerprint)
    same_window = (
        isinstance(fingerprint_previous, dict)
        and str(fingerprint_previous.get("status") or "").upper() != OUTAGE_RECOVERED
        and _within_window(fingerprint_previous.get("last_seen_at"), observed_at, window_value)
    )
    failure_count = int(fingerprint_previous.get("failure_count") or 0) + 1 if same_window else 1
    opened = threshold_value > 0 and failure_count >= threshold_value

    state = {
        "status": OUTAGE_OPEN if opened else OUTAGE_OBSERVED,
        "fingerprint": fingerprint,
        "provider": _text(details.get("provider")),
        "base_url": _text(details.get("base_url")),
        "model": _text(details.get("model")),
        "api_mode": _text(details.get("api_mode")),
        "error_category": _text(details.get("error_category")) or "provider_outage",
        "error_type": _text(details.get("error_type")),
        "status_code": details.get("status_code"),
        "message": _text(details.get("message")),
        "failure_count": failure_count,
        "threshold": threshold_value,
        "window_seconds": window_value,
        "first_seen_at": (
            str(fingerprint_previous.get("first_seen_at"))
            if same_window and fingerprint_previous.get("first_seen_at")
            else _format_timestamp(observed_at)
        ),
        "last_seen_at": _format_timestamp(observed_at),
        "tripped_at": (
            str(fingerprint_previous.get("tripped_at"))
            if same_window and fingerprint_previous.get("tripped_at")
            else (_format_timestamp(observed_at) if opened else None)
        ),
        "last_health_check_at": (
            str(fingerprint_previous.get("last_health_check_at"))
            if same_window and fingerprint_previous.get("last_health_check_at")
            else None
        ),
        "last_health_check_ok": None,
        "recovered_at": None,
    }
    fingerprints[fingerprint] = state
    return opened, state, fingerprints, not same_window


def build_provider_outage_health_state(
    previous: dict[str, Any] | None,
    *,
    healthy: bool,
    now: datetime | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Compute the durable result of a resume-time health check."""

    observed_at = _ensure_utc(now or datetime.now(timezone.utc))
    state = dict(previous) if isinstance(previous, dict) else {}
    state["last_health_check_at"] = _format_timestamp(observed_at)
    state["last_health_check_ok"] = bool(healthy)
    if message:
        state["health_check_message"] = _text(message)
    if healthy:
        state["status"] = OUTAGE_RECOVERED
        state["recovered_at"] = _format_timestamp(observed_at)
    else:
        state["status"] = OUTAGE_OPEN
    return state


def current_open_provider_outage(
    fingerprints: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return one currently open fingerprint state, if any."""

    for candidate in (fingerprints or {}).values():
        if isinstance(candidate, dict) and str(candidate.get("status") or "").upper() == OUTAGE_OPEN:
            return dict(candidate)
    return None


def _within_window(raw_timestamp: Any, now: datetime, window_seconds: int) -> bool:
    parsed = _parse_timestamp(raw_timestamp)
    if parsed is None:
        return False
    age = (now - parsed).total_seconds()
    return 0 <= age <= window_seconds


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _ensure_utc(value).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()

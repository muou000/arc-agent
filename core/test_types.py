"""Shared test-layer vocabulary used by manifest generation and TDD."""

from __future__ import annotations

from typing import Any


CANONICAL_TEST_TYPES = ("Unit", "Integration", "E2E")


def canonical_test_type(value: Any) -> str | None:
    """Return the canonical test-layer name for a case-insensitive value."""

    normalized = str(value or "").strip().lower()
    return next(
        (test_type for test_type in CANONICAL_TEST_TYPES if normalized == test_type.lower()),
        None,
    )

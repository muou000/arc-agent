"""The stage-agent session step budget must be bounded and overridable.

A runaway agent session (the model repeatedly re-editing a file it just
corrupted) used to be bounded only by ``recursion_limit=5000`` - roughly hours
of model calls on a single node. The default now reflects the empirical 12306
benchmark distribution (healthy sessions <= ~150 steps, pathological > 450),
and operators can raise it for a run via ``ARC_AGENT_RECURSION_LIMIT``.
"""

from __future__ import annotations

import pytest

from agents.runtime.runners import (
    DEFAULT_RECURSION_LIMIT,
    _MIN_RECURSION_LIMIT,
    _resolve_recursion_limit,
    build_agent_config,
)


def test_default_limit_is_bounded_below_legacy_5000() -> None:
    assert DEFAULT_RECURSION_LIMIT < 5000
    assert build_agent_config("t")["recursion_limit"] == DEFAULT_RECURSION_LIMIT


def test_env_override_raises_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_AGENT_RECURSION_LIMIT", "1500")
    assert _resolve_recursion_limit() == 1500
    assert build_agent_config("t")["recursion_limit"] == 1500


def test_invalid_or_tiny_values_fall_back_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for raw in ("", "abc", "-5", "0"):
        monkeypatch.setenv("ARC_AGENT_RECURSION_LIMIT", raw)
        assert _resolve_recursion_limit() >= _MIN_RECURSION_LIMIT

    monkeypatch.setenv("ARC_AGENT_RECURSION_LIMIT", "5")
    assert _resolve_recursion_limit() == _MIN_RECURSION_LIMIT

    monkeypatch.delenv("ARC_AGENT_RECURSION_LIMIT")
    assert _resolve_recursion_limit() == DEFAULT_RECURSION_LIMIT


def test_thread_id_still_reaches_the_config() -> None:
    config = build_agent_config("proj:REQ-1.1:IMPLEMENT")
    assert config["configurable"]["thread_id"] == "proj:REQ-1.1:IMPLEMENT"

"""Scheduling switches must never leak into unit tests from the host (issue #146).

``core.workflow`` runs ``load_project_env()`` at import time, copying the
repository ``.env`` into ``os.environ`` (without overriding existing
variables). When a host ``.env`` or shell sets one of the scheduling switches,
tests that assert default scheduling semantics
(``test_parallel_scheduling_rules``, ``test_parallel_worktree_drain``) go red
in a batch even though the code is fine. The autouse
``isolate_scheduling_switches`` fixture in ``tests/conftest.py`` deletes the
switches before every test; tests that exercise a switch explicitly
``monkeypatch.setenv`` over it (the fixture runs first, so the explicit value
wins).
"""

from __future__ import annotations

import os

import pytest

from core import workflow
from tests.conftest import SCHEDULING_SWITCH_ENV_VARS


@pytest.mark.parametrize("name", SCHEDULING_SWITCH_ENV_VARS)
def test_scheduling_switch_never_inherits_a_host_value(name: str) -> None:
    """The autouse scrub must have removed the switch before the test body."""

    value = os.environ.get(name)
    assert not value, (
        f"{name}={value!r} leaked from the host environment into the test "
        "process; scheduling-semantics tests assert default behaviour and "
        "would false-red in a batch (issue #146). Check "
        "isolate_scheduling_switches in tests/conftest.py."
    )


def test_scheduling_helpers_fall_back_to_defaults() -> None:
    """With no switch in the environment the helpers yield default semantics."""

    assert workflow._worktrees_enabled() is True
    assert workflow._affinity_depth() == 1
    assert workflow._design_pipelining_enabled() is False


def test_explicit_test_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that pin a switch explicitly keep working over the scrub."""

    monkeypatch.setenv("ARC_AFFINITY_DEPTH", "2")
    monkeypatch.setenv("ARC_DESIGN_GATE_PIPELINE", "1")
    assert workflow._affinity_depth() == 2
    assert workflow._design_pipelining_enabled() is True

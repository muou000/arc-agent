"""Shared pytest fixtures for arc-bench agent tests.

All fixtures isolate tests into a temporary project directory and clear the
ARC-Bench environment variables that `RuntimePaths.from_env()` reads so that
tests are deterministic.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from arcbench_agent_runtime import AgentRuntime


# Names of environment variables that influence path resolution. They are
# cleared (or overridden) so tests cannot accidentally inherit values from
# the developer's shell.
ENV_VARS_TO_CLEAR = (
    "ARCBENCH_OUTPUT_DIR",
    "ARCBENCH_PROJECT_DIR",
    "ARCBENCH_TEMPLATE_DIR",
    "ARCBENCH_RUNNER_EVENTS_PATH",
    "ARCBENCH_TRACEABILITY_DIR",
    "ARC_GIT_USER_NAME",
    "ARC_GIT_USER_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove any ARC-Bench environment variables that could leak from the host."""

    for key in ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def tmp_project_dir(tmp_path: Path, clean_env: None) -> Iterator[Path]:
    """Yield a fresh project directory with no ARC artefacts present.

    The returned ``Path`` is empty; tests are responsible for instantiating the
    runtime or running CLI scripts against it.
    """

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    yield project_dir


@pytest.fixture
def runtime(tmp_project_dir: Path) -> AgentRuntime:
    """Yield an ``AgentRuntime`` wired to ``tmp_project_dir`` with default paths."""

    return AgentRuntime.from_env(project_dir=str(tmp_project_dir))
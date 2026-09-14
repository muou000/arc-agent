"""Fixtures for agent-level e2e tests.

Builds on the shared fixtures from ``tests/conftest.py`` (``clean_env``,
``tmp_project_dir``) and additionally publishes the process-wide ARC runtime
(``core.service.configure_runtime``) and workspace root so that
``context_pipeline``, ``core.sessions`` and ``WorkflowPhaseRunner.traceability``
all resolve inside the temporary project directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.context.pipeline import context_pipeline
from agents.runtime.checkpointer import reset_checkpointer
from core import config as core_config
from core.service import configure_runtime, reset_runtime_for_tests


@pytest.fixture
def arc_runtime(tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Publish a real AgentRuntime bound to ``tmp_project_dir``.

    ``tmp_project_dir`` doubles as the agent workspace, so ``/workspace/...``
    virtual paths inside the deep-agents filesystem map to this directory.

    Provider env vars (``OPENAI_BASE_URL`` and friends) are scrubbed by the
    autouse ``isolate_model_env`` fixture in ``tests/conftest.py``, because
    ``core.workflow`` runs ``load_project_env()`` at import time and a host
    ``.env`` leaking ``OPENAI_BASE_URL`` would flip the ``response_format``
    capability decision (or trigger a real probe) and change agent behaviour
    mid-suite.
    """

    root = tmp_project_dir.resolve()
    monkeypatch.setattr(core_config, "_workspace_root", root)
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(root))
    reset_checkpointer()
    runtime = configure_runtime(project_dir=str(root))
    yield runtime
    reset_runtime_for_tests()
    reset_checkpointer()
    context_pipeline.cache.clear()

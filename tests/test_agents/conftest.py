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

    OpenAI env vars are scrubbed because ``core.workflow`` runs
    ``load_project_env()`` at import time: a host ``.env`` leaking
    ``OPENAI_BASE_URL`` would make ``build_stage_agent`` silently drop
    ``response_format`` and change agent behaviour mid-suite.
    """

    for key in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_project_dir.resolve()
    monkeypatch.setattr(core_config, "_workspace_root", root)
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(root))
    reset_checkpointer()
    runtime = configure_runtime(project_dir=str(root))
    yield runtime
    reset_runtime_for_tests()
    reset_checkpointer()
    context_pipeline.cache.clear()

"""Tests for ``arcbench_agent_runtime.context.RuntimePaths``."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcbench_agent_runtime.context import (
    DEFAULT_PROJECT_DIR,
    DEFAULT_RUNNER_EVENTS_PATH,
    DEFAULT_TRACEABILITY_DIR,
    RuntimePaths,
)


class TestRuntimePathsDefaults:
    def test_defaults_when_nothing_provided(self, clean_env: None) -> None:
        paths = RuntimePaths.from_env()
        assert paths.project_dir == Path(DEFAULT_PROJECT_DIR).expanduser().resolve()
        assert paths.runner_events_path == (
            Path(DEFAULT_PROJECT_DIR).expanduser().resolve() / DEFAULT_RUNNER_EVENTS_PATH
        )
        assert paths.traceability_dir == (
            Path(DEFAULT_PROJECT_DIR).expanduser().resolve() / DEFAULT_TRACEABILITY_DIR
        )

    def test_explicit_args_override_defaults(self, tmp_project_dir: Path) -> None:
        events = tmp_project_dir / "custom-events.jsonl"
        trace = tmp_project_dir / "custom-trace"
        paths = RuntimePaths.from_env(
            project_dir=str(tmp_project_dir),
            runner_events_path=str(events),
            traceability_dir=str(trace),
        )
        assert paths.project_dir == tmp_project_dir
        assert paths.runner_events_path == events
        assert paths.traceability_dir == trace


class TestRuntimePathsEnvPriority:
    def test_output_dir_takes_priority(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
        monkeypatch.setenv("ARCBENCH_OUTPUT_DIR", str(a))
        monkeypatch.setenv("ARCBENCH_PROJECT_DIR", str(b))
        monkeypatch.setenv("ARCBENCH_TEMPLATE_DIR", str(c))
        paths = RuntimePaths.from_env()
        assert paths.project_dir == a.resolve()

    def test_project_dir_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        b, c = tmp_path / "b", tmp_path / "c"
        monkeypatch.setenv("ARCBENCH_PROJECT_DIR", str(b))
        monkeypatch.setenv("ARCBENCH_TEMPLATE_DIR", str(c))
        paths = RuntimePaths.from_env()
        assert paths.project_dir == b.resolve()

    def test_template_dir_lowest_priority(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        c = tmp_path / "c"
        monkeypatch.setenv("ARCBENCH_TEMPLATE_DIR", str(c))
        paths = RuntimePaths.from_env()
        assert paths.project_dir == c.resolve()

    def test_explicit_arg_beats_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env_dir = tmp_path / "from-env"
        explicit = tmp_path / "explicit"
        monkeypatch.setenv("ARCBENCH_OUTPUT_DIR", str(env_dir))
        paths = RuntimePaths.from_env(project_dir=str(explicit))
        assert paths.project_dir == explicit.resolve()


class TestRuntimePathsRelativeResolution:
    def test_relative_runner_events_resolves_under_project(
        self, tmp_project_dir: Path
    ) -> None:
        paths = RuntimePaths.from_env(project_dir=str(tmp_project_dir))
        assert paths.runner_events_path == tmp_project_dir / DEFAULT_RUNNER_EVENTS_PATH

    def test_relative_traceability_resolves_under_project(
        self, tmp_project_dir: Path
    ) -> None:
        paths = RuntimePaths.from_env(project_dir=str(tmp_project_dir))
        assert paths.traceability_dir == tmp_project_dir / DEFAULT_TRACEABILITY_DIR

    def test_absolute_paths_are_preserved(self, tmp_path: Path) -> None:
        abs_events = tmp_path / "abs-events.jsonl"
        abs_trace = tmp_path / "abs-trace"
        paths = RuntimePaths.from_env(
            project_dir=str(tmp_path),
            runner_events_path=str(abs_events),
            traceability_dir=str(abs_trace),
        )
        assert paths.runner_events_path == abs_events
        assert paths.traceability_dir == abs_trace


class TestEnsureParentDirs:
    def test_creates_all_required_directories(self, tmp_project_dir: Path) -> None:
        paths = RuntimePaths.from_env(
            project_dir=str(tmp_project_dir),
            runner_events_path="nested/.arc/runner-events.jsonl",
            traceability_dir="nested/.arc/traceability",
        )
        paths.ensure_parent_dirs()
        assert paths.project_dir.is_dir()
        assert paths.runner_events_path.parent.is_dir()
        assert paths.traceability_dir.is_dir()

    def test_is_idempotent(self, tmp_project_dir: Path) -> None:
        paths = RuntimePaths.from_env(project_dir=str(tmp_project_dir))
        paths.ensure_parent_dirs()
        # second call must not raise even though dirs already exist
        paths.ensure_parent_dirs()
        assert paths.project_dir.is_dir()
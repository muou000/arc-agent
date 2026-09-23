"""A test run crosses the handler/phase seam as a structured ``TestRunResult``.

The historical protocol handed callers a rendered transcript that every
consumer re-parsed with its own regexes (``parse_test_results``'s write-only
``sub_batches``, two ``_extract_exit_code`` copies, digest-side verdict
scraping). The handler now fills the result object while it still knows the
facts structurally, and the transcription is only the model-facing rendering.
These tests drive real handler runs and assert exit code, outcome partitions
and verdicts through the object interface — never by re-parsing the text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app_type_handler import web as web_handler
from app_type_handler import e2e_attempt
from app_type_handler.backend_runtime import InMemoryBackendRuntime, _CommandResult


_VITEST_FAILURE_OUTPUT = """Exit Code: 1

 FAIL tests/authApi.test.js > Auth API > rejects duplicate username
AssertionError: expected 500 to be 200
 ✓ tests/authApi.test.js > Auth API > registers a new user

Exit Code: 1
"""


_VITEST_PASSING_OUTPUT = "Exit Code: 0\nSTDOUT:\n ✓ tests/authApi.test.js > Auth API > registers a new user\n"


class _ScriptedCommands:
    """Records shell commands; each needle gets a scripted outcome."""

    def __init__(self, scripts: dict[str, tuple[int, str]]) -> None:
        self.calls: list[str] = []
        self.scripts = scripts

    async def __call__(
        self,
        command: str,
        cwd: str,
        timeout: float = 60.0,
        extra_env: dict[str, str] | None = None,
        web_port: int | None = None,
    ) -> _CommandResult:
        self.calls.append(command)
        for needle, (code, text) in self.scripts.items():
            if needle in command:
                return _CommandResult(exit_code=code, text=text)
        return _CommandResult(exit_code=0, text=f"Exit Code: 0\nSTDOUT:\n{command} ran\n")


def _make_workspace(tmp_path: Path) -> Path:
    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "src" / "app.js").write_text("console.log('v1')\n", encoding="utf-8")
    (backend / "test-e2e").mkdir()
    (backend / "test-e2e" / "login.spec.ts").write_text("test('t', () => {});\n", encoding="utf-8")
    (backend / "package.json").write_text(
        '{"name": "backend", "scripts": {"start": "node src/index.js"}}\n',
        encoding="utf-8",
    )
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "main.js").write_text("console.log('v1')\n", encoding="utf-8")
    (frontend / "package.json").write_text('{"name": "frontend"}\n', encoding="utf-8")
    return tmp_path


def _make_handler(tmp_path: Path, backend_runtime=None) -> web_handler.WebAppType:
    kwargs: dict = {}
    if backend_runtime is not None:
        kwargs["backend_runtime"] = backend_runtime
    return web_handler.WebAppType(
        workspace_path=str(tmp_path),
        requirement_path=str(tmp_path / "requirements.yaml"),
        interface_designer=None,
        log_cb=lambda *args, **kwargs: None,
        **kwargs,
    )


def test_vitest_batch_result_carries_exit_code_and_partitions(tmp_path, monkeypatch) -> None:
    """A real vitest batch run exposes its verdict structurally."""

    workspace = _make_workspace(tmp_path)
    handler = _make_handler(workspace)
    commands = _ScriptedCommands(
        {"npx vitest run tests/authApi.test.js": (1, _VITEST_FAILURE_OUTPUT)}
    )
    monkeypatch.setattr(web_handler, "_execute_web_test_command", commands)

    result = asyncio.run(
        handler.run_test_group("integration", ["backend/tests/authApi.test.js"], web_port=4321)
    )

    assert result.exit_code == 1
    assert result.passed_run is False
    assert result.failed == ["FAIL tests/authApi.test.js > Auth API > rejects duplicate username"]
    assert result.passed == ["✓ tests/authApi.test.js > Auth API > registers a new user"]
    # No frontend build participates in vitest batches: no verdicts.
    assert result.build_note == ""
    assert result.served_verdict == ""
    assert result.environment_failure == ""


def test_vitest_batch_passing_run_zero_exit_code(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    handler = _make_handler(workspace)
    commands = _ScriptedCommands(
        {"npx vitest run tests/authApi.test.js": (0, _VITEST_PASSING_OUTPUT)}
    )
    monkeypatch.setattr(web_handler, "_execute_web_test_command", commands)

    result = asyncio.run(
        handler.run_test_group("integration", ["backend/tests/authApi.test.js"], web_port=4321)
    )

    assert result.exit_code == 0
    assert result.passed_run is True
    assert result.failed == []
    assert result.passed


def test_e2e_result_carries_build_and_served_verdicts(tmp_path, monkeypatch) -> None:
    """A real E2E run states what was built and served, as object fields."""

    workspace = _make_workspace(tmp_path)
    handler = _make_handler(workspace, backend_runtime=InMemoryBackendRuntime())
    commands = _ScriptedCommands(
        {
            "db:prepare:e2e": (0, "Exit Code: 0\nSTDOUT:\nprepared\n"),
            "npx playwright test": (1, _VITEST_FAILURE_OUTPUT),
        }
    )
    monkeypatch.setattr(e2e_attempt, "_execute_web_test_command", commands)
    dist_dir = workspace / "frontend" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>built</html>\n", encoding="utf-8")

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert result.exit_code == 1
    assert result.build_note.startswith("rebuilt frontend/dist from current sources")
    assert result.served_verdict.startswith("frontend/dist/index.html present at result time")
    # The structured verdicts agree with the transcription's own verdict line.
    fingerprint = e2e_attempt._frontend_dist_fingerprint(str(workspace / "frontend"))
    assert f"fingerprint {fingerprint[:12]}" in result.served_verdict


def test_e2e_environment_failure_is_a_structural_verdict(tmp_path, monkeypatch) -> None:
    """A missing-dependency E2E failure classifies as environmental on the object."""

    workspace = _make_workspace(tmp_path)
    handler = _make_handler(workspace, backend_runtime=InMemoryBackendRuntime())
    commands = _ScriptedCommands(
        {
            "db:prepare:e2e": (0, "Exit Code: 0\nSTDOUT:\nprepared\n"),
            "npx playwright test": (
                1,
                "Exit Code: 1\nError: Cannot find module '@playwright/test'\nExit Code: 1\n",
            ),
        }
    )
    monkeypatch.setattr(e2e_attempt, "_execute_web_test_command", commands)
    dist_dir = workspace / "frontend" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>built</html>\n", encoding="utf-8")

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert result.exit_code == 1
    assert result.environment_failure == "missing dependency: @playwright/test"


def test_structural_exit_code_agrees_with_the_transcription(tmp_path, monkeypatch) -> None:
    """The structural verdict matches the run's own nested command codes."""

    workspace = _make_workspace(tmp_path)
    handler = _make_handler(workspace, backend_runtime=InMemoryBackendRuntime())
    commands = _ScriptedCommands(
        {
            "db:prepare:e2e": (0, "Exit Code: 0\nSTDOUT:\nprepared\n"),
            "npx playwright test": (2, "Exit Code: 2\nSTDOUT:\ncrashed\n"),
        }
    )
    monkeypatch.setattr(e2e_attempt, "_execute_web_test_command", commands)
    dist_dir = workspace / "frontend" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>built</html>\n", encoding="utf-8")

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert result.exit_code == 2
    # The transcription renders the same verdict the object carries,
    # beneath the group header.
    assert "Exit Code: 2\n" in result.output

"""The E2E static host must be visible to the agent and self-heal once.

The 2026-09-20 arc-output1 run died because the backend could not stat
`frontend/dist/index.html` at request time while the build cache vouched for
the artifact — and the agent had no way to see that mismatch (`dist/` is
read-denied), so it burned 22 minutes diagnosing blind. Two behaviors are
pinned here:

- every E2E result body carries a deterministic ``Served index.html:`` verdict
  line checked on disk at result time (present/absent + dist fingerprint);
- a failed E2E run whose output carries the dead static-host signature
  (``NotFoundError`` from ``send`` under a ``sendFile`` frame) triggers
  exactly one system-side recovery: forced rebuild (cache bypassed) +
  backend restart + re-run, then the failure goes to the agent.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app_type_handler import web as web_handler
from app_type_handler.backend_runtime import InMemoryBackendRuntime


# The exact stack shape from the 2026-09-20 run (paths shortened).
_SPA_DEAD_HOST_OUTPUT = """Exit Code: 1

  ✘  1 test-e2e\\register.e2e.spec.js:53:3 › REQ-1 › happy path (5.1s)

  1) test-e2e\\register.e2e.spec.js:53:3 › REQ-1 › happy path

    Error: expect(locator).toBeVisible() failed

    NotFoundError: Not Found
        at createHttpError (D:\\ws\\node_modules\\send\\index.js:861:12)
        at SendStream.pipe (D:\\ws\\node_modules\\send\\index.js:468:14)
        at sendfile (D:\\ws\\node_modules\\express\\lib\\response.js:1014:8)
        at ServerResponse.sendFile (D:\\ws\\node_modules\\express\\lib\\response.js:411:3)
        at serveSpaShell (D:\\ws\\backend\\src\\app.js:38:9)

Exit Code: 1
"""

# Same failure without the send/sendFile frames: a plain NotFoundError from
# test code (e.g. a fetch against a missing route) must NOT trigger recovery.
_PLAIN_NOT_FOUND_OUTPUT = """Exit Code: 1

  1) test-e2e\\register.e2e.spec.js:53:3 › REQ-1 › happy path

    NotFoundError: Not Found
        at fetch (node:internal/deps/undici/undici:1111:22)

Exit Code: 1
"""


class _RecoveryRecorder:
    """Stands in for shell commands; fails the first Playwright run with the
    dead-static-host signature and passes the second."""

    def __init__(self, failure_output: str) -> None:
        self.failure_output = failure_output
        self.playwright_calls = 0
        self.build_calls: list[bool] = []  # force_rebuild flags in call order

    async def __call__(
        self,
        command: str,
        cwd: str,
        timeout: float = 60.0,
        extra_env: dict[str, str] | None = None,
        web_port: int | None = None,
    ) -> web_handler._CommandResult:
        if "playwright" in command:
            self.playwright_calls += 1
            if self.playwright_calls == 1:
                return web_handler._CommandResult(exit_code=1, text=self.failure_output)
            return web_handler._CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nall green\n")
        if "npm run build" in command:
            # `_build_frontend_dist` passes force_rebuild only when bypassing
            # the cache; the reuse path never reaches the command.
            self.build_calls.append(False)
            dist_dir = Path(cwd) / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")
            return web_handler._CommandResult(exit_code=0, text="Exit Code: 0\nSTDOUT:\nvite build\n")
        return web_handler._CommandResult(exit_code=0, text=f"Exit Code: 0\nSTDOUT:\n{command} ran\n")


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


def _make_fresh_start_runtime() -> InMemoryBackendRuntime:
    """A session runtime scripted for the recovery scenarios.

    Every attempt starts fresh (the reuse probe reports not-serving) and every
    start succeeds; restarts are read off ``runtime.started``.
    """

    runtime = InMemoryBackendRuntime()
    runtime.serving = False
    return runtime


class _FailingStopRuntime(InMemoryBackendRuntime):
    """Every teardown reports a cleanup failure (injected via the interface)."""

    async def _stop(self, session, context: str) -> str:
        self.stopped.append(context)
        return "Backend runtime cleanup failed: test injected"



def test_e2e_result_carries_serving_verdict_present(tmp_path, monkeypatch) -> None:
    """A healthy dist yields a present verdict naming the absolute path."""

    workspace = _make_workspace(tmp_path)
    dist_dir = workspace / "frontend" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>built</html>\n", encoding="utf-8")

    handler = _make_handler(workspace, backend_runtime=_make_fresh_start_runtime())
    recorder = _RecoveryRecorder("Exit Code: 1\nSTDOUT:\nunrelated\n")
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    # The verdict's fingerprint is computed at result time over whatever the
    # build left on disk (the stubbed build rewrites index.html), so derive
    # the expectation from the same on-disk state.
    fingerprint = web_handler._frontend_dist_fingerprint(str(workspace / "frontend"))
    assert fingerprint is not None
    expected = (
        f"Served index.html: {dist_dir / 'index.html'} (present, fingerprint {fingerprint[:12]})"
    )
    assert expected in result.output
    # The verdict sits with the build section, before the runtime env section.
    assert result.output.index("=== Frontend Build ===") < result.output.index(expected) < result.output.index(
        "=== E2E Runtime Env ==="
    )


def test_e2e_result_carries_serving_verdict_absent(tmp_path, monkeypatch) -> None:
    """A missing dist yields an absent verdict — the agent sees the mismatch."""

    workspace = _make_workspace(tmp_path)
    # The build stub never produces dist (frontend build "succeeds" per exit
    # code but the artifact stays missing is impossible through the cache
    # contract, so drive the verdict through a forced-absent workspace: the
    # recorded build is created, then the artifact is removed before the
    # result assembly checks it.

    handler = _make_handler(workspace, backend_runtime=_make_fresh_start_runtime())

    async def _fake_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        # Build "succeeds" but the artifact vanishes right after: this is the
        # exact race the verdict exists to expose (builder says OK, disk says
        # no). The verdict is checked at result-assembly time, after this.
        return web_handler._FrontendBuildOutcome(
            ok=True,
            note="rebuilt frontend/dist from current sources (fingerprint abc123def456)",
            output="Built `frontend/dist` from the current sources (fingerprint abc123def456).\n",
            exit_code=0,
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)
    recorder = _RecoveryRecorder("Exit Code: 1\nSTDOUT:\nunrelated\n")
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    expected = f"Served index.html: {workspace / 'frontend' / 'dist' / 'index.html'} (absent)"
    assert expected in result.output


def test_dead_static_host_signature_detected() -> None:
    assert web_handler._is_spa_static_host_failure(_SPA_DEAD_HOST_OUTPUT)
    assert not web_handler._is_spa_static_host_failure(_PLAIN_NOT_FOUND_OUTPUT)
    assert not web_handler._is_spa_static_host_failure("")
    assert not web_handler._is_spa_static_host_failure("Exit Code: 1\nSTDERR:\nboom\n")


def test_signature_does_not_splice_two_error_blocks() -> None:
    """Playwright separates error blocks with a blank line; frames must not
    jump across it. A NotFoundError block without send frames followed by a
    DIFFERENT error's block that happens to carry send/sendFile frames must
    not trigger the recovery (the reviewer's cross-stack splice scenario)."""

    two_blocks = (
        "  1) spec.ts:10 › unrelated route 404\n\n"
        "    NotFoundError: Not Found\n"
        "        at fetch (node:internal)\n"
        "\n"
        "  2) spec.ts:20 › other failure\n\n"
        "    TypeError: something else\n"
        "        at createHttpError (D:\\ws\\node_modules\\send\\index.js:861:12)\n"
        "        at sendfile (D:\\ws\\node_modules\\express\\lib\\response.js:1014:8)\n"
        "        at ServerResponse.sendFile (D:\\ws\\node_modules\\express\\lib\\response.js:411:3)\n"
    )
    assert not web_handler._is_spa_static_host_failure(two_blocks)


def test_dead_static_host_triggers_exactly_one_recovery(tmp_path, monkeypatch) -> None:
    """First failure with the signature: forced rebuild + restart + one re-run.

    The retried attempt's result is authoritative and the preamble keeps the
    first attempt for audit.
    """

    workspace = _make_workspace(tmp_path)
    runtime = _make_fresh_start_runtime()
    handler = _make_handler(workspace, backend_runtime=runtime)

    build_calls: list[bool] = []

    async def _fake_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        build_calls.append(force_rebuild)
        # Second (forced) build produces the artifact.
        if force_rebuild:
            dist_dir = Path(workspace_path) / "frontend" / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "index.html").write_text("<html>rebuilt</html>\n", encoding="utf-8")
        return web_handler._FrontendBuildOutcome(
            ok=True,
            note="rebuilt frontend/dist from current sources (fingerprint abc123def456)",
            output="Built `frontend/dist` from the current sources (fingerprint abc123def456).\n",
            exit_code=0,
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)
    recorder = _RecoveryRecorder(_SPA_DEAD_HOST_OUTPUT)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    # One recovery: two playwright runs, two backend starts, second build forced.
    assert recorder.playwright_calls == 2
    assert runtime.started == [4321, 4321]
    assert build_calls == [False, True]
    # The recovery is visible; the retried attempt leads and the failed one
    # survives as the superseded appendix.
    assert "=== SPA Static-Host Recovery Retry ===" in result.output
    assert "First attempt (superseded, kept for the failure evidence):" in result.output
    assert "Exit Code (superseded by the recovery retry): 1" in result.output
    # The overall exit code comes from the retried (passing) attempt.
    assert result.exit_code == 0
    assert "all green" in result.output
    # The one-shot budget is spent for this handler instance.
    assert handler._spa_static_host_recovery_used is True


def test_plain_not_found_failure_gets_no_recovery(tmp_path, monkeypatch) -> None:
    """A NotFoundError without the send/sendFile frames is the agent's problem."""

    workspace = _make_workspace(tmp_path)
    runtime = _make_fresh_start_runtime()
    handler = _make_handler(workspace, backend_runtime=runtime)

    async def _fake_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        dist_dir = Path(workspace_path) / "frontend" / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")
        return web_handler._FrontendBuildOutcome(
            ok=True,
            note="rebuilt frontend/dist from current sources (fingerprint abc123def456)",
            output="Built `frontend/dist` from the current sources (fingerprint abc123def456).\n",
            exit_code=0,
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)
    recorder = _RecoveryRecorder(_PLAIN_NOT_FOUND_OUTPUT)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert recorder.playwright_calls == 1
    assert runtime.started == [4321]
    assert "SPA Static-Host Recovery Retry" not in result.output
    assert handler._spa_static_host_recovery_used is False
    assert result.exit_code == 1


def test_recovery_budget_is_one_across_calls(tmp_path, monkeypatch) -> None:
    """A second dead-host failure in the same handler gets no second recovery."""

    workspace = _make_workspace(tmp_path)
    runtime = _make_fresh_start_runtime()
    handler = _make_handler(workspace, backend_runtime=runtime)

    async def _fake_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        dist_dir = Path(workspace_path) / "frontend" / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")
        return web_handler._FrontendBuildOutcome(
            ok=True,
            note="rebuilt frontend/dist from current sources (fingerprint abc123def456)",
            output="Built `frontend/dist` from the current sources (fingerprint abc123def456).\n",
            exit_code=0,
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)

    class _AlwaysDeadHost(_RecoveryRecorder):
        async def __call__(self, command, cwd, timeout=60.0, extra_env=None, web_port=None):
            result = await super().__call__(command, cwd, timeout, extra_env, web_port)
            if "playwright" in command:
                # Every playwright run fails with the signature.
                return web_handler._CommandResult(exit_code=1, text=self.failure_output)
            return result

    recorder = _AlwaysDeadHost(_SPA_DEAD_HOST_OUTPUT)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))
    assert handler._spa_static_host_recovery_used is True
    assert recorder.playwright_calls == 2  # first attempt + one recovery re-run

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))
    # The second run_test_group call: no further recovery, one playwright run.
    assert recorder.playwright_calls == 3


def test_recovery_retried_pass_with_failed_cleanup_reports_failure(tmp_path, monkeypatch) -> None:
    """A retried attempt that passes while its own cleanup failed is a failure.

    The recovery path re-checks the retried body (not the first attempt's
    appendix) for the cleanup-failure self-check, so a retried Exit Code: 0
    with "Backend runtime cleanup failed:" in the RETRIED body must flip to
    Exit Code: 1; a cleanup failure mentioned only in the superseded first
    attempt must not poison the retried verdict.
    """

    workspace = _make_workspace(tmp_path)
    runtime = _FailingStopRuntime()
    runtime.serving = False
    handler = _make_handler(workspace, backend_runtime=runtime)

    async def _fake_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        dist_dir = Path(workspace_path) / "frontend" / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text("<html></html>\n", encoding="utf-8")
        return web_handler._FrontendBuildOutcome(
            ok=True,
            note="rebuilt frontend/dist from current sources (fingerprint abc123def456)",
            output="Built `frontend/dist` from the current sources (fingerprint abc123def456).\n",
            exit_code=0,
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)

    recorder = _RecoveryRecorder(_SPA_DEAD_HOST_OUTPUT)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    # The recovery ran and its cleanup note failed.
    assert "=== SPA Static-Host Recovery Retry ===" in result.output
    assert "Backend runtime cleanup failed: test injected" in result.output
    # The retried attempt passed but its cleanup failed: overall exit is 1.
    assert "all green" in result.output
    assert result.exit_code == 1
    # Only one Exit Code line was rewritten to 1 (the retried leading one);
    # the superseded first attempt keeps its labeled form.
    assert "Exit Code (superseded by the recovery retry): 1" in result.output

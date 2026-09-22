"""The session-scoped E2E backend runtime must be reused across TDD attempts.

Every `run_tests(E2E)` call used to pay a full backend stop/start cycle
(`db:prepare:e2e` + `npm run start` + TCP wait + teardown), even inside one
TDD fix loop whose consecutive attempts target the same batch. The backend
runtime is now kept alive between attempts and reused while the backend
sources, port and E2E database are unchanged. The per-attempt database reset
happens at row level (`db:prepare:e2e` deletes the file, which cannot run
under a live server) with a `db:seed` re-run, and the runtime is shut down
when the node's IMPLEMENT phase finishes.

The lifecycle is owned by `app_type_handler.backend_runtime.BackendRuntime`;
the web handler drives it through the three interface actions (`ensure`,
`reset_db`, `terminate`). These tests inject `InMemoryBackendRuntime` (or the
process adapter with a stubbed command runner) and assert through the
interface instead of monkeypatching session privates. The only stubbed web
handler edges are scenario scaffolding that is not session state: the
frontend build outcome and the test command runner.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import sqlite3
from pathlib import Path

import pytest

from app_type_handler import backend_runtime as backend_runtime_module
from app_type_handler import web as web_handler
from app_type_handler.backend_runtime import (
    BackendSession,
    InMemoryBackendRuntime,
    ProcessBackendRuntime,
    _build_e2e_runtime_env,
    _CommandResult,
    _ProcessOutputTail,
    _reset_sqlite_database_rows,
    _wait_for_http_server,
    backend_source_fingerprint,
)


class _CommandRecorder:
    """Stands in for the web handler's shell-command edge, recording calls."""

    def __init__(self, exit_codes: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self.exit_codes = exit_codes or {}

    async def __call__(
        self,
        command: str,
        cwd: str,
        timeout: float = 60.0,
        extra_env: dict[str, str] | None = None,
        web_port: int | None = None,
    ) -> _CommandResult:
        self.calls.append(command)
        code = 0
        for needle, value in self.exit_codes.items():
            if needle in command:
                code = value
                break
        return _CommandResult(
            exit_code=code,
            text=f"Exit Code: {code}\nSTDOUT:\n{command} ran\n",
        )


def _make_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a minimal web workspace; return (workspace, backend fingerprint)."""

    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "src" / "app.js").write_text("console.log('v1')\n", encoding="utf-8")
    (backend / "test-e2e").mkdir()
    (backend / "test-e2e" / "login.spec.ts").write_text("test('t', () => {});\n", encoding="utf-8")
    (backend / "package.json").write_text(
        '{"name": "backend", "scripts": {"start": "node src/index.js"}}\n',
        encoding="utf-8",
    )
    fingerprint = backend_source_fingerprint(str(backend))
    assert fingerprint is not None
    return tmp_path, fingerprint


def _make_env(workspace: Path) -> dict[str, str]:
    """The E2E runtime env of one suite, with a real sqlite file behind it."""

    env = _build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])
    return env


def _make_sqlite(db_path: str, with_data: bool = True) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        if with_data:
            connection.execute("INSERT INTO users (name) VALUES ('alice')")
        connection.commit()
    finally:
        connection.close()


def _seed_live_session(
    runtime,
    *,
    port: int,
    db_path: str,
    fingerprint: str,
    handle=None,
) -> BackendSession:
    """Register a live session on a runtime as if ``ensure`` had started it.

    Test setup only: it lets the reuse and terminate paths be driven without
    spawning; the process adapter passes a duck-typed process handle, the
    in-memory adapter leaves it ``None``.
    """

    session = BackendSession(
        handle=handle,
        port=port,
        db_path=db_path,
        fingerprint=fingerprint,
        start_command="npm run start",
        startup_detail="started",
        instance_fingerprint="launcher:seeded",
    )
    runtime._session = session
    return session


def _make_handler(tmp_path: Path, backend_runtime=...) -> web_handler.WebAppType:
    kwargs: dict = {}
    if backend_runtime is not ...:
        kwargs["backend_runtime"] = backend_runtime
    return web_handler.WebAppType(
        workspace_path=str(tmp_path),
        requirement_path=str(tmp_path / "requirements.yaml"),
        interface_designer=None,
        log_cb=lambda *args, **kwargs: None,
        **kwargs,
    )


def _patch_scaffold(monkeypatch, recorder: _CommandRecorder) -> None:
    """Stub the non-session edges of the attempt: build and command runner."""

    async def _fake_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(ok=True, note="rebuilt frontend/dist from current sources", output="build ok", exit_code=0)

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)


def _run_e2e_group(handler: web_handler.WebAppType) -> web_handler.TestRunResult:
    return asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))


# --------------------------------------------------------------------------
# The interface: policy over the three actions (in-memory adapter)
# --------------------------------------------------------------------------


def test_ensure_reuses_the_live_session_when_the_key_matches() -> None:
    """Same port + db path + fingerprint, live server: reuse, one reset, no spawn."""

    runtime = InMemoryBackendRuntime()
    env = {"ARC_E2E_DB_PATH": "suite.sqlite"}
    first = asyncio.run(runtime.ensure(4321, "suite.sqlite", "fp1", runtime_env=env))
    assert not first.reused
    assert first.session is not None
    assert runtime.started == [4321]
    assert runtime.prepared == ["suite.sqlite"]  # the fresh start prepared the db

    second = asyncio.run(runtime.ensure(4321, "suite.sqlite", "fp1", runtime_env=env))
    assert second.reused
    assert second.session is first.session
    assert runtime.started == [4321]  # no second spawn
    assert runtime.resets == ["suite.sqlite"]  # the reuse hit paid its reset


def test_ensure_rebuilds_when_the_fingerprint_changes() -> None:
    """Edited backend sources terminate the stale server and start a new one."""

    runtime = InMemoryBackendRuntime()
    env = {"ARC_E2E_DB_PATH": "suite.sqlite"}
    first = asyncio.run(runtime.ensure(4321, "suite.sqlite", "fp1", runtime_env=env))
    stale_session = first.session
    assert stale_session is not None

    second = asyncio.run(runtime.ensure(4321, "suite.sqlite", "fp2", runtime_env=env))

    assert not second.reused
    assert second.session is not stale_session
    assert runtime.started == [4321, 4321]
    assert runtime.stopped == ["Stale E2E runtime cleanup"]


def test_ensure_never_reuses_a_missing_fingerprint() -> None:
    """A None fingerprint (backend directory gone) always takes the fresh path."""

    runtime = InMemoryBackendRuntime()
    _seed_live_session(runtime, port=4321, db_path="suite.sqlite", fingerprint="fp1")

    acquisition = asyncio.run(
        runtime.ensure(4321, "suite.sqlite", None, runtime_env={"ARC_E2E_DB_PATH": "suite.sqlite"})
    )

    assert not acquisition.reused
    assert runtime.started == [4321]


def test_terminate_clears_the_session_and_records_the_context() -> None:
    runtime = InMemoryBackendRuntime()
    _seed_live_session(runtime, port=4321, db_path="suite.sqlite", fingerprint="fp1")

    note = asyncio.run(runtime.terminate("Session teardown"))

    assert note == "released"
    assert runtime.stopped == ["Session teardown"]
    assert runtime.session is None
    # A second terminate with no live session is a no-op.
    assert asyncio.run(runtime.terminate("Session teardown")) == ""
    assert runtime.stopped == ["Session teardown"]


# --------------------------------------------------------------------------
# The web handler drives the same interface (in-memory adapter injected)
# --------------------------------------------------------------------------


def test_reuses_live_server_when_nothing_changed(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = _make_env(workspace)

    runtime = InMemoryBackendRuntime()
    seeded = _seed_live_session(runtime, port=4321, db_path=env["ARC_E2E_DB_PATH"], fingerprint=fingerprint)
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    result = _run_e2e_group(handler)

    assert runtime.started == []  # nothing was spawned
    assert runtime.resets == [env["ARC_E2E_DB_PATH"]]  # the reset really ran
    assert "Reused the live backend runtime" in result.output
    # The runtime stays registered for the next attempt.
    assert runtime.session is seeded
    assert result.exit_code == 0


def test_restarts_server_when_backend_source_changes(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = _make_env(workspace)

    runtime = InMemoryBackendRuntime()
    stale = _seed_live_session(runtime, port=4321, db_path=env["ARC_E2E_DB_PATH"], fingerprint=fingerprint)
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    # The agent edited backend code between attempts.
    (workspace / "backend" / "src" / "app.js").write_text("console.log('v2')\n", encoding="utf-8")

    result = _run_e2e_group(handler)

    assert runtime.started == [4321]
    assert runtime.stopped == ["Stale E2E runtime cleanup"]
    assert runtime.session is not stale
    assert runtime.session is not None
    assert "Reused the live backend runtime" not in result.output
    assert "Deferred: the backend runtime stays alive" in result.output


def test_restarts_server_when_the_previous_process_stopped_serving(tmp_path, monkeypatch) -> None:
    """A crashed process and a TCP-only zombie both surface as not-serving.

    The reuse probe demands a real HTTP round trip; whatever made the live
    server stop answering (process exit, silent socket) sends the attempt to
    the fresh-start path.
    """

    workspace, fingerprint = _make_workspace(tmp_path)
    env = _make_env(workspace)

    runtime = InMemoryBackendRuntime()
    _seed_live_session(runtime, port=4321, db_path=env["ARC_E2E_DB_PATH"], fingerprint=fingerprint)
    runtime.serving = False
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    _run_e2e_group(handler)

    assert runtime.started == [4321]


def test_restarts_server_when_e2e_database_changes(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = _make_env(workspace)

    runtime = InMemoryBackendRuntime()
    _seed_live_session(runtime, port=4321, db_path="some-other-database.sqlite", fingerprint=fingerprint)
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    _run_e2e_group(handler)

    assert runtime.started == [4321]


def test_falls_back_to_fresh_start_when_reseeding_fails(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = _make_env(workspace)

    runtime = InMemoryBackendRuntime()
    _seed_live_session(runtime, port=4321, db_path=env["ARC_E2E_DB_PATH"], fingerprint=fingerprint)
    runtime.reset_ok = False
    runtime.reset_note = "re-seeding via `npm run db:seed` did not"
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    result = _run_e2e_group(handler)

    assert runtime.started == [4321]  # fell back to a real spawn
    assert runtime.stopped == ["Stale E2E runtime cleanup"]
    assert "Reused the live backend runtime" not in result.output
    # The reset-failure reason must survive the fallback for debuggability.
    assert "fell back to a fresh start" in result.output
    assert "db:seed" in result.output


def test_runtime_session_is_strictly_per_instance(tmp_path, monkeypatch) -> None:
    """Parallel tasks build one handler per task; sessions must not leak across."""

    workspace, fingerprint = _make_workspace(tmp_path)
    env = _make_env(workspace)

    first_runtime = InMemoryBackendRuntime()
    first = _make_handler(workspace, backend_runtime=first_runtime)
    second_runtime = InMemoryBackendRuntime()
    second = _make_handler(workspace, backend_runtime=second_runtime)
    seeded = _seed_live_session(first_runtime, port=4321, db_path=env["ARC_E2E_DB_PATH"], fingerprint=fingerprint)

    # The second instance starts blind even while the first holds a live session.
    assert second_runtime.session is None

    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)
    result = _run_e2e_group(second)

    assert second_runtime.started == [4321]
    assert second_runtime.session is not None
    assert second_runtime.session is not seeded
    # Shutting down one instance leaves the other's session in place.
    asyncio.run(second.shutdown_e2e_runtime())
    assert first_runtime.session is seeded
    assert "Exit Code: 0" in result.output


def test_fresh_run_stores_session_and_defers_cleanup(tmp_path, monkeypatch) -> None:
    workspace, _fingerprint = _make_workspace(tmp_path)

    runtime = InMemoryBackendRuntime()
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    result = _run_e2e_group(handler)

    assert runtime.started == [4321]
    assert runtime.stopped == []  # cleanup deferred, nothing terminated
    assert runtime.session is not None
    assert runtime.session.port == 4321
    assert "Deferred: the backend runtime stays alive" in result.output


def test_shutdown_e2e_runtime_terminates_the_session(tmp_path) -> None:
    runtime = InMemoryBackendRuntime()
    handler = _make_handler(tmp_path, backend_runtime=runtime)
    _seed_live_session(runtime, port=4321, db_path="unused.sqlite", fingerprint="fp")

    asyncio.run(handler.shutdown_e2e_runtime())

    assert runtime.session is None
    assert runtime.stopped == ["E2E runtime session shutdown"]
    # A second shutdown with no live session is a no-op.
    asyncio.run(handler.shutdown_e2e_runtime())
    assert runtime.stopped == ["E2E runtime session shutdown"]


# --------------------------------------------------------------------------
# Failure bodies still flow through the single renderer (#98)
# --------------------------------------------------------------------------


def test_e2e_failure_bodies_come_from_one_renderer(tmp_path, monkeypatch) -> None:
    """Build, database and backend failures render through one body assembler.

    The three premature exits of the E2E attempt used to hand-write their own
    "Frontend Build / Runtime Env / Database Prepare / Backend Runtime Command /
    Previous Cleanup" assemblies from slightly different subsets, so sections
    drifted between paths (one forgot the runtime env, another the DB label).
    All three now flow through `_render_e2e_failure_body`: each body carries
    exactly the sections the attempt had gathered, in one fixed order, and the
    pre-attempt cleanup note survives to the end.
    """

    workspace, _fingerprint = _make_workspace(tmp_path)

    async def _failing_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(
            ok=False, note="frontend build failed", output="vite: build error", exit_code=1
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _failing_build)
    build_failed = asyncio.run(_make_handler(workspace).run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert build_failed.exit_code == 1
    assert "Frontend build failed before E2E startup." in build_failed.output
    assert "=== Frontend Build ===\nvite: build error" in build_failed.output
    assert "Served index.html:" in build_failed.output
    # The build never produced a runtime env, so no env/DB/backend sections.
    assert "=== E2E Runtime Env ===" not in build_failed.output
    assert "=== Database Prepare ===" not in build_failed.output
    assert "=== Backend Runtime Command ===" not in build_failed.output

    async def _ok_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(
            ok=True, note="rebuilt frontend/dist from current sources", output="build ok", exit_code=0
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _ok_build)
    runtime = InMemoryBackendRuntime()
    runtime.prepare_ok = False
    runtime.prepare_output = "db:prepare:e2e failed"
    db_failed = asyncio.run(
        _make_handler(workspace, backend_runtime=runtime).run_test_group(
            "e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321
        )
    )

    assert db_failed.exit_code == 1
    assert "E2E database preparation failed before backend startup." in db_failed.output
    assert "=== Frontend Build ===\nbuild ok" in db_failed.output
    # The database failure knows the runtime env the attempt had built...
    assert "=== E2E Runtime Env ===" in db_failed.output
    assert "DB Path:" in db_failed.output
    assert "=== Database Prepare ===\ndb:prepare:e2e failed" in db_failed.output
    # ...but never started a backend, so no backend command section.
    assert "=== Backend Runtime Command ===" not in db_failed.output

    start_runtime = InMemoryBackendRuntime()
    start_runtime.spawn_ok = False
    start_runtime.spawn_failure_detail = "crashed on boot: ERR_MODULE_NOT_FOUND"
    backend_failed = asyncio.run(
        _make_handler(workspace, backend_runtime=start_runtime).run_test_group(
            "e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321
        )
    )

    assert backend_failed.exit_code == 1
    assert "=== Backend Runtime Command ===\nnpm run start" in backend_failed.output
    assert "STDERR:\ncrashed on boot: ERR_MODULE_NOT_FOUND" in backend_failed.output
    assert "=== Database Prepare ===\nprepared" in backend_failed.output
    assert "=== E2E Runtime Env ===" in backend_failed.output


def test_e2e_recovery_cleanup_note_survives_into_failure_bodies(tmp_path, monkeypatch) -> None:
    """A prior-attempt cleanup note stays visible when the next attempt fails.

    The recovery re-run hands the teardown evidence of the attempt before it
    into `prior_cleanup_note`; the single renderer must keep surfacing it in
    the "Previous Backend Runtime Cleanup" section no matter which stage the
    new attempt dies on (here: the build, the earliest exit).
    """

    workspace, _fingerprint = _make_workspace(tmp_path)
    handler = _make_handler(workspace, backend_runtime=InMemoryBackendRuntime())
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    async def _failing_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(
            ok=False, note="frontend build failed", output="vite: build error", exit_code=1
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _failing_build)

    stage_timer = web_handler._StageTimer()
    result, _note = asyncio.run(
        handler._run_e2e_group_attempt(
            {
                "resolved_targets": ["test-e2e/login.spec.ts"],
                "working_directory": str(workspace / "backend"),
            },
            stage_timer,
            4321,
            force_rebuild=False,
            prior_cleanup_note="released port 4321 from the previous attempt",
        )
    )

    assert result.exit_code == 1
    assert (
        "=== Previous Backend Runtime Cleanup ===\nreleased port 4321 from the previous attempt"
        in result.output
    )


def test_e2e_timeout_has_a_single_source(tmp_path, monkeypatch) -> None:
    """The E2E runner command draws its timeout from one named constant.

    The batch path used to pass a one-off ``120.0`` to the Playwright command
    while the deleted single-file path rode the helper's 60s default. The
    named constant is the single source; a path fork reintroducing a literal
    (or dropping the override back to the default) changes the recorded value
    and fails here.
    """

    workspace, _fingerprint = _make_workspace(tmp_path)
    runtime = InMemoryBackendRuntime()
    handler = _make_handler(workspace, backend_runtime=runtime)

    seen_timeouts: list[float | None] = []

    async def _recording_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None, web_port=None):
        if "playwright" in command:
            seen_timeouts.append(timeout)
        return _CommandResult(exit_code=0, text=f"Exit Code: 0\nSTDOUT:\n{command} ran\n")

    async def _ok_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(ok=True, note="rebuilt frontend/dist from current sources", output="build ok", exit_code=0)

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _ok_build)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", _recording_command)

    _run_e2e_group(handler)

    # The attempt reached the Playwright stage (fresh backend start happened)
    # and its timeout is the one named constant.
    assert runtime.started == [4321]
    assert seen_timeouts == [web_handler.E2E_RUNNER_TIMEOUT_SECONDS]


def test_single_file_e2e_request_routes_to_the_group_executor(tmp_path, monkeypatch) -> None:
    """`run_test_file("e2e", ...)` is the grouped pipeline with one target.

    E2E has exactly one executor; a single-file request must not fork a
    second command path with a divergent timeout. The Vitest single-file path
    keeps its direct execution.
    """

    workspace, _fingerprint = _make_workspace(tmp_path)
    handler = _make_handler(workspace)
    group_calls: list[tuple[str, list[str]]] = []

    async def _fake_group(test_type: str, file_paths: list[str], web_port=None):
        group_calls.append((test_type, list(file_paths)))
        return web_handler.TestRunResult(exit_code=0, output="Exit Code: 0\n")

    monkeypatch.setattr(handler, "run_test_group", _fake_group)

    result = asyncio.run(handler.run_test_file("e2e", "backend/test-e2e/login.spec.ts", web_port=4321))

    assert group_calls == [("e2e", ["backend/test-e2e/login.spec.ts"])]
    assert result.exit_code == 0


def test_e2e_result_contains_stage_timing_breakdown(tmp_path, monkeypatch) -> None:
    """Each E2E round-trip reports the wall-clock cost of its stages
    (frontend build, database prep, backend runtime, playwright), so the
    model-facing output and the persisted tdd-run log carry the cost
    breakdown that previously had to be inferred from debug-log timestamps."""

    workspace, _fingerprint = _make_workspace(tmp_path)
    runtime = InMemoryBackendRuntime()
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    result = _run_e2e_group(handler)

    assert "=== Stage Timing ===" in result.output
    # A fresh E2E run pays all four stages; each is reported as name=<n>s.
    for stage in ("frontend_build", "database_prepare", "backend_runtime", "playwright"):
        assert stage + "=" in result.output


def test_e2e_stage_timing_reports_reused_stages(tmp_path, monkeypatch) -> None:
    """A reused runtime run still renders the timing section (build reuse is
    fast, the reset shows up under database_prepare)."""

    workspace, fingerprint = _make_workspace(tmp_path)
    env = _make_env(workspace)

    runtime = InMemoryBackendRuntime()
    _seed_live_session(runtime, port=4321, db_path=env["ARC_E2E_DB_PATH"], fingerprint=fingerprint)
    handler = _make_handler(workspace, backend_runtime=runtime)
    recorder = _CommandRecorder()
    _patch_scaffold(monkeypatch, recorder)

    result = _run_e2e_group(handler)

    assert "=== Stage Timing ===" in result.output
    assert "database_prepare=" in result.output
    assert runtime.started == []  # reuse confirmed: no fresh backend start


# --------------------------------------------------------------------------
# Backend source fingerprint (the reuse input)
# --------------------------------------------------------------------------


def test_backend_fingerprint_ignores_non_server_paths(tmp_path) -> None:
    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "src" / "app.js").write_text("console.log('v1')\n", encoding="utf-8")
    (backend / "package.json").write_text("{}\n", encoding="utf-8")
    before = backend_source_fingerprint(str(backend))

    (backend / "node_modules" / "pkg").mkdir(parents=True)
    (backend / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    (backend / ".arc-test-db").mkdir()
    (backend / ".arc-test-db" / "suite-1.sqlite").write_text("x\n", encoding="utf-8")
    (backend / "test-e2e").mkdir()
    (backend / "test-e2e" / "login.spec.ts").write_text("x\n", encoding="utf-8")

    assert backend_source_fingerprint(str(backend)) == before

    (backend / "src" / "app.js").write_text("console.log('v2')\n", encoding="utf-8")
    assert backend_source_fingerprint(str(backend)) != before


def test_backend_fingerprint_missing_directory(tmp_path) -> None:
    assert backend_source_fingerprint(str(tmp_path / "absent")) is None


def test_backend_fingerprint_ignores_runtime_env(tmp_path, monkeypatch) -> None:
    """The fingerprint is a pure source hash; env values must not break reuse.

    Port/DB-path dimensions are session-key comparisons in `ensure`, so a
    per-attempt env change (e.g. a new label) must not force a restart.
    """

    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "src" / "app.js").write_text("console.log('v1')\n", encoding="utf-8")

    before = backend_source_fingerprint(str(backend))
    monkeypatch.setattr(
        backend_runtime_module,
        "build_web_runtime_env",
        lambda **kwargs: {"SOME_RUNTIME_VALUE": "different-per-attempt"},
    )
    after = backend_source_fingerprint(str(backend))

    assert before is not None
    assert before == after


# --------------------------------------------------------------------------
# Row-level database reset
# --------------------------------------------------------------------------


def test_reset_sqlite_database_rows_keeps_schema_and_resets_counters(tmp_path) -> None:
    db_path = str(tmp_path / "suite.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        connection.execute("CREATE TABLE orders (id INTEGER, user_id INTEGER)")
        connection.execute("INSERT INTO users (name) VALUES ('alice')")
        connection.execute("INSERT INTO users (name) VALUES ('bob')")
        connection.execute("INSERT INTO orders VALUES (1, 1)")
        connection.commit()
    finally:
        connection.close()

    ok, output = _reset_sqlite_database_rows(db_path)

    assert ok
    assert "users" in output
    connection = sqlite3.connect(db_path)
    try:
        users_left = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        orders_left = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        sequence_left = connection.execute("SELECT COUNT(*) FROM sqlite_sequence").fetchone()[0]
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()
    assert users_left == 0
    assert orders_left == 0
    assert sequence_left == 0
    assert {row[0] for row in tables} == {"users", "orders"}


def test_reset_sqlite_database_rows_refuses_user_triggers(tmp_path) -> None:
    """DELETE fires triggers; a wipe could leave rows a fresh prepare never has.

    An AFTER DELETE trigger writing into an already-cleared table would keep
    those rows after the wipe, so the state would diverge from a fresh
    `db:prepare:e2e` + seed. The reset must refuse and let the caller fall
    back to the file-level prepare.
    """

    db_path = str(tmp_path / "audited.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("CREATE TRIGGER users_audit AFTER DELETE ON users BEGIN INSERT INTO audit_log (note) VALUES ('user deleted'); END")
        connection.execute("INSERT INTO users (name) VALUES ('alice')")
        connection.commit()
    finally:
        connection.close()

    ok, output = _reset_sqlite_database_rows(db_path)

    assert not ok
    assert "trigger" in output.lower()
    # Nothing was touched: the caller's fresh start gets the original file.
    connection = sqlite3.connect(db_path)
    try:
        users_left = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        log_left = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        connection.close()
    assert users_left == 1
    assert log_left == 0


def test_reset_sqlite_database_rows_reports_missing_file_and_empty_schema(tmp_path) -> None:
    ok, _output = _reset_sqlite_database_rows(str(tmp_path / "absent.sqlite"))
    assert not ok

    empty_path = str(tmp_path / "empty.sqlite")
    connection = sqlite3.connect(empty_path)
    connection.close()
    ok, output = _reset_sqlite_database_rows(empty_path)
    assert not ok
    assert "no user tables" in output


class _RecordingRunner:
    """Stands in for the process adapter's command runner (seed commands)."""

    def __init__(self, exit_codes: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self.exit_codes = exit_codes or {}

    async def __call__(self, command: str, cwd: str, timeout: float = 60.0, extra_env=None, web_port=None) -> _CommandResult:
        self.calls.append(command)
        code = 0
        for needle, value in self.exit_codes.items():
            if needle in command:
                code = value
                break
        return _CommandResult(exit_code=code, text=f"Exit Code: {code}\nSTDOUT:\n{command} ok\n")


def test_process_reset_db_wipes_rows_and_reseeds(tmp_path) -> None:
    """The process adapter's reset_db: real row-level wipe, seed via the runner."""

    db_path = str(tmp_path / "suite.sqlite")
    _make_sqlite(db_path)
    runner = _RecordingRunner()
    runtime = ProcessBackendRuntime(str(tmp_path), command_runner=runner)

    ok, note = asyncio.run(runtime.reset_db({"ARC_E2E_DB_PATH": db_path}))

    assert ok
    assert runner.calls == ["npm run db:seed"]
    assert "kept alive" in note
    connection = sqlite3.connect(db_path)
    try:
        rows_left = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()
    assert rows_left == 0
    assert tables


def test_process_reset_db_reports_wipe_refusal_and_skips_seed(tmp_path) -> None:
    """A refused wipe (no schema) fails the reset without a seed attempt."""

    empty_path = str(tmp_path / "empty.sqlite")
    sqlite3.connect(empty_path).close()
    runner = _RecordingRunner()
    runtime = ProcessBackendRuntime(str(tmp_path), command_runner=runner)

    ok, note = asyncio.run(runtime.reset_db({"ARC_E2E_DB_PATH": empty_path}))

    assert not ok
    assert "not possible" in note
    assert "no user tables" in note
    assert runner.calls == []


def test_process_reset_db_reports_seed_failure(tmp_path) -> None:
    db_path = str(tmp_path / "suite.sqlite")
    _make_sqlite(db_path)
    runner = _RecordingRunner(exit_codes={"db:seed": 1})
    runtime = ProcessBackendRuntime(str(tmp_path), command_runner=runner)

    ok, note = asyncio.run(runtime.reset_db({"ARC_E2E_DB_PATH": db_path}))

    assert not ok
    assert "re-seeding" in note


# --------------------------------------------------------------------------
# Process adapter teardown
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _DeadHandle:
    """Duck-typed dead process: finalize short-circuits, no real OS process."""

    def __init__(self) -> None:
        self.pid = 4321
        self.returncode = 0  # already dead


def test_process_teardown_surfaces_retained_crash_output(tmp_path) -> None:
    """A mid-session server crash must surface through the teardown note.

    The drain tails are the only place a crashed session server's output
    survives; the process adapter's stop appends the retained tail to its
    cleanup note so report bodies carry the crash evidence.
    """

    runtime = ProcessBackendRuntime(str(tmp_path))
    stdout_tail = _ProcessOutputTail()
    stderr_tail = _ProcessOutputTail()
    with stderr_tail._lock:
        stderr_tail._chunks.extend(
            b"TypeError: Cannot read properties of undefined (reading 'type')\n"
        )
    handle = _DeadHandle()
    handle._arc_output_tails = (stdout_tail, stderr_tail, [])
    _seed_live_session(
        runtime,
        port=_free_port(),
        db_path="unused.sqlite",
        fingerprint="fp",
        handle=handle,
    )

    note = asyncio.run(runtime.terminate("Session teardown"))

    assert "Backend Process Output (session teardown)" in note
    assert "STDERR:" in note
    assert "TypeError: Cannot read properties of undefined" in note


def test_process_teardown_without_retained_output_keeps_plain_note(tmp_path) -> None:
    runtime = ProcessBackendRuntime(str(tmp_path))
    handle = _DeadHandle()
    _seed_live_session(
        runtime,
        port=_free_port(),
        db_path="unused.sqlite",
        fingerprint="fp",
        handle=handle,
    )

    note = asyncio.run(runtime.terminate("Session teardown"))

    assert "released" in note
    assert "Backend Process Output" not in note


def test_process_serving_skips_the_http_probe_for_a_dead_process(tmp_path) -> None:
    """A known-dead process answers the serving probe False immediately."""

    runtime = ProcessBackendRuntime(str(tmp_path))
    handle = _DeadHandle()
    session = _seed_live_session(
        runtime,
        port=_free_port(),
        db_path="unused.sqlite",
        fingerprint="fp",
        handle=handle,
    )

    assert asyncio.run(runtime._serving(session)) is False


def test_wait_for_http_server_accepts_any_http_response_and_rejects_silence() -> None:
    """Any complete HTTP response proves readiness; silence or refusal does not.

    A 404 must still count: an app without the template's `/api/health`
    endpoint answers HTTP all the same, and demanding 200 would disable
    reuse for it entirely.
    """

    async def _scenario() -> None:
        async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()  # the request line
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def silent_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # Accepts the socket but never answers: TCP-ready, app not serving.
            writer.close()
            await writer.wait_closed()

        http_server = await asyncio.start_server(http_handler, "127.0.0.1", 0)
        silent_server = await asyncio.start_server(silent_handler, "127.0.0.1", 0)
        # Reserve a port and free it again to get a guaranteed-closed target.
        closed_probe = await asyncio.start_server(None, "127.0.0.1", 0)
        closed_port = closed_probe.sockets[0].getsockname()[1]
        closed_probe.close()
        await closed_probe.wait_closed()
        try:
            http_port = http_server.sockets[0].getsockname()[1]
            silent_port = silent_server.sockets[0].getsockname()[1]

            assert await _wait_for_http_server("127.0.0.1", http_port, timeout=5.0)
            assert await _wait_for_http_server("127.0.0.1", silent_port, timeout=0.3) is False
            assert await _wait_for_http_server("127.0.0.1", closed_port, timeout=0.3) is False
        finally:
            http_server.close()
            await http_server.wait_closed()
            silent_server.close()
            await silent_server.wait_closed()

    asyncio.run(_scenario())


# --------------------------------------------------------------------------
# The process adapter against a real server (slow, needs node)
# --------------------------------------------------------------------------


def _require_node() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH; the backend runtime lifecycle test needs a real node process")


def _make_node_server_workspace(tmp_path: Path, stderr_marker: str = "") -> Path:
    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "package.json").write_text(
        '{"name": "backend", "scripts": {"start": "node src/server.js"}}\n',
        encoding="utf-8",
    )
    (backend / "src" / "server.js").write_text(
        (
            "const http = require('http');\n"
            "http.createServer((req, res) => res.end('ok'))"
            ".listen(process.env.ARC_WEB_PORT, '127.0.0.1');\n"
            + (f"console.error({stderr_marker!r});\n" if stderr_marker else "")
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.slow
def test_process_runtime_reuses_rebuilds_and_releases_the_port(tmp_path) -> None:
    """Acceptance: the three lifecycle verdicts against a real server process.

    Driven only through the interface — reuse hit (same key, live server),
    fingerprint-mismatch rebuild (stale server torn down, port released, new
    server up), and terminate waiting for the port release — with the database
    commands running through a stubbed runner and everything else real.

    The whole lifecycle runs in one event loop on purpose: an asyncio
    subprocess is bound to its creating loop, and production keeps a session
    on one loop (the runner's) for its whole lifetime.
    """

    _require_node()
    workspace = _make_node_server_workspace(tmp_path, stderr_marker="boot marker")
    db_path = str(tmp_path / "suite.sqlite")
    _make_sqlite(db_path)
    runner = _RecordingRunner()
    runtime = ProcessBackendRuntime(str(workspace), command_runner=runner)
    port = _free_port()
    env = {"ARC_WEB_PORT": str(port), "ARC_E2E_DB_PATH": db_path}

    async def _lifecycle() -> None:
        fp1 = backend_source_fingerprint(str(workspace / "backend"))
        first = await runtime.ensure(port, db_path, fp1, runtime_env=env)

        assert not first.reused
        assert first.session is not None
        assert runner.calls == ["npm run db:prepare:e2e"]  # fresh start prepared the db

        second = await runtime.ensure(port, db_path, fp1, runtime_env=env)

        assert second.reused
        assert second.session is first.session
        assert runner.calls == ["npm run db:prepare:e2e", "npm run db:seed"]
        # The reuse hit's reset really wiped the rows the server had been serving.
        connection = sqlite3.connect(db_path)
        try:
            rows_left = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            connection.close()
        assert rows_left == 0

        # The agent edited backend code between attempts: rebuild on the same port.
        (workspace / "backend" / "src" / "server.js").write_text(
            (
                "const http = require('http');\n"
                "http.createServer((req, res) => res.end('ok v2'))"
                ".listen(process.env.ARC_WEB_PORT, '127.0.0.1');\n"
            ),
            encoding="utf-8",
        )
        fp2 = backend_source_fingerprint(str(workspace / "backend"))
        assert fp2 != fp1
        third = await runtime.ensure(port, db_path, fp2, runtime_env=env)

        assert not third.reused
        assert third.session is not first.session
        # The stale server's teardown evidence reached the acquisition (graceful
        # release or force-release of the port; the `terminate` context label
        # itself is never part of the note, same as before the extraction).
        assert "released" in third.cleanup_note
        assert "Backend Process Output (session teardown)" in third.cleanup_note

        teardown_note = await runtime.terminate("acceptance teardown")

        # Termination semantics: the teardown waited for the port to be
        # released together with the process, so a fresh bind on the same
        # port succeeds.
        assert "released" in teardown_note
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))

    asyncio.run(_lifecycle())

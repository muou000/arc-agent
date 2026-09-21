"""The session-scoped E2E backend runtime must be reused across TDD attempts.

Every `run_tests(E2E)` call used to pay a full backend stop/start cycle
(`db:prepare:e2e` + `npm run start` + TCP wait + teardown), even inside one
TDD fix loop whose consecutive attempts target the same batch. The backend
runtime is now kept alive between attempts and reused while the backend
sources, port and E2E database are unchanged. The per-attempt database reset
happens at row level (`db:prepare:e2e` deletes the file, which cannot run
under a live server) with a `db:seed` re-run, and the runtime is shut down
when the node's IMPLEMENT phase finishes.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import sqlite3
from pathlib import Path
from unittest import mock

from app_type_handler import web as web_handler


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.pid = 4321
        self.returncode = returncode


class _CommandRecorder:
    """Stands in for shell commands, recording invocations."""

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
    ) -> web_handler._CommandResult:
        self.calls.append(command)
        code = 0
        for needle, value in self.exit_codes.items():
            if needle in command:
                code = value
                break
        return web_handler._CommandResult(
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
    fingerprint = web_handler._backend_source_fingerprint(str(backend))
    assert fingerprint is not None
    return tmp_path, fingerprint


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


def _make_session(db_path: str, fingerprint: str, port: int = 4321) -> web_handler._E2EBackendSession:
    return web_handler._E2EBackendSession(
        process=_FakeProcess(),
        port=port,
        db_path=db_path,
        fingerprint=fingerprint,
        start_command="npm run start",
        startup_detail="started",
        instance_fingerprint="launcher:4321",
    )


def _patch_fresh_start(monkeypatch, recorder: _CommandRecorder, start_calls: list[str]) -> None:
    async def _fake_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(ok=True, note="rebuilt frontend/dist from current sources", output="build ok", exit_code=0)

    async def _fake_prepare(workspace_path: str, runtime_env: dict) -> tuple[bool, int | None, str]:
        return True, 0, "prepared"

    async def _fake_start(workspace_path: str, runtime_env: dict, web_port: int | None = None):
        start_calls.append("start")
        return _FakeProcess(), "npm run start", "startup ok", "launcher:4321"

    async def _fake_http(host: str, port: int, timeout: float = 20.0) -> bool:
        return True

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)
    monkeypatch.setattr(web_handler, "_prepare_e2e_database", _fake_prepare)
    monkeypatch.setattr(web_handler, "_start_backend_runtime", _fake_start)
    monkeypatch.setattr(web_handler, "_wait_for_http_server", _fake_http)
    monkeypatch.setattr(web_handler, "_execute_web_test_command", recorder)


def _make_handler(tmp_path: Path) -> web_handler.WebAppType:
    return web_handler.WebAppType(
        workspace_path=str(tmp_path),
        requirement_path=str(tmp_path / "requirements.yaml"),
        interface_designer=None,
        log_cb=lambda *args, **kwargs: None,
    )


def test_reuses_live_server_when_nothing_changed(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    handler = _make_handler(workspace)
    session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)
    handler._e2e_runtime_session = session

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == []
    assert "Reused the live backend runtime" in result.output
    assert "npm run db:seed" in recorder.calls
    # The database reset really ran: rows are gone, schema stays.
    connection = sqlite3.connect(env["ARC_E2E_DB_PATH"])
    try:
        remaining = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    assert remaining == 0
    assert tables
    # The runtime stays registered for the next attempt.
    assert handler._e2e_runtime_session is session
    assert result.exit_code == 0


def test_restarts_server_when_backend_source_changes(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    handler = _make_handler(workspace)
    stale_session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)
    handler._e2e_runtime_session = stale_session

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)
    terminated: list[int] = []

    async def _fake_terminate(process, port=None) -> str:
        terminated.append(process.pid)
        return "released"

    monkeypatch.setattr(web_handler, "_terminate_process", _fake_terminate)

    # The agent edited backend code between attempts.
    (workspace / "backend" / "src" / "app.js").write_text("console.log('v2')\n", encoding="utf-8")

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]
    assert terminated == [stale_session.process.pid]
    assert handler._e2e_runtime_session is not stale_session
    assert handler._e2e_runtime_session is not None
    assert "Reused the live backend runtime" not in result.output
    assert "Deferred: the backend runtime stays alive" in result.output


def test_restarts_server_when_previous_process_died(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    handler = _make_handler(workspace)
    session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)
    session.process.returncode = 1
    handler._e2e_runtime_session = session

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]


def test_falls_back_to_fresh_start_when_reuse_probe_not_serving(tmp_path, monkeypatch) -> None:
    """Reuse requires an HTTP round trip, not just an open TCP port."""

    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    handler = _make_handler(workspace)
    handler._e2e_runtime_session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    async def _http_not_ready(host: str, port: int, timeout: float = 5.0) -> bool:
        return False

    monkeypatch.setattr(web_handler, "_wait_for_http_server", _http_not_ready)

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]


def test_restarts_server_when_e2e_database_changes(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    handler = _make_handler(workspace)
    handler._e2e_runtime_session = _make_session("some-other-database.sqlite", fingerprint)

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]


def test_falls_back_to_fresh_start_when_reseeding_fails(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    handler = _make_handler(workspace)
    handler._e2e_runtime_session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)

    recorder = _CommandRecorder(exit_codes={"db:seed": 1})
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]
    assert "Reused the live backend runtime" not in result.output
    # The reset-failure reason must survive the fallback for debuggability.
    assert "fell back to a fresh start" in result.output
    assert "db:seed" in result.output


def test_runtime_session_is_strictly_per_instance(tmp_path, monkeypatch) -> None:
    """Parallel tasks build one handler per task; sessions must not leak across."""

    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    first = _make_handler(workspace)
    second = _make_handler(workspace)
    session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)
    first._e2e_runtime_session = session

    # The second instance starts blind even while the first holds a live session.
    assert second._e2e_runtime_session is None

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)
    result = asyncio.run(second.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]
    assert second._e2e_runtime_session is not None
    assert second._e2e_runtime_session is not session
    # Shutting down one instance leaves the other's session in place.
    asyncio.run(second.shutdown_e2e_runtime())
    assert first._e2e_runtime_session is session
    assert "Exit Code: 0" in result.output



def test_falls_back_to_fresh_start_when_database_has_no_schema(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"], with_data=False)
    # Recreate as a zero-table file: no schema was ever initialized.
    connection = sqlite3.connect(env["ARC_E2E_DB_PATH"])
    connection.execute("DROP TABLE users")
    connection.commit()
    connection.close()

    handler = _make_handler(workspace)
    handler._e2e_runtime_session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]


def test_fresh_run_stores_session_and_defers_cleanup(tmp_path, monkeypatch) -> None:
    workspace, _fingerprint = _make_workspace(tmp_path)

    handler = _make_handler(workspace)
    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)
    terminated: list[int] = []

    async def _fake_terminate(process, port=None) -> str:
        terminated.append(process.pid)
        return "released"

    monkeypatch.setattr(web_handler, "_terminate_process", _fake_terminate)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]
    assert terminated == []
    assert handler._e2e_runtime_session is not None
    assert handler._e2e_runtime_session.port == 4321
    assert "Deferred: the backend runtime stays alive" in result.output


def test_shutdown_e2e_runtime_terminates_the_session(tmp_path) -> None:
    handler = _make_handler(tmp_path)
    session = _make_session("unused.sqlite", "fp")
    handler._e2e_runtime_session = session
    terminated: list[int] = []

    async def _fake_terminate(process, port=None) -> str:
        terminated.append(process.pid)
        return "released"

    with mock.patch.object(web_handler, "_terminate_process", _fake_terminate):
        asyncio.run(handler.shutdown_e2e_runtime())
        assert handler._e2e_runtime_session is None
        assert terminated == [session.process.pid]
        # A second shutdown with no live session is a no-op.
        asyncio.run(handler.shutdown_e2e_runtime())
        assert terminated == [session.process.pid]


def test_session_teardown_surfaces_retained_crash_output(tmp_path) -> None:
    """A mid-session server crash must surface through the teardown note.

    The drain tails are the only place a crashed session server's output
    survives; `_terminate_e2e_session` appends the retained tail to its
    cleanup note so report bodies carry the crash evidence.
    """

    handler = _make_handler(tmp_path)
    session = _make_session("unused.sqlite", "fp")
    stdout_tail = web_handler._ProcessOutputTail()
    stderr_tail = web_handler._ProcessOutputTail()
    with stderr_tail._lock:
        stderr_tail._chunks.extend(
            b"TypeError: Cannot read properties of undefined (reading 'type')\n"
        )
    session.process._arc_output_tails = (stdout_tail, stderr_tail, [])  # type: ignore[attr-defined]
    handler._e2e_runtime_session = session

    async def _fake_terminate(process, port=None) -> str:
        return "released"

    with mock.patch.object(web_handler, "_terminate_process", _fake_terminate):
        note = asyncio.run(handler._terminate_e2e_session("Session teardown"))

    assert "Backend Process Output (session teardown)" in note
    assert "STDERR:" in note
    assert "TypeError: Cannot read properties of undefined" in note


def test_session_teardown_without_anchor_keeps_plain_note(tmp_path) -> None:
    handler = _make_handler(tmp_path)
    session = _make_session("unused.sqlite", "fp")
    handler._e2e_runtime_session = session

    async def _fake_terminate(process, port=None) -> str:
        return "released"

    with mock.patch.object(web_handler, "_terminate_process", _fake_terminate):
        note = asyncio.run(handler._terminate_e2e_session("Session teardown"))

    assert note == "released"
    assert "Backend Process Output" not in note


def test_backend_fingerprint_ignores_non_server_paths(tmp_path) -> None:
    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "src" / "app.js").write_text("console.log('v1')\n", encoding="utf-8")
    (backend / "package.json").write_text("{}\n", encoding="utf-8")
    before = web_handler._backend_source_fingerprint(str(backend))

    (backend / "node_modules" / "pkg").mkdir(parents=True)
    (backend / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    (backend / ".arc-test-db").mkdir()
    (backend / ".arc-test-db" / "suite-1.sqlite").write_text("x\n", encoding="utf-8")
    (backend / "test-e2e").mkdir()
    (backend / "test-e2e" / "login.spec.ts").write_text("x\n", encoding="utf-8")

    assert web_handler._backend_source_fingerprint(str(backend)) == before

    (backend / "src" / "app.js").write_text("console.log('v2')\n", encoding="utf-8")
    assert web_handler._backend_source_fingerprint(str(backend)) != before


def test_backend_fingerprint_missing_directory(tmp_path) -> None:
    assert web_handler._backend_source_fingerprint(str(tmp_path / "absent")) is None


def test_backend_fingerprint_ignores_runtime_env(tmp_path, monkeypatch) -> None:
    """The fingerprint is a pure source hash; env values must not break reuse.

    Port/DB-path dimensions are session-key comparisons in the reuse check, so
    a per-attempt env change (e.g. a new label) must not force a restart.
    """

    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "src" / "app.js").write_text("console.log('v1')\n", encoding="utf-8")

    before = web_handler._backend_source_fingerprint(str(backend))
    monkeypatch.setattr(
        web_handler,
        "build_web_runtime_env",
        lambda **kwargs: {"SOME_RUNTIME_VALUE": "different-per-attempt"},
    )
    after = web_handler._backend_source_fingerprint(str(backend))

    assert before is not None
    assert before == after


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

    ok, output = web_handler._reset_sqlite_database_rows(db_path)

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

    ok, output = web_handler._reset_sqlite_database_rows(db_path)

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


def test_falls_back_to_fresh_start_when_schema_has_triggers(tmp_path, monkeypatch) -> None:
    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])
    connection = sqlite3.connect(env["ARC_E2E_DB_PATH"])
    try:
        connection.execute(
            "CREATE TRIGGER users_audit AFTER DELETE ON users BEGIN INSERT INTO users (name) VALUES ('ghost'); END"
        )
        connection.commit()
    finally:
        connection.close()

    handler = _make_handler(workspace)
    handler._e2e_runtime_session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert start_calls == ["start"]


def test_reset_sqlite_database_rows_reports_missing_file_and_empty_schema(tmp_path) -> None:
    ok, _output = web_handler._reset_sqlite_database_rows(str(tmp_path / "absent.sqlite"))
    assert not ok

    empty_path = str(tmp_path / "empty.sqlite")
    connection = sqlite3.connect(empty_path)
    connection.close()
    ok, output = web_handler._reset_sqlite_database_rows(empty_path)
    assert not ok
    assert "no user tables" in output


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

            assert await web_handler._wait_for_http_server("127.0.0.1", http_port, timeout=5.0)
            assert await web_handler._wait_for_http_server("127.0.0.1", silent_port, timeout=0.3) is False
            assert await web_handler._wait_for_http_server("127.0.0.1", closed_port, timeout=0.3) is False
        finally:
            http_server.close()
            await http_server.wait_closed()
            silent_server.close()
            await silent_server.wait_closed()

    asyncio.run(_scenario())


def test_e2e_result_contains_stage_timing_breakdown(tmp_path, monkeypatch) -> None:
    """Each E2E round-trip reports the wall-clock cost of its stages
    (frontend build, database prep, backend runtime, playwright), so the
    model-facing output and the persisted tdd-run log carry the cost
    breakdown that previously had to be inferred from debug-log timestamps."""

    workspace, _fingerprint = _make_workspace(tmp_path)

    handler = _make_handler(workspace)
    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert "=== Stage Timing ===" in result.output
    # A fresh E2E run pays all four stages; each is reported as name=<n>s.
    for stage in ("frontend_build", "database_prepare", "backend_runtime", "playwright"):
        assert stage + "=" in result.output


def test_e2e_stage_timing_reports_reused_stages(tmp_path, monkeypatch) -> None:
    """A reused runtime run still renders the timing section (build reuse is
    fast, the reset shows up under database_prepare)."""

    workspace, fingerprint = _make_workspace(tmp_path)
    env = web_handler._build_e2e_runtime_env(str(workspace), ["test-e2e/login.spec.ts"], web_port=4321)
    _make_sqlite(env["ARC_E2E_DB_PATH"])

    handler = _make_handler(workspace)
    handler._e2e_runtime_session = _make_session(env["ARC_E2E_DB_PATH"], fingerprint)

    async def _fake_reset(runtime_env: dict) -> tuple[bool, str]:
        return True, "reset ok"

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)
    monkeypatch.setattr(handler, "_reset_live_e2e_database", _fake_reset)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert "=== Stage Timing ===" in result.output
    assert "database_prepare=" in result.output
    assert start_calls == []  # reuse confirmed: no fresh backend start


# --------------------------------------------------------------------------
# The single E2E executor (issue #98)
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
    handler = _make_handler(workspace)
    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    async def _failing_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(
            ok=False, note="frontend build failed", output="vite: build error", exit_code=1
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _failing_build)
    build_failed = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert build_failed.exit_code == 1
    assert "Frontend build failed before E2E startup." in build_failed.output
    assert "=== Frontend Build ===\nvite: build error" in build_failed.output
    assert "Served index.html:" in build_failed.output
    # The build never produced a runtime env, so no env/DB/backend sections.
    assert "=== E2E Runtime Env ===" not in build_failed.output
    assert "=== Database Prepare ===" not in build_failed.output
    assert "=== Backend Runtime Command ===" not in build_failed.output

    async def _failing_prepare(workspace_path: str, runtime_env: dict) -> tuple[bool, int | None, str]:
        return False, 1, "db:prepare:e2e failed"

    async def _ok_build(workspace_path: str, *, force_rebuild: bool = False) -> web_handler._FrontendBuildOutcome:
        return web_handler._FrontendBuildOutcome(
            ok=True, note="rebuilt frontend/dist from current sources", output="build ok", exit_code=0
        )

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _ok_build)
    monkeypatch.setattr(web_handler, "_prepare_e2e_database", _failing_prepare)
    db_failed = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert db_failed.exit_code == 1
    assert "E2E database preparation failed before backend startup." in db_failed.output
    assert "=== Frontend Build ===\nbuild ok" in db_failed.output
    # The database failure knows the runtime env the attempt had built...
    assert "=== E2E Runtime Env ===" in db_failed.output
    assert "DB Path:" in db_failed.output
    assert "=== Database Prepare ===\ndb:prepare:e2e failed" in db_failed.output
    # ...but never started a backend, so no backend command section.
    assert "=== Backend Runtime Command ===" not in db_failed.output

    async def _failing_start(workspace_path: str, runtime_env: dict, web_port: int | None = None):
        return None, "npm run start", "crashed on boot: ERR_MODULE_NOT_FOUND", "no-fingerprint"

    async def _ok_prepare(workspace_path: str, runtime_env: dict) -> tuple[bool, int | None, str]:
        return True, 0, "prepared"

    monkeypatch.setattr(web_handler, "_start_backend_runtime", _failing_start)
    monkeypatch.setattr(web_handler, "_prepare_e2e_database", _ok_prepare)
    backend_failed = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

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
    handler = _make_handler(workspace)
    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

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
    handler = _make_handler(workspace)

    seen_timeouts: list[float | None] = []

    async def _recording_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None, web_port=None):
        if "playwright" in command:
            seen_timeouts.append(timeout)
        return web_handler._CommandResult(exit_code=0, text=f"Exit Code: 0\nSTDOUT:\n{command} ran\n")

    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)
    # The fresh-start harness routes _execute_web_test_command through its
    # own recorder; layer the timeout probe on top of it.
    monkeypatch.setattr(web_handler, "_execute_web_test_command", _recording_command)

    asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    # The attempt reached the Playwright stage (fresh backend start happened)
    # and its timeout is the one named constant.
    assert start_calls == ["start"]
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


# ---------------------------------------------------------------------------
# Retry-round case filter (#115): the digest's failed case names reach the
# Playwright command as --grep; unfiltered rounds stay untouched.
# ---------------------------------------------------------------------------


def test_case_grep_pattern_maps_display_titles_to_leaf_names() -> None:
    """Digest display titles become regex-escaped leaf names in an alternation."""

    pattern = web_handler._build_case_grep_pattern(
        [
            "register › rejects duplicate username",
            "register › rejects duplicate username",  # deduped
            "登录 › 拒绝缺失或格式错误的资料",
        ]
    )
    assert pattern == "|".join(
        [re.escape("rejects duplicate username"), re.escape("拒绝缺失或格式错误的资料")]
    )
    # The pattern matches the space-joined full title Playwright greps against
    # and the › display form the digest printed.
    assert re.search(pattern, "register rejects duplicate username")
    assert re.search(pattern, "register › rejects duplicate username")
    # Vitest-style list names ("suite > case") split on the same separator.
    assert web_handler._build_case_grep_pattern(["Auth API > rejects duplicate username with 409"]) == re.escape(
        "rejects duplicate username with 409"
    )
    # Unusable names degrade to the empty pattern (full run).
    assert web_handler._build_case_grep_pattern(["", "   "]) == ""
    assert web_handler._build_case_grep_pattern(None) == ""
    assert web_handler._build_case_grep_pattern([]) == ""


def test_retry_round_playwright_command_carries_grep_filter(tmp_path, monkeypatch) -> None:
    """A retry round's runner command filters to the previously failing cases.

    The failed-case names ride ``run_test_group(..., failed_case_names=[...])``
    into the one E2E executor and land as a single quoted ``--grep`` word; the
    resolved targets stay on the command, and the result header says the round
    was partial so pass/fail verdicts are read in that light.
    """

    workspace, _fingerprint = _make_workspace(tmp_path)
    handler = _make_handler(workspace)
    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    result = asyncio.run(
        handler.run_test_group(
            "e2e",
            ["backend/test-e2e/login.spec.ts"],
            web_port=4321,
            failed_case_names=["register › rejects duplicate username"],
        )
    )

    assert result.exit_code == 0
    playwright_commands = [command for command in recorder.calls if "playwright" in command]
    assert len(playwright_commands) == 1
    command = playwright_commands[0]
    # The executor normalizes targets backend-relative; the filter appends as
    # one quoted shell word after them (re.escape backslash-escapes spaces).
    assert command.startswith("npx playwright test test-e2e/login.spec.ts --grep ")
    assert shlex.quote(re.escape("rejects duplicate username")) in command
    # The suite display segment must not leak into the pattern.
    assert "register" not in command.split("--grep", 1)[1]
    # The result header tells the agent the round was case-filtered.
    assert "Failed Case Filter" in result.output


def test_unfiltered_round_keeps_the_plain_playwright_command(tmp_path, monkeypatch) -> None:
    """Without failed-case names the command and header stay unfiltered."""

    workspace, _fingerprint = _make_workspace(tmp_path)
    handler = _make_handler(workspace)
    recorder = _CommandRecorder()
    start_calls: list[str] = []
    _patch_fresh_start(monkeypatch, recorder, start_calls)

    result = asyncio.run(handler.run_test_group("e2e", ["backend/test-e2e/login.spec.ts"], web_port=4321))

    assert result.exit_code == 0
    playwright_commands = [command for command in recorder.calls if "playwright" in command]
    assert playwright_commands == ["npx playwright test test-e2e/login.spec.ts"]
    assert "Failed Case Filter" not in result.output

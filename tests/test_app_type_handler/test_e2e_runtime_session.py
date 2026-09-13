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
import sqlite3
from pathlib import Path
from unittest import mock

from app_type_handler import web as web_handler
from app_type_handler.test_results import parse_test_results


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
    ) -> str:
        self.calls.append(command)
        code = 0
        for needle, value in self.exit_codes.items():
            if needle in command:
                code = value
                break
        return f"Exit Code: {code}\nSTDOUT:\n{command} ran\n"


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
    async def _fake_build(workspace_path: str) -> tuple[bool, str]:
        return True, "build ok"

    async def _fake_prepare(workspace_path: str, runtime_env: dict) -> tuple[bool, str]:
        return True, "prepared"

    async def _fake_start(workspace_path: str, runtime_env: dict, web_port: int | None = None):
        start_calls.append("start")
        return _FakeProcess(), "npm run start", "startup ok", "launcher:4321"

    async def _fake_tcp(host: str, port: int, timeout: float = 20.0) -> bool:
        return True

    monkeypatch.setattr(web_handler, "_build_frontend_dist", _fake_build)
    monkeypatch.setattr(web_handler, "_prepare_e2e_database", _fake_prepare)
    monkeypatch.setattr(web_handler, "_start_backend_runtime", _fake_start)
    monkeypatch.setattr(web_handler, "_wait_for_tcp_server", _fake_tcp)
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
    assert "Reused the live backend runtime" in result
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
    assert parse_test_results(result)["exit_code"] == 0


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
    assert "Reused the live backend runtime" not in result
    assert "Deferred: the backend runtime stays alive" in result


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
    assert "Reused the live backend runtime" not in result


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
    assert "Deferred: the backend runtime stays alive" in result


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


def test_reset_sqlite_database_rows_reports_missing_file_and_empty_schema(tmp_path) -> None:
    ok, _output = web_handler._reset_sqlite_database_rows(str(tmp_path / "absent.sqlite"))
    assert not ok

    empty_path = str(tmp_path / "empty.sqlite")
    connection = sqlite3.connect(empty_path)
    connection.close()
    ok, output = web_handler._reset_sqlite_database_rows(empty_path)
    assert not ok
    assert "no user tables" in output

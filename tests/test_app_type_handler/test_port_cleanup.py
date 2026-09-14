"""Port cleanup must never terminate an unrelated listener."""

from __future__ import annotations

import asyncio

import pytest

from app_type_handler import web


def test_pre_start_cleanup_refuses_unknown_port_owners(monkeypatch) -> None:
    killed: list[int] = []

    async def server_still_running(*_args, **_kwargs):
        return False

    async def record_kill(pid: int):
        killed.append(pid)

    monkeypatch.setattr(web, "_wait_for_tcp_server_shutdown", server_still_running)
    monkeypatch.setattr(web, "_list_port_owner_pids", lambda _port: [4242])
    monkeypatch.setattr(web, "_force_kill_pid", record_kill)

    with pytest.raises(RuntimeError, match="refusing to terminate unknown"):
        asyncio.run(web._ensure_port_released(3301, context="Pre-start port cleanup"))

    assert killed == []


def test_cleanup_only_terminates_explicitly_owned_processes(monkeypatch) -> None:
    killed: list[int] = []
    shutdown_checks = iter([False, True])
    owned_fingerprint = {
        "pid": "4242",
        "ppid": "3131",
        "name": "node.exe",
        "exe": "C:/node.exe",
        "command": "node server.js",
        "cwd": "C:/workspace/backend",
    }

    async def server_shutdown(*_args, **_kwargs):
        return next(shutdown_checks)

    async def record_kill(pid: int):
        killed.append(pid)

    monkeypatch.setattr(web, "_wait_for_tcp_server_shutdown", server_shutdown)
    monkeypatch.setattr(web, "_list_port_owner_pids", lambda _port: [4242, 5252])
    monkeypatch.setattr(web, "_get_process_fingerprint", lambda pid: {**owned_fingerprint, "pid": str(pid)})
    monkeypatch.setattr(web, "_force_kill_pid", record_kill)

    result = asyncio.run(
        web._ensure_port_released(
            3301,
            context="Backend runtime cleanup",
            allowed_processes={4242: owned_fingerprint},
        )
    )

    assert killed == [4242]
    assert "4242" in result
    assert "5252" not in result


def test_cleanup_kills_orphaned_child_whose_ppid_changed(monkeypatch) -> None:
    """The launcher died and POSIX re-parented the backend child.

    Its ppid no longer matches the capture-time fingerprint, but the identity
    keys (name/exe/command/cwd) still do - force-release must proceed instead of
    refusing, otherwise the orphan wedges the port for every later E2E run.
    """
    killed: list[int] = []
    shutdown_checks = iter([False, True])
    captured = {
        "pid": "4242",
        "ppid": "3131",
        "name": "node.exe",
        "exe": "C:/node.exe",
        "command": "node server.js",
        "cwd": "C:/workspace/backend",
    }
    orphaned = {**captured, "ppid": "1"}

    async def server_shutdown(*_args, **_kwargs):
        return next(shutdown_checks)

    async def record_kill(pid: int):
        killed.append(pid)

    monkeypatch.setattr(web, "_wait_for_tcp_server_shutdown", server_shutdown)
    monkeypatch.setattr(web, "_list_port_owner_pids", lambda _port: [4242])
    monkeypatch.setattr(web, "_get_process_fingerprint", lambda _pid: orphaned)
    monkeypatch.setattr(web, "_force_kill_pid", record_kill)

    result = asyncio.run(
        web._ensure_port_released(
            3301,
            context="Backend runtime cleanup",
            allowed_processes={4242: captured},
        )
    )

    assert killed == [4242]
    assert "4242" in result


def test_cleanup_refuses_reused_pid_with_different_fingerprint(monkeypatch) -> None:
    killed: list[int] = []

    async def server_still_running(*_args, **_kwargs):
        return False

    async def record_kill(pid: int):
        killed.append(pid)

    expected = {
        "pid": "4242",
        "ppid": "3131",
        "name": "node.exe",
        "exe": "C:/node.exe",
        "command": "node server.js",
        "cwd": "C:/workspace/backend",
    }
    reused = {**expected, "command": "unrelated.exe"}
    monkeypatch.setattr(web, "_wait_for_tcp_server_shutdown", server_still_running)
    monkeypatch.setattr(web, "_list_port_owner_pids", lambda _port: [4242])
    monkeypatch.setattr(web, "_get_process_fingerprint", lambda _pid: reused)
    monkeypatch.setattr(web, "_force_kill_pid", record_kill)

    with pytest.raises(RuntimeError, match="still occupied after forced cleanup"):
        asyncio.run(
            web._ensure_port_released(
                3301,
                context="Backend runtime cleanup",
                allowed_processes={4242: expected},
            )
        )

    assert killed == []


def test_capture_owned_port_processes_filters_unrelated_owners(monkeypatch) -> None:
    fingerprints = {
        3131: {"pid": "3131", "ppid": "2020", "name": "cmd.exe"},
        4242: {"pid": "4242", "ppid": "3131", "name": "node.exe"},
        5252: {"pid": "5252", "ppid": "9999", "name": "other.exe"},
        9999: {"pid": "9999", "ppid": "1", "name": "service.exe"},
        1: {"pid": "1", "ppid": "0", "name": "init"},
    }
    monkeypatch.setattr(web, "_list_port_owner_pids", lambda _port: [4242, 5252])
    monkeypatch.setattr(web, "_get_process_fingerprint", lambda pid: fingerprints[pid])

    owned = web._capture_owned_port_processes(3301, launcher_pid=3131)

    assert owned == {4242: fingerprints[4242]}


class _FakeUrlopenResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeProbeProcess:
    pid = 3131


def _stub_probe_boot(monkeypatch, terminate) -> None:
    async def fake_start(*_args, **_kwargs):
        return _FakeProbeProcess(), "npm run start", "", "fingerprint"

    monkeypatch.setattr(web, "_resolve_backend_start_command", lambda _backend: "npm run start")
    monkeypatch.setattr(web, "_build_e2e_runtime_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(web, "_start_backend_runtime", fake_start)
    monkeypatch.setattr(web, "_terminate_process", terminate)
    monkeypatch.setattr(web.urllib.request, "urlopen", lambda _url, timeout=5: _FakeUrlopenResponse())


def test_probe_gate_fails_when_backend_teardown_fails(monkeypatch) -> None:
    """A swallowed cleanup failure must not report merge-gate success.

    The probe owns the merged workspace only while the backend runs; if the
    teardown leaks the process (file locks on Windows, port held), the gate
    must surface the failure instead of landing the merge as healthy.
    """
    terminated: list[int] = []

    async def failing_terminate(process, *, port=None):
        terminated.append(process.pid)
        raise RuntimeError(f"port {port} is still occupied after forced cleanup.")

    _stub_probe_boot(monkeypatch, failing_terminate)

    reason = asyncio.run(web.probe_backend_health("ws", 3301))

    assert terminated == [3131]
    assert reason is not None
    assert "cleanup failed" in reason
    assert "3301" in reason


def test_probe_gate_returns_none_when_healthy_and_teardown_succeeds(monkeypatch) -> None:
    async def ok_terminate(_process, *, port=None):
        return f"port {port} is released."

    _stub_probe_boot(monkeypatch, ok_terminate)

    assert asyncio.run(web.probe_backend_health("ws", 3301)) is None

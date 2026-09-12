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

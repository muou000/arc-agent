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


def test_cleanup_only_terminates_explicitly_owned_pids(monkeypatch) -> None:
    killed: list[int] = []
    shutdown_checks = iter([False, True])

    async def server_shutdown(*_args, **_kwargs):
        return next(shutdown_checks)

    async def record_kill(pid: int):
        killed.append(pid)

    monkeypatch.setattr(web, "_wait_for_tcp_server_shutdown", server_shutdown)
    monkeypatch.setattr(web, "_list_port_owner_pids", lambda _port: [4242, 5252])
    monkeypatch.setattr(web, "_force_kill_pid", record_kill)

    result = asyncio.run(
        web._ensure_port_released(
            3301,
            context="Backend runtime cleanup",
            allowed_pids={4242},
        )
    )

    assert killed == [4242]
    assert "4242" in result
    assert "5252" not in result

"""The web app-type must gate on a Node runtime that can run the test stack.

The template's jsdom dependency chain needs unflagged require(esm). Node 22.11
passes a PATH existence check yet crashes every vitest forks worker, which no
code edit can fix - discovered per node it burns the entire TDD budget of all
117 leaves (observed on the 12306 benchmark).
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app_type_handler.base import AppTypeHandler
from app_type_handler.web import WebAppType, _node_supports_require_esm


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("v24.20.0", True),
        ("v23.2.0", True),
        ("v23.1.0", False),
        ("v22.12.0", True),
        ("v22.11.0", False),
        ("v22.0.0", False),
        ("v21.7.3", False),
        ("v20.19.0", True),
        ("v20.18.1", False),
        ("v18.0.0", False),
        ("v16.20.2", False),
        ("", False),
        ("not-a-version", False),
    ],
)
def test_node_version_require_esm_matrix(version: str, expected: bool) -> None:
    assert _node_supports_require_esm(version) is expected


def _fake_node_run(version_stdout: str):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=version_stdout, stderr="", returncode=0)

    return fake_run


def test_web_gate_accepts_new_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_node_run("v24.20.0\n"))
    assert asyncio.run(WebAppType.check_runtime_versions(log_cb=None)) is True


def test_web_gate_rejects_node_without_require_esm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[tuple] = []

    async def log_cb(agent_name, message, status=None, node_id=None):
        logs.append((agent_name, message, status))

    monkeypatch.setattr(subprocess, "run", _fake_node_run("v22.11.0\n"))
    assert asyncio.run(WebAppType.check_runtime_versions(log_cb=log_cb)) is False
    assert logs and "22.11.0" in logs[0][1] and logs[0][2] == "error"


def test_web_gate_rejects_broken_node_install(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_run(*args, **kwargs):
        raise FileNotFoundError("node")

    monkeypatch.setattr(subprocess, "run", broken_run)
    assert asyncio.run(WebAppType.check_runtime_versions(log_cb=None)) is False


def test_base_handler_accepts_every_runtime() -> None:
    assert asyncio.run(AppTypeHandler.check_runtime_versions(log_cb=None)) is True

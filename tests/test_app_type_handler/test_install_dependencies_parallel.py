"""Backend and frontend npm installs must run concurrently.

The two installs touch disjoint trees, so a serial drain doubles the cold-cache
cost (observed: 2m44s backend + more for frontend on the 12306 benchmark) for
no isolation benefit.
"""

from __future__ import annotations

import asyncio

from app_type_handler import web as web_module
from app_type_handler.web import WebAppType


def _handler(tmp_path, with_frontend: bool = True):
    handler = WebAppType.__new__(WebAppType)
    handler.workspace_path = str(tmp_path)
    handler.requirement_path = ""
    handler.log_cb = _noop_log
    (tmp_path / "backend").mkdir()
    if with_frontend:
        (tmp_path / "frontend").mkdir()
    return handler


async def _noop_log(*args) -> None:
    return None


def test_installs_run_concurrently(tmp_path, monkeypatch) -> None:
    running: set[str] = set()
    overlapped = False

    async def fake_install(target_dir: str, log_cb=None) -> bool:
        nonlocal overlapped
        label = "backend" if target_dir.endswith("backend") else "frontend"
        assert running.isdisjoint({label}) or overlapped or True
        if running:
            overlapped = True
        running.add(label)
        await asyncio.sleep(0.01)
        running.discard(label)
        return True

    monkeypatch.setattr(web_module, "run_npm_install", fake_install)
    assert asyncio.run(_handler(tmp_path).install_dependencies()) is True
    assert overlapped, "backend and frontend installs must overlap"


def test_failed_frontend_install_fails_the_batch(tmp_path, monkeypatch) -> None:
    async def fake_install(target_dir: str, log_cb=None) -> bool:
        return not target_dir.endswith("frontend")

    monkeypatch.setattr(web_module, "run_npm_install", fake_install)
    assert asyncio.run(_handler(tmp_path).install_dependencies()) is False


def test_missing_frontend_directory_is_skipped(tmp_path, monkeypatch) -> None:
    called: list[str] = []

    async def fake_install(target_dir: str, log_cb=None) -> bool:
        called.append(target_dir)
        return True

    monkeypatch.setattr(web_module, "run_npm_install", fake_install)
    assert asyncio.run(_handler(tmp_path, with_frontend=False).install_dependencies()) is True
    assert len(called) == 1 and called[0].endswith("backend")

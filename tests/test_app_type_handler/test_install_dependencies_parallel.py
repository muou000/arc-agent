"""Backend and frontend npm installs must run concurrently.

The two installs touch disjoint trees, so a serial drain doubles the cold-cache
cost (observed: 2m44s backend + more for frontend on the 12306 benchmark) for
no isolation benefit.
"""

from __future__ import annotations

import asyncio
import json

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
    started: list[str] = []

    async def fake_install(target_dir: str, log_cb=None) -> bool:
        nonlocal overlapped
        label = "backend" if target_dir.endswith("backend") else "frontend"
        started.append(label)
        if running:
            overlapped = True
        running.add(label)
        await asyncio.sleep(0.01)
        running.discard(label)
        return True

    monkeypatch.setattr(web_module, "run_npm_install", fake_install)
    assert asyncio.run(_handler(tmp_path).install_dependencies()) is True
    assert overlapped, "backend and frontend installs must overlap"
    assert sorted(started) == ["backend", "frontend"]


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


def test_playwright_install_overlaps_npm_install_after_cli_is_ready(tmp_path, monkeypatch) -> None:
    handler = _handler(tmp_path)
    backend = tmp_path / "backend"
    (backend / "node_modules").mkdir()
    (backend / "package.json").write_text(
        json.dumps({"devDependencies": {"playwright": "^1.57.0"}}),
        encoding="utf-8",
    )

    running: set[str] = set()
    browser_overlapped = False

    async def fake_install(target_dir: str, log_cb=None) -> bool:
        label = "backend" if target_dir.endswith("backend") else "frontend"
        running.add(label)
        if label == "backend":
            await asyncio.sleep(0.01)
            playwright = backend / "node_modules" / "playwright"
            playwright.mkdir()
            (playwright / "cli.js").write_text("", encoding="utf-8")
        await asyncio.sleep(0.03)
        running.discard(label)
        return True

    async def fake_command(command: str, cwd: str, timeout: float = 60.0, extra_env=None):
        nonlocal browser_overlapped
        if "playwright install" in command:
            browser_overlapped = bool(running)
        await asyncio.sleep(0.01)
        return "Exit Code: 0\nSTDOUT:\nok\n"

    async def fake_peer_patch(_handler: WebAppType, _frontend_dir: str) -> None:
        return None

    monkeypatch.setattr(web_module, "run_npm_install", fake_install)
    monkeypatch.setattr(web_module, "_execute_web_test_command", fake_command)
    monkeypatch.setattr(web_module, "PLAYWRIGHT_CLI_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(WebAppType, "_ensure_testing_library_dom", fake_peer_patch)

    assert asyncio.run(handler.install_dependencies()) is True
    assert browser_overlapped, "Playwright browser installation must overlap npm install"

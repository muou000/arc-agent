"""The ``@testing-library/dom`` peer patch must survive npm 10.x.

The officially provisioned template does not declare ``@testing-library/dom``
even though ``@testing-library/react`` 16 lists it as a peer, so the runtime
installs it after the frontend install. On npm 10.x the plain resolver crashes
with ``Cannot read properties of null (reading 'edgesOut')``, which is exactly
why the main install already falls back to ``--legacy-peer-deps`` - the patch
is only ever reached in that world, so it needs the same flag or it fails and
every generated component test dies on ``Cannot find module
'@testing-library/dom'``.
"""

from __future__ import annotations

import asyncio

import pytest

from app_type_handler import web as web_module
from app_type_handler.web import WebAppType


def _handler(tmp_path):
    handler = WebAppType.__new__(WebAppType)
    handler.workspace_path = str(tmp_path)
    handler.requirement_path = ""
    handler.log_cb = _collect
    handler.logs = []
    return handler


async def _collect(*args) -> None:
    return None


@pytest.fixture
def frontend_dir(tmp_path):
    directory = tmp_path / "frontend"
    (directory / "node_modules" / "@testing-library").mkdir(parents=True)
    return directory


def test_peer_patch_uses_legacy_peer_deps(frontend_dir, monkeypatch) -> None:
    commands: list[tuple[str, str]] = []

    async def fake_npm(command: str, cwd: str, timeout: float = 0.0):
        commands.append((command, cwd))
        (frontend_dir / "node_modules" / "@testing-library" / "dom").mkdir()
        return 0, "", ""

    monkeypatch.setattr(web_module, "_run_npm_command", fake_npm)
    handler = _handler(frontend_dir.parent)

    asyncio.run(handler._ensure_testing_library_dom(str(frontend_dir)))

    assert len(commands) == 1
    command, cwd = commands[0]
    assert web_module.LEGACY_PEER_DEPS_FLAG in command, command
    assert "--no-save" in command and "--no-package-lock" in command
    assert "@testing-library/dom@^10.4.0" in command
    assert cwd == str(frontend_dir)


def test_peer_patch_is_skipped_when_already_installed(frontend_dir, monkeypatch) -> None:
    (frontend_dir / "node_modules" / "@testing-library" / "dom").mkdir()

    async def fail_npm(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("npm must not run when the peer is present")

    monkeypatch.setattr(web_module, "_run_npm_command", fail_npm)
    handler = _handler(frontend_dir.parent)

    asyncio.run(handler._ensure_testing_library_dom(str(frontend_dir)))


def test_peer_patch_failure_warns_instead_of_raising(frontend_dir, monkeypatch) -> None:
    messages: list[tuple[str, ...]] = []

    async def fake_npm(command: str, cwd: str, timeout: float = 0.0):
        return 1, "", "npm error Cannot read properties of null (reading 'edgesOut')"

    async def collect(*args) -> None:
        messages.append(args)

    monkeypatch.setattr(web_module, "_run_npm_command", fake_npm)
    handler = _handler(frontend_dir.parent)
    handler.log_cb = collect

    asyncio.run(handler._ensure_testing_library_dom(str(frontend_dir)))

    joined = [" ".join(str(part) for part in entry) for entry in messages]
    assert any("Could not install @testing-library/dom" in entry for entry in joined)
    assert any("edgesOut" in entry for entry in joined), "the npm error must reach the log"

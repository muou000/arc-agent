"""Timeout cleanup must kill the whole process tree, not just the launcher.

Issue #181: build/test commands run through a shell or npm wrapper, so the
spawned PID is only the launcher — on timeout its descendants survived,
holding port slots and writing into the generated workspace. The spawn
helpers in ``core.processes`` pair with ``finalize_subprocess`` to tear the
tree down (POSIX process group, Windows Job Object), and every
``app_type_handler`` spawn site must go through them.

Real-subprocess tree tests are marked ``slow`` (the fast suite keeps only
fake and one-shot-process pins); the POSIX variant is skipped on Windows and
the Windows variant everywhere else — each platform's test asserts the
behavior of its own mechanism on its own platform.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app_type_handler import backend_runtime
from core import processes
from core.processes import (
    finalize_subprocess,
    start_subprocess_exec,
    start_subprocess_shell,
)


_APP_TYPE_HANDLER_ROOT = Path(processes.__file__).resolve().parent.parent / "app_type_handler"

_RAW_SPAWN_NAMES = {"create_subprocess_exec", "create_subprocess_shell"}

_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Fast: spawn helpers attach the tree-kill scope
# ---------------------------------------------------------------------------


class _FakeSpawn:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _capture_asyncio_spawns(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_exec(program, *args, **kwargs):
        calls.append(("exec", program, args, kwargs))
        return _FakeSpawn(pid=4321)

    async def fake_shell(command, **kwargs):
        calls.append(("shell", command, (), kwargs))
        return _FakeSpawn(pid=4322)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    return calls


def test_spawn_helpers_pass_kwargs_through_and_attach_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helpers are pure passthroughs plus the tree-cleanup enablement."""

    calls = _capture_asyncio_spawns(monkeypatch)
    env = {"PATH": "/usr/bin"}

    process = asyncio.run(
        start_subprocess_exec("npm", "run", "build", cwd="/tmp", env=env)
    )
    kind, program, args, kwargs = calls[-1]
    assert (kind, program, args) == ("exec", "npm", ("run", "build"))
    assert kwargs["cwd"] == "/tmp"
    assert kwargs["env"] == env

    shell_process = asyncio.run(
        start_subprocess_shell("npm install", cwd="/tmp", env=env)
    )
    kind, command, _, shell_kwargs = calls[-1]
    assert (kind, command) == ("shell", "npm install")
    assert shell_kwargs["cwd"] == "/tmp"

    scope = getattr(process, "_arc_kill_scope", None)
    assert scope is not None, "the exec spawn must carry a tree-kill scope"
    shell_scope = getattr(shell_process, "_arc_kill_scope", None)
    assert shell_scope is not None, "the shell spawn must carry a tree-kill scope"

    if _IS_WINDOWS:
        # A fake process cannot be assigned to a real Job Object; the scope
        # must still attach so finalize degrades to the taskkill sweep.
        assert scope.job_handle is None
        assert shell_scope.job_handle is None
        assert "start_new_session" not in kwargs
        assert "start_new_session" not in shell_kwargs
    else:
        assert scope.pgid == 4321
        assert shell_scope.pgid == 4322
        # The POSIX enablement rides in as the spawn kwarg itself.
        assert kwargs["start_new_session"] is True
        assert shell_kwargs["start_new_session"] is True


def test_spawn_failure_propagates_without_a_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn that never starts keeps raising — cleanup never masks it."""

    async def refuse_to_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_to_spawn)

    with pytest.raises(OSError, match="spawn failed"):
        asyncio.run(start_subprocess_shell("anything"))


# ---------------------------------------------------------------------------
# Fast: finalize semantics
# ---------------------------------------------------------------------------


class _FakeLegacyProcess:
    """Minimal process double for the no-scope (legacy) finalize path."""

    def __init__(self, *, stuck_until_killed: bool) -> None:
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._stuck_until_killed = stuck_until_killed

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self) -> None:
        if self._stuck_until_killed and self.kill_calls == 0:
            await asyncio.sleep(30)
        if self.returncode is None:
            self.returncode = 0


def test_finalize_without_scope_keeps_legacy_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No scope: graceful terminate, bounded wait, then the kill escalation."""

    monkeypatch.setattr(processes, "_TREE_KILL_GRACE_SECONDS", 0.05)
    process = _FakeLegacyProcess(stuck_until_killed=True)

    asyncio.run(finalize_subprocess(process))

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode is not None


def test_finalize_force_kill_without_scope_skips_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processes, "_TREE_KILL_GRACE_SECONDS", 0.05)
    process = _FakeLegacyProcess(stuck_until_killed=True)

    asyncio.run(finalize_subprocess(process, force_kill=True))

    assert process.terminate_calls == 0
    assert process.kill_calls == 1


def test_finalize_on_already_exited_process_is_a_noop() -> None:
    process = _FakeLegacyProcess(stuck_until_killed=False)
    process.returncode = 0

    asyncio.run(finalize_subprocess(process))

    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_finalize_on_none_is_a_noop() -> None:
    asyncio.run(finalize_subprocess(None))


def test_real_spawn_carries_tree_scope_and_finalize_terminates_it() -> None:
    """A real one-shot process gets the scope; finalize terminates it quietly."""

    async def _run() -> tuple[object, object]:
        process = await start_subprocess_exec(sys.executable, "-c", "import sys; sys.exit(0)")
        scope = getattr(process, "_arc_kill_scope", None)
        await finalize_subprocess(process)
        return scope, process

    scope, process = asyncio.run(_run())

    assert scope is not None, "a real spawn must carry the tree-kill scope"
    if _IS_WINDOWS:
        # The launcher was alive when assigned, so the Job Object attach must
        # have succeeded for a real process handle.
        assert scope.job_handle is not None
    else:
        assert scope.pgid == process.pid
    assert process.returncode is not None


# ---------------------------------------------------------------------------
# Fast: mechanical pin — every spawn site goes through the tree-aware helper
# ---------------------------------------------------------------------------


def test_every_app_type_spawn_site_uses_the_tree_aware_helper() -> None:
    """No raw ``asyncio.create_subprocess_*`` call may remain in the handlers.

    A raw spawn silently opts out of tree cleanup (issue #181) — the exact
    regression this fix closes. Workload spawns must call
    ``core.processes.start_subprocess_exec``/``start_subprocess_shell``; the
    only asyncio spawn allowed inside the package is none at all (the helpers
    themselves live in ``core``).
    """

    offenders: list[str] = []
    for path in sorted(_APP_TYPE_HANDLER_ROOT.rglob("*.py")):
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RAW_SPAWN_NAMES
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "app_type_handler spawn sites must go through "
        "core.processes.start_subprocess_exec/start_subprocess_shell so "
        f"finalize_subprocess can kill the whole tree: {offenders}"
    )


# ---------------------------------------------------------------------------
# Slow: real process-tree teardown on the running platform
# ---------------------------------------------------------------------------

_CHILD_SOURCE = """\
import subprocess, sys, time
subprocess.Popen([sys.executable, "-u", sys.argv[1], sys.argv[2]])
time.sleep(120)
"""

_GRANDCHILD_SOURCE = """\
import json, os, socket, sys, time
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(("127.0.0.1", 0))
server.listen(1)
with open(sys.argv[1], "w", encoding="utf-8") as marker:
    json.dump({"port": server.getsockname()[1], "pid": os.getpid()}, marker)
time.sleep(120)
"""


def _write_tree_scripts(tmp_path: Path) -> tuple[Path, Path]:
    child = tmp_path / "child.py"
    grandchild = tmp_path / "grandchild.py"
    child.write_text(_CHILD_SOURCE, encoding="utf-8")
    grandchild.write_text(_GRANDCHILD_SOURCE, encoding="utf-8")
    return child, grandchild


async def _port_is_open(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    return True


async def _wait_port_closed(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not await _port_is_open(port):
            return True
        await asyncio.sleep(0.25)
    return False


def _windows_pid_alive(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return str(pid) in (result.stdout or "")


def _posix_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.slow
@pytest.mark.skipif(not _IS_WINDOWS, reason="the Job Object teardown runs on Windows")
def test_timeout_finalization_sweeps_the_windows_process_tree(tmp_path: Path) -> None:
    """cmd /c -> python -> python(port owner): the tree dies with the launcher.

    Mirrors the production shape (a shell command whose descendants outlive a
    timeout): the command is left running past its wait budget, then torn down
    through the same ``finalize_subprocess`` call the runners use. The
    grandchild owns a TCP port — the acceptance signal is that the port is
    released and the descendant PID is gone.
    """

    child, grandchild = _write_tree_scripts(tmp_path)
    marker = tmp_path / "grandchild.json"

    async def _run() -> None:
        process = await start_subprocess_shell(
            f'"{sys.executable}" "{child}" "{grandchild}" "{marker}"',
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=processes.build_subprocess_env(),
        )
        scope = getattr(process, "_arc_kill_scope", None)
        assert scope is not None and scope.job_handle is not None, (
            "a real Windows spawn must be assigned to a kill-on-close Job Object"
        )

        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert marker.exists(), "the grandchild never came up (no marker file)"
        info = json.loads(marker.read_text(encoding="utf-8"))

        assert await _port_is_open(info["port"]), "the grandchild must own the port before teardown"

        # The production timeout shape: the command outlives its budget.
        try:
            await asyncio.wait_for(process.communicate(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
        assert process.returncode is None, "the launcher must still be alive at teardown"

        await finalize_subprocess(process, force_kill=True)

        assert process.returncode is not None, "the launcher itself must be terminated"
        assert await _wait_port_closed(info["port"], timeout=10.0), (
            f"the grandchild still holds port {info['port']} after the tree teardown"
        )
        deadline = time.monotonic() + 10
        while _windows_pid_alive(info["pid"]) and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        assert not _windows_pid_alive(info["pid"]), (
            f"grandchild PID {info['pid']} survived the tree teardown"
        )

    asyncio.run(_run())


@pytest.mark.slow
@pytest.mark.skipif(_IS_WINDOWS, reason="the process-group teardown runs on POSIX")
@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups required")
def test_timeout_finalization_sweeps_the_posix_process_tree(tmp_path: Path) -> None:
    """sh -> python -> python(port owner): the group dies with the launcher.

    The launcher is spawned as a process-group leader (``start_new_session``);
    the grandchild inherits the group, so the same ``finalize_subprocess`` the
    runners use must release its port and leave the PID unkillable-alive.
    """

    child, grandchild = _write_tree_scripts(tmp_path)
    marker = tmp_path / "grandchild.json"

    async def _run() -> None:
        process = await start_subprocess_shell(
            f'"{sys.executable}" "{child}" "{grandchild}" "{marker}"',
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=processes.build_subprocess_env(),
        )
        scope = getattr(process, "_arc_kill_scope", None)
        assert scope is not None and scope.pgid == process.pid

        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert marker.exists(), "the grandchild never came up (no marker file)"
        info = json.loads(marker.read_text(encoding="utf-8"))

        assert await _port_is_open(info["port"]), "the grandchild must own the port before teardown"

        try:
            await asyncio.wait_for(process.communicate(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
        assert process.returncode is None, "the launcher must still be alive at teardown"

        await finalize_subprocess(process, force_kill=True)

        assert process.returncode is not None, "the launcher itself must be terminated"
        assert await _wait_port_closed(info["port"], timeout=10.0), (
            f"the grandchild still holds port {info['port']} after the tree teardown"
        )
        deadline = time.monotonic() + 10
        while _posix_pid_alive(info["pid"]) and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        assert not _posix_pid_alive(info["pid"]), (
            f"grandchild PID {info['pid']} survived the tree teardown"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Cross-module sanity: the backend teardown still flows through finalize
# ---------------------------------------------------------------------------


def test_backend_teardown_goes_through_finalize_subprocess() -> None:
    """``terminate_backend_process`` keeps finalize as its single teardown call."""

    import inspect

    source = inspect.getsource(backend_runtime.terminate_backend_process)
    assert "finalize_subprocess" in source

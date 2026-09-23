"""BackendRuntime: the single owner of the E2E backend session lifecycle.

The session-scoped backend runtime used to be assembled by its call paths
from private functions scattered across the web handler: startup here, the
reuse probe there, teardown and port release in a third place, with the live
session state parked on the handler. This module owns all of it.

The interface is three actions over one live session:

- ``ensure(port, db_path, fingerprint, runtime_env=...)`` -> ``RuntimeAcquisition``
- ``reset_db(runtime_env)`` -> ``(ok, note)``
- ``terminate(context)`` -> note

Two adapters implement the same policy (the policy lives in the
``BackendRuntime`` base class; adapters supply the mechanics):

- ``ProcessBackendRuntime``: the production adapter. Real subprocess, real
  HTTP readiness probe, real port ownership/release, real sqlite reset.
- ``InMemoryBackendRuntime``: the test adapter. No processes, sockets, files
  or npm; it records every action and scripts the outcomes, so tests drive
  the lifecycle through the interface instead of monkeypatching privates.

The merge-gate health probe (``probe_backend_health`` in the web handler) is
a stateless consumer: it calls ``spawn_backend_process`` and
``terminate_backend_process`` directly and never touches session state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.config import build_web_runtime_env, get_web_port
from core.processes import finalize_subprocess

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared command execution
# ---------------------------------------------------------------------------


def _tail(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


@dataclass
class _CommandResult:
    """Structured outcome of one shell command plus its rendered text.

    ``exit_code`` is ``None`` when the command never produced one (timeout,
    spawn failure); ``text`` is the model-facing rendering, whose shape is
    unchanged from the days when callers parsed the code back out of it.
    """

    exit_code: int | None
    text: str


async def _execute_web_test_command(
    command: str,
    cwd: str,
    timeout: float = 60.0,
    extra_env: dict[str, str] | None = None,
    web_port: int | None = None,
) -> _CommandResult:
    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8",
                **build_web_runtime_env(web_port=web_port),
                **(extra_env or {}),
            },
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")

        result = f"Exit Code: {process.returncode}\n"
        if output:
            result += f"STDOUT:\n{output}\n"
        if error:
            result += f"STDERR:\n{error}\n"
        if len(result) > 4000:
            result = result[:2000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-2000:]
        return _CommandResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            text=result,
        )
    except asyncio.TimeoutError:
        if process:
            await finalize_subprocess(process, force_kill=True)
        return _CommandResult(exit_code=None, text=f"Command timed out after {timeout} seconds.")
    except Exception as exc:
        return _CommandResult(exit_code=None, text=f"Execution failed: {str(exc)}")


# The command runner used for the runtime's own database commands. The web
# handler passes its (possibly test-patched) module-level runner at runtime
# construction; the default keeps the process adapter usable standalone.
CommandRunner = Callable[..., Awaitable["_CommandResult"]]


# ---------------------------------------------------------------------------
# Readiness probes
# ---------------------------------------------------------------------------


async def _wait_for_http_server(host: str, port: int, timeout: float = 20.0) -> bool:
    """Wait until the port answers with a complete HTTP response.

    A TCP listener alone does not prove the application layer is serving:
    startup work (route registration, asynchronous database initialization)
    may still be in flight when the socket starts accepting. Both backend
    startup and session reuse therefore require an HTTP round trip. Any
    response status counts - a 404 from an app without the template's
    `/api/health` endpoint still proves the HTTP stack answers requests -
    while connection failures and silent sockets keep the probe polling.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.5)
            continue
        try:
            request = (
                f"GET /api/health HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.5)
            continue
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        if status_line.startswith(b"HTTP/"):
            return True
        await asyncio.sleep(0.5)

    return False


async def _wait_for_tcp_server_shutdown(host: str, port: int, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.25)
        except OSError:
            return True

    return False


# ---------------------------------------------------------------------------
# Port ownership and force release
# ---------------------------------------------------------------------------


def _list_port_owner_pids(port: int) -> list[int]:
    normalized_port = str(int(port))
    pids: set[int] = set()

    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            for line in output.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                protocol, local_address, _, state, pid_text = parts[:5]
                if protocol.upper() != "TCP":
                    continue
                if state.upper() != "LISTENING":
                    continue
                if not local_address.endswith(f":{normalized_port}"):
                    continue
                try:
                    pid = int(pid_text)
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
        else:
            result = subprocess.run(
                ["lsof", "-ti", f"TCP:{normalized_port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pid = int(line)
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
    except Exception:
        return []

    current_pid = os.getpid()
    return sorted(pid for pid in pids if pid != current_pid)


def _read_linux_process_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return ""


def _read_unix_process_ps(pid: int) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "comm=", "-o", "args="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        line = (result.stdout or "").strip()
        match = re.match(r"^\s*(\d+)\s+(\S+)\s+(.*)$", line)
        if not match:
            return {}
        return {
            "ppid": match.group(1).strip(),
            "name": match.group(2).strip(),
            "command": match.group(3).strip(),
        }
    except Exception:
        return {}


def _read_macos_process_cwd(pid: int) -> str:
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        for line in (result.stdout or "").splitlines():
            if line.startswith("n"):
                return line[1:].strip()
    except Exception:
        return ""
    return ""


def _read_windows_process_info(pid: int) -> dict[str, str]:
    powershell_candidates = [
        ["powershell", "-NoProfile", "-Command"],
        ["pwsh", "-NoProfile", "-Command"],
    ]
    script = (
        f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; '
        'if ($p) { '
        'Write-Output ("PPID=" + [string]$p.ParentProcessId); '
        'Write-Output ("NAME=" + [string]$p.Name); '
        'Write-Output ("EXE=" + [string]$p.ExecutablePath); '
        'Write-Output ("CMD=" + [string]$p.CommandLine); '
        '}'
    )
    for prefix in powershell_candidates:
        try:
            result = subprocess.run(
                [*prefix, script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except Exception:
            continue

        info: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            info[key.strip().lower()] = value.strip()
        if info:
            return info
    return {}


def _get_process_fingerprint(pid: int) -> dict[str, str]:
    info: dict[str, str] = {"pid": str(pid)}

    if os.name == "nt":
        windows_info = _read_windows_process_info(pid)
        info["ppid"] = windows_info.get("ppid", "")
        info["name"] = windows_info.get("name", "")
        info["exe"] = windows_info.get("exe", "")
        info["command"] = windows_info.get("cmd", "")
        info["cwd"] = ""
        return info

    unix_info = _read_unix_process_ps(pid)
    info["ppid"] = unix_info.get("ppid", "")
    info["name"] = unix_info.get("name", "")
    info["command"] = unix_info.get("command", "")
    info["exe"] = ""
    if sys.platform.startswith("linux"):
        info["cwd"] = _read_linux_process_cwd(pid)
    elif sys.platform == "darwin":
        info["cwd"] = _read_macos_process_cwd(pid)
    else:
        info["cwd"] = ""
    return info


def _format_backend_instance_fingerprint(*, launcher_pid: int | None, port: int) -> str:
    owner_pids = _list_port_owner_pids(port)
    fingerprint_pids: list[int] = []
    if launcher_pid and launcher_pid > 0:
        fingerprint_pids.append(launcher_pid)
    fingerprint_pids.extend(pid for pid in owner_pids if pid not in fingerprint_pids)

    lines = [
        f"Platform: {sys.platform}",
        f"Launcher PID: {launcher_pid if launcher_pid and launcher_pid > 0 else 'unknown'}",
        f"Port Owner PID(s): {', '.join(str(pid) for pid in owner_pids) if owner_pids else 'none detected'}",
    ]
    if launcher_pid and launcher_pid > 0 and launcher_pid not in owner_pids:
        lines.append(
            "Note: launcher PID does not own the port directly. This is expected when `npm` or a shell spawns the actual backend child process."
        )

    for pid in fingerprint_pids:
        info = _get_process_fingerprint(pid)
        lines.extend(
            [
                f"- PID {pid}",
                f"  PPID: {info.get('ppid') or 'unknown'}",
                f"  Name: {info.get('name') or 'unknown'}",
                f"  Executable: {info.get('exe') or 'unknown'}",
                f"  Command: {info.get('command') or 'unknown'}",
                f"  CWD: {info.get('cwd') or 'unavailable'}",
            ]
        )

    return "\n".join(lines)


async def _force_kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return

    try:
        if os.name == "nt":
            process = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.communicate()
            return
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return


def _is_process_descendant(pid: int, ancestor_pid: int) -> bool:
    if pid == ancestor_pid:
        return True

    seen: set[int] = set()
    current_pid = pid
    while current_pid > 0 and current_pid not in seen:
        seen.add(current_pid)
        raw_parent_pid = _get_process_fingerprint(current_pid).get("ppid", "")
        try:
            parent_pid = int(raw_parent_pid)
        except (TypeError, ValueError):
            return False
        if parent_pid == ancestor_pid:
            return True
        current_pid = parent_pid
    return False


def _capture_owned_port_processes(port: int, launcher_pid: int) -> dict[int, dict[str, str]]:
    return {
        pid: _get_process_fingerprint(pid)
        for pid in _list_port_owner_pids(port)
        if _is_process_descendant(pid, launcher_pid)
    }


def _process_fingerprint_matches(expected: dict[str, str], current: dict[str, str]) -> bool:
    # `ppid` is deliberately excluded. Force-release is only needed when graceful
    # termination failed to kill the backend child, and in exactly that scenario
    # the launcher is dead - on POSIX the surviving child is re-parented, so its
    # ppid no longer matches the capture-time value. name/exe/command/cwd still
    # pin the identity against PID reuse.
    identity_keys = ("name", "exe", "command", "cwd")
    comparable_keys = [key for key in identity_keys if expected.get(key)]
    return bool(comparable_keys) and all(current.get(key) == expected[key] for key in comparable_keys)


async def _force_release_port(
    port: int,
    *,
    allowed_processes: dict[int, dict[str, str]],
) -> list[int]:
    killed_pids: list[int] = []
    for pid in _list_port_owner_pids(port):
        expected_fingerprint = allowed_processes.get(pid)
        if expected_fingerprint is None:
            continue
        current_fingerprint = _get_process_fingerprint(pid)
        if not _process_fingerprint_matches(expected_fingerprint, current_fingerprint):
            continue
        await _force_kill_pid(pid)
        killed_pids.append(pid)
    return killed_pids


async def _ensure_port_released(
    port: int,
    *,
    context: str,
    timeout: float = 5.0,
    allowed_processes: dict[int, dict[str, str]] | None = None,
) -> str:
    if await _wait_for_tcp_server_shutdown("127.0.0.1", port, timeout=timeout):
        return f"{context}: port {port} is released."

    owners_before_force = _list_port_owner_pids(port)
    if not allowed_processes:
        raise RuntimeError(
            f"{context}: port {port} is still occupied; refusing to terminate unknown "
            f"owner PID(s): {owners_before_force or 'unknown'}."
        )
    killed_pids = await _force_release_port(port, allowed_processes=allowed_processes)

    if await _wait_for_tcp_server_shutdown("127.0.0.1", port, timeout=10.0):
        if killed_pids:
            return (
                f"{context}: force-released port {port} by terminating PID(s) "
                f"{', '.join(str(pid) for pid in killed_pids)}."
            )
        return f"{context}: port {port} is released."

    owners_after_force = _list_port_owner_pids(port)
    raise RuntimeError(
        f"{context}: port {port} is still occupied after forced cleanup. "
        f"Owners before force: {owners_before_force or 'unknown'}. "
        f"Killed: {killed_pids or 'none'}. "
        f"Remaining owners: {owners_after_force or 'unknown'}."
    )


async def terminate_backend_process(
    process: asyncio.subprocess.Process | None, *, port: int | None = None
) -> str:
    """Tear one backend process down and wait for its port to be released.

    The single teardown mechanism of the module: graceful finalize, await the
    anchored output-tail drains, then a bounded wait on the port — force-killing
    only the processes captured as launcher descendants before the kill, and
    refusing (RuntimeError) when unknown owners still hold the port.
    """

    owned_processes = (
        _capture_owned_port_processes(port, process.pid)
        if process is not None and port is not None
        else {}
    )
    await finalize_subprocess(process, force_kill=False)
    await _await_output_tail_drains(process)

    if port is None:
        return "No port cleanup required."

    return await _ensure_port_released(
        port,
        context="Backend runtime cleanup",
        allowed_processes=owned_processes,
    )


# ---------------------------------------------------------------------------
# Backend output tails
# ---------------------------------------------------------------------------

# How much of a failed backend's console output is echoed into the test
# failure body. Startup crashes (the Express 5 wildcard-route throw, a bad
# import) print a stack trace well under this size; the cap keeps a chatty
# server that never became ready from flooding the TDD repair context.
_BACKEND_STARTUP_OUTPUT_LIMIT = 4000

# The tail buffer keeps this many raw bytes per stream so a startup crash is
# still observable when the process wrote a lot before dying. Ring size is
# deliberately larger than the echo limit: the newest bytes survive, and the
# memory cost per backend runtime is bounded.
_BACKEND_OUTPUT_TAIL_BYTES = 64 * 1024


class _ProcessOutputTail:
    """Background consumer of one subprocess pipe, keeping the newest bytes.

    The backend runtime's pipes must be read continuously for the whole
    process lifetime: a server that prints more than the OS pipe buffer would
    otherwise block on its next write and appear to hang. The newest
    ``_BACKEND_OUTPUT_TAIL_BYTES`` are retained so a failed startup can still
    echo the crashing output into the test failure body.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: bytearray = bytearray()

    async def consume(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            try:
                chunk = await stream.read(65536)
            except (OSError, ValueError):
                # A closed/invalid pipe ends the drain; the retained tail stays.
                return
            except asyncio.CancelledError:
                # Cancellation is a stop request, not an error: exit without
                # swallowing it, so `Task.cancel()` keeps its meaning for
                # test-harness teardowns and loop shutdown paths.
                raise
            if not chunk:
                # Cancelled reads surface as EOF (the StreamReader ends its
                # pending waiters with an empty result); a real EOF ends here
                # too. Either way the newest bytes are already retained.
                return
            with self._lock:
                self._chunks.extend(chunk)
                if len(self._chunks) > _BACKEND_OUTPUT_TAIL_BYTES:
                    del self._chunks[:-_BACKEND_OUTPUT_TAIL_BYTES]

    def text(self) -> str:
        with self._lock:
            raw = bytes(self._chunks)
        text = raw.decode("utf-8", errors="replace")
        # The ring cut can split a multi-byte UTF-8 sequence at the buffer
        # head, decoding to a stray U+FFFD that would lead the echoed output.
        # Drop that one leading replacement character; every later character
        # is a complete sequence.
        if text.startswith("\ufffd"):
            text = text[1:]
        return text


def _start_output_tails(
    process: asyncio.subprocess.Process,
) -> tuple[_ProcessOutputTail, _ProcessOutputTail]:
    """Spawn consumers for both pipes of a freshly started runtime.

    The consuming tasks and their tails are anchored on the ``Process`` object
    itself: asyncio keeps only weak references to running tasks, so a task
    created here and dropped would be garbage-collected mid-session and the
    pipes would fill up again. Attaching to the process (which every caller
    holds for the runtime's whole lifetime) keeps the drains alive until the
    process is torn down.
    """

    stdout_tail = _ProcessOutputTail()
    stderr_tail = _ProcessOutputTail()
    drains: list[asyncio.Task[None]] = []
    for tail, stream in ((stdout_tail, process.stdout), (stderr_tail, process.stderr)):
        try:
            drains.append(asyncio.get_running_loop().create_task(tail.consume(stream)))
        except RuntimeError:
            # Only reachable if a future caller invokes this outside a running
            # loop (today every call site is inside an async function, which
            # always has one). Never silent: an undrained pipe would freeze a
            # chatty backend, and that failure mode must be diagnosable.
            logger.warning(
                "No running event loop to anchor the %s pipe drain; the backend "
                "runtime's output pipe may block once the OS buffer fills.",
                stream,
            )
            continue
    # Strong reference for the process lifetime: the caller holds the Process
    # (E2E session state, test locals), which transitively keeps the drain
    # tasks alive — the running loop alone would not. The attribute must stay
    # populated for as long as the Process object lives: a second
    # terminate_backend_process call on the same object still reads it, and
    # dropping it mid-flight would orphan still-pending drains back to weak
    # references.
    process._arc_output_tails = (stdout_tail, stderr_tail, drains)  # type: ignore[attr-defined]
    return stdout_tail, stderr_tail


async def _await_output_tail_drains(
    process: asyncio.subprocess.Process | None,
    timeout: float = 2.0,
) -> None:
    """Wait for the anchored pipe drains of a terminated process to finish.

    The process death closes the pipes, so the drain tasks normally exit on
    their next read; awaiting them here keeps teardown deterministic (no
    pending-task warnings when the surrounding event loop closes right after)
    and bounds how long a stuck drain can outlive its process.
    """

    if process is None:
        return
    drains = getattr(process, "_arc_output_tails", None)
    if not drains:
        return
    pending = [task for task in drains[2] if not task.done()]
    if not pending:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        for task in pending:
            task.cancel()


async def _format_backend_output(
    stdout_tail: _ProcessOutputTail,
    stderr_tail: _ProcessOutputTail,
) -> str:
    """Render the retained console output of a backend, newest bytes first.

    Callers invoke this after ``terminate_backend_process`` has already
    awaited the drain tasks (process death closed the pipes, every buffered
    byte is in the tails), so no flush wait is needed here.
    """

    sections: list[str] = []
    stdout_text = _tail(stdout_tail.text(), _BACKEND_STARTUP_OUTPUT_LIMIT)
    stderr_text = _tail(stderr_tail.text(), _BACKEND_STARTUP_OUTPUT_LIMIT)
    if stdout_text:
        sections.append(f"STDOUT:\n{stdout_text}")
    if stderr_text:
        sections.append(f"STDERR:\n{stderr_text}")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Backend start command and E2E runtime env
# ---------------------------------------------------------------------------


def _read_package_scripts(package_dir: str) -> dict[str, str]:
    package_json_path = os.path.join(package_dir, "package.json")
    if not os.path.exists(package_json_path):
        return {}

    try:
        with open(package_json_path, "r", encoding="utf-8") as package_file:
            package_data = json.load(package_file)
    except Exception:
        return {}

    scripts = package_data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _resolve_backend_start_command(backend_path: str) -> str | None:
    scripts = _read_package_scripts(backend_path)
    if "start" in scripts:
        return "npm run start"
    return None


def _slugify_identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return normalized or "playwright-e2e"


# Cap on the human-readable prefix of the E2E database filename: the joined
# target list is unbounded, and only the suite hash below carries the
# isolation semantics (see `_build_e2e_runtime_env`).
_E2E_DB_SUITE_LABEL_MAX_LENGTH = 24


def _build_e2e_runtime_env(workspace_path: str, targets: list[str], web_port: int | None = None) -> dict[str, str]:
    normalized_targets = [target.replace("\\", "/").strip() for target in targets if target and str(target).strip()]
    # The joined target list is unbounded (a handful of E2E specs alone spelled
    # a ~140-char filename, approaching Windows MAX_PATH once the directory
    # prefix and sqlite's -wal/-shm siblings are added). The suite hash below
    # already carries the isolation semantics, so the label only keeps a short
    # human-readable prefix.
    suite_label = (
        _slugify_identifier("-".join(normalized_targets) or "playwright-e2e")[
            :_E2E_DB_SUITE_LABEL_MAX_LENGTH
        ].rstrip("-")
    )
    suite_hash = hashlib.sha1("\n".join(normalized_targets or ["playwright-e2e"]).encode("utf-8")).hexdigest()[:10]
    backend_path = os.path.join(workspace_path, "backend")
    e2e_db_root = os.path.join(backend_path, ".arc-test-db")
    e2e_db_path = os.path.abspath(os.path.join(e2e_db_root, f"{suite_label}-{suite_hash}.sqlite"))
    resolved_port = int(web_port) if web_port is not None else get_web_port()
    return {
        **build_web_runtime_env(web_port=resolved_port),
        # The template's `playwright.config.js` and the agent-facing stack notes
        # both document `PLAYWRIGHT_BASE_URL` as the origin under test. Nothing
        # used to set it, so Playwright fell back to its own default port and
        # every E2E run navigated to a dead origin.
        "PLAYWRIGHT_BASE_URL": f"http://127.0.0.1:{resolved_port}",
        "ARC_DB_FILE": e2e_db_path,
        "ARC_E2E_DB_PATH": e2e_db_path,
        "ARC_E2E_DB_LABEL": suite_label,
    }


# ---------------------------------------------------------------------------
# Backend source fingerprint (reuse input)
# ---------------------------------------------------------------------------

_BACKEND_FINGERPRINT_SKIPPED_DIRS = frozenset(
    # `test-e2e` specs run in the Playwright process, never inside the express
    # server, so spec-only edits must not force a server restart.
    # `.arc-test-db` holds the per-suite sqlite files, not server code.
    {"node_modules", ".arc-test-db", "dist", "dist-ssr", "coverage", ".git", "test-e2e"}
)


def backend_source_fingerprint(backend_path: str) -> str | None:
    """Content hash of the backend sources a running E2E server executes.

    Feeds the session-scoped E2E runtime reuse decision: a live server may
    only be reused while the code it loaded is byte-for-byte unchanged.
    Returns ``None`` when the backend directory is missing, which makes the
    caller fall back to a fresh start.

    Deliberately a pure source-tree hash: the other reuse dimensions (web
    port, E2E database path) are session-key comparisons in
    ``BackendRuntime.ensure``, and runtime-env values that vary per attempt
    would spuriously break reuse here.
    """

    root = Path(backend_path)
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    visited_real_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real_dir = os.path.realpath(dirpath)
        if real_dir in visited_real_dirs:
            dirnames[:] = []
            continue
        visited_real_dirs.add(real_dir)
        dirnames[:] = sorted(name for name in dirnames if name not in _BACKEND_FINGERPRINT_SKIPPED_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Database preparation for the backend session
# ---------------------------------------------------------------------------

# A fresh `db:prepare:e2e` right after a stale teardown can hit
# SQLITE_CANTOPEN: on Windows the force-killed backend's file handles are
# released asynchronously, so the sqlite file may still be locked for a few
# hundred milliseconds after the port already reads as released. Retries are
# bounded and the wait doubles as the handle-release grace period; the port
# is re-checked between attempts (adapter hook) so a known leftover is
# cleaned before spending another prepare run.
_E2E_DB_PREPARE_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.3, 0.5)


async def _prepare_e2e_database(
    workspace_path: str,
    runtime_env: dict[str, str],
    runner: CommandRunner,
) -> tuple[bool, int | None, str]:
    backend_path = os.path.join(workspace_path, "backend")
    prepare_result = await runner(
        "npm run db:prepare:e2e",
        cwd=backend_path,
        timeout=60.0,
        extra_env=runtime_env,
    )
    return prepare_result.exit_code == 0, prepare_result.exit_code, prepare_result.text


async def _seed_e2e_database(
    workspace_path: str,
    runtime_env: dict[str, str],
    runner: CommandRunner,
) -> tuple[bool, str]:
    backend_path = os.path.join(workspace_path, "backend")
    seed_result = await runner(
        "npm run db:seed",
        cwd=backend_path,
        timeout=60.0,
        extra_env=runtime_env,
    )
    return seed_result.exit_code == 0, seed_result.text


def _reset_sqlite_database_rows(db_path: str) -> tuple[bool, str]:
    """Delete every row of every user table while keeping schema objects.

    The file-level reset in `db:prepare:e2e` deletes the sqlite file, which
    cannot run while the reused E2E server holds an open handle on it (an
    unrecoverable in-use error on Windows). A row-level wipe reproduces the
    prepared state - schema intact, zero rows, autoincrement counters reset -
    against the same file the live server reads. Schema sources are guaranteed
    unchanged by the backend fingerprint check that gates the reuse.

    A schema with user triggers refuses the wipe: `DELETE` fires them, and a
    trigger writing into an already-cleared table would leave rows behind that
    a fresh `db:prepare:e2e` would never contain. Refusing keeps the caller on
    the fresh-start path, which is always semantically equivalent.
    """

    if not os.path.exists(db_path):
        return False, f"E2E database file is missing: {db_path}"
    try:
        connection = sqlite3.connect(db_path, timeout=5.0)
    except (sqlite3.Error, OSError) as exc:
        # OSError/PermissionError included: on Windows a sharing violation on
        # the file the live server holds open surfaces here, and the caller
        # must take the fresh-start fallback instead of crashing.
        return False, f"{type(exc).__name__}: {exc}"
    try:
        connection.execute("PRAGMA foreign_keys = OFF;")
        trigger_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        if trigger_names:
            return False, (
                "E2E database schema defines user triggers ("
                + ", ".join(trigger_names)
                + "); a row-level wipe would fire them and diverge from the "
                "file-level `db:prepare:e2e` state."
            )
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        if not table_names:
            return False, "E2E database has no user tables; the schema was never initialized."
        for name in table_names:
            connection.execute('DELETE FROM "' + name.replace('"', '""') + '"')
        has_sequence = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
        ).fetchone()
        if has_sequence is not None:
            connection.execute("DELETE FROM sqlite_sequence")
        connection.commit()
        return True, "Cleared rows of: " + ", ".join(table_names)
    except (sqlite3.Error, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Backend process spawn
# ---------------------------------------------------------------------------


@dataclass
class BackendSpawn:
    """Outcome of one backend process spawn.

    ``handle`` is ``None`` on failure; ``detail`` is the startup cleanup note
    on success and the failure body (with the captured process output) on
    failure.
    """

    handle: Any | None
    start_command: str
    detail: str
    instance_fingerprint: str


async def spawn_backend_process(
    workspace_path: str,
    runtime_env: dict[str, str],
    web_port: int | None = None,
) -> BackendSpawn:
    """Start the backend runtime and wait until it serves HTTP.

    The returned handle carries the anchored pipe drains
    (``_arc_output_tails``); the caller owns that object for the runtime's
    whole lifetime and must clean it up through ``terminate_backend_process``
    (session runtimes do so via ``BackendRuntime.terminate``) — the single
    teardown path that releases the port and awaits the drains.
    """

    backend_path = os.path.join(workspace_path, "backend")
    resolved_port = int(web_port) if web_port is not None else get_web_port()
    start_command = _resolve_backend_start_command(backend_path)
    if not start_command:
        return BackendSpawn(
            None,
            "",
            "Backend package.json must define `start` so the backend can host the built frontend on the single web port.",
            "",
        )

    try:
        startup_cleanup_note = await _ensure_port_released(
            resolved_port,
            context="Pre-start port cleanup",
            timeout=1.0,
        )
    except RuntimeError as exc:
        return BackendSpawn(None, start_command, str(exc), "")

    try:
        backend_process = await asyncio.create_subprocess_shell(
            start_command,
            cwd=backend_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                **runtime_env,
            },
        )
    except Exception as exc:
        return BackendSpawn(
            None, start_command, f"Failed to start backend runtime with `{start_command}`: {str(exc)}", ""
        )

    # Consume the pipes from the first moment: a chatty server would otherwise
    # block on a full OS pipe buffer during the startup wait itself. The tails
    # also retain the newest output for the failure body below.
    stdout_tail, stderr_tail = _start_output_tails(backend_process)

    server_ready = await _wait_for_http_server("127.0.0.1", resolved_port, timeout=20.0)
    if not server_ready:
        cleanup_note = ""
        try:
            cleanup_note = await terminate_backend_process(backend_process, port=resolved_port)
        except Exception as cleanup_exc:
            cleanup_note = f"Backend runtime cleanup after failed startup also failed: {cleanup_exc}"
        captured_output = await _format_backend_output(stdout_tail, stderr_tail)
        return BackendSpawn(
            None,
            start_command,
            (
                f"Failed to start backend runtime with `{start_command}` on port {resolved_port} "
                "within 20 seconds.\n"
                f"{startup_cleanup_note}\n"
                f"{cleanup_note}\n"
                f"=== Backend Process Output ===\n{captured_output or '(the process produced no output)'}"
            ),
            "",
        )

    instance_fingerprint = _format_backend_instance_fingerprint(
        launcher_pid=backend_process.pid,
        port=resolved_port,
    )
    return BackendSpawn(backend_process, start_command, startup_cleanup_note, instance_fingerprint)


# ---------------------------------------------------------------------------
# The session lifecycle: three actions, two adapters
# ---------------------------------------------------------------------------


@dataclass
class BackendSession:
    """A backend runtime kept alive across E2E attempts within one TDD session.

    ``port``, ``db_path`` and ``fingerprint`` are the reuse key compared by
    ``BackendRuntime.ensure``; ``handle`` is adapter-private (an asyncio
    subprocess for the process adapter) and opaque to the policy.
    """

    handle: Any | None
    port: int
    db_path: str
    fingerprint: str
    start_command: str
    startup_detail: str
    instance_fingerprint: str


@dataclass
class RuntimeAcquisition:
    """Outcome of one ``BackendRuntime.ensure`` call.

    ``session`` is ``None`` when no runtime could be acquired; ``failure_stage``
    then says which stage failed (``"database"`` before any server existed, or
    ``"start"``). ``db_output`` is the database section's content (reset note on
    reuse, prepare output on a fresh start); ``cleanup_note`` carries stale
    teardown evidence and reset-fallback reasons; ``stage_seconds`` reports the
    internally measured ``backend_runtime`` / ``database_prepare`` durations so
    the caller's stage timing stays exact.
    """

    session: BackendSession | None = None
    reused: bool = False
    db_output: str = ""
    cleanup_note: str = ""
    failure_stage: str | None = None
    start_command: str = ""
    startup_detail: str = ""
    stage_seconds: dict[str, float] = field(default_factory=dict)


class BackendRuntime:
    """Owner of the session-scoped E2E backend runtime lifecycle.

    Three actions cover the whole lifecycle; both adapters share the policy
    implemented here and only supply the mechanics hooks below.

    Reuse decision (``ensure``) — inputs, confirmed on this interface:

    - the requested port, E2E database path and backend source fingerprint
      must all equal the ones the live session was started with; any mismatch
      rebuilds;
    - the fingerprint is ``backend_source_fingerprint``: a pure content hash
      of the backend sources a running server executes. Installed libraries
      (``node_modules``), build outputs (``dist``/``dist-ssr``), the per-suite
      database directory (``.arc-test-db``), E2E specs (``test-e2e``),
      coverage and ``.git`` are excluded — ``npm install`` or a frontend
      rebuild never forces a restart, any edit to executed source does;
    - the frontend build cache is NOT a reuse input here: the build outcome
      gates the attempt before the runtime is consulted;
    - a ``None`` fingerprint (backend directory missing) never reuses;
    - the server must still be alive and answer an HTTP round trip — an open
      TCP port is not enough.

    A reuse hit still pays one ``reset_db`` (row-level wipe + re-seed) so the
    attempt starts from the prepared-and-seeded state. When that reset is
    impossible, ``ensure`` falls back to terminate + file-level prepare + fresh
    start within the same call and reports the reason in
    ``RuntimeAcquisition.cleanup_note``.

    A failed fresh-start database prepare is retried with short backoffs
    (``_E2E_DB_PREPARE_RETRY_DELAYS_SECONDS``): on Windows the force-killed
    stale backend releases its sqlite file handles asynchronously, so an
    immediately following prepare can fail non-deterministically. Each retry
    re-checks the port first (``_prepare_retry_cleanup``); exhaustion returns
    ``failure_stage="database"`` with the retry history in ``cleanup_note``.

    Termination semantics (``terminate``), confirmed on this interface: yes —
    teardown waits for the port to be released together with the process.
    One teardown path: graceful finalize of the launcher, await of the
    anchored output-tail drains, then a bounded wait on the port with
    force-kill only for processes captured as launcher descendants before the
    kill; unknown port owners are never killed (the note reports the refusal).
    A mid-session crash's retained output tail is appended to the teardown
    note. ``terminate`` never raises; failures come back as notes.

    ``ensure``/``terminate`` never leave a half-state: a failed fresh start
    clears the session, so the next ``ensure`` starts over.
    """

    def __init__(self) -> None:
        self._session: BackendSession | None = None

    @property
    def session(self) -> BackendSession | None:
        """The live session, if one is currently held."""
        return self._session

    async def ensure(
        self,
        port: int,
        db_path: str,
        fingerprint: str | None,
        *,
        runtime_env: dict[str, str],
    ) -> RuntimeAcquisition:
        """Return a live session matching the requested key, starting one if needed.

        See the class docstring for the reuse inputs and the fallback order.
        """

        loop = asyncio.get_running_loop()
        stage_seconds: dict[str, float] = {}
        cleanup_note = ""

        def _record(stage: str, started: float) -> None:
            stage_seconds[stage] = stage_seconds.get(stage, 0.0) + (loop.time() - started)

        session = self._session
        if (
            session is not None
            and fingerprint is not None
            and session.port == port
            and session.db_path == db_path
            and session.fingerprint == fingerprint
        ):
            started = loop.time()
            serving = await self._serving(session)
            _record("backend_runtime", started)
            if serving:
                started = loop.time()
                reset_ok, reset_output = await self.reset_db(runtime_env)
                _record("database_prepare", started)
                if reset_ok:
                    return RuntimeAcquisition(
                        session=session,
                        reused=True,
                        db_output=reset_output,
                        stage_seconds=stage_seconds,
                    )
                cleanup_note = (
                    "Live E2E runtime reset was not possible; fell back to a fresh start: "
                    f"{reset_output}"
                )

        stale_note = await self.terminate("Stale E2E runtime cleanup")
        if stale_note:
            cleanup_note = f"{cleanup_note}\n{stale_note}" if cleanup_note else stale_note

        db_output = ""
        retry_notes: list[str] = []
        prepare_ok = False
        # One initial attempt plus one retry per configured delay; the sleep
        # and the port re-check happen before each retry, never before the
        # first attempt.
        total_attempts = len(_E2E_DB_PREPARE_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(total_attempts):
            if attempt > 0:
                await asyncio.sleep(_E2E_DB_PREPARE_RETRY_DELAYS_SECONDS[attempt - 1])
                retry_cleanup_note = await self._prepare_retry_cleanup(port)
                if retry_cleanup_note:
                    retry_notes.append(retry_cleanup_note)
            started = loop.time()
            prepare_ok, _prepare_exit, prepare_output = await self._prepare_db(runtime_env)
            _record("database_prepare", started)
            db_output = prepare_output
            if prepare_ok:
                break
            retry_notes.append(f"prepare attempt {attempt + 1} of {total_attempts} failed")
        if retry_notes:
            if prepare_ok:
                retry_summary = (
                    "E2E database prepare succeeded on retry after earlier failed attempt(s)."
                )
            else:
                retry_summary = (
                    f"E2E database prepare failed after {total_attempts} attempts; "
                    "retries exhausted."
                )
            cleanup_note = f"{cleanup_note}\n{retry_summary}" if cleanup_note else retry_summary
            cleanup_note = f"{cleanup_note}\n" + "\n".join(retry_notes)
        if not prepare_ok:
            return RuntimeAcquisition(
                db_output=db_output,
                cleanup_note=cleanup_note,
                failure_stage="database",
                stage_seconds=stage_seconds,
            )

        started = loop.time()
        spawn = await self._spawn(port, runtime_env)
        _record("backend_runtime", started)
        if spawn.handle is None:
            return RuntimeAcquisition(
                db_output=db_output,
                cleanup_note=cleanup_note,
                failure_stage="start",
                start_command=spawn.start_command,
                startup_detail=spawn.detail,
                stage_seconds=stage_seconds,
            )

        self._session = BackendSession(
            handle=spawn.handle,
            port=port,
            db_path=db_path,
            fingerprint=fingerprint or "",
            start_command=spawn.start_command,
            startup_detail=spawn.detail,
            instance_fingerprint=spawn.instance_fingerprint,
        )
        return RuntimeAcquisition(
            session=self._session,
            reused=False,
            db_output=db_output,
            cleanup_note=cleanup_note,
            stage_seconds=stage_seconds,
        )

    async def reset_db(self, runtime_env: dict[str, str]) -> tuple[bool, str]:
        """Reset the E2E database rows while the backend runtime stays alive.

        ``db:prepare:e2e`` recreates the database file, which cannot run
        against a server that holds the file open. The row-level wipe plus a
        ``db:seed`` re-run reproduces the prepared-and-seeded state on the same
        file; a failure here makes ``ensure`` fall back to a fresh start.
        """

        raise NotImplementedError

    async def terminate(self, context: str) -> str:
        """Tear down the session runtime, if one is alive. Never raises."""

        session = self._session
        if session is None:
            return ""
        self._session = None
        try:
            return await self._stop(session, context)
        except Exception as exc:
            return f"Backend runtime cleanup failed: {exc}"

    # Adapter hooks ------------------------------------------------------------

    async def _spawn(self, port: int, runtime_env: dict[str, str]) -> BackendSpawn:
        raise NotImplementedError

    async def _serving(self, session: BackendSession) -> bool:
        raise NotImplementedError

    async def _stop(self, session: BackendSession, context: str) -> str:
        raise NotImplementedError

    async def _prepare_db(self, runtime_env: dict[str, str]) -> tuple[bool, int | None, str]:
        raise NotImplementedError

    async def _prepare_retry_cleanup(self, port: int) -> str:
        """Best-effort cleanup between database prepare retries. Never raises.

        Default is a no-op; the process adapter re-checks the port and reports
        its state before the next attempt.
        """

        return ""


class ProcessBackendRuntime(BackendRuntime):
    """Production adapter: real subprocess, HTTP probe, port release, sqlite."""

    def __init__(self, workspace_path: str, *, command_runner: CommandRunner | None = None) -> None:
        super().__init__()
        self.workspace_path = workspace_path
        self._command_runner = command_runner

    def _runner(self) -> CommandRunner:
        return self._command_runner or _execute_web_test_command

    async def _spawn(self, port: int, runtime_env: dict[str, str]) -> BackendSpawn:
        return await spawn_backend_process(self.workspace_path, runtime_env, web_port=port)

    async def _serving(self, session: BackendSession) -> bool:
        process = session.handle
        if not isinstance(process, asyncio.subprocess.Process):
            return False
        if process.returncode is not None:
            # A dead process cannot serve; skip the HTTP probe's full timeout.
            return False
        return await _wait_for_http_server("127.0.0.1", session.port, timeout=5.0)

    async def _stop(self, session: BackendSession, context: str) -> str:
        note = await terminate_backend_process(session.handle, port=session.port)
        # A server that crashed mid-session (the chatty-output scenario the
        # drains defend against) leaves its dying words only in the tail
        # buffers; surface them here so the teardown note carries the crash
        # evidence, mirroring the startup-failure body.
        anchor = getattr(session.handle, "_arc_output_tails", None)
        if anchor is not None:
            captured = await _format_backend_output(anchor[0], anchor[1])
            if captured:
                note = (
                    f"{note}\n=== Backend Process Output (session teardown) ===\n{captured}"
                )
        return note

    async def _prepare_db(self, runtime_env: dict[str, str]) -> tuple[bool, int | None, str]:
        return await _prepare_e2e_database(self.workspace_path, runtime_env, runner=self._runner())

    async def _prepare_retry_cleanup(self, port: int) -> str:
        # No session survives to this point, so no owned-process set exists;
        # an occupied port here belongs to an unknown owner that must not be
        # killed — surface it as a note and let the retry proceed regardless.
        try:
            return await _ensure_port_released(
                port,
                context="E2E database prepare retry",
                timeout=1.0,
                allowed_processes={},
            )
        except RuntimeError as exc:
            return str(exc)

    async def reset_db(self, runtime_env: dict[str, str]) -> tuple[bool, str]:
        db_path = runtime_env.get("ARC_E2E_DB_PATH", "")
        # Off the event loop: a row-level wipe of a large sqlite file is pure
        # blocking I/O and must not freeze concurrent runner work.
        reset_ok, reset_output = await asyncio.to_thread(_reset_sqlite_database_rows, db_path)
        if not reset_ok:
            return False, f"Row-level reset of the live E2E database was not possible: {reset_output}"
        seed_ok, seed_output = await _seed_e2e_database(self.workspace_path, runtime_env, runner=self._runner())
        if not seed_ok:
            return False, (
                "Row-level reset of the live E2E database succeeded, but re-seeding "
                f"via `npm run db:seed` did not.\n{seed_output}"
            )
        return True, (
            "Reset the live E2E database at row level and re-seeded it; the backend runtime was kept alive.\n"
            f"{reset_output}\n{seed_output}"
        )


class InMemoryBackendRuntime(BackendRuntime):
    """Test adapter: records every action, scripts every outcome.

    Same policy as the process adapter (``ensure``/``terminate`` live in the
    base class); the hooks record into inspectable lists and return the
    scripted flags below. No processes, sockets, files or npm are touched.
    """

    def __init__(self) -> None:
        super().__init__()
        # Inspectable recordings, in call order.
        self.started: list[int] = []
        self.stopped: list[str] = []
        self.resets: list[str] = []
        self.prepared: list[str] = []
        # Scripted outcomes a test flips between calls.
        self.serving = True
        self.prepare_ok = True
        self.reset_ok = True
        self.spawn_ok = True
        self.prepare_output = "prepared"
        self.reset_note = "reset ok"
        self.spawn_failure_detail = "crashed on boot: ERR_MODULE_NOT_FOUND"
        self.stop_note = "released"
        self.start_command = "npm run start"
        self.startup_detail = "startup ok"
        self.instance_fingerprint = "launcher:4321"

    async def _spawn(self, port: int, runtime_env: dict[str, str]) -> BackendSpawn:
        self.started.append(port)
        if not self.spawn_ok:
            return BackendSpawn(None, self.start_command, self.spawn_failure_detail, "")
        return BackendSpawn(
            object(), self.start_command, self.startup_detail, self.instance_fingerprint
        )

    async def _serving(self, session: BackendSession) -> bool:
        return self.serving

    async def _stop(self, session: BackendSession, context: str) -> str:
        self.stopped.append(context)
        return self.stop_note

    async def _prepare_db(self, runtime_env: dict[str, str]) -> tuple[bool, int | None, str]:
        self.prepared.append(runtime_env.get("ARC_E2E_DB_PATH", ""))
        return self.prepare_ok, 0 if self.prepare_ok else 1, self.prepare_output

    async def reset_db(self, runtime_env: dict[str, str]) -> tuple[bool, str]:
        db_path = runtime_env.get("ARC_E2E_DB_PATH", "")
        self.resets.append(db_path)
        return self.reset_ok, self.reset_note

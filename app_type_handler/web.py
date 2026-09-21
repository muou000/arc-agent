import re
import os
import json
import sys
import asyncio
import shutil
import sqlite3
import logging
import subprocess
import signal
import hashlib
import inspect
import threading
import time
import urllib.request
from contextlib import suppress

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .base import AppTypeHandler, GlueAnchorSpec, TEMPLATE_ID_BY_APP_TYPE
from .path_validation import is_scoped_test_path, normalize_safe_relative_path
from .test_results import TestRunResult, parse_test_run
from .template_patches import (
    ALREADY_APPLIED,
    APPLIED,
    SKIPPED,
    UNRECOGNIZED,
    apply_template_patches,
)
from core.config import build_web_runtime_env, get_web_base_url, get_web_port
from core.processes import finalize_subprocess

logger = logging.getLogger(__name__)

async def _emit_log(log_cb: Callable[..., Awaitable[None] | None], *args) -> None:
    result = log_cb(*args)
    if inspect.isawaitable(result):
        await result


NPM_INSTALL_TIMEOUT_SECONDS = 900.0
# The plain attempt is capped tighter on purpose: on npm 10.x the optional-peer
# resolution for vitest can wedge arborist instead of failing fast, and waiting
# the full budget before trying the fallback wastes minutes on every install.
NPM_PRIMARY_ATTEMPT_TIMEOUT_SECONDS = 240.0
LEGACY_PEER_DEPS_FLAG = "--legacy-peer-deps"

# Node release lines where unflagged require(esm) is available (the template's
# jsdom dependency chain needs it). Line 21 never received the backport.
_REQUIRE_ESM_MINIMUMS = ((20, 19), (22, 12), (23, 2))


def _node_supports_require_esm(version_text: str) -> bool:
    match = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", str(version_text or "").strip())
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    if major > 23:
        return True
    return any(major == line_major and minor >= line_minor for line_major, line_minor in _REQUIRE_ESM_MINIMUMS)
# Generous because a cold machine downloads ~150 MB of browser binaries. Once
# the machine-wide Playwright cache is warm the command exits in seconds.
PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS = 900.0
# The single timeout source for the E2E test-runner command (the Playwright
# batch). The helper's own npm-script invocations inside the E2E attempt
# (db:prepare / db:seed, frontend build) keep their individual budgets —
# they are not per-path forks of the runner timeout this constant replaced.
E2E_RUNNER_TIMEOUT_SECONDS = 120.0
# The browser task starts with npm, but waits until npm has materialized the
# local Playwright CLI before touching the workspace. This preserves the
# package-version coupling while overlapping the browser download with the
# remainder of dependency installation.
PLAYWRIGHT_CLI_WAIT_TIMEOUT_SECONDS = 60.0
PLAYWRIGHT_CLI_POLL_INTERVAL_SECONDS = 0.1
# Escape hatch for machines that intentionally run without browser binaries or
# without the network access the download requires.
_BROWSER_INSTALL_SKIP_VALUES = {"1", "true", "yes", "on"}

# Files every web workspace must keep resolving the runtime origin from; the
# post-template gate asserts them on the scaffolded workspace. The officially
# provisioned template (baked into the ARC-Bench runner image) reads the
# Playwright origin from ``PLAYWRIGHT_BASE_URL``, which the E2E runner exports,
# so any env-driven origin satisfies the contract - only a config that
# hardcodes the port with no environment escape hatch fails it.
PORT_TEMPLATE_CONTRACT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("backend/src/index.js", ("process.env.PORT",)),
    ("frontend/vite.config.js", ("process.env.ARC_WEB_PORT",)),
    (
        "backend/playwright.config.js",
        (
            "process.env.PLAYWRIGHT_BASE_URL",
            "process.env.ARC_WEB_BASE_URL",
            "process.env.ARC_WEB_PORT",
        ),
    ),
)


def _browser_install_skipped() -> bool:
    return os.environ.get("ARC_SKIP_BROWSER_INSTALL", "").strip().lower() in _BROWSER_INSTALL_SKIP_VALUES


def _browser_install_command(backend_dir: str) -> str:
    """Browser install command that works with either template flavour.

    The bundled template exposes an ``e2e:install-browsers`` script; the
    officially provisioned one (ARC-Bench runner image) does not, so fall back
    to the equivalent ``npx`` invocation - the CLI resolves from the locally
    installed ``playwright`` package, whose browser build matches the
    ``@playwright/test`` runner because both dependency ranges float to the
    same latest 1.x release.
    """
    manifest = os.path.join(backend_dir, "package.json")
    try:
        with open(manifest, "r", encoding="utf-8") as file:
            scripts = json.loads(file.read()).get("scripts") or {}
    except (OSError, ValueError):
        return "npm run e2e:install-browsers"
    if scripts.get("e2e:install-browsers"):
        return "npm run e2e:install-browsers"
    return "npx playwright install chromium chromium-headless-shell"


def _playwright_dependency_declared(backend_dir: str) -> bool:
    manifest = os.path.join(backend_dir, "package.json")
    try:
        with open(manifest, "r", encoding="utf-8") as file:
            package = json.loads(file.read())
    except (OSError, ValueError):
        return False
    for section_name in ("dependencies", "devDependencies", "optionalDependencies"):
        section = package.get(section_name) or {}
        if "playwright" in section or "@playwright/test" in section:
            return True
    return False


def _playwright_cli_ready(backend_dir: str) -> bool:
    candidates = (
        os.path.join(backend_dir, "node_modules", ".bin", "playwright"),
        os.path.join(backend_dir, "node_modules", ".bin", "playwright.cmd"),
        os.path.join(backend_dir, "node_modules", "playwright", "cli.js"),
    )
    return any(os.path.isfile(candidate) for candidate in candidates)


def node_modules_ready(target_dir: str) -> bool:
    """Return True only when an install actually produced packages.

    A zero exit code is not proof of success: when ``package.json`` declares no
    dependencies (for example because the template never got copied), npm exits
    0 without installing anything. Treating that as success used to let a
    broken workspace through, after which every build and test step failed and
    the TDD loop burned its whole retry budget on every requirement.
    """
    node_modules = os.path.join(target_dir, "node_modules")
    if not os.path.isdir(node_modules):
        return False
    try:
        return any(not name.startswith(".") for name in os.listdir(node_modules))
    except OSError:
        return False


def _tail(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


class _StageTimer:
    """Per-stage wall-clock timing for one ``run_tests`` execution.

    The online-run analysis had to infer build/DB/server/test costs by diffing
    adjacent debug-log timestamps; recording them inline in the returned body
    (which is both model-facing and persisted under ``.arc/tdd_runs``) makes
    each E2E round-trip's cost breakdown directly measurable.
    """

    def __init__(self) -> None:
        self._stages: dict[str, float] = {}

    async def measure(self, stage: str, awaitable):
        started = time.monotonic()
        try:
            return await awaitable
        finally:
            self._stages[stage] = self._stages.get(stage, 0.0) + (time.monotonic() - started)

    def render(self) -> str:
        if not self._stages:
            return ""
        parts = [f"{stage}={elapsed:.1f}s" for stage, elapsed in self._stages.items()]
        return "\n\n=== Stage Timing ===\n" + " | ".join(parts) + "\n"


def _resolve_executable(program: str) -> str:
    """Resolve ``program`` to a real file ``create_subprocess_exec`` can spawn.

    ``CreateProcess`` (unlike a shell) does not apply ``PATHEXT`` resolution,
    so the bare name ``npm`` cannot be spawned on Windows where the real file
    is ``npm.cmd``; without this the install crashes the whole IMPLEMENT task
    with ``FileNotFoundError: [WinError 2]`` (observed on the 2026-09-20
    test1 run). Resolution stays on PATH exactly like a shell would. An
    unresolvable name is passed through unchanged so the OS error names the
    program.
    """
    resolved = shutil.which(program)
    return resolved or program


async def _run_npm_command(
    command: str | list[str],
    target_dir: str,
    timeout: float = NPM_INSTALL_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    # Callers must treat an OSError from this helper (unspawnable program,
    # missing platform tool) as an ordinary failed command: catch it and
    # surface an exit-code-1 style result rather than letting it escape into
    # the agent graph. Every call site guards this way (run_npm_install's
    # attempt loop, install_package's spawn guard, the dom-peer patch below);
    # a new call site must do the same or an environment surprise will crash
    # the running IMPLEMENT task instead of failing one command.
    # A list command bypasses the shell entirely (no quoting/injection
    # surface); a string command keeps the historical shell behavior for
    # the flag-carrying install lines built from module constants.
    if isinstance(command, list):
        process = await asyncio.create_subprocess_exec(
            _resolve_executable(command[0]),
            *command[1:],
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await finalize_subprocess(process, force_kill=True)
        return 124, "", f"command timed out after {timeout:.0f}s"
    return (
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def run_npm_install(
    target_dir: str,
    log_cb: Callable[..., Awaitable[None] | None],
) -> bool:
    """Install dependencies in ``target_dir`` and verify the outcome.

    Tries a plain ``npm install`` first, then falls back to
    ``--legacy-peer-deps``. The fallback is required on npm 10.x, where the
    optional-peer resolution for vitest makes arborist crash with
    ``Cannot read properties of null (reading 'edgesOut')``.

    Returns True only when npm succeeded *and* ``node_modules`` is non-empty.
    """
    failures: list[str] = []
    attempts = (
        ("npm install", NPM_PRIMARY_ATTEMPT_TIMEOUT_SECONDS),
        (f"npm install {LEGACY_PEER_DEPS_FLAG}", NPM_INSTALL_TIMEOUT_SECONDS),
    )
    for command, timeout in attempts:
        try:
            returncode, stdout, stderr = await _run_npm_command(command, target_dir, timeout)
        except Exception as exc:
            failures.append(f"{command} raised {type(exc).__name__}: {exc}")
            continue

        if returncode == 0 and node_modules_ready(target_dir):
            await _emit_log(log_cb, "System", f"NPM install success in {target_dir}")
            return True

        if returncode == 0:
            failures.append(
                f"{command} exited 0 but node_modules is empty "
                "(nothing was installed; check the template and package.json)"
            )
        else:
            failures.append(f"{command} exited {returncode}: {_tail(stderr) or _tail(stdout)}")

    await _emit_log(
        log_cb,
        "System",
        f"NPM install failed in {target_dir}: " + " | ".join(failures),
        "error",
        None,
    )
    return False


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


def _normalize_backend_test_path(file_path: str) -> str:
    normalized = (file_path or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    if os.path.isabs(file_path):
        return normalized

    normalized = normalized.lstrip("./")
    if normalized.startswith("backend/"):
        normalized = normalized[len("backend/"):]
    return normalized


def _is_valid_web_e2e_test_path(file_path: str) -> bool:
    return is_scoped_test_path(
        file_path,
        prefixes=("backend/test-e2e/",),
        suffixes=(".js", ".jsx", ".ts", ".tsx"),
    )


def _is_valid_web_vitest_test_path(file_path: str) -> bool:
    return is_scoped_test_path(
        file_path,
        prefixes=("frontend/tests/", "backend/tests/"),
        suffixes=(
            ".test.js", ".test.jsx", ".test.ts", ".test.tsx",
            ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
        ),
    )


def _validate_web_test_path(test_type: str, file_path: str) -> str | None:
    normalized_type = (test_type or "").strip().lower()
    if normalized_type in {"unit", "integration"} and _is_valid_web_vitest_test_path(file_path):
        return normalize_safe_relative_path(file_path)
    if normalized_type == "e2e" and _is_valid_web_e2e_test_path(file_path):
        return normalize_safe_relative_path(file_path)
    return None


def _resolve_web_test_target(file_path: str, workspace_path: str) -> tuple[str, str]:
    normalized = (file_path or "").strip().replace("\\", "/")
    backend_path = os.path.join(workspace_path, "backend")
    frontend_path = os.path.join(workspace_path, "frontend")

    if not normalized:
        return backend_path, ""
    if os.path.isabs(file_path):
        return backend_path, normalized

    normalized = normalized.lstrip("./")
    if normalized.startswith("backend/"):
        return backend_path, normalized[len("backend/"):]
    if normalized.startswith("frontend/"):
        return frontend_path, normalized[len("frontend/"):]
    return backend_path, normalized


def _build_web_test_execution(
    test_type: str,
    file_path: str,
    workspace_path: str,
    web_port: int | None = None,
) -> dict[str, str]:
    """Build the execution plan for one Vitest test file.

    ``e2e`` is not a valid single-file type here: every E2E run goes through
    the grouped attempt pipeline (see ``run_test_group``), so ``run_test_file``
    routes e2e requests there before reaching this builder.
    """
    normalized_type = (test_type or "").strip().lower()
    safe_file_path = _validate_web_test_path(normalized_type, file_path)
    if safe_file_path is None:
        raise ValueError(f"Invalid web test path for type {test_type!r}: {file_path!r}")
    working_directory, resolved_file_path = _resolve_web_test_target(safe_file_path, workspace_path)
    resolved_port = int(web_port) if web_port is not None else get_web_port()
    base_url = get_web_base_url(resolved_port)

    if normalized_type in {"unit", "integration"}:
        runner = "Vitest"
        command = f"npx vitest run {resolved_file_path}" if resolved_file_path else "npx vitest run"
    else:
        raise ValueError("Unknown test type. Must be 'unit' or 'integration' (e2e runs grouped).")

    return {
        "runner": runner,
        "command": command,
        "working_directory": working_directory,
        "requested_test_file": file_path or "",
        "resolved_test_file": resolved_file_path,
        "web_port": str(resolved_port),
        "base_url": base_url,
    }


def _build_web_group_execution(
    test_type: str,
    file_paths: list[str],
    workspace_path: str,
    web_port: int | None = None,
) -> dict[str, str]:
    normalized_type = (test_type or "").strip().lower()
    requested_files = [str(path or "").strip() for path in file_paths if str(path or "").strip()]
    resolved_port = int(web_port) if web_port is not None else get_web_port()

    if normalized_type in {"unit", "integration"}:
        backend_targets: list[str] = []
        frontend_targets: list[str] = []
        for file_path in requested_files:
            safe_file_path = _validate_web_test_path(normalized_type, file_path)
            if safe_file_path is None:
                raise ValueError(f"Invalid web test path for type {test_type!r}: {file_path!r}")
            working_directory, resolved_file_path = _resolve_web_test_target(safe_file_path, workspace_path)
            normalized_resolved = resolved_file_path.replace("\\", "/")
            if working_directory == os.path.join(workspace_path, "frontend"):
                frontend_targets.append(normalized_resolved)
            else:
                backend_targets.append(normalized_resolved)

        return {
            "runner": "Vitest",
            "test_type": test_type,
            "requested_test_files": requested_files,
            "backend_working_directory": os.path.join(workspace_path, "backend"),
            "frontend_working_directory": os.path.join(workspace_path, "frontend"),
            "backend_targets": backend_targets,
            "frontend_targets": frontend_targets,
            "backend_requested_files": [
                file_path for file_path in requested_files if _resolve_web_test_target(file_path, workspace_path)[0] == os.path.join(workspace_path, "backend")
            ],
            "frontend_requested_files": [
                file_path for file_path in requested_files if _resolve_web_test_target(file_path, workspace_path)[0] == os.path.join(workspace_path, "frontend")
            ],
            "web_port": str(resolved_port),
            "base_url": get_web_base_url(resolved_port),
        }

    if normalized_type == "e2e":
        safe_paths = []
        for file_path in requested_files:
            safe_file_path = _validate_web_test_path(normalized_type, file_path)
            if safe_file_path is None:
                raise ValueError(f"Invalid web test path for type {test_type!r}: {file_path!r}")
            safe_paths.append(safe_file_path)
        resolved_targets = [_normalize_backend_test_path(file_path) for file_path in safe_paths]
        return {
            "runner": "Playwright",
            "test_type": test_type,
            "requested_test_files": requested_files,
            "working_directory": os.path.join(workspace_path, "backend"),
            "resolved_targets": resolved_targets,
            "requested_resolved_pairs": [
                {"requested_file": file_path, "resolved_target": _normalize_backend_test_path(file_path)}
                for file_path in safe_paths
            ],
            "web_port": str(resolved_port),
            "base_url": get_web_base_url(resolved_port),
        }

    raise ValueError("Unknown test type. Must be 'unit', 'integration', or 'e2e'.")


def _prepend_test_execution_header(execution: dict[str, str], test_result: str) -> str:
    header = "\n".join(
        [
            f"Runner: {execution['runner']}",
            f"Command: {execution['command']}",
            f"Working Directory: {execution['working_directory']}",
            f"Requested Test File: {execution['requested_test_file']}",
            f"Resolved Test File: {execution['resolved_test_file']}",
            f"Web Port: {execution['web_port']}",
            f"Base URL: {execution['base_url']}",
        ]
    )
    return f"{header}\n{test_result}"


def _prepend_group_execution_header(execution: dict[str, str], test_result: str) -> str:
    lines = [
        f"Runner: {execution['runner']}",
        f"Batch Test Type: {execution['test_type']}",
        f"Web Port: {execution['web_port']}",
        f"Base URL: {execution['base_url']}",
        "Requested Test Files:",
    ]
    lines.extend(f"- {file_path}" for file_path in execution.get("requested_test_files", []))

    if execution["runner"] == "Vitest":
        lines.append(f"Backend Working Directory: {execution['backend_working_directory']}")
        lines.append(f"Frontend Working Directory: {execution['frontend_working_directory']}")
        if execution.get("backend_targets"):
            lines.append("Backend Targets:")
            lines.extend(f"- {file_path}" for file_path in execution["backend_targets"])
        if execution.get("frontend_targets"):
            lines.append("Frontend Targets:")
            lines.extend(f"- {file_path}" for file_path in execution["frontend_targets"])
    else:
        lines.append(f"Working Directory: {execution['working_directory']}")
        lines.append("Resolved Targets:")
        lines.extend(f"- {file_path}" for file_path in execution.get("resolved_targets", []))

    return f"{chr(10).join(lines)}\n\n{test_result}"


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


async def _terminate_process(process: asyncio.subprocess.Process | None, *, port: int | None = None) -> str:
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


def _build_e2e_runtime_env(workspace_path: str, targets: list[str], web_port: int | None = None) -> dict[str, str]:
    normalized_targets = [target.replace("\\", "/").strip() for target in targets if target and str(target).strip()]
    suite_label = _slugify_identifier("-".join(normalized_targets) or "playwright-e2e")
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


FRONTEND_BUILD_FINGERPRINT_FILENAME = ".arc-build-fingerprint.json"

# Directories that never contribute to `npm run build` output. `dist` is
# excluded so the recorded fingerprint does not hash itself.
_FRONTEND_FINGERPRINT_SKIPPED_DIRS = frozenset(
    {"node_modules", "dist", "dist-ssr", "coverage", ".git", ".vite"}
)


def _frontend_source_fingerprint(frontend_path: str) -> str | None:
    """Content hash of the frontend sources that feed ``npm run build``.

    Returns ``None`` when the frontend directory is missing, which makes the
    caller fall back to always building.

    Directory symlinks are followed so a linked ``frontend/src`` directory
    contributes to the fingerprint (otherwise edits behind the link could leave
    E2E tests on a stale ``dist``). Cycles are broken by tracking the real path
    of every visited directory.
    """

    root = Path(frontend_path)
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for key, value in sorted(build_web_runtime_env().items()):
        digest.update(key.encode("utf-8"))
        digest.update(b"=")
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")
    visited_real_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real_dir = os.path.realpath(dirpath)
        if real_dir in visited_real_dirs:
            dirnames[:] = []
            continue
        visited_real_dirs.add(real_dir)
        dirnames[:] = sorted(name for name in dirnames if name not in _FRONTEND_FINGERPRINT_SKIPPED_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


def _frontend_build_fingerprint_path(frontend_path: str) -> str:
    # Stored inside `dist` so it shares the build artefact's lifetime: deleting
    # the output also discards the fingerprint and forces a rebuild.
    return os.path.join(frontend_path, "dist", FRONTEND_BUILD_FINGERPRINT_FILENAME)


def _frontend_dist_fingerprint(frontend_path: str) -> str | None:
    """Return a content hash for the built frontend output.

    The source fingerprint alone cannot detect a build that was interrupted
    after it started rewriting ``dist``.  Hashing the output lets cache reuse
    fail closed when a previous build left a partial or externally modified
    artifact behind.
    """

    dist_root = Path(frontend_path) / "dist"
    dist_index_path = dist_root / "index.html"
    if not dist_index_path.is_file():
        return None

    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(dist_root):
        dirnames.sort()
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.name == FRONTEND_BUILD_FINGERPRINT_FILENAME:
                continue
            try:
                content = path.read_bytes()
            except OSError:
                return None
            digest.update(path.relative_to(dist_root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
    return digest.hexdigest()


def _read_recorded_frontend_build(frontend_path: str) -> tuple[str, str] | None:
    try:
        with open(_frontend_build_fingerprint_path(frontend_path), "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source_fingerprint = str(payload.get("fingerprint", "") or "").strip()
    dist_fingerprint = str(payload.get("dist_fingerprint", "") or "").strip()
    if not source_fingerprint or not dist_fingerprint:
        return None
    return source_fingerprint, dist_fingerprint


def _read_recorded_frontend_fingerprint(frontend_path: str) -> str | None:
    recorded_build = _read_recorded_frontend_build(frontend_path)
    return recorded_build[0] if recorded_build is not None else None


def _clear_recorded_frontend_fingerprint(frontend_path: str) -> None:
    with suppress(OSError):
        os.remove(_frontend_build_fingerprint_path(frontend_path))


def _record_frontend_fingerprint(frontend_path: str, fingerprint: str, dist_fingerprint: str) -> None:
    try:
        with open(_frontend_build_fingerprint_path(frontend_path), "w", encoding="utf-8") as file:
            json.dump({"fingerprint": fingerprint, "dist_fingerprint": dist_fingerprint}, file)
            file.write("\n")
    except OSError:
        # Best effort: losing the fingerprint only costs one extra build.
        return


# Digest-phrased verdict for a build that failed before the E2E run could
# start. The failure bodies embed the same sentence (with the trailing
# period) as their headline; the note form is what the failure digest echoes.
_FRONTEND_BUILD_FAILED_NOTE = "frontend build failed before E2E startup"


@dataclass
class _FrontendBuildOutcome:
    """Structured frontend-build verdict plus its rendered section text.

    ``note`` is the build verdict in the failure-digest phrasing; the handler
    knows it at build time, so nothing downstream re-parses the rendered
    output to recover it.
    """

    ok: bool
    note: str
    output: str
    exit_code: int = 1


async def _build_frontend_dist(workspace_path: str, *, force_rebuild: bool = False) -> _FrontendBuildOutcome:
    frontend_path = os.path.join(workspace_path, "frontend")
    dist_index_path = Path(frontend_path) / "dist" / "index.html"
    fingerprint = _frontend_source_fingerprint(frontend_path)
    dist_fingerprint = _frontend_dist_fingerprint(frontend_path)
    recorded_build = _read_recorded_frontend_build(frontend_path)

    # Every E2E attempt rebuilt the frontend from scratch (~tens of seconds),
    # even when the previous attempt already produced a valid `dist` for the
    # same sources. Reuse it only when both sides of the cache are unchanged.
    # `force_rebuild` bypasses the cache for the SPA-static-host recovery: the
    # backend failed to stat an artifact the cache still vouches for, so the
    # record is exactly what must not be trusted this once.
    if (
        not force_rebuild
        and fingerprint is not None
        and dist_fingerprint is not None
        and recorded_build == (fingerprint, dist_fingerprint)
    ):
        return _FrontendBuildOutcome(
            ok=True,
            note=f"reused existing frontend/dist (fingerprint {fingerprint[:12]})",
            output=(
                "Reused the existing `frontend/dist` because the frontend sources are unchanged "
                f"since the last successful build (fingerprint {fingerprint[:12]}).\n"
            ),
            exit_code=0,
        )

    # A failed/interrupted build may leave a partial output tree behind. The
    # old record must not make that tree eligible for reuse on the next run.
    _clear_recorded_frontend_fingerprint(frontend_path)
    build_result = await _execute_web_test_command(
        "npm run build",
        cwd=frontend_path,
        timeout=120.0,
    )
    build_exit_code = build_result.exit_code if build_result.exit_code is not None else 1
    build_ok = build_result.exit_code == 0 and dist_index_path.is_file()
    build_output = build_result.text
    if build_ok:
        rebuilt_dist_fingerprint = _frontend_dist_fingerprint(frontend_path)
        if fingerprint is not None and rebuilt_dist_fingerprint is not None:
            _record_frontend_fingerprint(frontend_path, fingerprint, rebuilt_dist_fingerprint)
        build_note = "rebuilt frontend/dist from current sources"
        if fingerprint is not None:
            # The reuse path states its verdict in prose; the rebuild path used
            # to leave only raw npm output, so nothing in the run result named
            # what was actually served. Emit the same kind of deterministic
            # verdict (with the source fingerprint) the failure digest echoes.
            build_note += f" (fingerprint {fingerprint[:12]})"
            build_output += (
                "\nBuilt `frontend/dist` from the current sources "
                f"(fingerprint {fingerprint[:12]}).\n"
            )
        return _FrontendBuildOutcome(ok=True, note=build_note, output=build_output, exit_code=build_exit_code)

    if dist_index_path.exists():
        return _FrontendBuildOutcome(
            ok=False, note=_FRONTEND_BUILD_FAILED_NOTE, output=build_output, exit_code=build_exit_code
        )

    return _FrontendBuildOutcome(
        ok=False,
        note=_FRONTEND_BUILD_FAILED_NOTE,
        output=build_output
        + "\nFrontend build did not produce `frontend/dist/index.html`, so backend hosting cannot start.\n",
        exit_code=1,
    )


# The verdict line's authoritative state is checked on disk at render time, not
# captured at build time: the 2026-09-20 arc-output1 run died precisely because
# the builder said "Built/Reused" while the backend's request-time stat failed.
# Re-checking at result-assembly time reports what the backend can serve NOW,
# and the digest-side regex parses this exact line shape.
_SERVED_VERDICT_PREFIX = "Served index.html: "
# Marker carried by a backend-runtime cleanup that itself failed (the teardown
# exception note). A run whose cleanup failed is a failure for the agent even
# when the tests passed, so the exit verdict flips when this marker is present.
_CLEANUP_FAILURE_MARKER = "Backend runtime cleanup failed:"
# Truncation shared by the verdict line and its digest-phrased twin
# (``_frontend_serving_note``): the two renderings cross-reference the same
# fingerprint, so they must never drift.
SERVED_VERDICT_FINGERPRINT_CHARS = 12


def _frontend_serving_verdict(workspace_path: str) -> str:
    """Deterministic one-line statement of the SPA shell the backend serves.

    ``dist/`` is read-denied for agents (generated output), so the run result
    is the only channel that can align the agent's view of the static host
    with the backend's. The line names the absolute path the template's SPA
    fallback resolves, whether ``index.html`` is on disk right now, and the
    dist content fingerprint the build cache would reuse.
    """

    dist_root = Path(workspace_path) / "frontend" / "dist"
    dist_index_path = dist_root / "index.html"
    fingerprint = _frontend_dist_fingerprint(os.path.join(workspace_path, "frontend"))
    if not dist_index_path.is_file():
        state = "absent"
        detail = ""
    else:
        state = "present"
        # Mirror the fingerprint truncation the build verdicts already use so
        # the two lines cross-reference without a full hash. The digest-side
        # regex accepts any hex length and echoes it, so this truncation is a
        # display choice, not a parse contract.
        detail = (
            f", fingerprint {(fingerprint or '')[:SERVED_VERDICT_FINGERPRINT_CHARS] or 'unavailable'}"
        )
    return f"{_SERVED_VERDICT_PREFIX}{dist_index_path} ({state}{detail})"


def _frontend_serving_note(workspace_path: str) -> str:
    """Digest-phrased twin of :func:`_frontend_serving_verdict`.

    States the same on-disk fact in the phrasing the failure digest echoes,
    computed from the same facts at assembly time — the historical regex
    round-trip through the rendered line (and its re-extraction guards) is
    gone because nothing re-parses the transcription anymore.
    """

    dist_index_path = Path(workspace_path) / "frontend" / "dist" / "index.html"
    if not dist_index_path.is_file():
        return f"frontend/dist/index.html absent at result time (checked {dist_index_path})"
    fingerprint = _frontend_dist_fingerprint(os.path.join(workspace_path, "frontend"))
    detail = (fingerprint or "")[:SERVED_VERDICT_FINGERPRINT_CHARS] or "unavailable"
    return (
        f"frontend/dist/index.html present at result time (fingerprint {detail}) "
        f"at {dist_index_path}"
    )


# Failure signature of a dead SPA static host (the 2026-09-20 arc-output1 run):
# the backend's SPA fallback called sendFile, `send` re-stats the file per
# request, and the stat failed although the build cache vouched for the
# artifact — `NotFoundError: Not Found` raised from send's internals with a
# `sendfile` frame from Express on the stack. The generated fallback handler's
# own function/file names drift between agent edits, so the anchors are the
# stable library frames plus the sendFile call. Every anchor is line-anchored
# and consecutive anchors may be separated by at most two NON-EMPTY lines of
# the same stack block (a blank line separates Playwright error blocks, so the
# pattern cannot splice frames from two different stacks in one output).
_SPA_STATIC_HOST_FAILURE = re.compile(
    r"NotFoundError:\s*Not Found[^\r\n]*\r?\n"
    r"(?:[^\r\n]+[^\r\n]*\r?\n){0,2}?"
    r"[^\r\n]*at\s+(?:createHttpError|SendStream\.pipe)\b[^\r\n]*\r?\n"
    r"(?:[^\r\n]+[^\r\n]*\r?\n){0,2}?"
    r"[^\r\n]*at\s+sendfile\b[^\r\n]*\r?\n"
    r"(?:[^\r\n]+[^\r\n]*\r?\n){0,2}?"
    r"[^\r\n]*at\s+\S*sendFile\b[^\r\n]*"
)


def _is_spa_static_host_failure(output: str) -> bool:
    """True when a failed E2E output carries the dead-static-host signature."""

    return bool(_SPA_STATIC_HOST_FAILURE.search(output or ""))


@dataclass
class _E2EAttemptFacts:
    """Facts one E2E attempt has established when a failure ends it.

    Fields fill in as the attempt progresses (build, then runtime env, then
    database, then backend), so the single failure renderer
    :func:`_render_e2e_failure_body` can assemble the body of any premature
    exit — build failure, database-prepare failure, backend-startup failure —
    from whatever the attempt had gathered by then, instead of each exit
    point hand-writing its own body from a slightly different subset.
    """

    #: Output of the frontend build stage (always known: it runs first).
    build_output: str
    #: Accumulated teardown evidence: what this attempt inherited from the
    #: attempt before it (recovery path) plus any stale-session cleanup that
    #: happened during this attempt.
    cleanup_note: str = ""
    #: Runtime env of this attempt (known once the build succeeded).
    runtime_env: dict[str, str] = field(default_factory=dict)


def _render_e2e_failure_body(
    facts: _E2EAttemptFacts,
    *,
    headline: str | None,
    served_verdict: str,
    stage_timer: _StageTimer,
    database_prepare_output: str = "",
    backend_start_command: str = "",
    backend_startup_detail: str = "",
) -> str:
    """Assemble the failure body of an E2E attempt that ended early.

    ``headline`` leads the body for build/database failures; the
    backend-startup failure passes ``None`` because its own sections (the
    backend runtime command and its startup detail) are the failure evidence.
    Sections appear only when the attempt got far enough to know them.

    The layout reproduces the three hand-written bodies this renderer
    replaced, byte for byte. Their shared shape: ``Exit Code: 1``, the
    headline, build + serving verdict, then whichever facts exist; the timing
    block closes the assembled part; the previous-cleanup note is appended
    last. Two layout quirks of the old bodies are kept on purpose so the
    model-facing text does not shift: the backend-startup failure orders
    Database Prepare *before* the runtime env and ends its STDERR section
    with a trailing newline (the build/db failures use the runtime-env-first
    order and no trailing newline), and each failure's final append produced
    its own blank-line count before the cleanup note.
    """

    backend_startup_failed = bool(backend_startup_detail or backend_start_command)
    sections: list[str] = []
    if headline:
        sections.append(headline)
    sections.append(f"=== Frontend Build ===\n{facts.build_output}")
    sections.append(served_verdict)
    if facts.runtime_env:
        sections.append(
            "=== E2E Runtime Env ===\n"
            f"DB Path: {facts.runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}"
        )
    if database_prepare_output:
        if backend_startup_failed:
            # The old backend-startup body ordered this section ahead of the
            # runtime env; keep the exact ordering. The runtime env always
            # exists here: a backend-startup failure implies the build
            # succeeded, and the env is built right after it.
            prepare_index = next(
                i for i, section in enumerate(sections) if section.startswith("=== E2E Runtime Env ===")
            )
            sections.insert(prepare_index, f"=== Database Prepare ===\n{database_prepare_output}")
        else:
            sections.append(f"=== Database Prepare ===\n{database_prepare_output}")
    if backend_startup_failed:
        stderr_section = (
            f"=== Backend Runtime Command ===\n{backend_start_command or 'Unavailable'}\n\n"
            f"STDERR:\n{backend_startup_detail or 'No startup detail recorded.'}\n"
        )
        sections.append(stderr_section)

    body = "Exit Code: 1\n\n" + "\n\n".join(sections) + stage_timer.render()
    if facts.cleanup_note:
        if not facts.runtime_env:
            # Build failure: the note joined with a blank line after the
            # timing block.
            body += f"\n\n=== Previous Backend Runtime Cleanup ===\n{facts.cleanup_note}"
        else:
            # The timing block ends with one newline; the old appends added
            # one more for the db failure ("\n\n") and relied on the STDERR
            # section's trailing newline for the backend failure ("\n").
            separator = "\n" if backend_startup_failed else "\n\n"
            body += f"{separator}=== Previous Backend Runtime Cleanup ===\n{facts.cleanup_note}"
    return body


async def _prepare_e2e_database(workspace_path: str, runtime_env: dict[str, str]) -> tuple[bool, int | None, str]:
    backend_path = os.path.join(workspace_path, "backend")
    prepare_result = await _execute_web_test_command(
        "npm run db:prepare:e2e",
        cwd=backend_path,
        timeout=60.0,
        extra_env=runtime_env,
    )
    return prepare_result.exit_code == 0, prepare_result.exit_code, prepare_result.text


async def _seed_e2e_database(workspace_path: str, runtime_env: dict[str, str]) -> tuple[bool, str]:
    backend_path = os.path.join(workspace_path, "backend")
    seed_result = await _execute_web_test_command(
        "npm run db:seed",
        cwd=backend_path,
        timeout=60.0,
        extra_env=runtime_env,
    )
    return seed_result.exit_code == 0, seed_result.text


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
    # _terminate_process call on the same object still reads it, and dropping
    # it mid-flight would orphan still-pending drains back to weak references.
    process._arc_output_tails = (stdout_tail, stderr_tail, drains)  # type: ignore[attr-defined]
    return stdout_tail, stderr_tail


async def _format_backend_output(
    stdout_tail: _ProcessOutputTail,
    stderr_tail: _ProcessOutputTail,
) -> str:
    """Render the retained console output of a backend, newest bytes first.

    Callers invoke this after ``_terminate_process`` has already awaited the
    drain tasks (process death closed the pipes, every buffered byte is in
    the tails), so no flush wait is needed here.
    """

    sections: list[str] = []
    stdout_text = _tail(stdout_tail.text(), _BACKEND_STARTUP_OUTPUT_LIMIT)
    stderr_text = _tail(stderr_tail.text(), _BACKEND_STARTUP_OUTPUT_LIMIT)
    if stdout_text:
        sections.append(f"STDOUT:\n{stdout_text}")
    if stderr_text:
        sections.append(f"STDERR:\n{stderr_text}")
    return "\n".join(sections)


async def _start_backend_runtime(
    workspace_path: str,
    runtime_env: dict[str, str],
    web_port: int | None = None,
) -> tuple[asyncio.subprocess.Process | None, str, str, str]:
    """Start the backend runtime and wait until it serves HTTP.

    The returned ``Process`` carries the anchored pipe drains
    (``_arc_output_tails``); the caller owns that object for the runtime's
    whole lifetime and must clean it up through ``_terminate_process`` -
    the single teardown path that releases the port and awaits the drains.
    Every current call site (probe_backend_health,
    run_test_group's session) funnels there; a new call site bypassing it
    would leave the drains pending on a dead process.
    """

    backend_path = os.path.join(workspace_path, "backend")
    resolved_port = int(web_port) if web_port is not None else get_web_port()
    start_command = _resolve_backend_start_command(backend_path)
    if not start_command:
        return None, "", (
            "Backend package.json must define `start` so the backend can host the built frontend on the single web port."
        ), ""

    try:
        startup_cleanup_note = await _ensure_port_released(
            resolved_port,
            context="Pre-start port cleanup",
            timeout=1.0,
        )
    except RuntimeError as exc:
        return None, start_command, str(exc), ""

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
        return None, start_command, f"Failed to start backend runtime with `{start_command}`: {str(exc)}", ""

    # Consume the pipes from the first moment: a chatty server would otherwise
    # block on a full OS pipe buffer during the startup wait itself. The tails
    # also retain the newest output for the failure body below.
    stdout_tail, stderr_tail = _start_output_tails(backend_process)

    server_ready = await _wait_for_http_server("127.0.0.1", resolved_port, timeout=20.0)
    if not server_ready:
        cleanup_note = ""
        try:
            cleanup_note = await _terminate_process(backend_process, port=resolved_port)
        except Exception as cleanup_exc:
            cleanup_note = f"Backend runtime cleanup after failed startup also failed: {cleanup_exc}"
        captured_output = await _format_backend_output(stdout_tail, stderr_tail)
        return None, start_command, (
            f"Failed to start backend runtime with `{start_command}` on port {resolved_port} "
            "within 20 seconds.\n"
            f"{startup_cleanup_note}\n"
            f"{cleanup_note}\n"
            f"=== Backend Process Output ===\n{captured_output or '(the process produced no output)'}"
        ), ""

    instance_fingerprint = _format_backend_instance_fingerprint(
        launcher_pid=backend_process.pid,
        port=resolved_port,
    )
    return backend_process, start_command, startup_cleanup_note, instance_fingerprint


@dataclass
class _E2EBackendSession:
    """A backend runtime kept alive across E2E attempts within one TDD session."""

    process: asyncio.subprocess.Process
    port: int
    db_path: str
    fingerprint: str
    start_command: str
    startup_detail: str
    instance_fingerprint: str


_BACKEND_FINGERPRINT_SKIPPED_DIRS = frozenset(
    # `test-e2e` specs run in the Playwright process, never inside the express
    # server, so spec-only edits must not force a server restart.
    # `.arc-test-db` holds the per-suite sqlite files, not server code.
    {"node_modules", ".arc-test-db", "dist", "dist-ssr", "coverage", ".git", "test-e2e"}
)


def _backend_source_fingerprint(backend_path: str) -> str | None:
    """Content hash of the backend sources a running E2E server executes.

    Feeds the session-scoped E2E runtime reuse decision: a live server may
    only be reused while the code it loaded is byte-for-byte unchanged.
    Returns ``None`` when the backend directory is missing, which makes the
    caller fall back to a fresh start.

    Deliberately a pure source-tree hash: the other reuse dimensions (web
    port, E2E database path) are session-key comparisons in
    `_try_reuse_e2e_backend_session`, and runtime-env values that vary per
    attempt would spuriously break reuse here.
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


async def probe_backend_health(workspace_path: str, port: int | None = None) -> str | None:
    """Boot the workspace's backend and check its health endpoint.

    Used as the merge gate after an additively resolved worktree merge: the
    mechanical resolution can produce syntactically valid but semantically
    broken registrations (duplicate identifiers, double route mounts), and the
    cheapest full check is "the integrated backend boots and serves
    ``/api/health``".

    Returns ``None`` on success. Workspaces without a backend ``start``
    command have nothing to verify and also return ``None``; a failed
    teardown or any other failure returns a short reason string.
    """

    backend_path = os.path.join(workspace_path, "backend")
    resolved_port = int(port) if port is not None else get_web_port()
    if _resolve_backend_start_command(backend_path) is None:
        return None

    runtime_env = _build_e2e_runtime_env(workspace_path, ["merge-health-probe"], web_port=resolved_port)
    process, _start_command, _cleanup_note, _fingerprint = await _start_backend_runtime(
        workspace_path,
        runtime_env,
        web_port=resolved_port,
    )
    if process is None:
        return "backend runtime failed to start on the merged workspace"

    health_url = f"http://127.0.0.1:{resolved_port}/api/health"
    last_error = "health endpoint did not respond"
    cleanup_error = ""
    try:
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(1.0)

            def _request(url: str = health_url) -> int:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return int(response.status)

            try:
                status = await asyncio.to_thread(_request)
                if status == 200:
                    last_error = None
                    break
                last_error = f"health endpoint returned HTTP {status}"
            except Exception as exc:
                last_error = f"health endpoint unreachable: {type(exc).__name__}: {exc}"
    finally:
        try:
            await _terminate_process(process, port=resolved_port)
        except Exception as exc:
            cleanup_error = f"backend runtime cleanup failed: {type(exc).__name__}: {exc}"

    if last_error is None and cleanup_error:
        # The backend served /api/health, but its teardown failed: the leaked
        # process keeps running inside the merged workspace (holding file locks
        # on Windows), so the gate must not report success.
        return cleanup_error
    if cleanup_error:
        return f"{last_error}; {cleanup_error}"
    return last_error


class WebAppType(AppTypeHandler):
    name = "web"

    # Template files whose whole-file rewrite silently drops runtime wiring:
    # static serving + SPA fallback + health (app.js), the server entry with
    # the PORT contract (index.js), the database lifecycle the test harness
    # and the runtime share (database/*), the root React render that owns
    # BrowserRouter/StrictMode (main.tsx), the route table (App.tsx), and the
    # shared axios client with the relative /api baseURL (api/index.ts).
    # Distinct from ``workspace_glue_anchor_specs``: those drive workspace-map
    # summaries, this list is the stage-discipline write policy.
    template_shared_surfaces: frozenset[str] = frozenset(
        {
            "backend/src/app.js",
            "backend/src/index.js",
            "backend/src/database/index.js",
            "backend/src/database/init_db.js",
            "backend/src/database/db_runtime.js",
            "backend/src/database/seed_db.js",
            "backend/src/database/prepare_e2e.js",
            "backend/src/database/test_harness.js",
            "frontend/src/main.tsx",
            "frontend/src/App.tsx",
            "frontend/src/api/index.ts",
        }
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Session-scoped E2E backend runtime (see `_try_reuse_e2e_backend_session`).
        # Strictly per instance: parallel worktree tasks build one handler per
        # task, and a task must never observe another task's live runtime.
        self._e2e_runtime_session: _E2EBackendSession | None = None
        # One-shot budget for the SPA static-host self-heal (dead `send`
        # NotFoundError in a sendFile frame): a second occurrence of the same
        # signature means the rebuild did not cure it, and the failure must go
        # to the agent instead of looping system-side.
        self._spa_static_host_recovery_used: bool = False

    @classmethod
    def prerequisite_commands(cls) -> list[str]:
        return ["node", "npm"]

    @classmethod
    async def check_runtime_versions(cls, log_cb=None) -> bool:
        """Reject Node runtimes that cannot run the template's test stack.

        The frontend test tree (jsdom 27 -> html-encoding-sniffer 6 ->
        ESM-only @exodus/bytes) needs unflagged ``require(esm)``. On older
        runtimes (observed on Node 22.11) every vitest forks worker crashes at
        startup, which no code edit can fix - without this gate the failure is
        discovered per node and burns the entire TDD budget of every leaf.
        """

        async def log_error(message: str) -> None:
            if log_cb is not None:
                await _emit_log(log_cb, "System", message, "error")

        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                ["node", "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            await log_error(
                f"Node.js version check failed ({exc}); the web template requires "
                "Node >= 22.12 (or >= 20.19 / >= 23.2)."
            )
            return False

        version_text = (completed.stdout or "").strip()
        if _node_supports_require_esm(version_text):
            return True
        await log_error(
            f"Node.js {version_text or '<unknown>'} does not support unflagged require(esm); "
            "the web template requires Node >= 22.12 (or >= 20.19 / >= 23.2). "
            "Upgrade Node.js before compiling."
        )
        return False

    @classmethod
    def runtime_contract_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del android_package
        resolved_port = int(web_port or get_web_port())
        return [
            "For web apps, the hosted runtime is backend-led: enter `frontend` and run `npm run build`, then enter `backend` and run `npm run start` to serve the built frontend dist.",
            f"The backend process is responsible for hosting `frontend/dist` on the single web port `{resolved_port}`; do not assume a separate frontend dev server is part of the runtime.",
            "E2E and runtime verification should target the backend-hosted origin after the frontend build completes.",
            "The backend runs Express 5, where a bare wildcard route string (`app.get('*', ...)`, `app.use('*')`) throws `TypeError: Cannot read properties of undefined (reading 'type')` at route registration and crashes the server at startup. For SPA fallback use the template pattern in `backend/src/app.js` (a regex like `/^(?!\\/api(?:\\/$|\\/)).*/`) or the named wildcard `'/{*splat}'`; never `'*'` or `'/*'`.",
        ]

    @classmethod
    def project_structure_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del android_package
        resolved_port = int(web_port or get_web_port())
        return [
            "- Web structure rules:",
            f"  - Single runtime port: backend serves frontend dist on port {resolved_port}",
            "  - Web runtime sequence: frontend/npm run build -> backend/npm run start",
            "  - Backend runtime root: backend/",
            "  - Frontend source root: frontend/src/",
            "  - Frontend shared test setup: frontend/test/setup.ts",
            "  - Backend source root: backend/src/",
            "  - Shared database scaffold: backend/src/database/",
            "  - Backend Vitest tests: backend/tests/...",
            "  - Frontend Vitest tests: frontend/tests/...",
            "  - Playwright E2E tests: backend/test-e2e/...",
            "  - Database-using tests must allocate an isolated test DB through the scaffold.",
            "  - Prefer entrypoints, route files, and owner files before broader search.",
        ]

    @classmethod
    def test_harness_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return [
            "Test manifest `type` must be one of `Unit`, `Integration`, or `E2E`.",
            "Unit tests: place under `frontend/tests/...` for UI/unit code or `backend/tests/...` for backend/service code.",
            "Integration tests: place under `frontend/tests/...` for frontend integration or `backend/tests/...` for API/service/database integration.",
            "E2E tests: place under `backend/test-e2e/...` and use a JavaScript or TypeScript test filename.",
            "Database-using tests must use the app-type-provided isolated test harness/scaffold.",
        ]

    @classmethod
    def workspace_glue_anchor_specs(cls) -> list[GlueAnchorSpec]:
        return [
            GlueAnchorSpec(
                path="frontend/src/App.tsx",
                label="frontend route registration",
                extractors=("react_routes", "page_imports"),
            ),
            GlueAnchorSpec(
                path="backend/src/app.js",
                label="backend route registration",
                extractors=("express_routes",),
            ),
            GlueAnchorSpec(
                path="backend/src/database/init_db.js",
                label="database schema",
                extractors=("sql_tables",),
            ),
        ]

    @classmethod
    def scaffold_context_files(cls) -> list[str]:
        return [
            # Database scaffold: the prompt tells agents to extend these files
            # instead of one-off helpers, so every DB-related node re-reads
            # them to learn the harness API.
            "backend/src/database/init_db.js",
            "backend/src/database/db_runtime.js",
            "backend/src/database/seed_db.js",
            "backend/src/database/test_harness.js",
            "backend/src/database/prepare_e2e.js",
            "backend/src/database/index.js",
            # Build/test infrastructure that pins the test setup contract.
            "frontend/vite.config.js",
            "frontend/test/setup.ts",
            "backend/vitest.config.js",
            "backend/playwright.config.js",
            # Dependency manifests agents consult to see what is installed.
            "backend/package.json",
            "frontend/package.json",
        ]

    def validate_test_path(self, test_type: str, file_path: str) -> str | None:
        normalized_type = (test_type or "").strip().lower()
        if normalized_type not in {"unit", "integration", "e2e"}:
            return "Web test `type` must be one of `Unit`, `Integration`, or `E2E`."
        if normalized_type in {"unit", "integration"} and not _is_valid_web_vitest_test_path(file_path):
            return (
                "Web Unit and Integration tests must live under `frontend/tests/...` or `backend/tests/...` "
                "and use a Vitest test/spec filename. "
                f"Received: {file_path}"
            )
        if normalized_type == "e2e" and not _is_valid_web_e2e_test_path(file_path):
            return (
                "Web E2E tests must live under `backend/test-e2e/...` and use a JavaScript or TypeScript source filename. "
                f"Received: {file_path}"
            )
        return None

    async def _copy_requirement_assets(self) -> None:
        """Copy requirement-provided image assets into the served frontend.

        Requirements reference images such as ``assets/logo.png`` in their
        descriptions and agents render those paths into components. Without a
        copy step the agent fabricates binary files with ``write_file``
        (observed: 0-byte PNGs on the 12306 benchmark) and every image-bearing
        acceptance check fails. Assets are static inputs, so a plain copy into
        Vite's public directory is enough; subdirectories are mirrored by
        relative path, existing files are never overwritten, and a missing or
        unreadable assets directory is not fatal.
        """

        if not self.requirement_path:
            return
        requirements_dir = Path(self.requirement_path).expanduser().resolve().parent
        assets_dir = requirements_dir / "assets"
        if not assets_dir.is_dir():
            return
        public_assets = Path(self.workspace_path) / "frontend" / "public" / "assets"
        copied = 0
        try:
            public_assets.mkdir(parents=True, exist_ok=True)
            for item in sorted(assets_dir.rglob("*")):
                if not item.is_file():
                    continue
                target = public_assets / item.relative_to(assets_dir)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copy2, item, target)
                copied += 1
        except OSError as exc:
            await self._log(
                "System",
                f"Failed to copy requirement assets from {assets_dir}: {exc}",
                "warning",
                None,
            )
            return
        if copied:
            await self._log(
                "System",
                f"Copied {copied} requirement asset(s) into frontend/public/assets.",
            )

    async def post_template_setup(self) -> bool:
        """Assert the scaffolded runtime files resolve the web port from the environment.

        There is deliberately no placeholder substitution here. Every runtime
        entry point reads the port from an environment variable at process
        start, which keeps a resumed compile on a different ``--port`` working.
        The previous implementation replaced ``__ARC_WEB_PORT__`` in three files,
        but no template file ever contained that token, so it silently did
        nothing - and ``playwright.config.js`` was free to drift to a hardcoded
        port that no E2E run could reach. Verifying the contract turns that
        silent no-op into a gate.
        """

        await self._copy_requirement_assets()
        patches_ok = await self._apply_template_patches()
        if not patches_ok:
            return False
        unconfigured: list[str] = []
        for relative_path, markers in PORT_TEMPLATE_CONTRACT:
            file_path = os.path.join(self.workspace_path, *relative_path.split("/"))
            if not os.path.exists(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as file:
                    content = file.read()
            except OSError as exc:
                await self._log("System", f"Failed to read {relative_path}: {exc}", "error")
                return False
            if not any(marker in content for marker in markers):
                unconfigured.append(relative_path)

        if unconfigured:
            await self._log(
                "System",
                "Web template port configuration is broken in "
                + ", ".join(unconfigured)
                + f": these files must resolve the web port from the environment, otherwise the "
                f"workspace does not honour port {get_web_port()}.",
                "error",
            )
            return False

        await self._log(
            "System",
            f"Configured web template for single-port backend hosting on port {get_web_port()}.",
        )
        return True

    async def _apply_template_patches(self) -> bool:
        """Deliver template fixes the provisioned template may not carry.

        The platform provisions the template (see ``template_patches``), so a
        fix that must reach every generated workspace cannot be a repo template
        edit. It is applied to the copied workspace here, before any node runs.

        Returns False when a patch targets a file this template ships but whose
        shape matches nothing known: the fix is load-bearing for every node's
        TDD loop, and continuing would only move the failure into the compile.
        Overwriting the unrecognized file would trade that visible stop for an
        invisible regression, so the scaffold fails instead. A patch whose
        targets are entirely absent is reported and skipped - that template
        never had the file the fix repairs.
        """

        outcomes = apply_template_patches(
            self.workspace_path,
            TEMPLATE_ID_BY_APP_TYPE.get(self.name, ""),
        )
        if not outcomes:
            return True

        applied = [outcome.patch_name for outcome in outcomes if outcome.status == APPLIED]
        already = [outcome.patch_name for outcome in outcomes if outcome.status == ALREADY_APPLIED]
        skipped = [outcome.patch_name for outcome in outcomes if outcome.status == SKIPPED]
        if applied:
            await self._log(
                "System",
                "Applied template patch(es): " + ", ".join(applied) + ".",
            )
        if already:
            await self._log(
                "System",
                "Template patch(es) already present in the provisioned template: "
                + ", ".join(already)
                + ".",
            )
        if skipped:
            await self._log(
                "System",
                "Template patch(es) skipped - their target files are absent from this "
                "template: " + ", ".join(skipped) + ".",
            )

        unrecognized = [
            outcome
            for outcome in outcomes
            if outcome.status == UNRECOGNIZED
        ]
        for outcome in unrecognized:
            await self._log(
                "System",
                f"Template patch {outcome.patch_name!r} was not applied: {outcome.detail}. "
                "The workspace keeps the template as-is.",
                "warning",
                None,
            )
        if unrecognized:
            await self._log(
                "System",
                "Template patches did not apply cleanly; aborting before the node loop. "
                "The affected fixes will not reach this workspace, and continuing would "
                "surface the failure as per-node test errors instead of a single "
                "actionable startup error.",
                "error",
                None,
            )
            return False
        return True

    async def install_dependencies(self) -> bool:
        targets = (
            ("backend", os.path.join(self.workspace_path, "backend")),
            ("frontend", os.path.join(self.workspace_path, "frontend")),
        )
        installable = [
            (label, target_path)
            for label, target_path in targets
            if os.path.exists(target_path)
        ]
        if not installable:
            return True

        # The two installs write disjoint trees (separate package.json /
        # node_modules), so their npm processes run concurrently; a serial
        # drain pays the slower package resolution twice on cold caches.
        for label, _target_path in installable:
            await self._log(
                "System",
                f"Installing {label} dependencies. This might take a moment...",
            )

        browser_task: asyncio.Task[bool] | None = None
        backend_dir = os.path.join(self.workspace_path, "backend")
        if (
            os.path.isdir(backend_dir)
            and not _browser_install_skipped()
            and _playwright_dependency_declared(backend_dir)
        ):
            await self._log(
                "System",
                "Installing Playwright browsers alongside npm dependencies...",
            )
            browser_task = asyncio.create_task(
                self._verify_e2e_runner(wait_for_cli=True)
            )

        try:
            results = await asyncio.gather(
                *(run_npm_install(target_path, self.log_cb) for _label, target_path in installable)
            )
            if not all(results):
                return False

            for label, target_path in installable:
                if label == "frontend":
                    await self._ensure_testing_library_dom(target_path)

            return browser_task is None or await browser_task
        finally:
            # A failed npm install must not leave a browser download waiting on
            # a CLI that will never appear in the incomplete node_modules tree.
            if browser_task is not None and not browser_task.done():
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)

    async def _ensure_testing_library_dom(self, frontend_dir: str) -> None:
        """Install the missing ``@testing-library/dom`` peer without editing files.

        ``@testing-library/react`` 16 lists ``@testing-library/dom`` as a peer,
        but the officially provisioned template does not declare it. A primary
        ``npm install`` auto-installs peers, while the ``--legacy-peer-deps``
        fallback (needed when arborist crashes on vitest's optional peers) does
        not - and every generated component test then fails to import it with
        no way for the agent to recover. ``--no-save --no-package-lock`` keeps
        the provided template files untouched.

        The patch runs with ``--legacy-peer-deps`` too, and not only for
        symmetry: it is reached precisely when the plain install could not be
        used, so a plain resolution here hits the same arborist crash on npm
        10.x and silently leaves the peer missing. ``@testing-library/dom``
        declares no peers of its own, so skipping peer resolution for it is
        free.
        """
        dom_package = os.path.join(frontend_dir, "node_modules", "@testing-library", "dom")
        if os.path.isdir(dom_package):
            return
        await self._log(
            "System",
            "Installing missing @testing-library/dom peer (required by "
            "@testing-library/react 16, not declared by the provided template)...",
        )
        # Spawn-level failures (unspawnable npm, PATH-less sandbox) degrade to
        # the same warning as a nonzero install: the peer stays missing, which
        # the E2E gates report, instead of an exception escaping into the
        # workspace-verification path that calls this.
        try:
            returncode, _stdout, stderr = await _run_npm_command(
                "npm install --no-save --no-package-lock "
                f'{LEGACY_PEER_DEPS_FLAG} "@testing-library/dom@^10.4.0"',
                frontend_dir,
                NPM_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            returncode, stderr = 1, f"{type(exc).__name__}: {exc}"
        if returncode != 0:
            await self._log(
                "System",
                "Could not install @testing-library/dom; generated component tests "
                "may fail to import it. " + _tail(stderr),
                "warning",
            )

    async def verify_workspace(self) -> bool:
        """Fail fast when the scaffolded workspace cannot build or cannot run E2E.

        Without this gate a broken template or a half-finished install only
        shows up per node, where it burns the entire TDD retry budget of every
        single requirement.
        """
        frontend_dir = os.path.join(self.workspace_path, "frontend")
        if not os.path.isdir(frontend_dir):
            return True

        await self._log("System", "Verifying workspace: building frontend...")
        result = await _execute_web_test_command(
            "npm run build",
            cwd=frontend_dir,
            timeout=180.0,
        )
        if result.exit_code != 0:
            await self._log(
                "System",
                "Workspace verification failed: frontend build did not succeed. "
                "Aborting before the node loop. " + _tail(result.text),
                "error",
                None,
            )
            return False
        await self._log("System", "Workspace verification passed: frontend build succeeded.")

        return await self._verify_e2e_runner()

    async def _wait_for_playwright_cli(self, backend_dir: str) -> None:
        if not _playwright_dependency_declared(backend_dir):
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + PLAYWRIGHT_CLI_WAIT_TIMEOUT_SECONDS
        while not _playwright_cli_ready(backend_dir):
            if loop.time() >= deadline:
                await self._log(
                    "System",
                    "Playwright CLI did not appear during npm install; continuing with "
                    "the configured browser install command.",
                    "warning",
                )
                return
            await asyncio.sleep(PLAYWRIGHT_CLI_POLL_INTERVAL_SECONDS)

    async def _verify_e2e_runner(self, *, wait_for_cli: bool = False) -> bool:
        """Provision the Playwright browsers before the node loop starts.

        The template declares `@playwright/test`, but npm only installs the
        runner - the browser binaries are downloaded separately. Without this
        step the first E2E run fails with "Executable doesn't exist", and the
        agent cannot recover on its own: `execute` is disabled, so it has no way
        to run `playwright install` and ends up patching the generated
        `package.json` to smuggle the install into another npm script.

        Installing here is idempotent and the browser cache is machine-wide, so
        this costs seconds once the browsers exist and is paid once per machine
        rather than once per node.

        Set `ARC_SKIP_BROWSER_INSTALL=1` to bypass the download on machines that
        intentionally run without browser binaries (or without network); E2E
        tests will then fail on a missing browser until they are provided
        another way.
        """

        backend_dir = os.path.join(self.workspace_path, "backend")
        if not os.path.isdir(backend_dir):
            return True
        if getattr(self, "_playwright_browsers_ready", False):
            return True
        if _browser_install_skipped():
            await self._log(
                "System",
                "Skipping Playwright browser install (ARC_SKIP_BROWSER_INSTALL is set). "
                "E2E tests will fail on a missing browser until the binaries are installed.",
            )
            self._playwright_browsers_ready = True
            return True

        if wait_for_cli:
            await self._wait_for_playwright_cli(backend_dir)

        await self._log("System", "Verifying workspace: installing Playwright browsers...")
        result = await _execute_web_test_command(
            _browser_install_command(backend_dir),
            cwd=backend_dir,
            timeout=PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS,
        )
        if result.exit_code == 0:
            await self._log("System", "Workspace verification passed: Playwright browsers ready.")
            self._playwright_browsers_ready = True
            return True

        await self._log(
            "System",
            "Workspace verification failed: Playwright browsers could not be installed, so every "
            "E2E test would fail on a missing browser. Aborting before the node loop. " + _tail(result.text),
            "error",
            None,
        )
        return False

    async def install_package(self, package: str, target: str = "") -> str:
        """Install one named npm package into ``backend`` or ``frontend``.

        Used by the TDD-stage ``install_dependencies`` tool when ``run_tests``
        reports a missing package (``Cannot find module 'x'``). The package is
        installed with ``--no-save --no-package-lock`` so the provided
        template's ``package.json`` and lockfile stay untouched — the install
        only fixes the runtime ``node_modules`` tree of this workspace. The
        agent is still free to declare the dependency in ``package.json`` by
        editing it (the file is writable), but nothing forces that edit.

        ``--legacy-peer-deps`` matches the fallback posture of the primary
        install: on npm 10.x a plain resolution can crash arborist on vitest's
        optional peers, and reaching this method means the plain tree already
        exists — the incremental add must not regress it.
        """
        name = (package or "").strip().strip("'\"")
        if not re.match(r"^@?[A-Za-z0-9][A-Za-z0-9._/@-]*$", name):
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"Invalid package name: {name!r}. Pass a single npm package name, e.g. 'cookie-parser'.\n"
            )
        label = (target or "backend").strip().lower()
        if label not in ("backend", "frontend"):
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"Unknown install target: {target!r}. Use 'backend' or 'frontend'.\n"
            )
        target_dir = os.path.join(self.workspace_path, label)
        if not os.path.isdir(target_dir):
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"Install target directory does not exist: {label}/\n"
            )
        await self._log(
            "System",
            f"Installing npm package '{name}' into {label}/ (no-save)...",
        )
        # LEGACY_PEER_DEPS_FLAG is a single-token npm flag; split() keeps the
        # argv form honest if it ever grows, and a multi-token value would be
        # a breaking change to audit at its definition, not at each use site.
        # The subprocess itself is guarded: an unspawnable npm (or any other
        # platform-level surprise) must surface as an ordinary failed install
        # the agent can recover from, never as an exception that crashes the
        # whole IMPLEMENT task (observed: WinError 2 killed REQ-1 and blocked
        # REQ-2/ROOT on the 2026-09-20 test1 run).
        try:
            returncode, _stdout, stderr = await _run_npm_command(
                [
                    "npm",
                    "install",
                    "--no-save",
                    "--no-package-lock",
                    *LEGACY_PEER_DEPS_FLAG.split(),
                    name,
                ],
                target_dir,
                NPM_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            await self._log(
                "System",
                f"npm install of '{name}' into {label}/ could not run: {type(exc).__name__}: {exc}",
                "warning",
            )
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"npm install of '{name}' into {label}/ could not run: "
                f"{type(exc).__name__}: {exc}\n"
                "Fall back to a standard-library or local implementation.\n"
            )
        if returncode != 0:
            await self._log(
                "System",
                f"npm install of '{name}' into {label}/ failed: {_tail(stderr)}",
                "warning",
            )
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"npm install of '{name}' into {label}/ failed:\n{_tail(stderr)}\n"
                "If the package name is wrong or the registry is unreachable, fall back to "
                "a standard-library or local implementation.\n"
            )
        await self._log("System", f"npm install of '{name}' into {label}/ succeeded.")
        return (
            f"Exit Code: 0\n"
            f"Installed '{name}' into {label}/node_modules (no-save; package.json and "
            "lockfile untouched). Re-run run_tests to validate the repair.\n"
        )

    async def run_build(self) -> str:
        frontend_result = await _execute_web_test_command(
            "npm run build",
            cwd=os.path.join(self.workspace_path, "frontend"),
            timeout=120.0,
        )
        backend_result = await _execute_web_test_command(
            "npm run build --if-present",
            cwd=os.path.join(self.workspace_path, "backend"),
            timeout=120.0,
        )
        return (
            f"=== Frontend Build Result ===\n{frontend_result.text}\n\n"
            f"=== Backend Build Result ===\n{backend_result.text}"
        )

    async def run_test_file(self, test_type: str, file_path: str, web_port: int | None = None) -> TestRunResult:
        resolved_port = int(web_port) if web_port is not None else get_web_port()
        # E2E has exactly one executor (the grouped attempt pipeline); a
        # single-file request is that pipeline with one target, not a second,
        # divergent command path with its own timeout budget. Redirect before
        # the execution log so the file is logged once, by the group runner.
        if (test_type or "").strip().lower() == "e2e":
            return await self.run_test_group("e2e", [file_path], web_port=resolved_port)
        await self._log("System", f"System test execution ({test_type}): {file_path}")
        validation_error = self.validate_test_path(test_type, file_path)
        if validation_error:
            return TestRunResult(exit_code=1, output=f"Exit Code: 1\nSTDERR:\n{validation_error}\n")
        try:
            execution = _build_web_test_execution(test_type, file_path, self.workspace_path, web_port=resolved_port)
        except ValueError as exc:
            return TestRunResult(exit_code=1, output=str(exc))

        command_result = await _execute_web_test_command(
            execution["command"],
            cwd=execution["working_directory"],
            web_port=resolved_port,
        )
        return parse_test_run(
            _prepend_test_execution_header(execution, command_result.text),
            exit_code=command_result.exit_code if command_result.exit_code is not None else -1,
        )

    async def _try_reuse_e2e_backend_session(
        self,
        e2e_runtime_env: dict[str, str],
        resolved_port: int,
        backend_fingerprint: str | None,
    ) -> _E2EBackendSession | None:
        """Return the live session runtime when this batch can run on it.

        Reuse requires the previous server process to still be alive and
        serving, and the backend sources, web port and E2E database path to be
        identical to the ones it was started with. Any mismatch returns
        ``None`` and the caller takes the fresh-start path.
        """

        session = self._e2e_runtime_session
        if session is None:
            return None
        if session.process.returncode is not None:
            return None
        if session.port != resolved_port:
            return None
        if session.db_path != e2e_runtime_env.get("ARC_E2E_DB_PATH", ""):
            return None
        if backend_fingerprint is None or session.fingerprint != backend_fingerprint:
            return None
        if not await _wait_for_http_server("127.0.0.1", resolved_port, timeout=5.0):
            return None
        return session

    async def _reset_live_e2e_database(self, e2e_runtime_env: dict[str, str]) -> tuple[bool, str]:
        """Reset the E2E database rows while the backend runtime stays alive.

        `db:prepare:e2e` recreates the database file, which cannot run against
        a server that holds the file open. The row-level wipe plus a
        `db:seed` re-run reproduces the prepared-and-seeded state on the same
        file; a failure here makes the caller rebuild everything from scratch.
        """

        reset_ok, reset_output = await asyncio.to_thread(
            _reset_sqlite_database_rows,
            e2e_runtime_env.get("ARC_E2E_DB_PATH", ""),
        )
        if not reset_ok:
            return False, f"Row-level reset of the live E2E database was not possible: {reset_output}"
        seed_ok, seed_output = await _seed_e2e_database(self.workspace_path, e2e_runtime_env)
        if not seed_ok:
            return False, (
                "Row-level reset of the live E2E database succeeded, but re-seeding "
                f"via `npm run db:seed` did not.\n{seed_output}"
            )
        return True, (
            "Reset the live E2E database at row level and re-seeded it; the backend runtime was kept alive.\n"
            f"{reset_output}\n{seed_output}"
        )

    async def _terminate_e2e_session(self, context: str) -> str:
        """Tear down the session-scoped E2E runtime, if one is alive."""

        session = self._e2e_runtime_session
        if session is None:
            return ""
        self._e2e_runtime_session = None
        try:
            note = await _terminate_process(session.process, port=session.port)
        except Exception as exc:
            return f"Backend runtime cleanup failed: {exc}"
        # A server that crashed mid-session (the chatty-output scenario the
        # drains defend against) leaves its dying words only in the tail
        # buffers; surface them here so the teardown note carries the crash
        # evidence, mirroring the startup-failure body.
        anchor = getattr(session.process, "_arc_output_tails", None)
        if anchor is not None:
            captured = await _format_backend_output(anchor[0], anchor[1])
            if captured:
                note = (
                    f"{note}\n=== Backend Process Output (session teardown) ===\n{captured}"
                )
        return note

    async def shutdown_e2e_runtime(self) -> None:
        note = await self._terminate_e2e_session("E2E runtime session shutdown")
        if note:
            await self._log("System", f"Session-scoped E2E backend runtime shut down. {note}")

    async def run_test_group(self, test_type: str, file_paths: list[str], web_port: int | None = None) -> TestRunResult:
        resolved_port = int(web_port) if web_port is not None else get_web_port()
        normalized_type = (test_type or "").strip().lower()
        if not file_paths:
            return TestRunResult(
                exit_code=1,
                output=(
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"No test files were configured for the current {test_type} batch.\n"
                ),
            )

        for file_path in file_paths:
            await self._log("System", f"System test execution ({test_type}): {file_path}")

        validation_errors = [self.validate_test_path(test_type, file_path) for file_path in file_paths]
        invalid_errors = [error for error in validation_errors if error]
        if invalid_errors:
            error_lines = ["Exit Code: 1", "STDERR:"]
            error_lines.extend(invalid_errors)
            return TestRunResult(exit_code=1, output="\n".join(error_lines) + "\n")

        try:
            execution = _build_web_group_execution(test_type, file_paths, self.workspace_path, web_port=resolved_port)
        except ValueError as exc:
            return TestRunResult(exit_code=1, output=str(exc))

        if normalized_type in {"unit", "integration"}:
            sections: list[str] = []
            exit_codes: list[int] = []

            if execution.get("backend_targets"):
                backend_command = "npx vitest run " + " ".join(execution["backend_targets"])
                backend_result = await _execute_web_test_command(
                    backend_command,
                    cwd=execution["backend_working_directory"],
                    web_port=resolved_port,
                )
                sections.append(f"=== Backend Vitest Batch ===\n{backend_result.text}")
                exit_codes.append(backend_result.exit_code if backend_result.exit_code is not None else 1)

            if execution.get("frontend_targets"):
                frontend_command = "npx vitest run " + " ".join(execution["frontend_targets"])
                frontend_result = await _execute_web_test_command(
                    frontend_command,
                    cwd=execution["frontend_working_directory"],
                    web_port=resolved_port,
                )
                sections.append(f"=== Frontend Vitest Batch ===\n{frontend_result.text}")
                exit_codes.append(frontend_result.exit_code if frontend_result.exit_code is not None else 1)

            if not sections:
                return parse_test_run(
                    _prepend_group_execution_header(
                        execution,
                        "Exit Code: 1\nSTDERR:\nNo resolvable Vitest targets were found for this batch.\n",
                    ),
                    exit_code=1,
                )

            batch_exit_code = 0 if exit_codes and all(code == 0 for code in exit_codes) else 1
            body = f"Exit Code: {batch_exit_code}\n\n" + "\n\n".join(sections)
            return parse_test_run(
                _prepend_group_execution_header(execution, body),
                exit_code=batch_exit_code,
            )

        stage_timer = _StageTimer()

        try:
            result, backend_cleanup_note = await self._run_e2e_group_attempt(
                execution,
                stage_timer,
                resolved_port,
                force_rebuild=False,
                prior_cleanup_note="",
            )
        except Exception as exc:
            return TestRunResult(
                exit_code=1,
                output=(
                    f"Failed to start grouped E2E execution: {str(exc)}"
                    + stage_timer.render()
                ),
            )

        # Self-heal the dead static host once (the 2026-09-20 arc-output1 run
        # burned 22 agent minutes on this failure): the backend could not stat
        # an artifact the build cache vouched for, so distrust the cache and
        # the live runtime this once — force a rebuild, restart the backend,
        # rerun the batch. The retried body leads; the failed attempt survives
        # as a superseded appendix for the failure evidence.
        retried_cleanup_note = backend_cleanup_note
        if (
            result.exit_code != 0
            and not self._spa_static_host_recovery_used
            and _is_spa_static_host_failure(result.output)
        ):
            self._spa_static_host_recovery_used = True
            await self._log(
                "System",
                "E2E failure matches the SPA static-host signature (send NotFoundError in sendFile); "
                "forcing one frontend rebuild + backend restart retry.",
            )
            recovery_note = await self._terminate_e2e_session("SPA static-host recovery cleanup")
            try:
                retried_result, retried_cleanup_note = await self._run_e2e_group_attempt(
                    execution,
                    stage_timer,
                    resolved_port,
                    force_rebuild=True,
                    prior_cleanup_note=recovery_note or backend_cleanup_note,
                )
            except Exception as exc:
                retried_result = TestRunResult(
                    exit_code=1,
                    output=(
                        f"Failed to retry grouped E2E execution after SPA static-host recovery: {str(exc)}"
                        + stage_timer.render()
                    ),
                )
            # Same self-check the non-recovery path applies below, on the
            # retried attempt alone: a retried pass whose cleanup failed is
            # still a failure for the agent.
            if _CLEANUP_FAILURE_MARKER in retried_cleanup_note and retried_result.exit_code == 0:
                retried_result.exit_code = 1
                retried_result.output = retried_result.output.replace("Exit Code: 0", "Exit Code: 1", 1)
            # The retried attempt leads so exit-code parsing and the agent both
            # read the retried verdict first; the failed attempt survives as an
            # appendix for the failure evidence (the NotFoundError stack).
            first_attempt_exit = result.exit_code
            appendix = result.output
            appendix = appendix.replace(
                f"Exit Code: {first_attempt_exit}",
                f"Exit Code (superseded by the recovery retry): {first_attempt_exit}",
                1,
            )
            retried_result.output = (
                f"{retried_result.output.rstrip()}\n\n"
                f"=== SPA Static-Host Recovery Retry ===\n"
                "The first attempt of this batch failed with the dead static-host signature "
                "(the backend could not stat `frontend/dist/index.html` at request time "
                "even though the build cache vouched for it). The system forced one "
                "frontend rebuild and backend restart, then re-ran the batch; the result "
                "above is the retried attempt.\n\n"
                "First attempt (superseded, kept for the failure evidence):\n\n"
                f"{appendix}"
            )
            result = retried_result

        if (
            _CLEANUP_FAILURE_MARKER in (backend_cleanup_note + "\n" + retried_cleanup_note)
            and result.exit_code == 0
        ):
            result.exit_code = 1
            result.output = result.output.replace("Exit Code: 0", "Exit Code: 1", 1)
        result.output = _prepend_group_execution_header(execution, result.output)
        return result

    async def _run_e2e_group_attempt(
        self,
        execution: dict[str, str],
        stage_timer: _StageTimer,
        resolved_port: int,
        *,
        force_rebuild: bool,
        prior_cleanup_note: str,
    ) -> tuple[TestRunResult, str]:
        """Run one grouped E2E attempt end to end and return its result.

        ``run_test_group`` calls this once, and a second time with
        ``force_rebuild=True`` when the first attempt died on the SPA
        static-host signature (see the recovery block there). The runtime env
        is rebuilt per attempt so each carries its own database label.

        Returns ``(result, backend_cleanup_note)``; the note lets the recovery
        re-run carry the teardown evidence of the attempt before it.
        """

        build = await stage_timer.measure(
            "frontend_build",
            _build_frontend_dist(self.workspace_path, force_rebuild=force_rebuild),
        )
        facts = _E2EAttemptFacts(
            build_output=build.output,
            cleanup_note=prior_cleanup_note,
        )
        # Failure bodies check the verdict here (post-build, pre-Playwright);
        # the success body re-checks after Playwright — see the comment there.
        served_verdict = _frontend_serving_verdict(self.workspace_path)
        if not build.ok:
            return (
                parse_test_run(
                    _render_e2e_failure_body(
                        facts,
                        headline="Frontend build failed before E2E startup.",
                        served_verdict=served_verdict,
                        stage_timer=stage_timer,
                    ),
                    exit_code=1,
                    build_note=build.note,
                    served_verdict=_frontend_serving_note(self.workspace_path),
                ),
                prior_cleanup_note,
            )

        e2e_runtime_env = _build_e2e_runtime_env(
            self.workspace_path,
            execution.get("resolved_targets", []),
            web_port=resolved_port,
        )
        facts.runtime_env = e2e_runtime_env

        backend_start_command = ""
        backend_startup_detail = ""
        backend_instance_fingerprint = ""
        backend_cleanup_note = prior_cleanup_note
        database_prepare_output = ""
        reused_runtime = False
        # Off the event loop: hashing a large backend tree is pure blocking I/O
        # and must not freeze concurrent runner work on the same loop.
        backend_fingerprint = await asyncio.to_thread(
            _backend_source_fingerprint,
            os.path.join(self.workspace_path, "backend"),
        )
        reused_session = await stage_timer.measure(
            "backend_runtime",
            self._try_reuse_e2e_backend_session(
                e2e_runtime_env,
                resolved_port,
                backend_fingerprint,
            ),
        )
        if reused_session is not None:
            reset_ok, reset_output = await stage_timer.measure(
                "database_prepare", self._reset_live_e2e_database(e2e_runtime_env)
            )
            database_prepare_output = reset_output
            if reset_ok:
                reused_runtime = True
                backend_start_command = reused_session.start_command
                backend_startup_detail = reused_session.startup_detail
                backend_instance_fingerprint = reused_session.instance_fingerprint
            else:
                # The fresh start below overwrites database_prepare_output,
                # so carry the reset failure reason in the cleanup note:
                # both failure bodies and the deferred-cleanup section
                # surface it there.
                backend_cleanup_note = (
                    f"{backend_cleanup_note}\n" if backend_cleanup_note else ""
                ) + (
                    "Live E2E runtime reset was not possible; fell back to a fresh start: "
                    f"{reset_output}"
                )

        if not reused_runtime:
            stale_note = await self._terminate_e2e_session("Stale E2E runtime cleanup")
            if stale_note:
                backend_cleanup_note = (
                    f"{backend_cleanup_note}\n{stale_note}" if backend_cleanup_note else stale_note
                )
            database_ready, _prepare_exit, database_prepare_output = await stage_timer.measure(
                "database_prepare",
                _prepare_e2e_database(self.workspace_path, e2e_runtime_env),
            )
            if not database_ready:
                facts.cleanup_note = backend_cleanup_note
                return (
                    parse_test_run(
                        _render_e2e_failure_body(
                            facts,
                            headline="E2E database preparation failed before backend startup.",
                            served_verdict=served_verdict,
                            database_prepare_output=database_prepare_output,
                            stage_timer=stage_timer,
                        ),
                        exit_code=1,
                        build_note=build.note,
                        served_verdict=_frontend_serving_note(self.workspace_path),
                    ),
                    backend_cleanup_note,
                )

            (
                backend_process,
                backend_start_command,
                backend_startup_detail,
                backend_instance_fingerprint,
            ) = await stage_timer.measure(
                "backend_runtime",
                _start_backend_runtime(
                    self.workspace_path, e2e_runtime_env, web_port=resolved_port
                ),
            )
            if backend_process is None:
                facts.cleanup_note = backend_cleanup_note
                return (
                    parse_test_run(
                        _render_e2e_failure_body(
                            facts,
                            headline=None,  # the backend sections carry the failure
                            served_verdict=served_verdict,
                            database_prepare_output=database_prepare_output,
                            backend_start_command=backend_start_command,
                            backend_startup_detail=backend_startup_detail,
                            stage_timer=stage_timer,
                        ),
                        exit_code=1,
                        build_note=build.note,
                        served_verdict=_frontend_serving_note(self.workspace_path),
                    ),
                    backend_cleanup_note,
                )
            self._e2e_runtime_session = _E2EBackendSession(
                process=backend_process,
                port=resolved_port,
                db_path=e2e_runtime_env.get("ARC_E2E_DB_PATH", ""),
                fingerprint=backend_fingerprint or "",
                start_command=backend_start_command,
                startup_detail=backend_startup_detail,
                instance_fingerprint=backend_instance_fingerprint,
            )

        playwright_command = "npx playwright test"
        if execution.get("resolved_targets"):
            playwright_command += " " + " ".join(execution["resolved_targets"])
        playwright_result = await stage_timer.measure(
            "playwright",
            _execute_web_test_command(
                playwright_command,
                cwd=execution["working_directory"],
                timeout=E2E_RUNNER_TIMEOUT_SECONDS,
                extra_env=e2e_runtime_env,
                web_port=resolved_port,
            ),
        )
        playwright_exit_code = playwright_result.exit_code if playwright_result.exit_code is not None else 1
        if reused_runtime:
            backend_runtime_section = (
                "Reused the live backend runtime from an earlier E2E attempt in this TDD session "
                "(backend sources, port and E2E database unchanged).\n"
                f"Command: {backend_start_command}\n"
                f"Port: {resolved_port}\n\n"
                f"Startup Cleanup: {backend_startup_detail or 'No startup cleanup note recorded.'}"
            )
        else:
            backend_runtime_section = (
                f"Command: {backend_start_command}\n"
                f"Port: {resolved_port}\n\n"
                f"Startup Cleanup: {backend_startup_detail or 'No startup cleanup note recorded.'}"
            )
        deferred_cleanup = (
            "Deferred: the backend runtime stays alive for subsequent E2E attempts of this "
            "session and is shut down when the node's IMPLEMENT phase finishes."
        )
        cleanup_section = deferred_cleanup
        if backend_cleanup_note:
            cleanup_section = f"Previous runtime cleanup: {backend_cleanup_note}\n{deferred_cleanup}"
        body = (
            f"Exit Code: {playwright_exit_code}\n\n"
            # Checked here — after Playwright ran — not right after the build:
            # the verdict's job is to expose the artifact-vanished-after-build
            # race, which a pre-Playwright snapshot cannot see.
            f"=== Frontend Build ===\n{build.output}\n\n"
            f"{_frontend_serving_verdict(self.workspace_path)}\n\n"
            f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n"
            f"DB Label: {e2e_runtime_env.get('ARC_E2E_DB_LABEL', 'unknown')}\n\n"
            f"=== Database Prepare ===\n{database_prepare_output}\n\n"
            f"=== Backend Runtime ===\n{backend_runtime_section}\n\n"
            f"=== Backend Instance Fingerprint ===\n{backend_instance_fingerprint or 'No backend instance fingerprint recorded.'}\n\n"
            f"{playwright_result.text}\n\n"
            f"=== Backend Runtime Cleanup ===\n{cleanup_section}"
            + stage_timer.render()
        )
        return (
            parse_test_run(
                body,
                exit_code=playwright_exit_code,
                build_note=build.note,
                served_verdict=_frontend_serving_note(self.workspace_path),
            ),
            backend_cleanup_note,
        )

    @classmethod
    def build_stack_block(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> str:
        del android_package
        resolved_port = int(web_port or get_web_port())
        base_url = f"http://localhost:{resolved_port}"
        return (
            "### Main Stack\n"
            "- backend: nodejs\n"
            "- frontend: react\n"
            "- database: sqlite\n"
            f"- web_port: {resolved_port}\n"
            "\n"
            "### Runtime And Hosting\n"
            f"* **Single Web Port**: {resolved_port}\n"
            f"* **Base URL Under Test**: {base_url}\n"
            "* **Hosting Model**: Enter `frontend` and run `npm run build`, then enter `backend` and run `npm run start` so the Express backend serves `frontend/dist` on the same origin.\n"
            "* **Deployment Rule**: Do not rely on a separate frontend dev server for deployment or E2E.\n"
            "\n"
            "### Frontend\n"
            "* **Framework**: React 18+ (Vite)\n"
            "* **Language**: TypeScript + TSX (preferred default for frontend source files)\n"
            "* **Styling**: Tailwind CSS v4 via utility classes in component markup, not only bare CSS imports\n"
            "* **HTTP**: Axios (Must use Interceptors for global error handling)\n"
            "* **Testing**: Vitest for frontend unit/integration tests in `frontend/tests/...`.\n"
            "* **Frontend Test Infrastructure**: `vitest` + `jsdom` + `@testing-library/react` + `@testing-library/jest-dom` + `@testing-library/user-event` are preinstalled and configured through `frontend/vite.config.js` and `frontend/test/setup.ts`.\n"
            "\n"
            "### Backend\n"
            "* **Runtime**: Node.js (LTS)\n"
            "* **Framework**: Express.js\n"
            "* **Database**: SQLite3 (`sqlite3` driver, file-based)\n"
            "* **Database Scaffold**:\n"
            "  * Runtime bootstrap and schema lifecycle: `backend/src/database/init_db.js`\n"
            "  * Shared query helpers: `backend/src/database/db_runtime.js`\n"
            "  * Shared seed entrypoint: `backend/src/database/seed_db.js`\n"
            "  * Shared test DB harness: `backend/src/database/test_harness.js`\n"
            "  * Barrel export for reuse: `backend/src/database/index.js`\n"
            "  * Extend these scaffold files instead of creating one-off DB connection/reset helpers in feature folders.\n"
            "* **Testing**:\n"
            "  * Vitest: Used for backend Unit and Integration testing.\n"
            "  * Supertest: Used with Vitest for API route testing.\n"
            "  * Playwright: Used for End-to-End (E2E) testing, located in `backend/test-e2e`, configured by `backend/playwright.config.js`, and expected to use `process.env.PLAYWRIGHT_BASE_URL`.\n"
            "  * If a test uses the database, it must create an isolated test DB via the scaffold, prepare test data through the scaffold, and clean the test DB up after the suite finishes.\n"
        )

    @classmethod
    def default_stack_summary(cls) -> str:
        return f"backend=nodejs, frontend=react, database=sqlite, web_port={get_web_port()}"

    @classmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        backend = re.search(r"-\s*backend:\s*(.+)", metadata_content, re.IGNORECASE)
        frontend = re.search(r"-\s*frontend:\s*(.+)", metadata_content, re.IGNORECASE)
        database = re.search(r"-\s*database:\s*(.+)", metadata_content, re.IGNORECASE)
        web_port = re.search(r"-\s*web_port:\s*(.+)", metadata_content, re.IGNORECASE)
        return (
            f"backend={backend.group(1).strip() if backend else 'N/A'}, "
            f"frontend={frontend.group(1).strip() if frontend else 'N/A'}, "
            f"database={database.group(1).strip() if database else 'N/A'}, "
            f"web_port={web_port.group(1).strip() if web_port else get_web_port()}"
        )

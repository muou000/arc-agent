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

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .base import AppTypeHandler, GlueAnchorSpec, TEMPLATE_ID_BY_APP_TYPE
from .path_validation import is_scoped_test_path, normalize_safe_relative_path
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


async def _run_npm_command(
    command: str,
    target_dir: str,
    timeout: float = NPM_INSTALL_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
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


async def _execute_web_test_command(
    command: str,
    cwd: str,
    timeout: float = 60.0,
    extra_env: dict[str, str] | None = None,
    web_port: int | None = None,
) -> str:
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
        return result
    except asyncio.TimeoutError:
        if process:
            await finalize_subprocess(process, force_kill=True)
        return f"Command timed out after {timeout} seconds."
    except Exception as exc:
        return f"Execution failed: {str(exc)}"

def _extract_exit_code(command_output: str) -> int | None:
    for line in (command_output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Exit Code:"):
            try:
                return int(stripped.split("Exit Code:", 1)[1].strip())
            except ValueError:
                return None
    return None


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
    elif normalized_type == "e2e":
        runner = "Playwright"
        working_directory = os.path.join(workspace_path, "backend")
        resolved_file_path = _normalize_backend_test_path(safe_file_path)
        command = f"npx playwright test {resolved_file_path}" if resolved_file_path else "npx playwright test"
    else:
        raise ValueError("Unknown test type. Must be 'unit', 'integration', or 'e2e'.")

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


async def _build_frontend_dist(workspace_path: str) -> tuple[bool, str]:
    frontend_path = os.path.join(workspace_path, "frontend")
    dist_index_path = Path(frontend_path) / "dist" / "index.html"
    fingerprint = _frontend_source_fingerprint(frontend_path)
    dist_fingerprint = _frontend_dist_fingerprint(frontend_path)
    recorded_build = _read_recorded_frontend_build(frontend_path)

    # Every E2E attempt rebuilt the frontend from scratch (~tens of seconds),
    # even when the previous attempt already produced a valid `dist` for the
    # same sources. Reuse it only when both sides of the cache are unchanged.
    if (
        fingerprint is not None
        and dist_fingerprint is not None
        and recorded_build == (fingerprint, dist_fingerprint)
    ):
        return True, (
            "Reused the existing `frontend/dist` because the frontend sources are unchanged "
            f"since the last successful build (fingerprint {fingerprint[:12]}).\n"
        )

    # A failed/interrupted build may leave a partial output tree behind. The
    # old record must not make that tree eligible for reuse on the next run.
    _clear_recorded_frontend_fingerprint(frontend_path)
    frontend_build_output = await _execute_web_test_command(
        "npm run build",
        cwd=frontend_path,
        timeout=120.0,
    )
    build_ok = _extract_exit_code(frontend_build_output) == 0 and dist_index_path.is_file()
    if build_ok:
        rebuilt_dist_fingerprint = _frontend_dist_fingerprint(frontend_path)
        if fingerprint is not None and rebuilt_dist_fingerprint is not None:
            _record_frontend_fingerprint(frontend_path, fingerprint, rebuilt_dist_fingerprint)
        return True, frontend_build_output

    if dist_index_path.exists():
        return False, frontend_build_output

    return (
        False,
        frontend_build_output
        + "\nFrontend build did not produce `frontend/dist/index.html`, so backend hosting cannot start.\n",
    )


async def _prepare_e2e_database(workspace_path: str, runtime_env: dict[str, str]) -> tuple[bool, str]:
    backend_path = os.path.join(workspace_path, "backend")
    prepare_output = await _execute_web_test_command(
        "npm run db:prepare:e2e",
        cwd=backend_path,
        timeout=60.0,
        extra_env=runtime_env,
    )
    return _extract_exit_code(prepare_output) == 0, prepare_output


async def _seed_e2e_database(workspace_path: str, runtime_env: dict[str, str]) -> tuple[bool, str]:
    backend_path = os.path.join(workspace_path, "backend")
    seed_output = await _execute_web_test_command(
        "npm run db:seed",
        cwd=backend_path,
        timeout=60.0,
        extra_env=runtime_env,
    )
    return _extract_exit_code(seed_output) == 0, seed_output


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
    Every current call site (probe_backend_health, run_test_file,
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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Session-scoped E2E backend runtime (see `_try_reuse_e2e_backend_session`).
        # Strictly per instance: parallel worktree tasks build one handler per
        # task, and a task must never observe another task's live runtime.
        self._e2e_runtime_session: _E2EBackendSession | None = None

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

    @classmethod
    def template_contract_violations(cls, template_dir: str) -> list[str]:
        """Report template files that would fail the post-template port gate.

        Runs at template-selection time (see ``AppTypeHandler._select_template``)
        so a usable but stale external template is swapped for the bundled one
        instead of scaffolding a workspace that initialization then aborts on.
        """

        violations: list[str] = []
        for relative_path, marker in PORT_TEMPLATE_CONTRACT:
            file_path = os.path.join(template_dir, *relative_path.split("/"))
            if not os.path.exists(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as file:
                    content = file.read()
            except OSError:
                violations.append(relative_path)
                continue
            if marker not in content:
                violations.append(relative_path)
        return violations

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
        returncode, _stdout, stderr = await _run_npm_command(
            "npm install --no-save --no-package-lock "
            f'{LEGACY_PEER_DEPS_FLAG} "@testing-library/dom@^10.4.0"',
            frontend_dir,
            NPM_INSTALL_TIMEOUT_SECONDS,
        )
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
        if _extract_exit_code(result) != 0:
            await self._log(
                "System",
                "Workspace verification failed: frontend build did not succeed. "
                "Aborting before the node loop. " + _tail(result),
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
        if _extract_exit_code(result) == 0:
            await self._log("System", "Workspace verification passed: Playwright browsers ready.")
            self._playwright_browsers_ready = True
            return True

        await self._log(
            "System",
            "Workspace verification failed: Playwright browsers could not be installed, so every "
            "E2E test would fail on a missing browser. Aborting before the node loop. " + _tail(result),
            "error",
            None,
        )
        return False

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
        return f"=== Frontend Build Result ===\n{frontend_result}\n\n=== Backend Build Result ===\n{backend_result}"

    async def run_test_file(self, test_type: str, file_path: str, web_port: int | None = None) -> str:
        resolved_port = int(web_port) if web_port is not None else get_web_port()
        await self._log("System", f"System test execution ({test_type}): {file_path}")
        normalized_type = test_type.lower()
        validation_error = self.validate_test_path(test_type, file_path)
        if validation_error:
            return f"Exit Code: 1\nSTDERR:\n{validation_error}\n"
        try:
            execution = _build_web_test_execution(test_type, file_path, self.workspace_path, web_port=resolved_port)
        except ValueError as exc:
            return str(exc)
        backend_process = None
        frontend_build_output = ""
        database_prepare_output = ""
        backend_start_command = ""
        backend_startup_detail = ""
        backend_instance_fingerprint = ""
        backend_cleanup_note = ""
        e2e_runtime_env: dict[str, str] = {}
        result_body = ""

        if normalized_type == "e2e":
            # Single-file runs get their own target set and therefore their own
            # E2E database, so they never ride the session runtime. Clear any
            # live session first so the fresh start below cannot collide with a
            # port the session still holds.
            await self._terminate_e2e_session("Single-file E2E pre-start cleanup")
            e2e_runtime_env = _build_e2e_runtime_env(
                self.workspace_path,
                [execution.get("resolved_test_file", "")],
                web_port=resolved_port,
            )
            build_ok, frontend_build_output = await _build_frontend_dist(self.workspace_path)
            if not build_ok:
                return _prepend_test_execution_header(
                    execution,
                    "Frontend build failed before E2E startup.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}",
                )

            database_ready, database_prepare_output = await _prepare_e2e_database(
                self.workspace_path,
                e2e_runtime_env,
            )
            if not database_ready:
                return _prepend_test_execution_header(
                    execution,
                    "E2E database preparation failed before backend startup.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}",
                )

            (
                backend_process,
                backend_start_command,
                backend_startup_detail,
                backend_instance_fingerprint,
            ) = await _start_backend_runtime(self.workspace_path, e2e_runtime_env, web_port=resolved_port)
            if backend_process is None:
                return _prepend_test_execution_header(
                    execution,
                    "Failed to start backend server for E2E testing.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                    f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n\n"
                    f"=== Backend Runtime Command ===\n{backend_start_command or 'Unavailable'}\n\n"
                    f"=== Backend Runtime Error ===\n{backend_startup_detail or 'No startup detail recorded.'}\n",
                )

        try:
            result_body = await _execute_web_test_command(
                execution["command"],
                cwd=execution["working_directory"],
                extra_env=e2e_runtime_env if normalized_type == "e2e" else None,
                web_port=resolved_port,
            )
            if normalized_type == "e2e":
                result_body = (
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n"
                    f"DB Label: {e2e_runtime_env.get('ARC_E2E_DB_LABEL', 'unknown')}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                    f"=== Backend Runtime ===\nCommand: {backend_start_command}\n"
                    f"Port: {resolved_port}\n"
                    f"Startup Cleanup: {backend_startup_detail or 'No startup cleanup note recorded.'}\n\n"
                    f"=== Backend Instance Fingerprint ===\n{backend_instance_fingerprint or 'No backend instance fingerprint recorded.'}\n\n"
                    f"{result_body}"
                )
        finally:
            if normalized_type == "e2e":
                try:
                    backend_cleanup_note = await _terminate_process(backend_process, port=resolved_port)
                except Exception as cleanup_exc:
                    backend_cleanup_note = f"Backend runtime cleanup failed: {cleanup_exc}"

        if normalized_type == "e2e":
            result_body = (
                f"{result_body}\n\n"
                f"=== Backend Runtime Cleanup ===\n{backend_cleanup_note or 'No cleanup note recorded.'}"
            )
            if "Backend runtime cleanup failed:" in backend_cleanup_note and "Exit Code: 0" in result_body:
                result_body = result_body.replace("Exit Code: 0", "Exit Code: 1", 1)

        return _prepend_test_execution_header(execution, result_body)

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

    async def run_test_group(self, test_type: str, file_paths: list[str], web_port: int | None = None) -> str:
        resolved_port = int(web_port) if web_port is not None else get_web_port()
        normalized_type = (test_type or "").strip().lower()
        if not file_paths:
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch.\n"
            )

        for file_path in file_paths:
            await self._log("System", f"System test execution ({test_type}): {file_path}")

        validation_errors = [self.validate_test_path(test_type, file_path) for file_path in file_paths]
        invalid_errors = [error for error in validation_errors if error]
        if invalid_errors:
            error_lines = ["Exit Code: 1", "STDERR:"]
            error_lines.extend(invalid_errors)
            return "\n".join(error_lines) + "\n"

        try:
            execution = _build_web_group_execution(test_type, file_paths, self.workspace_path, web_port=resolved_port)
        except ValueError as exc:
            return str(exc)

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
                sections.append(f"=== Backend Vitest Batch ===\n{backend_result}")
                backend_exit_code = _extract_exit_code(backend_result)
                if backend_exit_code is None:
                    backend_exit_code = 1
                exit_codes.append(backend_exit_code)

            if execution.get("frontend_targets"):
                frontend_command = "npx vitest run " + " ".join(execution["frontend_targets"])
                frontend_result = await _execute_web_test_command(
                    frontend_command,
                    cwd=execution["frontend_working_directory"],
                    web_port=resolved_port,
                )
                sections.append(f"=== Frontend Vitest Batch ===\n{frontend_result}")
                frontend_exit_code = _extract_exit_code(frontend_result)
                if frontend_exit_code is None:
                    frontend_exit_code = 1
                exit_codes.append(frontend_exit_code)

            if not sections:
                return _prepend_group_execution_header(
                    execution,
                    "Exit Code: 1\nSTDERR:\nNo resolvable Vitest targets were found for this batch.\n",
                )

            batch_exit_code = 0 if exit_codes and all(code == 0 for code in exit_codes) else 1
            body = f"Exit Code: {batch_exit_code}\n\n" + "\n\n".join(sections)
            return _prepend_group_execution_header(execution, body)

        stage_timer = _StageTimer()
        build_ok, frontend_build_output = await stage_timer.measure(
            "frontend_build", _build_frontend_dist(self.workspace_path)
        )
        if not build_ok:
            return _prepend_group_execution_header(
                execution,
                "Frontend build failed before E2E startup.\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}"
                + stage_timer.render(),
            )

        e2e_runtime_env = _build_e2e_runtime_env(
            self.workspace_path,
            execution.get("resolved_targets", []),
            web_port=resolved_port,
        )

        backend_process = None
        backend_start_command = ""
        backend_startup_detail = ""
        backend_instance_fingerprint = ""
        backend_cleanup_note = ""
        database_prepare_output = ""
        reused_runtime = False
        # Off the event loop: hashing a large backend tree is pure blocking I/O
        # and must not freeze concurrent runner work on the same loop.
        backend_fingerprint = await asyncio.to_thread(
            _backend_source_fingerprint,
            os.path.join(self.workspace_path, "backend"),
        )
        try:
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
                    backend_process = reused_session.process
                    backend_start_command = reused_session.start_command
                    backend_startup_detail = reused_session.startup_detail
                    backend_instance_fingerprint = reused_session.instance_fingerprint
                else:
                    # The fresh start below overwrites database_prepare_output,
                    # so carry the reset failure reason in the cleanup note:
                    # both failure bodies and the deferred-cleanup section
                    # surface it there.
                    backend_cleanup_note = (
                        "Live E2E runtime reset was not possible; fell back to a fresh start: "
                        f"{reset_output}"
                    )

            if not reused_runtime:
                stale_note = await self._terminate_e2e_session("Stale E2E runtime cleanup")
                if stale_note:
                    backend_cleanup_note = (
                        f"{backend_cleanup_note}\n{stale_note}" if backend_cleanup_note else stale_note
                    )
                database_ready, database_prepare_output = await stage_timer.measure(
                    "database_prepare",
                    _prepare_e2e_database(self.workspace_path, e2e_runtime_env),
                )
                if not database_ready:
                    failure_body = (
                        "E2E database preparation failed before backend startup.\n\n"
                        f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                        f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n\n"
                        f"=== Database Prepare ===\n{database_prepare_output}"
                        + stage_timer.render()
                    )
                    if backend_cleanup_note:
                        failure_body += f"\n\n=== Previous Backend Runtime Cleanup ===\n{backend_cleanup_note}"
                    return _prepend_group_execution_header(execution, failure_body)

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
                    failure_body = (
                        "Exit Code: 1\n\n"
                        f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                        f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                        f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n\n"
                        f"=== Backend Runtime Command ===\n{backend_start_command or 'Unavailable'}\n\n"
                        f"STDERR:\n{backend_startup_detail or 'No startup detail recorded.'}\n"
                        + stage_timer.render()
                    )
                    if backend_cleanup_note:
                        failure_body += f"\n=== Previous Backend Runtime Cleanup ===\n{backend_cleanup_note}"
                    return _prepend_group_execution_header(execution, failure_body)
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
                    timeout=120.0,
                    extra_env=e2e_runtime_env,
                    web_port=resolved_port,
                ),
            )
            playwright_exit_code = _extract_exit_code(playwright_result)
            if playwright_exit_code is None:
                playwright_exit_code = 1
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
                f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n"
                f"DB Label: {e2e_runtime_env.get('ARC_E2E_DB_LABEL', 'unknown')}\n\n"
                f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                f"=== Backend Runtime ===\n{backend_runtime_section}\n\n"
                f"=== Backend Instance Fingerprint ===\n{backend_instance_fingerprint or 'No backend instance fingerprint recorded.'}\n\n"
                f"{playwright_result}\n\n"
                f"=== Backend Runtime Cleanup ===\n{cleanup_section}"
                + stage_timer.render()
            )
        except Exception as exc:
            return f"Failed to start grouped E2E execution: {str(exc)}"

        if "Backend runtime cleanup failed:" in body and "Exit Code: 0" in body:
            body = body.replace("Exit Code: 0", "Exit Code: 1", 1)
        return _prepend_group_execution_header(execution, body)

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

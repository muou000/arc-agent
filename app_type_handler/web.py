import re
import os
import json
import sys
import asyncio
import subprocess
import signal
import hashlib
import inspect

from pathlib import Path
from typing import Awaitable, Callable

from .base import AppTypeHandler
from .path_validation import is_scoped_test_path, normalize_safe_relative_path
from core.config import build_web_runtime_env, get_web_base_url, get_web_port
from core.processes import finalize_subprocess

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
# Generous because a cold machine downloads ~150 MB of browser binaries. Once
# the machine-wide Playwright cache is warm the command exits in seconds.
PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS = 900.0


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
                **build_web_runtime_env(),
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


def _build_web_test_execution(test_type: str, file_path: str, workspace_path: str) -> dict[str, str]:
    normalized_type = (test_type or "").strip().lower()
    safe_file_path = _validate_web_test_path(normalized_type, file_path)
    if safe_file_path is None:
        raise ValueError(f"Invalid web test path for type {test_type!r}: {file_path!r}")
    working_directory, resolved_file_path = _resolve_web_test_target(safe_file_path, workspace_path)
    web_port = str(get_web_port())
    base_url = get_web_base_url()

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
        "web_port": web_port,
        "base_url": base_url,
    }


def _build_web_group_execution(test_type: str, file_paths: list[str], workspace_path: str) -> dict[str, str]:
    normalized_type = (test_type or "").strip().lower()
    requested_files = [str(path or "").strip() for path in file_paths if str(path or "").strip()]

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
            "web_port": str(get_web_port()),
            "base_url": get_web_base_url(),
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
            "web_port": str(get_web_port()),
            "base_url": get_web_base_url(),
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


async def _wait_for_tcp_server(host: str, port: int, timeout: float = 20.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
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

    if port is None:
        return "No port cleanup required."

    return await _ensure_port_released(
        port,
        context="Backend runtime cleanup",
        allowed_processes=owned_processes,
    )


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


def _build_e2e_runtime_env(workspace_path: str, targets: list[str]) -> dict[str, str]:
    normalized_targets = [target.replace("\\", "/").strip() for target in targets if target and str(target).strip()]
    suite_label = _slugify_identifier("-".join(normalized_targets) or "playwright-e2e")
    suite_hash = hashlib.sha1("\n".join(normalized_targets or ["playwright-e2e"]).encode("utf-8")).hexdigest()[:10]
    backend_path = os.path.join(workspace_path, "backend")
    e2e_db_root = os.path.join(backend_path, ".arc-test-db")
    e2e_db_path = os.path.abspath(os.path.join(e2e_db_root, f"{suite_label}-{suite_hash}.sqlite"))
    return {
        **build_web_runtime_env(),
        # The template's `playwright.config.js` and the agent-facing stack notes
        # both document `PLAYWRIGHT_BASE_URL` as the origin under test. Nothing
        # used to set it, so Playwright fell back to its own default port and
        # every E2E run navigated to a dead origin.
        "PLAYWRIGHT_BASE_URL": f"http://127.0.0.1:{get_web_port()}",
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
        digest.update(b"\\0")
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


def _read_recorded_frontend_fingerprint(frontend_path: str) -> str | None:
    try:
        with open(_frontend_build_fingerprint_path(frontend_path), "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return str(payload.get("fingerprint", "") or "").strip() or None


def _record_frontend_fingerprint(frontend_path: str, fingerprint: str) -> None:
    try:
        with open(_frontend_build_fingerprint_path(frontend_path), "w", encoding="utf-8") as file:
            json.dump({"fingerprint": fingerprint}, file)
            file.write("\n")
    except OSError:
        # Best effort: losing the fingerprint only costs one extra build.
        return


async def _build_frontend_dist(workspace_path: str) -> tuple[bool, str]:
    frontend_path = os.path.join(workspace_path, "frontend")
    dist_index_path = os.path.join(frontend_path, "dist", "index.html")
    fingerprint = _frontend_source_fingerprint(frontend_path)

    # Every E2E attempt rebuilt the frontend from scratch (~tens of seconds),
    # even when the previous attempt already produced a `dist` for the same
    # sources. Reuse it when the tree is byte-for-byte unchanged.
    if fingerprint is not None and os.path.exists(dist_index_path):
        if _read_recorded_frontend_fingerprint(frontend_path) == fingerprint:
            return True, (
                "Reused the existing `frontend/dist` because the frontend sources are unchanged "
                f"since the last successful build (fingerprint {fingerprint[:12]}).\n"
            )

    frontend_build_output = await _execute_web_test_command(
        "npm run build",
        cwd=frontend_path,
        timeout=120.0,
    )
    build_ok = _extract_exit_code(frontend_build_output) == 0 and os.path.exists(dist_index_path)
    if build_ok:
        if fingerprint is not None:
            _record_frontend_fingerprint(frontend_path, fingerprint)
        return True, frontend_build_output

    if os.path.exists(dist_index_path):
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


async def _start_backend_runtime(
    workspace_path: str,
    runtime_env: dict[str, str],
) -> tuple[asyncio.subprocess.Process | None, str, str, str]:
    backend_path = os.path.join(workspace_path, "backend")
    start_command = _resolve_backend_start_command(backend_path)
    if not start_command:
        return None, "", (
            "Backend package.json must define `start` so the backend can host the built frontend on the single web port."
        ), ""

    try:
        startup_cleanup_note = await _ensure_port_released(
            get_web_port(),
            context="Pre-start port cleanup",
            timeout=1.0,
        )
    except RuntimeError as exc:
        return None, start_command, str(exc), ""

    try:
        backend_process = await asyncio.create_subprocess_shell(
            start_command,
            cwd=backend_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                **os.environ,
                **runtime_env,
            },
        )
    except Exception as exc:
        return None, start_command, f"Failed to start backend runtime with `{start_command}`: {str(exc)}", ""

    server_ready = await _wait_for_tcp_server("127.0.0.1", get_web_port(), timeout=20.0)
    if not server_ready:
        cleanup_note = ""
        try:
            cleanup_note = await _terminate_process(backend_process, port=get_web_port())
        except Exception as cleanup_exc:
            cleanup_note = f"Backend runtime cleanup after failed startup also failed: {cleanup_exc}"
        return None, start_command, (
            f"Failed to start backend runtime with `{start_command}` on port {get_web_port()} "
            "within 20 seconds.\n"
            f"{startup_cleanup_note}\n"
            f"{cleanup_note}"
        ), ""

    instance_fingerprint = _format_backend_instance_fingerprint(
        launcher_pid=backend_process.pid,
        port=get_web_port(),
    )
    return backend_process, start_command, startup_cleanup_note, instance_fingerprint


class WebAppType(AppTypeHandler):
    name = "web"

    @classmethod
    def prerequisite_commands(cls) -> list[str]:
        return ["node", "npm"]

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

        port_contract = (
            ("backend/src/index.js", "process.env.PORT"),
            ("frontend/vite.config.js", "process.env.ARC_WEB_PORT"),
            ("backend/playwright.config.js", "process.env.ARC_WEB_PORT"),
        )
        unconfigured: list[str] = []
        for relative_path, marker in port_contract:
            file_path = os.path.join(self.workspace_path, *relative_path.split("/"))
            if not os.path.exists(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as file:
                    content = file.read()
            except OSError as exc:
                await self._log("System", f"Failed to read {relative_path}: {exc}", "error")
                return False
            if marker not in content:
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

    async def install_dependencies(self) -> bool:
        targets = (
            ("backend", os.path.join(self.workspace_path, "backend")),
            ("frontend", os.path.join(self.workspace_path, "frontend")),
        )
        all_ok = True
        for label, target_path in targets:
            if not os.path.exists(target_path):
                continue
            await self._log(
                "System",
                f"Installing {label} dependencies. This might take a moment...",
            )
            if not await run_npm_install(target_path, self.log_cb):
                all_ok = False
        return all_ok

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

    async def _verify_e2e_runner(self) -> bool:
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
        """

        backend_dir = os.path.join(self.workspace_path, "backend")
        if not os.path.isdir(backend_dir):
            return True

        await self._log("System", "Verifying workspace: installing Playwright browsers...")
        result = await _execute_web_test_command(
            "npm run e2e:install-browsers",
            cwd=backend_dir,
            timeout=PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS,
        )
        if _extract_exit_code(result) == 0:
            await self._log("System", "Workspace verification passed: Playwright browsers ready.")
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

    async def run_test_file(self, test_type: str, file_path: str) -> str:
        await self._log("System", f"System test execution ({test_type}): {file_path}")
        normalized_type = test_type.lower()
        validation_error = self.validate_test_path(test_type, file_path)
        if validation_error:
            return f"Exit Code: 1\nSTDERR:\n{validation_error}\n"
        try:
            execution = _build_web_test_execution(test_type, file_path, self.workspace_path)
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
            e2e_runtime_env = _build_e2e_runtime_env(
                self.workspace_path,
                [execution.get("resolved_test_file", "")],
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
            ) = await _start_backend_runtime(self.workspace_path, e2e_runtime_env)
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
            )
            if normalized_type == "e2e":
                result_body = (
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n"
                    f"DB Label: {e2e_runtime_env.get('ARC_E2E_DB_LABEL', 'unknown')}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                    f"=== Backend Runtime ===\nCommand: {backend_start_command}\n"
                    f"Port: {get_web_port()}\n"
                    f"Startup Cleanup: {backend_startup_detail or 'No startup cleanup note recorded.'}\n\n"
                    f"=== Backend Instance Fingerprint ===\n{backend_instance_fingerprint or 'No backend instance fingerprint recorded.'}\n\n"
                    f"{result_body}"
                )
        finally:
            if normalized_type == "e2e":
                try:
                    backend_cleanup_note = await _terminate_process(backend_process, port=get_web_port())
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

    async def run_test_group(self, test_type: str, file_paths: list[str]) -> str:
        normalized_type = (test_type or "").strip().lower()
        if not file_paths:
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch.\n"
            )

        for file_path in file_paths:
            await self._log("System", f"System test execution ({test_type}): {file_path}")

        invalid_paths = [file_path for file_path in file_paths if self.validate_test_path(test_type, file_path)]
        if invalid_paths:
            error_lines = ["Exit Code: 1", "STDERR:"]
            for file_path in invalid_paths:
                error_lines.append(self.validate_test_path(test_type, file_path) or "")
            return "\n".join(error_lines) + "\n"

        try:
            execution = _build_web_group_execution(test_type, file_paths, self.workspace_path)
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

        build_ok, frontend_build_output = await _build_frontend_dist(self.workspace_path)
        if not build_ok:
            return _prepend_group_execution_header(
                execution,
                "Frontend build failed before E2E startup.\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}",
            )

        e2e_runtime_env = _build_e2e_runtime_env(
            self.workspace_path,
            execution.get("resolved_targets", []),
        )
        database_ready, database_prepare_output = await _prepare_e2e_database(
            self.workspace_path,
            e2e_runtime_env,
        )
        if not database_ready:
            return _prepend_group_execution_header(
                execution,
                "E2E database preparation failed before backend startup.\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n\n"
                f"=== Database Prepare ===\n{database_prepare_output}",
            )

        backend_process = None
        backend_start_command = ""
        backend_startup_detail = ""
        backend_instance_fingerprint = ""
        backend_cleanup_note = ""
        body = ""
        try:
            (
                backend_process,
                backend_start_command,
                backend_startup_detail,
                backend_instance_fingerprint,
            ) = await _start_backend_runtime(self.workspace_path, e2e_runtime_env)
            if backend_process is None:
                return _prepend_group_execution_header(
                    execution,
                    "Exit Code: 1\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                    f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n\n"
                    f"=== Backend Runtime Command ===\n{backend_start_command or 'Unavailable'}\n\n"
                    f"STDERR:\n{backend_startup_detail or 'No startup detail recorded.'}\n",
                )

            playwright_command = "npx playwright test"
            if execution.get("resolved_targets"):
                playwright_command += " " + " ".join(execution["resolved_targets"])
            playwright_result = await _execute_web_test_command(
                playwright_command,
                cwd=execution["working_directory"],
                timeout=120.0,
                extra_env=e2e_runtime_env,
            )
            playwright_exit_code = _extract_exit_code(playwright_result)
            if playwright_exit_code is None:
                playwright_exit_code = 1
            body = (
                f"Exit Code: {playwright_exit_code}\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                f"=== E2E Runtime Env ===\nDB Path: {e2e_runtime_env.get('ARC_E2E_DB_PATH', 'unknown')}\n"
                f"DB Label: {e2e_runtime_env.get('ARC_E2E_DB_LABEL', 'unknown')}\n\n"
                f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                f"=== Backend Runtime ===\nCommand: {backend_start_command}\n"
                f"Port: {get_web_port()}\n\n"
                f"Startup Cleanup: {backend_startup_detail or 'No startup cleanup note recorded.'}\n\n"
                f"=== Backend Instance Fingerprint ===\n{backend_instance_fingerprint or 'No backend instance fingerprint recorded.'}\n\n"
                f"{playwright_result}"
            )
        except Exception as exc:
            return f"Failed to start grouped E2E execution: {str(exc)}"
        finally:
            try:
                backend_cleanup_note = await _terminate_process(backend_process, port=get_web_port())
            except Exception as cleanup_exc:
                backend_cleanup_note = f"Backend runtime cleanup failed: {cleanup_exc}"

        body = (
            f"{body}\n\n"
            f"=== Backend Runtime Cleanup ===\n{backend_cleanup_note or 'No cleanup note recorded.'}"
        )
        if "Backend runtime cleanup failed:" in backend_cleanup_note and "Exit Code: 0" in body:
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

import re
import os
import json
import sys
import asyncio
import shutil
import logging
import subprocess
import inspect
import urllib.request

from pathlib import Path
from typing import Awaitable, Callable

from .base import AppTypeHandler, GlueAnchorSpec, TEMPLATE_ID_BY_APP_TYPE
from .backend_runtime import (
    BackendRuntime,
    ProcessBackendRuntime,
    _build_e2e_runtime_env,
    _execute_web_test_command,
    _resolve_backend_start_command,
    _tail,
    spawn_backend_process,
    terminate_backend_process,
)
from .e2e_attempt import (
    E2EAttemptRunner,
    _build_case_grep_pattern,
)
from .path_validation import is_scoped_test_path, normalize_safe_relative_path
from .test_results import TestRunResult, parse_test_run
from .template_patches import (
    ALREADY_APPLIED,
    APPLIED,
    SKIPPED,
    UNRECOGNIZED,
    apply_template_patches,
)
from core.config import get_web_base_url, get_web_port
from core.processes import (
    build_subprocess_env,
    finalize_subprocess,
    start_subprocess_exec,
    start_subprocess_shell,
)

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
        process = await start_subprocess_exec(
            _resolve_executable(command[0]),
            *command[1:],
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_subprocess_env(),
        )
    else:
        process = await start_subprocess_shell(
            command,
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_subprocess_env(),
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
    failed_case_names: list[str] | None = None,
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
            # Empty when no failed-case names were supplied (first round and
            # full revalidation rounds): the runner command then stays unfiltered.
            "failed_case_grep": _build_case_grep_pattern(failed_case_names),
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
        case_grep = execution.get("failed_case_grep", "")
        if case_grep:
            # Runner-side fact for the agent; the agent-facing directive to
            # re-run the full layer lives in core/phases' ARC_RETRY_FILTER_NOTE.
            lines.append(
                "Failed Case Filter: this retry round re-ran only the previously "
                f"failing case(s) (--grep {case_grep}); cases that passed in earlier "
                "rounds were not re-verified here."
            )

    return f"{chr(10).join(lines)}\n\n{test_result}"


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

    A stateless consumer of the backend_runtime module: it spawns and tears
    the probe process down through the module's mechanism functions and keeps
    no session state (nothing to reuse, no database to prepare).
    """

    backend_path = os.path.join(workspace_path, "backend")
    resolved_port = int(port) if port is not None else get_web_port()
    if _resolve_backend_start_command(backend_path) is None:
        return None

    runtime_env = _build_e2e_runtime_env(workspace_path, ["merge-health-probe"], web_port=resolved_port)
    spawn = await spawn_backend_process(
        workspace_path,
        runtime_env,
        web_port=resolved_port,
    )
    if spawn.handle is None:
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
            await terminate_backend_process(spawn.handle, port=resolved_port)
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
    shared_test_resources: frozenset[str] = frozenset(
        {
            "frontend/test/setup.ts",
            "frontend/vite.config.js",
            "backend/vitest.config.js",
            "backend/playwright.config.js",
            "backend/src/database/prepare_e2e.js",
            "backend/src/database/test_harness.js",
        }
    )

    def __init__(self, *args, backend_runtime: BackendRuntime | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Session-scoped E2E backend runtime owner (app_type_handler.backend_runtime).
        # Strictly per instance: parallel worktree tasks build one handler per
        # task, and a task must never observe another task's live runtime.
        # Tests inject InMemoryBackendRuntime; production resolves the process
        # adapter lazily at first use (see `_backend_runtime_or_default`).
        self._backend_runtime: BackendRuntime | None = backend_runtime
        # The E2E attempt runner (app_type_handler.e2e_attempt), resolved at
        # first use like the backend runtime and reused across this handler's
        # run_test_group calls: its one-shot SPA static-host recovery budget
        # spans the handler's lifetime, so attempt-level state lives in the
        # module, not here.
        self._e2e_attempt_runner: E2EAttemptRunner | None = None

    def _backend_runtime_or_default(self) -> BackendRuntime:
        if self._backend_runtime is None:
            # Resolved at first use so the process adapter captures the
            # module-level command runner as it reads right then: tests that
            # monkeypatch the runner before driving a run still reach the
            # runtime's database commands through the patched runner.
            self._backend_runtime = ProcessBackendRuntime(
                self.workspace_path, command_runner=_execute_web_test_command
            )
        return self._backend_runtime

    def _e2e_attempt_runner_or_default(self) -> E2EAttemptRunner:
        if self._e2e_attempt_runner is None:
            self._e2e_attempt_runner = E2EAttemptRunner(
                self.workspace_path,
                backend_runtime=self._backend_runtime_or_default(),
            )
        return self._e2e_attempt_runner

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
            "When the stage pipeline is active, place node-local tests and fixtures under the matching root's `generated/<stable-node-id>/...` namespace; shared runner configuration and fixtures are read-only.",
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
            "lockfile untouched). If the TDD layer is still open, re-run run_tests "
            "to validate the repair. If ARC_TDD_HARD_STOP has closed it, do not retry; "
            "record the installation as unverified in the failure report for a fresh "
            "TDD pass. Do not inspect node_modules or dist to verify the installation.\n"
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

    async def shutdown_e2e_runtime(self) -> None:
        note = await self._backend_runtime_or_default().terminate("E2E runtime session shutdown")
        if note:
            await self._log("System", f"Session-scoped E2E backend runtime shut down. {note}")

    async def run_test_group(
        self,
        test_type: str,
        file_paths: list[str],
        web_port: int | None = None,
        failed_case_names: list[str] | None = None,
    ) -> TestRunResult:
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
            execution = _build_web_group_execution(
                test_type,
                file_paths,
                self.workspace_path,
                web_port=resolved_port,
                failed_case_names=failed_case_names,
            )
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

        # The whole attempt-level policy (first attempt, the one-shot SPA
        # static-host recovery with its superseded appendix, and the
        # cleanup-failure verdict flips) lives in the attempt module; this
        # branch only decides to launch one group call and prepends the
        # group execution header to whatever came back.
        result = await self._e2e_attempt_runner_or_default().run_group(execution, resolved_port)
        result.output = _prepend_group_execution_header(execution, result.output)
        return result

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

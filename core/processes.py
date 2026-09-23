from __future__ import annotations

import asyncio
import inspect
import os
import shutil
from collections.abc import Mapping
from typing import Any, Awaitable, Callable


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# Child-process environment whitelist (issue #180)
# ---------------------------------------------------------------------------
#
# Generated-app code (agent-editable npm scripts, test files) runs inside the
# build/test subprocesses. Handing them ``{**os.environ}`` lets that code read
# the host's model credentials — ``core.config.load_project_env`` copies the
# repository ``.env`` into ``os.environ`` — and exfiltrate them through the
# command output that flows back into the model context. Filesystem deny rules
# cannot stop environment reads, so child processes get a whitelist.
#
# The failure mode of a whitelist miss is a diagnosable build failure (add the
# variable below with a one-line reason); the failure mode of a blacklist miss
# is an undiagnosable credential leak. Keep the list minimal and justified.

# Toolchain plumbing the templates' build/test/install commands demonstrably
# need. Resolution against the host environment is case-insensitive per name,
# so a Windows host spelling ``Path`` still matches.
_SUBPROCESS_ENV_ALLOWLIST: tuple[str, ...] = (
    # Process / executable resolution and the POSIX home directory.
    "PATH",
    "HOME",
    "USERPROFILE",
    # Temp directories (POSIX reads TMPDIR, Windows TEMP/TMP).
    "TEMP",
    "TMP",
    "TMPDIR",
    # Windows process plumbing; Node's crypto breaks without SystemRoot.
    "SystemRoot",
    "SystemDrive",
    "COMSPEC",
    "windir",
    "PATHEXT",
    # Windows per-user dirs: npm prefix/cache and the Playwright browser
    # cache live under them.
    "APPDATA",
    "LOCALAPPDATA",
    # Node runtime mode flags read by package scripts.
    "NODE_ENV",
    # Python child encoding.
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    # Java/Android toolchain: JDK discovery, JVM options, SDK location and
    # the wrapper's distribution cache.
    "JAVA_HOME",
    "JAVA_TOOL_OPTIONS",
    "ANDROID_SDK_ROOT",
    "ANDROID_HOME",
    "GRADLE_USER_HOME",
    # Playwright browser install/runtime location and download mirrors.
    "PLAYWRIGHT_BROWSERS_PATH",
    "PLAYWRIGHT_DOWNLOAD_HOST",
    "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
    "PLAYWRIGHT_SKIP_BROWSER_VALIDATION",
)

# Proxies are emitted under both conventional casings: npm/node/git each read
# a different one, and without them package installs fail on proxied hosts.
# They are handled separately from the plain allowlist for that both-case
# emission; they are not credentials beyond what the host already exposes to
# any network operation.
_PROXY_ENV_NAMES: tuple[str, ...] = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")

# arc's own runtime-contract namespace (ports, package names, template
# wiring). Generated app code legitimately reads these
# (``process.env.ARC_WEB_PORT`` in the template's vite/playwright configs);
# provider credentials live in non-ARC names.
_ARC_ENV_PREFIX = "ARC_"


def build_subprocess_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the environment for a generated-app build/test/install subprocess.

    Layers, later wins: the host whitelist, every host ``ARC_*`` variable,
    then ``extra`` (runtime-contract values like the web port and fixed
    encoding/tool overrides the caller pins).
    """

    host_by_upper: dict[str, str] = {}
    for name, value in os.environ.items():
        host_by_upper.setdefault(name.upper(), value)

    env: dict[str, str] = {}
    for name in _SUBPROCESS_ENV_ALLOWLIST:
        value = host_by_upper.get(name.upper())
        if value is not None:
            env[name] = value
    for proxy in _PROXY_ENV_NAMES:
        value = host_by_upper.get(proxy)
        if value is not None:
            env[proxy] = value
            env[proxy.lower()] = value
    for name, value in os.environ.items():
        if name.startswith(_ARC_ENV_PREFIX):
            env[name] = value
    if extra:
        env.update(extra)
    return env


async def finalize_subprocess(process: Any, *, force_kill: bool = False) -> None:
    if process is None or getattr(process, "returncode", None) is not None:
        return
    if force_kill:
        process.kill()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def check_prerequisites(app_type: str, log_cb: LogCallback | None = None) -> bool:
    normalized = (app_type or "web").strip().lower()
    from app_type_handler import get_app_type_handler_class

    handler_class = get_app_type_handler_class(normalized)
    required = handler_class.prerequisite_commands()
    missing = [command for command in required if shutil.which(command) is None]
    if missing:
        await _emit_log(
            log_cb,
            "System",
            f"Missing required command(s) for app_type={normalized}: {', '.join(missing)}",
            status="error",
        )
        return False
    if not await handler_class.check_runtime_versions(log_cb=log_cb):
        return False
    await _emit_log(log_cb, "System", f"Prerequisite check passed for app_type={normalized}.")
    return True


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import shutil
import signal
import weakref
from collections.abc import Mapping
from contextlib import suppress
from typing import Any, Awaitable, Callable


logger = logging.getLogger(__name__)


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

# arc's own runtime-contract keys, explicitly enumerated (review round on
# PR #201: a prefix wildcard would also auto-pass a future credential-shaped
# ``ARC_*`` name, which is exactly the unprovable leak mode the whitelist
# exists to prevent). These are the names generated-app code actually reads
# (``process.env.ARC_WEB_PORT`` in the template's vite/playwright configs,
# ``ARC_DB_FILE``/``ARC_E2E_DB_LABEL`` in the database scaffold); provider
# credentials live in non-ARC names. The per-attempt values are layered as
# caller extras (``build_web_runtime_env``/``_build_e2e_runtime_env``); this
# set only governs what may arrive from the host environment. A newly needed
# contract key is added here with a one-line reason, same as the allowlist.
_ARC_CONTRACT_ENV_KEYS: tuple[str, ...] = (
    "ARC_WEB_PORT",
    "ARC_WEB_BASE_URL",
    "ARC_DB_FILE",
    "ARC_E2E_DB_LABEL",
)


def build_subprocess_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the environment for a generated-app build/test/install subprocess.

    Layers, later wins: the host whitelist, arc's enumerated runtime-contract
    keys, then ``extra`` (runtime-contract values like the web port and fixed
    encoding/tool overrides the caller pins). Every emitted name is canonical
    (list) casing regardless of how the host spelled it.
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
    for name in _ARC_CONTRACT_ENV_KEYS:
        value = host_by_upper.get(name)
        if value is not None:
            env[name] = value
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Process-tree spawn and cleanup (issue #181)
# ---------------------------------------------------------------------------
#
# Build/test/install commands run through a shell or an npm wrapper, so the
# process arc spawns is only the launcher: on timeout the direct PID dies but
# its children (npm -> node -> Playwright browsers, `cmd /c` -> gradle -> java)
# survive, keep holding port slots and writing into the generated workspace —
# the port-conflict and E2E-database-race amplifier. The spawn helpers below
# pair with `finalize_subprocess` so a timeout tears down the whole tree:
#
# - POSIX: the launcher is spawned as a session/process-group leader
#   (`start_new_session=True`); descendants inherit the group unless they
#   deliberately `setsid` away, and finalize signals the whole group.
# - Windows: the launcher is assigned to a kernel Job Object with
#   ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``; every descendant joins the job
#   automatically, `TerminateJobObject` kills them all at once, and closing
#   the last job handle is a final safety net (also when arc itself crashes —
#   the handle dies with the process). A side effect: a member that
#   deliberately outlives its command (a Gradle daemon) dies at job close
#   instead of being reused — workspace hygiene wins over daemon reuse.
#
# Every enablement step is best-effort: a failure degrades to the legacy
# single-PID cleanup (plus a best-effort `taskkill /T /F` sweep on Windows),
# never to a failed spawn.

_PROCESS_KILL_SCOPE_ATTRIBUTE = "_arc_kill_scope"

# Grace period between the graceful signal (SIGTERM group / launcher
# terminate) and the forced escalation, mirroring the legacy finalize wait.
_TREE_KILL_GRACE_SECONDS = 3.0


class ProcessKillScope:
    """The tree-kill anchors attached to one spawned launcher process.

    ``pgid`` is the POSIX process group (the launcher's PID via
    ``start_new_session``); ``job_handle`` is the Windows Job Object HANDLE.
    ``close_job`` releases the job handle (idempotent); it is also registered
    as a ``weakref.finalize`` on the Process object so a normally-completing
    command releases the job, and KILL_ON_JOB_CLOSE sweeps anything a caller
    dropped without a teardown.
    """

    __slots__ = ("pgid", "job_handle", "close_job")

    def __init__(self) -> None:
        self.pgid: int | None = None
        self.job_handle: int | None = None
        self.close_job: Callable[[], None] | None = None


async def start_subprocess_exec(
    program: Any, *args: Any, **kwargs: Any
) -> asyncio.subprocess.Process:
    """`asyncio.create_subprocess_exec`, with process-tree cleanup enabled.

    All keyword arguments pass through unchanged (``cwd``/``env``/``stdout``
    ...), so this is a drop-in for the raw call; the only additions are the
    platform's tree-cleanup enablement. Callers keep owning the returned
    ``Process`` and clean it up through ``finalize_subprocess``.
    """

    tree_enabled = _apply_tree_spawn_kwargs(kwargs)
    process = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    _attach_kill_scope(process, tree_enabled=tree_enabled)
    return process


async def start_subprocess_shell(command: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    """`asyncio.create_subprocess_shell`, with process-tree cleanup enabled.

    Same contract as ``start_subprocess_exec``: pure passthrough kwargs plus
    the tree-cleanup enablement, no behavior change on the command itself.
    """

    tree_enabled = _apply_tree_spawn_kwargs(kwargs)
    process = await asyncio.create_subprocess_shell(command, **kwargs)
    _attach_kill_scope(process, tree_enabled=tree_enabled)
    return process


def _apply_tree_spawn_kwargs(kwargs: dict[str, Any]) -> bool:
    """Enable the platform's tree-cleanup spawn mode. False = stay legacy.

    POSIX only: make the launcher a session and process group leader so the
    whole descendant tree shares one killable group. A caller that explicitly
    opted out of the new session keeps the legacy path — signaling a pgid the
    launcher does not lead would reach arc's own process group. Windows gets
    the same guarantee from the Job Object assigned after the spawn (passing
    start_new_session there is a ValueError, hence the platform branch).
    """

    if os.name == "nt":
        return True
    if kwargs.get("start_new_session") is False:
        return False
    kwargs.setdefault("start_new_session", True)
    return True


def _attach_kill_scope(process: Any, *, tree_enabled: bool = True) -> None:
    """Attach ``ProcessKillScope`` to a freshly spawned process. Never raises.

    Failure modes degrade, in order of preference: an unassignable Job Object
    still attaches a pid-only scope (finalize then sweeps with
    ``taskkill /T /F``); a scope that cannot even be attached leaves the
    legacy single-PID cleanup in place.

    Known residual window: the job is assigned after the spawn returns, so a
    launcher that spawns a descendant inside that microseconds-wide gap
    escapes the job; the unconditional taskkill sweep at finalize closes it
    in practice (see ``_finalize_process_tree_windows``).
    """

    if not tree_enabled:
        return
    try:
        scope = ProcessKillScope()
        if os.name == "nt":
            job_handle, close_job = _create_kill_on_close_job()
            if job_handle is not None:
                if _assign_process_to_job(job_handle, process):
                    scope.job_handle = job_handle
                    scope.close_job = close_job
                    # Normal completion never reaches finalize_subprocess:
                    # release the job when the Process object is collected.
                    # With KILL_ON_JOB_CLOSE that close also kills members a
                    # caller dropped without a teardown.
                    weakref.finalize(process, close_job)
                else:
                    # Assignment failed (nested-job policy, exited launcher):
                    # release the unused job and fall back to the taskkill
                    # sweep via the pid-only scope.
                    if close_job is not None:
                        close_job()
        else:
            scope.pgid = int(process.pid)
        setattr(process, _PROCESS_KILL_SCOPE_ATTRIBUTE, scope)
    except Exception as exc:
        logger.debug("process-tree cleanup not enabled for spawn: %s", exc)


if os.name == "nt":
    import ctypes
    import ctypes.wintypes as _wt

    _ULONG_PTR = ctypes.c_size_t
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9  # JOBOBJECTINFOCLASS enum value

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", _wt.DWORD),
            ("MinimumWorkingSetSize", _ULONG_PTR),
            ("MaximumWorkingSetSize", _ULONG_PTR),
            ("ActiveProcessLimit", _wt.DWORD),
            ("Affinity", _ULONG_PTR),
            ("PriorityClass", _wt.DWORD),
            ("SchedulingClass", _wt.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", _ULONG_PTR),
            ("JobMemoryLimit", _ULONG_PTR),
            ("PeakProcessMemoryUsed", _ULONG_PTR),
            ("PeakJobMemoryUsed", _ULONG_PTR),
        ]


def _create_kill_on_close_job() -> tuple[int | None, Callable[[], None] | None]:
    """Create a Windows Job Object that kills every member on last close.

    Returns ``(handle, close)``; ``(None, None)`` when creation failed and the
    caller should fall back to the taskkill sweep.
    """

    kernel32 = ctypes.windll.kernel32
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None, None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(job)
        return None, None

    closed = False

    def _close_job() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        with suppress(Exception):
            kernel32.CloseHandle(job)

    return int(job), _close_job


def _assign_process_to_job(job: int, process: Any) -> bool:
    """Assign the spawned launcher to the job. Failure is non-fatal."""

    try:
        # asyncio's subprocess Process wraps a subprocess.Popen whose
        # ``_handle`` is the process HANDLE; ``int()`` unwraps
        # ``subprocess.Handle`` to the raw value ctypes needs.
        handle = int(process._transport._proc._handle)  # type: ignore[attr-defined]
    except Exception:
        return False
    try:
        return bool(ctypes.windll.kernel32.AssignProcessToJobObject(job, handle))
    except Exception:
        return False


def _terminate_job_members(job: int) -> None:
    try:
        ctypes.windll.kernel32.TerminateJobObject(job, 1)
    except Exception:
        pass


async def _taskkill_tree(pid: int) -> None:
    """Best-effort Windows tree sweep for spawns without a Job Object.

    ``taskkill /T`` walks parent PIDs as observed at call time: children of an
    already-exited parent were re-parented and are missed — the residual risk
    that makes the Job Object the primary mechanism rather than this fallback.
    """

    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=build_subprocess_env(),
        )
    except Exception:
        return
    with suppress(Exception):
        await asyncio.wait_for(killer.wait(), timeout=5.0)


async def finalize_subprocess(process: Any, *, force_kill: bool = False) -> None:
    """Tear down one spawned subprocess — its whole tree when cleanup was enabled.

    Processes spawned through ``start_subprocess_exec``/``start_subprocess_shell``
    carry a ``ProcessKillScope``: POSIX signals the process group (SIGTERM,
    then SIGKILL after the grace period) and Windows terminates the Job Object,
    so shell/npm/gradle descendants die with the launcher. Any other process
    keeps the legacy single-PID semantics below, unchanged for existing callers.

    Shape of the grace period: on POSIX the group gets a SIGTERM stage; on
    Windows ``TerminateProcess`` is the only termination primitive, so the
    tree (unlike the launcher's terminate→wait→kill escalation) is hard-killed
    immediately — there is no graceful stage for tree members.

    An already-reaped launcher (``returncode`` set) returns immediately and
    sweeps nothing: descendants that outlived a dead launcher are the caller's
    second-line machinery's job (e.g. the backend port capture in
    ``app_type_handler.backend_runtime``).
    """

    if process is None or getattr(process, "returncode", None) is not None:
        return
    scope: ProcessKillScope | None = getattr(process, _PROCESS_KILL_SCOPE_ATTRIBUTE, None)
    if scope is not None:
        try:
            if os.name == "nt":
                await _finalize_process_tree_windows(process, scope, force_kill=force_kill)
            else:
                await _finalize_process_tree_posix(process, scope, force_kill=force_kill)
            return
        except Exception as exc:
            # A tree-kill helper failure must not skip the direct kill either.
            logger.debug("process-tree finalize degraded to direct kill: %s", exc)
    if force_kill:
        process.kill()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=_TREE_KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _finalize_process_tree_posix(
    process: Any, scope: ProcessKillScope, *, force_kill: bool
) -> None:
    pgid = scope.pgid
    if pgid is None:
        raise RuntimeError("process-tree scope carries no POSIX process group")
    if not force_kill:
        _signal_process_group(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=_TREE_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            pass
    # Covers both the timed-out graceful stage and a launcher that died while
    # descendants (same group) survived the SIGTERM.
    if _process_group_alive(pgid):
        _signal_process_group(pgid, signal.SIGKILL)
    await process.wait()


def _signal_process_group(pgid: int, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _process_group_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _finalize_process_tree_windows(
    process: Any, scope: ProcessKillScope, *, force_kill: bool
) -> None:
    if scope.job_handle is not None:
        _terminate_job_members(scope.job_handle)
    # The launcher itself: keep the legacy terminate -> wait -> kill escalation
    # so wait() unblocks even when the job was missing or already closed.
    if force_kill:
        _kill_process_quietly(process)
    else:
        with suppress(OSError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=_TREE_KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _kill_process_quietly(process)
        await process.wait()
    # The sweep runs even with a live job: a descendant spawned in the
    # pre-assignment window between spawn and AssignProcessToJobObject is not
    # a job member, and /T walks the launcher's parent-PID tree (still intact
    # here — the launcher is only now being reaped). Best-effort with the
    # re-parenting limitation documented on ``_taskkill_tree``.
    await _taskkill_tree(process.pid)
    if scope.close_job is not None:
        scope.close_job()


def _kill_process_quietly(process: Any) -> None:
    with suppress(OSError):
        process.kill()


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

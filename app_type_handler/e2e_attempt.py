"""One grouped E2E attempt as a module, behind a small interface.

The full per-attempt pipeline used to live on ``WebAppType``: a private
attempt method with the frontend-build verdict, the served-artifact verdicts,
the failure-body renderer, the Playwright command assembly and the stage
timer scattered around it in the same file. This module owns all of it, so
``run_test_group``'s e2e branch is a thin app-type adapter that launches one
group call instead of hosting the pipeline.

The interface follows the ``BackendRuntime`` precedent - policy in the
module, mechanics injected:

- the session lifecycle arrives as a ``BackendRuntime`` instance (the process
  adapter in production, ``InMemoryBackendRuntime`` in tests), so the attempt
  drives the three-action interface and never touches session state itself;
- the shell-command edge (the frontend ``npm run build`` and the Playwright
  batch) is the module-level ``_execute_web_test_command`` runner imported
  from ``backend_runtime``; tests stub it here, on this module.

Output bodies are byte-for-byte the pre-extraction assemblies; the move is
mechanical and every rendering quirk (section order, blank-line counts, the
backend-startup body's Database-Prepare-before-runtime-env order) is
preserved on purpose. Attempt-level recovery policy lives here as well:
``E2EAttemptRunner.run_group`` detects the dead-static-host signature, spends
the one-shot recovery budget (forced rebuild + backend restart + re-run,
rendered with the failed attempt as a superseded appendix), and flips a
green attempt whose backend-runtime cleanup failed to a failure. The web
handler holds no attempt-level state: it builds one runner per handler
instance and reuses it across ``run_test_group`` calls, so the one-shot
recovery budget spans the handler's lifetime exactly like the
pre-extraction handler flag did.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .backend_runtime import (
    BackendRuntime,
    _build_e2e_runtime_env,
    _execute_web_test_command,
    backend_source_fingerprint,
)
from .test_results import TestRunResult, parse_test_run
from core.config import build_web_runtime_env

logger = logging.getLogger(__name__)

# The single timeout source for the E2E test-runner command (the Playwright
# batch). The helper's own npm-script invocations inside the E2E attempt
# (db:prepare / db:seed, frontend build) keep their individual budgets —
# they are not per-path forks of the runner timeout this constant replaced.
E2E_RUNNER_TIMEOUT_SECONDS = 120.0


# ---------------------------------------------------------------------------
# Retry-round case filter: digest names -> Playwright --grep
# ---------------------------------------------------------------------------


# Retry-round case filter: digest names are reporter display titles
# ("suite › case" for Playwright, "suite > case" for Vitest list lines), so the
# leaf case name is the segment that stays a contiguous substring of the full
# title regardless of how the runner joins describe blocks (space in the
# matched title, › in the printed form). Over-matching a same-named case in
# another suite only re-runs a passing test; the final full run still decides.
_FAILED_CASE_TITLE_SEPARATOR = re.compile(r"\s*[›>]\s*")


def _shell_single_arg(value: str) -> str:
    r"""Quote ``value`` as one shell argument for the running platform.

    The test commands run through ``create_subprocess_shell``: cmd.exe on
    Windows, /bin/sh elsewhere. cmd.exe ignores POSIX single quotes and does
    not treat ``\`` as an escape, so a shlex-quoted pattern would arrive at
    Playwright split on its spaces; double quotes are the form both cmd.exe
    and the MSVCRT argv parser hand over intact.
    """

    if os.name == "nt":
        if re.search(r'[\s"&|<>^()]', value):
            return '"' + value.replace('"', '\\"') + '"'
        return value
    return shlex.quote(value)


def _build_case_grep_pattern(failed_case_names: list[str] | None) -> str:
    """Build the Playwright ``--grep`` regex for a retry round's failed cases.

    Returns "" when no usable name survives (the caller then runs the full
    layer). Each name contributes its leaf segment, regex-escaped; segments
    are joined as an alternation.
    """

    leaves: list[str] = []
    for raw_name in failed_case_names or []:
        leaf = _FAILED_CASE_TITLE_SEPARATOR.split(str(raw_name or "").strip())[-1].strip()
        if leaf and leaf not in leaves:
            leaves.append(leaf)
    return "|".join(re.escape(leaf) for leaf in leaves)


# ---------------------------------------------------------------------------
# Stage timing
# ---------------------------------------------------------------------------


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
            self.record(stage, time.monotonic() - started)

    def record(self, stage: str, elapsed: float) -> None:
        """Merge one measured duration into the stage breakdown.

        ``measure`` wraps awaitables with this; the attempt also folds the
        backend runtime's own sub-stage durations in (``BackendRuntime.ensure``
        reports them on the acquisition), so both paths attribute cost to the
        same stages.
        """
        self._stages[stage] = self._stages.get(stage, 0.0) + elapsed

    def render(self) -> str:
        if not self._stages:
            return ""
        parts = [f"{stage}={elapsed:.1f}s" for stage, elapsed in self._stages.items()]
        return "\n\n=== Stage Timing ===\n" + " | ".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Frontend build verdict (the attempt's first gate)
# ---------------------------------------------------------------------------


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

    ``note`` is the build verdict in the failure-digest phrasing; the attempt
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


# ---------------------------------------------------------------------------
# Served-artifact verdicts (the agent's only view of the read-denied dist/)
# ---------------------------------------------------------------------------


# The verdict line's authoritative state is checked on disk at render time, not
# captured at build time: the 2026-09-20 arc-output1 run died precisely because
# the builder said "Built/Reused" while the backend's request-time stat failed.
# Re-checking at result-assembly time reports what the backend can serve NOW,
# and the digest-side regex parses this exact line shape.
_SERVED_VERDICT_PREFIX = "Served index.html: "
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


# ---------------------------------------------------------------------------
# Dead-static-host failure signature (the recovery trigger's input)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The premature-exit failure renderer
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The attempt runner: one attempt, one call; one group, its recovery policy
# ---------------------------------------------------------------------------


# Marker carried by a backend-runtime cleanup that itself failed (the teardown
# exception note). A run whose cleanup failed is a failure for the agent even
# when the tests passed, so the exit verdict flips when this marker is present.
_CLEANUP_FAILURE_MARKER = "Backend runtime cleanup failed:"


@dataclass
class E2EAttemptOutcome:
    """What one ``run_attempt`` established: the result plus teardown evidence.

    ``backend_cleanup_note`` is the accumulated cleanup evidence (the prior
    attempt's note plus this attempt's stale-session teardown) — the material
    a recovery re-run carries forward and the group-level verdict flips read.
    """

    result: TestRunResult
    backend_cleanup_note: str


class E2EAttemptRunner:
    """Owner of one grouped E2E attempt's full execution path.

    The pipeline: frontend build verdict (with the ``force_rebuild`` cache
    bypass and its failure body), backend-runtime acquisition through
    ``BackendRuntime.ensure`` with its ``stage_seconds`` folded into the
    attempt's own timing, the Playwright command (with the retry-round case
    filter), and the success/failure evidence bodies in their fixed section
    order.

    The interface follows the ``BackendRuntime`` precedent — policy here,
    mechanics injected: the session lifecycle arrives as a ``BackendRuntime``
    (the process adapter in production, ``InMemoryBackendRuntime`` in tests);
    the shell-command edge is the module-level ``_execute_web_test_command``
    runner, stubbed on this module by tests.

    Attempt-level recovery policy lives here too:

    - ``run_group`` is the one entry ``run_test_group``'s e2e branch calls.
      It launches the first attempt and, when that attempt dies on the dead
      static-host signature, spends the one-shot recovery budget (forced
      rebuild + backend restart + re-run) and renders the retried body with
      the failed attempt as a superseded appendix;
    - the recovery budget is per runner instance. The web handler builds one
      runner per handler and reuses it across ``run_test_group`` calls, so
      the one-shot semantics match the pre-extraction handler flag exactly;
    - a green attempt whose backend-runtime cleanup failed is flipped to a
      failure, on the recovery path and the plain path alike.

    One runner per ``run_test_group`` call would reset the budget per call;
    the handler therefore caches the runner (see ``WebAppType``), and tests
    drive the same runner the handler drives.
    """

    def __init__(self, workspace_path: str, *, backend_runtime: BackendRuntime) -> None:
        self.workspace_path = workspace_path
        self._backend_runtime = backend_runtime
        self._stage_timer = _StageTimer()
        # Cleanup evidence accumulated so far (the prior attempt's note plus
        # any stale-session teardown this runner has already driven). Kept on
        # the runner, not in a local, so the caller's exception fallback can
        # still surface it: an attempt that dies mid-acquisition has already
        # paid stale teardowns whose notes must reach the failure body.
        self._cleanup_note = ""
        # One-shot budget for the SPA static-host self-heal (dead `send`
        # NotFoundError in a sendFile frame): a second occurrence of the same
        # signature means the rebuild did not cure it, and the failure must go
        # to the agent instead of looping system-side.
        self._spa_static_host_recovery_used: bool = False

    def accumulated_cleanup_note(self) -> str:
        """The cleanup evidence gathered across this runner's attempts so far.

        The attempt's own bodies embed it already; this accessor exists for
        the caller's exception fallbacks, where no attempt body was produced.
        It also folds in the backend runtime's ``last_cleanup_note`` — the
        evidence an ``ensure`` call had gathered before dying mid-flight,
        which never reached this runner's accumulation.
        """

        runtime_note = getattr(self._backend_runtime, "last_cleanup_note", "")
        runtime_note = runtime_note() if callable(runtime_note) else (runtime_note or "")
        if self._cleanup_note and runtime_note:
            return f"{self._cleanup_note}\n{runtime_note}"
        return self._cleanup_note or runtime_note

    @property
    def spa_static_host_recovery_used(self) -> bool:
        """Whether the one-shot dead-static-host recovery budget is spent."""

        return self._spa_static_host_recovery_used

    def render_stage_timing(self) -> str:
        """The stage-timing block accumulated so far.

        The attempt's own bodies embed it already; this accessor exists for
        the caller's exception fallbacks, where no attempt body was produced.
        """

        return self._stage_timer.render()

    async def run_group(
        self,
        execution: dict[str, str],
        resolved_port: int,
    ) -> TestRunResult:
        """Run the group's attempts: one attempt plus at most one recovery.

        This is the whole attempt-level recovery policy: signature detection,
        the one-shot budget, the forced-rebuild retry, the superseded-appendix
        rendering and the cleanup-failure verdict flips (both the retried
        attempt's own flip and the group-level flip across both attempts'
        cleanup notes). The caller only decides whether to launch a group.
        """

        try:
            attempt = await self.run_attempt(
                execution,
                resolved_port,
                force_rebuild=False,
                prior_cleanup_note="",
            )
        except Exception as exc:
            return TestRunResult(
                exit_code=1,
                output=(
                    f"Failed to start grouped E2E execution: {str(exc)}"
                    + self.render_stage_timing()
                ),
            )
        result = attempt.result
        backend_cleanup_note = attempt.backend_cleanup_note

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
            logger.info(
                "E2E failure matches the SPA static-host signature (send NotFoundError in sendFile); "
                "forcing one frontend rebuild + backend restart retry."
            )
            recovery_note = await self._backend_runtime.terminate(
                "SPA static-host recovery cleanup"
            )
            try:
                retried_attempt = await self.run_attempt(
                    execution,
                    resolved_port,
                    force_rebuild=True,
                    prior_cleanup_note=recovery_note or backend_cleanup_note,
                )
                retried_result = retried_attempt.result
                retried_cleanup_note = retried_attempt.backend_cleanup_note
            except Exception as exc:
                retried_result = TestRunResult(
                    exit_code=1,
                    output=(
                        f"Failed to retry grouped E2E execution after SPA static-host recovery: {str(exc)}"
                        + self.render_stage_timing()
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
        return result

    async def run_attempt(
        self,
        execution: dict[str, str],
        resolved_port: int,
        *,
        force_rebuild: bool = False,
        prior_cleanup_note: str = "",
    ) -> E2EAttemptOutcome:
        """Run one grouped E2E attempt end to end and return its outcome.

        ``run_group`` calls this once, and a second time with
        ``force_rebuild=True`` when the first attempt died on the SPA
        static-host signature (see the recovery block there). The runtime env
        is rebuilt per attempt so each carries its own database label.

        Raises on unexpected execution errors — the group-level caller renders
        them with :meth:`render_stage_timing`, and its fallback message
        differs per call site (the recovery re-run's fallback names the
        retry). The outcome's ``backend_cleanup_note`` lets the recovery
        re-run carry the teardown evidence of the attempt before it.
        """

        build = await self._stage_timer.measure(
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
            return E2EAttemptOutcome(
                result=parse_test_run(
                    _render_e2e_failure_body(
                        facts,
                        headline="Frontend build failed before E2E startup.",
                        served_verdict=served_verdict,
                        stage_timer=self._stage_timer,
                    ),
                    exit_code=1,
                    build_note=build.note,
                    served_verdict=_frontend_serving_note(self.workspace_path),
                ),
                backend_cleanup_note=prior_cleanup_note,
            )

        e2e_runtime_env = _build_e2e_runtime_env(
            self.workspace_path,
            execution.get("resolved_targets", []),
            web_port=resolved_port,
        )
        facts.runtime_env = e2e_runtime_env

        backend_cleanup_note = prior_cleanup_note
        # Off the event loop: hashing a large backend tree is pure blocking I/O
        # and must not freeze concurrent runner work on the same loop.
        backend_fingerprint = await asyncio.to_thread(
            backend_source_fingerprint,
            os.path.join(self.workspace_path, "backend"),
        )
        # One session owner: reuse-or-rebuild, database reset and stale teardown
        # all live in BackendRuntime.ensure now (app_type_handler/backend_runtime.py).
        acquisition = await self._backend_runtime.ensure(
            resolved_port,
            e2e_runtime_env.get("ARC_E2E_DB_PATH", ""),
            backend_fingerprint,
            runtime_env=e2e_runtime_env,
        )
        for stage, elapsed in acquisition.stage_seconds.items():
            self._stage_timer.record(stage, elapsed)
        if acquisition.cleanup_note:
            backend_cleanup_note = (
                f"{backend_cleanup_note}\n{acquisition.cleanup_note}"
                if backend_cleanup_note
                else acquisition.cleanup_note
            )
        # Mirror the accumulated evidence onto the runner before any further
        # await: if a later stage raises, the caller's exception fallback
        # reads it from here instead of losing the stale teardown notes this
        # attempt already paid for.
        self._cleanup_note = backend_cleanup_note
        database_prepare_output = acquisition.db_output

        if acquisition.session is None:
            facts.cleanup_note = backend_cleanup_note
            if acquisition.failure_stage == "database":
                return E2EAttemptOutcome(
                    result=parse_test_run(
                        _render_e2e_failure_body(
                            facts,
                            headline="E2E database preparation failed before backend startup.",
                            served_verdict=served_verdict,
                            database_prepare_output=database_prepare_output,
                            stage_timer=self._stage_timer,
                        ),
                        exit_code=1,
                        build_note=build.note,
                        served_verdict=_frontend_serving_note(self.workspace_path),
                    ),
                    backend_cleanup_note=backend_cleanup_note,
                )
            return E2EAttemptOutcome(
                result=parse_test_run(
                    _render_e2e_failure_body(
                        facts,
                        headline=None,  # the backend sections carry the failure
                        served_verdict=served_verdict,
                        database_prepare_output=database_prepare_output,
                        backend_start_command=acquisition.start_command,
                        backend_startup_detail=acquisition.startup_detail,
                        stage_timer=self._stage_timer,
                    ),
                    exit_code=1,
                    build_note=build.note,
                    served_verdict=_frontend_serving_note(self.workspace_path),
                ),
                backend_cleanup_note=backend_cleanup_note,
            )

        reused_runtime = acquisition.reused
        acquired_session = acquisition.session
        backend_start_command = acquired_session.start_command
        backend_startup_detail = acquired_session.startup_detail
        backend_instance_fingerprint = acquired_session.instance_fingerprint

        playwright_command = "npx playwright test"
        if execution.get("resolved_targets"):
            playwright_command += " " + " ".join(execution["resolved_targets"])
        # Retry rounds run only the previous round's failed cases; the SPA
        # static-host recovery re-runs the same command on purpose, so the
        # filter rides on the execution dict and survives that second attempt.
        case_grep = execution.get("failed_case_grep", "")
        if case_grep:
            playwright_command += " --grep " + _shell_single_arg(case_grep)
        playwright_result = await self._stage_timer.measure(
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
            + self._stage_timer.render()
        )
        return E2EAttemptOutcome(
            result=parse_test_run(
                body,
                exit_code=playwright_exit_code,
                build_note=build.note,
                served_verdict=_frontend_serving_note(self.workspace_path),
            ),
            backend_cleanup_note=backend_cleanup_note,
        )

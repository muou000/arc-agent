"""Tool-level guardrails for ARC's staged agent workflow."""

from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from agents.runtime.capabilities import capability_for, is_test_file_path, normalize_manifest_path
from agents.runtime.import_checks import (
    ImportViolation,
    build_import_block_message,
    classify_import,
    extract_relative_esm_imports,
    is_js_source_path,
)
from agents.tools.test_manifest import TestManifestLock
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from core.file_claims import FileClaimGate

_FILE_WRITE_TOOLS = frozenset({"edit_file", "write_file"})
_ADDITIVE_FILE_WRITE_TOOLS = frozenset({"append_file"})
_VALIDATION_TOOLS = frozenset({"run_build", "run_tests"})
# Marker prefix of results produced by _blocked(); tool-usage observability
# uses it to tell blocked round-trips apart from tool errors.
BLOCKED_RESULT_PREFIX = "Error: ARC stage discipline:"
# DESIGN write budget: distinct file paths whose first successful touch
# (write_file/edit_file/append_file all count the same) a pass may
# materialize. The budget counts *paths*, not call count: rewriting or
# appending to a path already touched never consumes budget again. A leaf
# pass owns ~7 new modules plus small additive wiring edits on shared
# surfaces (run6: 7 owned + 5 shared = 12 legal paths), so its ceiling is 12.
# A non-leaf shell pass composes many small presentational files around its
# mount points (arc-output4 ROOT materialized 12 in one legal batch), so its
# ceiling is 16.
_MAX_DESIGN_WRITES = 12
MAX_DESIGN_WRITES = _MAX_DESIGN_WRITES
_MAX_NON_LEAF_DESIGN_WRITES = 16
MAX_NON_LEAF_DESIGN_WRITES = _MAX_NON_LEAF_DESIGN_WRITES
# The DESIGN line gates are both gone (issue #158 removed the per-write
# gate, issue #161 removed the append tool's per-file ceiling): line counts
# were the rework-loop generator ADR 0005 rejects; the main defense is the
# shape-only skeleton definition plus the mutation sniffing below.
MAX_APPEND_LINES = 80
MAX_APPENDS_PER_FILE = 3
# Read-only probe stall (issue #217): a stage agent that keeps probing the
# same artifact without ever writing is archaeology, not progress. The
# arc-output-serial-4 REQ-1 storm burned 85 greps plus a dozen 10-line window
# reads on one Integration log with zero writes for 35 minutes; the only
# guardrail was the 300-step recursion ceiling. Calibration across six full
# serial runs (37 stage passes): a healthy pass never exceeds 11 probes of
# one target between writes, while the two storm passes reached 65 and 92 -
# the threshold sits in that gap. Counted per normalized target since the
# last successful write-family call; other tools (run_tests, traceability
# queries) neither count nor reset, because the issue's condition is "zero
# writes", not "no other activity". A nudge fires once per zero-write window
# and re-arms on the next write - guidance, not a gate.
_PROBE_TOOLS = frozenset({"read_file", "grep", "glob", "ls"})
_WRITE_FAMILY_TOOLS = frozenset({"write_file", "edit_file", "append_file", "delete"})
_MAX_PROBES_PER_TARGET = 20
MAX_PROBES_PER_TARGET = _MAX_PROBES_PER_TARGET


def append_line_limit_message(received_lines: int) -> str:
    """One rejection message for oversized appends, shared by the middleware
    and the ``append_file`` tool so the two cannot drift apart."""

    return (
        f"append_file accepts at most {MAX_APPEND_LINES} lines per call; received {received_lines}. "
        "If the next section does not fit in one compact append, it is not skeleton "
        "material - put the behavior in your stage response for TestDrivenDeveloper "
        "instead of extending this file."
    )


def probe_stall_hint(*, target: str, target_probes: int) -> str:
    """One convergence nudge for the read-only probe storm (issue #217).

    Single-sourced here so the middleware and the pin tests cannot drift
    apart. The text names the concrete shape - which artifact was probed how
    often - instead of a generic "stop searching" lecture, and demands one of
    the two exits the issue asks for: a repair action, or an explicit
    abandonment with a summary.
    """

    where = f"`{target}`" if target else "the whole workspace"
    return (
        f"PROBE STALL: {target_probes} read-only lookups on {where} with zero writes since "
        "your last edit - the context you already gathered is enough to form a repair "
        "hypothesis. Either apply the fix now (write_file/edit_file), or explicitly declare "
        "this lead abandoned: summarize what you ruled out and move on. Another identical "
        "lookup only returns text you already have."
    )

_MAX_READ_LIMIT = 200
# Fresh overlapping re-reads allowed per path before the block returns. The
# hard block exists for the run7 loop (50 consecutive offset probes), but the
# fbd4a73b TDD run showed the other edge: an agent whose legitimate re-read is
# refused does not stop wanting the content — it burned 10 consecutive greps
# rebuilding one file, at a higher round-trip cost than the re-read itself.
# A small per-path budget serves the legitimate case and still caps the loop.
_MAX_REPEATED_READS_PER_PATH = 2
# Successful repair attempts allowed per test-file path in one test_generation
# or implementation pass. Two channels share this one budget: a targeted
# same-path ``edit_file`` fix on an already-written test asset (issue #240)
# and a full delete-then-rewrite cycle. The delete release exists so a
# legitimate fix does not wait for an accidental failure to unlock; the
# 2026-09-19 arc-output3 run showed its unbounded edge: a TestGenerator that
# *believed* its writes had been truncated (they had not — no truncation error
# ever occurred) re-ran the delete+write cycle 5-7 times per file, ~10M input
# tokens, until the step budget crashed the whole DESIGN task. Counting the
# cycle on the delete keeps the last written version on disk when the cap
# trips. The arc-output-serial-6 run showed the other edge: with delete+write
# as the only sanctioned channel, a one-apostrophe fix found by grep became a
# 6.2KB whole-file rewrite (11+ delete→write pairs, ~$0.5-0.7 of the stage),
# and the rewrite itself introduced new errors — hence the cheaper edit
# channel drawing from the same budget instead of a separate one.
_MAX_TEST_ASSET_REPAIRS_PER_PATH = 2
_DESIGN_MUTATION_PATTERNS = (
    re.compile(r"\b(?:INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.IGNORECASE),
    re.compile(
        r"\.\s*(?:execute|exec|run|query|prepare|insert|upsert|update|delete|save|create)\s*\(",
        re.IGNORECASE,
    ),
)
_COMMENT_LINE_PREFIXES = ("//", "#", "--", "/*", "<!--")


def _without_comment_lines(content: str) -> str:
    """Drop whole-line comments so documented SQL never trips the guard."""

    kept: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(_COMMENT_LINE_PREFIXES):
            continue
        # JSDoc/block-comment continuation. A `*gen()` declaration is kept:
        # only an asterisk followed by whitespace (or nothing) is a comment.
        if stripped.startswith("*") and (len(stripped) == 1 or stripped[1] in " \t"):
            continue
        kept.append(line)
    return "\n".join(kept)

# Stage-specific exits appended to the repeated-write block: a generic
# "wait for an error" gave stages without reachable errors (test_generation
# has validation tools disabled) no visible way out. Kept to one sentence —
# the message re-enters the context on every blocked attempt.
_WRITE_BLOCK_EXITS = {
    "test_generation": (
        "To change it: apply a targeted `edit_file` fix to the written version "
        "(at most two repair attempts per path, shared with the delete-rewrite "
        "escape); if the test assets are ready, stop editing and return the "
        "updated manifest."
    ),
    "implementation": (
        "To change it: run the tests (a failing run unlocks written files for fixes) "
        "or write the revision to a new path."
    ),
    "interface_design": (
        "To change it: record the remaining interfaces in your response instead of "
        "rewriting this skeleton."
    ),
}

_INTERFACE_DESIGN_FIRST_WRITE_BLOCK_EXIT = (
    "Unlock condition: only a failed file operation on this path unlocks it. "
    "DESIGN has no build or test validation to trigger that unlock, and "
    "`delete` is disabled too. Do not retry this path; keep the skeleton and "
    "put its contract in the final response's `interfaces` array."
)


class StageDisciplineState(TypedDict, total=False):
    """Run-local state used to prevent redundant file-tool loops."""

    arc_read_summaries: NotRequired[dict[str, str]]
    arc_written_paths: NotRequired[list[str]]


class StageDisciplineMiddleware(AgentMiddleware[StageDisciplineState, Any, Any]):
    """Enforce stage boundaries and stop post-write self-review loops."""

    state_schema = StageDisciplineState

    def __init__(
        self,
        *,
        stage: Literal["interface_design", "test_generation", "implementation"],
        file_claim_gate: "FileClaimGate | None" = None,
        test_manifest_lock: TestManifestLock | None = None,
        pending_contract_registry: Any | None = None,
        template_shared_surfaces: frozenset[str] | None = None,
        max_design_writes: int | None = None,
        workspace_root: str | None = None,
    ) -> None:
        self._stage = stage
        self._file_claim_gate = file_claim_gate
        # Manifest-first gate for the test_generation stage: once set, test
        # files may only be written/edited/deleted on declared paths. ``None``
        # keeps non-TestGenerator uses of this middleware (tests, other
        # stages) on the classic behavior.
        self._test_manifest_lock = test_manifest_lock
        # interface_design only: contract bookkeeping for freshly written
        # files (see ``agents.design.contract_skeleton.PendingContractRegistry``).
        self._pending_contract_registry = pending_contract_registry
        # Workspace-relative template files no stage may replace wholesale
        # (see ``AppTypeHandler.template_shared_surfaces``). ``edit_file`` /
        # ``append_file`` stay allowed; the ban is absolute within the stage
        # and is deliberately not unlocked by validation failures — a failing
        # test never makes destroying runtime wiring the right repair.
        self._template_shared_surfaces = template_shared_surfaces or frozenset()
        # DESIGN write budget for this pass (interface_design only). Leaf
        # nodes default to 8; a non-leaf shell pass may raise it (see
        # ``InterfaceDesigner._max_design_writes``).
        self._max_design_writes = max_design_writes if max_design_writes is not None else _MAX_DESIGN_WRITES
        # Anchor for the write-time test-import validation (issue #156):
        # the agent's filesystem root — the same root the ``/workspace/``
        # backend route maps to — so import resolution can check whether the
        # imported file exists. ``None`` (direct constructions in tests,
        # tooling) keeps the checks off entirely; they only ever add
        # rejections, never permissions, so degrading to off is fail-open.
        self._import_probe_root = Path(workspace_root) if workspace_root else None
        self._import_fail_open_count = 0
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._repeated_read_counts: dict[str, int] = {}
        self._written_paths: set[str] = set()
        # Call-ordered log of every successful write/edit/append/delete path,
        # repeats included (``_written_paths`` is a set: a re-edit of the same
        # file never reappears there). The TDD failure-digest stall hint keys
        # on this to tell "kept editing the same test file" from "edited
        # source".
        self._write_events: list[str] = []
        self._failed_paths: set[str] = set()
        self._validation_failed = False
        # Paths that consumed budget at validation time. The agent emits file
        # tools in parallel batches, so counting only on success lets a whole
        # batch (arc-output4 ROOT: 12 writes in one tool-call burst) observe
        # the same stale count and overshoot the cap. Reserving at validation
        # and releasing on failure keeps the cap exact without punishing a
        # batch that respects it.
        self._design_write_reservations: set[str] = set()
        self._append_counts: dict[str, int] = {}
        # Per-path repair budget (issue #240): successful delete-rewrite
        # cycles and budgeted same-path repair edits both consume one unit
        # per path — one ledger, so swapping the expensive channel for the
        # cheap one cannot inflate the total.
        self._repair_counts: dict[str, int] = {}
        # Repair units reserved at validation time but not yet settled,
        # keyed by tool_call_id -> path. Reserving at validation keeps the
        # cap exact for parallel tool-call batches (same reasoning as the
        # DESIGN write reservations below); the id key lets ``_record_result``
        # release exactly the failed call's unit without touching a sibling
        # call's reservation on the same path.
        self._repair_reservations: dict[str, str] = {}
        self._write_block_counts: dict[str, int] | None = {} if stage == "interface_design" else None
        # Read-only probe ledger (issue #217): probes per normalized target
        # since the last successful write-family call, plus whether this
        # zero-write window's nudge already fired.
        self._probe_counts: dict[str, int] = {}
        self._probe_nudge_fired = False

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        blocked = self._validate_tool_call(request)
        if blocked:
            return self._blocked(request, blocked)
        request = self._with_bounded_read(request)
        nudge = self._track_probe(request)
        result = handler(request)
        self._record_result(request, result)
        result = self._annotate_pending_contract(request, result)
        return self._append_probe_stall(result, nudge)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        blocked = self._validate_tool_call(request)
        if blocked:
            return self._blocked(request, blocked)
        request = self._with_bounded_read(request)
        nudge = self._track_probe(request)
        result = await handler(request)
        self._record_result(request, result)
        result = self._annotate_pending_contract(request, result)
        return self._append_probe_stall(result, nudge)

    def _validate_tool_call(self, request: ToolCallRequest) -> str | None:
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args", {}) or {}
        path = _discipline_path(args)
        # The shared-surface guard runs ahead of the capability table so its
        # remediation message keeps precedence: a TestGenerator writing
        # template wiring (never a test asset) must be told to extend the
        # surface additively, not just that the asset is out of scope. Both
        # deny; only the message order is pinned here.
        if name == "write_file" and self._template_shared_surfaces:
            if blocked := self._validate_shared_surface(args):
                return blocked
        verdict = capability_for(self._stage, name, path)
        if not verdict.allowed:
            return verdict.message
        # What remains is runtime state the static table cannot see: the
        # manifest lock, write budgets, session ownership of deletes.
        if name == "delete":
            return self._validate_delete_channel(args)
        if name == "read_file":
            return self._validate_read(args)
        if name in _ADDITIVE_FILE_WRITE_TOOLS:
            return self._validate_append(args)
        if name in _FILE_WRITE_TOOLS:
            return self._validate_write(args, tool=name, call_id=str(request.tool_call.get("id", "")))
        return None

    def _validate_delete_channel(self, args: dict[str, Any]) -> str | None:
        """Runtime gates on the delete channels the capability table allows.

        The table's static verdict already narrowed delete to test-asset
        paths (test_generation and implementation); this adds what only the
        running session knows. test_generation: the manifest lock on
        declared paths plus the delete-rewrite budget. implementation: the
        session also owns the channel (#89) — only a test file written this
        pass may be removed (a diagnostic probe cleanup); registered
        manifest tests and product files were not written here, so they stay
        blocked with the table's own disabled message.
        """

        path = _discipline_path(args)
        if self._stage == "test_generation":
            manifest_block = self._validate_test_manifest_path(args, operation="delete")
            if manifest_block:
                return manifest_block
            return self._validate_delete_rewrite_budget(args)
        if path in self._written_paths:
            return self._validate_delete_rewrite_budget(args)
        # Not a session-owned path: reuse the table's own disabled verdict
        # (an empty path never matches the test-asset rule, so this is the
        # unconditional disabled message) to keep the text single-sourced.
        return capability_for(self._stage, "delete", "").message

    def _validate_shared_surface(self, args: dict[str, Any]) -> str | None:
        """Reject whole-file rewrites of the template's shared runtime surfaces.

        The template's app entry, server bootstrap, database lifecycle, and
        root render files carry wiring that no node owns: static serving, the
        SPA fallback, health/PORT contracts, the DB lifecycle the test harness
        shares with the runtime, and the root React render. In the 0aca31c5
        run a DESIGN skeleton replaced app.js wholesale, TDD rebuilt the
        static serving from scratch, and the worktree-relative dist path
        404'd every asset — a 47-minute blank-page debug loop. Extending these
        files additively is exactly the intended integration pattern, so
        ``edit_file``/``append_file`` stay allowed; the ban is deliberately
        not unlockable by validation failures.
        """

        path = _discipline_path(args)
        if not path:
            return None
        if normalize_manifest_path(path) not in self._template_shared_surfaces:
            return None
        return (
            f"Template shared surface blocked: {path} carries the copied template's runtime "
            "wiring (static serving, SPA fallback, server/DB bootstrap, or the root render) "
            "and no stage may replace it wholesale — that is how a past run lost its static "
            "serving and burned the budget debugging blank pages and 404 assets. Read the "
            "current file and extend it with `edit_file` (or `append_file`) at the "
            "established mount point, keep every existing export/mount intact, and declare "
            "the file as a reused interface in your response."
        )

    def _reserve_design_write(self, path: str) -> str | None:
        """Consume one unit of the DESIGN write budget for ``path``.

        Returns the block message when the budget is exhausted. The budget
        counts distinct paths (write_file, edit_file, and append_file all
        count a path's first touch the same); re-touching a path already
        reserved costs nothing. Reserving at validation time is what makes a
        parallel tool-call batch respect the cap: counting only on success
        let every call in the batch observe the same stale count and overshoot
        (arc-output4 ROOT: 12 writes in one burst, cap 8). A later failure on
        the path releases the reservation, so an errored attempt does not
        burn budget either.

        Invariant for future tool additions: exactly two call sites reserve —
        ``_validate_append`` (append_file) and the interface_design branch of
        ``_validate_write`` (write_file/edit_file). Any tool added to
        ``_FILE_WRITE_TOOLS`` or ``_ADDITIVE_FILE_WRITE_TOOLS`` routes through
        those validators and is therefore reserved automatically; a new write
        tool that bypasses them must call this helper at the same position in
        the check order — after per-write content checks, before the file
        claim gate, so a content-rejected write never burns budget and a
        claim-rejected one already holds its unit.
        """

        if path in self._design_write_reservations:
            return None
        if len(self._design_write_reservations) >= self._max_design_writes:
            used = len(self._design_write_reservations)
            return (
                f"Design write budget blocked: this pass may touch at most {self._max_design_writes} "
                f"distinct files (first write_file/edit_file/append_file on a path each count once; "
                f"re-touching a file you already wrote costs nothing). {used} file(s) are already "
                "reserved. Record the remaining interfaces in your response instead of writing "
                "more files - TestDrivenDeveloper owns the implementation work."
            )
        self._design_write_reservations.add(path)
        return None

    def _validate_append(self, args: dict[str, Any]) -> str | None:
        """Validate the DESIGN-stage additive continuation tool.

        Stage availability is the capability table's verdict (enforced
        pre-flight in ``_validate_tool_call``). The filesystem tool owns the
        existence and permission checks; this middleware keeps ownership and
        per-pass policy here so appending cannot bypass claims, write-count
        limits, or the observability used by InterfaceDesigner.
        """

        path = _discipline_path(args)
        if not path:
            return "append_file requires a workspace file_path."
        count = self._append_counts.get(path, 0)
        if count >= MAX_APPENDS_PER_FILE:
            return (
                f"InterfaceDesigner may append to {path} at most {MAX_APPENDS_PER_FILE} times in one pass. "
                "Keep the skeleton compact and record remaining contract detail in the response for TDD."
            )
        content = args.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return "append_file requires non-empty string content."
        if violation := self._validate_design_content(content):
            return violation
        line_count = len(content.splitlines())
        if line_count > MAX_APPEND_LINES:
            return append_line_limit_message(line_count)
        if import_block := self._validate_test_imports(path, content):
            return import_block
        if budget_block := self._reserve_design_write(path):
            return budget_block
        if self._file_claim_gate is not None:
            blocked = self._file_claim_gate.check_and_claim(path)
            if blocked:
                return blocked
        # Reserve the append budget before invoking the filesystem tool. A
        # failed append still represents a model attempt and must not become
        # an unbounded retry loop around a missing or denied file.
        self._append_counts[path] = count + 1
        return None

    def _validate_test_manifest_path(self, args: dict[str, Any], *, operation: str) -> str | None:
        """Manifest-first gate for test files (test_generation stage only).

        Test files (``.test.``/``.spec.`` names) must be declared through
        ``declare_test_manifest`` before they can be written or deleted, and
        every subsequent touch must stay on a declared path. This removes the
        rename/duplicate-file churn class at write time: an undeclared path
        cannot be created at all, so "try another name" and "write the same
        coverage twice" become hard errors. Helpers and runner configs are
        exempt — they carry no manifest entry and stay freely writable.

        A failed declaration deliberately does NOT unlock the gate: the model
        may retry the declaration until it validates; the stage can always end
        by returning an empty manifest instead.
        """

        if self._test_manifest_lock is None:
            return None
        path = _discipline_path(args)
        if not is_test_file_path(path):
            return None
        relative = normalize_manifest_path(path)
        if self._test_manifest_lock.contains(relative):
            return None
        if not self._test_manifest_lock.locked:
            return (
                f"Manifest-first blocked: {path} is a test file, but the test-file "
                "manifest has not been declared yet. Call `declare_test_manifest` "
                "first with every planned test file (path + type + interface ids), "
                "then write the files. Test helpers and runner configs do not need "
                "a declaration."
            )
        declared = ", ".join(sorted(self._test_manifest_lock.declared_files))
        return (
            f"Manifest lock blocked: {path} is not in the declared test-file manifest "
            f"({operation} on undeclared test paths is not allowed). Declared files: "
            f"{declared}. Rewriting a test under a different name or duplicating its "
            "coverage on a second path is not permitted; rework the content of a "
            "declared file instead."
        )

    def _validate_delete_rewrite_budget(self, args: dict[str, Any]) -> str | None:
        """Cap the delete-then-rewrite escape per test-file path.

        A successful delete releases the write lock by design, so nothing in
        the write path can see how often the cycle repeated. The cycle count
        lives on the delete itself: once a path has gone through
        ``_MAX_TEST_ASSET_REPAIRS_PER_PATH`` repair attempts (delete-rewrite
        cycles and budgeted same-path repair edits share one ledger, issue
        #240), the next delete is refused and the last written version
        stands. A failed file operation on the path keeps its generic unlock
        (``_path_unlocked``): the budget limits self-review churn, not
        repair after an error.
        """

        path = _discipline_path(args)
        if not path or self._path_unlocked(path):
            return None
        if self._repair_counts.get(path, 0) < _MAX_TEST_ASSET_REPAIRS_PER_PATH:
            return None
        return self._repair_budget_blocked(path, prefix="Rewrite budget")

    def _repair_budget_blocked(self, path: str, *, prefix: str) -> str:
        """The shared exhausted-budget block for both repair channels (#240).

        Single-sourced so the delete-rewrite and edit channels cannot drift
        apart in what the budget covers or where the exit leads. ``prefix``
        names the blocked channel ("Rewrite budget" for the delete escape,
        "Repair budget" for a targeted edit) so run logs can tell the two
        blocks apart.
        """

        attempts = (
            f"{_MAX_TEST_ASSET_REPAIRS_PER_PATH} repair attempts (targeted edits and "
            "delete-rewrite cycles share one budget)"
        )
        fully_written = self._manifest_fully_written()
        if fully_written is not None:
            declared = ", ".join(fully_written)
            return (
                f"{prefix} blocked: {path} has already used {attempts} in this pass, and every "
                f"declared manifest file is written ({declared}). The current files are final for "
                "this stage: stop editing and return your manifest response now."
            )
        return (
            f"{prefix} blocked: {path} has already used {attempts} in this pass; the version on "
            "disk stands and the content you wrote is in your context. Continue with your "
            "remaining declared files and return the manifest instead of polishing this one."
        )

    def _validate_test_asset_edit(self, path: str) -> str | None:
        """Budget gate of the same-path repair channel for test assets (#240).

        The repeated-write lock would refuse any second touch of a written
        test asset; the repair channel opens a narrow exception for
        ``edit_file``: a targeted anchor fix on the version already on disk
        is the cheap repair the delete-rewrite channel used to force into a
        whole-file rewrite (arc-output-serial-6: one apostrophe fix became a
        6.2KB rewrite that itself introduced new errors). The channel draws
        from the same per-path repair ledger as delete-rewrite
        (``_MAX_TEST_ASSET_REPAIRS_PER_PATH``), so the runaway cap survives
        with a cheaper unit. A failed file operation on the path still
        unlocks it through ``_path_unlocked`` — the budget limits
        self-review churn, not repair after an error.

        Called only for test_generation ``edit_file`` calls on written,
        locked paths (the caller's ``repair_edit`` decision). Written paths
        in test_generation are test assets by construction — the capability
        table denies every other write — so no asset predicate is needed
        here. The budget unit is reserved by the caller at the end of the
        validation ladder, after the manifest and import checks.
        """

        if self._repair_counts.get(path, 0) < _MAX_TEST_ASSET_REPAIRS_PER_PATH:
            return None
        return self._repair_budget_blocked(path, prefix="Repair budget")

    def _reserve_test_asset_edit(self, path: str, call_id: str) -> None:
        """Reserve one repair unit for a validated budgeted edit (#240).

        Reserving at validation time keeps the cap exact for parallel
        tool-call batches (same reasoning as the DESIGN write reservations);
        keying by call id lets ``_record_result`` settle exactly this call's
        unit — consumed on success, released on failure — without touching a
        sibling call's reservation on the same path.
        """

        self._repair_reservations[call_id] = path

    def _manifest_fully_written(self) -> list[str] | None:
        """Declared manifest paths when every one of them is materialized.

        ``None`` when there is no locked manifest, at least one declared
        file was never written, or the stage is not the test generator —
        the caller then falls back to the generic rewrite-budget wording.
        The implementation stage receives a manifest lock as read-only
        import-check metadata (issue #156); its delete-rewrite budget
        wording must stay the generic one, because "return your manifest
        response" is TestGenerator vocabulary a TDD session cannot act on.
        """

        if self._stage != "test_generation":
            return None
        if self._test_manifest_lock is None or not self._test_manifest_lock.locked:
            return None
        written = {normalize_manifest_path(path) for path in self._written_paths}
        if not all(path in written for path in self._test_manifest_lock.declared_files):
            return None
        return sorted(self._test_manifest_lock.declared_files)

    def _validate_read(self, args: dict[str, Any]) -> str | None:
        path = _discipline_path(args)
        if not path:
            return None
        if path in self._written_paths and not self._path_unlocked(path):
            return (
                f"Read blocked: {path} was already written in this stage; you know its content. "
                "Continue with the next action instead of re-reading it, and never rewrite the "
                "file to verify it — the written version stands and a suspected imperfection is "
                "not evidence. Any retry shape (smaller limit, shifted offset) is blocked too."
            )
        offset = _as_nonnegative_int(args.get("offset"), default=0)
        limit = min(_as_nonnegative_int(args.get("limit"), default=100), _MAX_READ_LIMIT)
        previous = self._read_ranges.get(path, [])
        if not previous or self._path_unlocked(path):
            return None
        if any(_ranges_overlap(offset, offset + limit, start, end) for start, end in previous):
            if self._repeated_read_counts.get(path, 0) >= _MAX_REPEATED_READS_PER_PATH:
                return (
                    f"Repeated read blocked: {path} was re-read {_MAX_REPEATED_READS_PER_PATH} time(s) "
                    "in this stage already. Use the earlier results and continue; if the file needs "
                    "changes, follow the write options instead of probing offsets to bypass the cache. "
                    "A narrower limit or shifted offset is the same blocked read."
                )
            return None
        return None

    def _validate_write(self, args: dict[str, Any], *, tool: str = "write_file", call_id: str = "") -> str | None:
        """Runtime gates on the writes the capability table already allowed.

        One deliberate, behavior-preserving check-order change from the
        pre-table middleware: the test_generation asset verdict moved out of
        this validator's interior (it used to sit between the repeated-write
        block and the manifest gate) into the table's pre-flight check in
        ``_validate_tool_call``. The swap cannot change outcomes — only test
        assets can enter ``_written_paths`` in test_generation (product
        writes and non-test-asset deletes were always denied), so an asset
        verdict firing before the repeated-write check never redirects a
        call the repeated-write check would have caught. The remaining order
        here is the runtime-state ladder: repeated-write lock (with the
        test_generation budgeted same-path edit repair channel opened inside
        it, issue #240), manifest declaration, test-import validation,
        DESIGN content/budget, file-claim gate — and the repair unit
        reservation last of all, so a content-rejected repair edit never
        burns budget.
        """

        path = _discipline_path(args)
        if not path:
            return None
        # Whether this repeated write enters the budgeted same-path repair
        # channel (issue #240): a targeted ``edit_file`` fix on a written
        # test asset in test_generation. The decision is taken here but the
        # budget unit is only reserved at the end of the ladder, after the
        # manifest and import checks — a content-rejected repair edit must
        # not burn budget (same invariant as the DESIGN write budget).
        repair_edit = (
            tool == "edit_file"
            and self._stage == "test_generation"
            and path in self._written_paths
            and not self._path_unlocked(path)
        )
        if path in self._written_paths and not self._path_unlocked(path) and not repair_edit:
            exit_text = _WRITE_BLOCK_EXITS[self._stage]
            if self._write_block_counts is not None:
                block_count = self._write_block_counts.get(path, 0) + 1
                self._write_block_counts[path] = block_count
                if block_count == 1:
                    exit_text = _INTERFACE_DESIGN_FIRST_WRITE_BLOCK_EXIT
            return (
                f"Repeated write blocked: {path} was already changed in this stage. "
                f"{exit_text}"
            )
        if repair_edit:
            if budget_block := self._validate_test_asset_edit(path):
                return budget_block
        if self._stage == "test_generation":
            # Whether the path is a test asset at all was already answered by
            # the capability table (pre-flight); the manifest gate below adds
            # the declaration requirement on top of it.
            blocked = self._validate_test_manifest_path(args, operation="write")
            if blocked:
                return blocked
        # The import check sees the would-be final file for an edit, but the
        # existing DESIGN content guard must continue to inspect only the
        # edit payload: a small anchor edit on a large existing test file
        # must not be re-judged against the whole file's content.
        edit_content = str(args.get("content", args.get("new_string", "")) or "")
        content = edit_content
        if (
            tool == "edit_file"
            and self._import_probe_root is not None
            and is_test_file_path(path)
            and is_js_source_path(path)
        ):
            merged = self._merged_edit_content(path, args)
            if merged is not None:
                content = merged
        if import_block := self._validate_test_imports(path, content):
            return import_block
        if self._stage == "interface_design":
            if violation := self._validate_design_content(edit_content):
                return violation
            if budget_block := self._reserve_design_write(path):
                return budget_block
        if self._file_claim_gate is not None:
            # Cross-node ownership of new files (parallel worktrees): claim
            # the path for this node or reject a sibling's claimed path. The
            # claim is recorded only for writes the discipline allows above,
            # so a skeleton-limit rejection never claims a path.
            claim_block = self._file_claim_gate.check_and_claim(path)
            if claim_block:
                return claim_block
        if repair_edit:
            # Reserve the repair unit last: every content/manifest/claim
            # check above has passed, so the edit will run (a later tool
            # error releases the unit in ``_record_result``).
            self._reserve_test_asset_edit(path, call_id)
        return None

    def _validate_design_content(self, content: str) -> str | None:
        """Reject obvious business mutations from the DESIGN skeleton channel.

        Deliberately a pattern heuristic: it catches the plain SQL and
        repository-method shapes observed leaking whole implementations into
        DESIGN skeletons, and does not attempt semantic analysis of ORM
        wrappers, async side effects, or frontend handlers. The authoritative
        false-green gates are elsewhere - the owned-RED baseline witness and
        the IMPLEMENT-stage ownership rules - so this guard only needs to stop
        the obvious case early, not to be exhaustive.

        Whole-line comments are dropped before matching, because a contract
        skeleton legitimately documents row shapes and endpoints with SQL or
        query verbs in comments. Dropping them can only reduce blocking; the
        authoritative gates above still cover anything hidden this way.
        """

        if self._stage != "interface_design":
            return None
        code = _without_comment_lines(content)
        if any(pattern.search(code) for pattern in _DESIGN_MUTATION_PATTERNS):
            return (
                "InterfaceDesigner may only materialize contract skeletons; this write contains "
                "an apparent persistence or business mutation. A skeleton is shape-only: imports, "
                "types/constants, typed signature exports, route tables, and TODO(TDD) markers. "
                "Do not split or append around this rejection - put the complete behavior "
                "description in your stage response for TestDrivenDeveloper."
            )
        return None

    def _validate_test_imports(self, path: str, content: str) -> str | None:
        """Static import validation on manifest-locked test writes (issue #156).

        The 2026-09-22 serial run burned an entire IMPLEMENT retry budget
        fixing two mechanically detectable import defects in a test file:
        a relative import one ``../`` short and an ESM import without its
        ``.js`` extension. Both classes are checked here, at write time,
        for every stage that receives the current node's manifest lock
        (TestGenerator's declaration lock and TDD's pre-seeded view of that
        manifest) — the rejection names the exact correction so the fix is
        one round-trip, not a flip-flop.

        Deliberately fail-open everywhere the check cannot be sure: no
        manifest lock, a path outside that lock, no workspace root,
        non-JS test files (the CLI app type's ``test_*.py``), bare
        specifiers, bundler aliases, and dynamic imports whose argument is
        not a string literal. A static check must never become the new
        dead-loop source, so anything unparseable is skipped and counted
        (:meth:`import_check_fail_opens`).

        Only the *written* content is scanned: existing test files that are
        never re-touched keep whatever imports they already had.
        """

        if (
            self._import_probe_root is None
            or not content
            or not is_test_file_path(path)
            or self._test_manifest_lock is None
            or not self._test_manifest_lock.contains(normalize_manifest_path(path))
        ):
            return None
        if not is_js_source_path(path):
            return None
        try:
            specifiers, opaque = extract_relative_esm_imports(content)
        except Exception:
            # The scanner must never break the write path; anything it
            # cannot chew is a fail-open, not a tool error.
            self._record_import_fail_open(path, 1)
            return None
        if opaque:
            self._record_import_fail_open(path, opaque)
        if not specifiers:
            return None
        importer_dir = posixpath.dirname(normalize_manifest_path(path))
        violations: list[ImportViolation] = []
        for specifier in specifiers:
            try:
                violation = classify_import(specifier, importer_dir, self._import_target_exists)
            except Exception:
                self._record_import_fail_open(path, 1)
                continue
            if violation is not None:
                violations.append(violation)
        if not violations:
            return None
        return build_import_block_message(path, violations)

    def _import_target_exists(self, workspace_relative: str) -> bool:
        """Whether an imported target is real: on disk, or reserved this pass.

        Files written earlier in this session are already on disk, so the
        probe covers them. The reservation set covers the parallel-batch
        case the disk cannot: a stage that reserves a skeleton and a test
        importing it in one tool-call batch validates both before either
        handler has materialized a file. (Reservations exist only in the
        interface_design budget; today's pipeline runs the DESIGN-phase
        test writes through TestGenerator, which materializes each file
        before the next batch — but the reservation check is cheap and
        correct whenever a lock-bearing stage batches writes.)
        """

        for reservation in self._design_write_reservations:
            if normalize_manifest_path(reservation) == workspace_relative:
                return True
        try:
            # ``is_file``, not ``exists``: a directory hit would classify a
            # bare directory import ("../../src/database") as resolved
            # exactly as written instead of pointing at its index file.
            return (self._import_probe_root / workspace_relative).is_file()
        except OSError:
            return False

    def _merged_edit_content(self, path: str, args: dict[str, Any]) -> str | None:
        """The file content an ``edit_file`` would produce, when reconstructable.

        Reads the file on disk and applies the edit's replacement so the
        import scan sees the surviving imports, not just the replaced
        fragment. ``None`` (missing file, unreadable content, replacement
        text absent from disk, or an ambiguous anchor — the real tool
        errors on a duplicate ``old_string`` without ``replace_all``, and
        this reconstruction must not guess which occurrence it meant) falls
        back to scanning the ``new_string`` alone.
        """

        if self._import_probe_root is None:
            return None
        relative = normalize_manifest_path(path)
        if not relative:
            return None
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return None
        replace_all = bool(args.get("replace_all", False))
        try:
            current = (self._import_probe_root / relative).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        occurrences = current.count(old_string)
        if occurrences == 0:
            return None
        if occurrences > 1 and not replace_all:
            return None
        return current.replace(old_string, new_string)

    def _record_import_fail_open(self, path: str, count: int) -> None:
        self._import_fail_open_count += count
        logging.getLogger(__name__).info(
            "test-import check fail-open: %d unparseable import shape(s) in %s passed unvalidated",
            count,
            path,
        )

    def import_check_fail_opens(self) -> int:
        """How many import shapes the validation skipped as unparseable."""

        return self._import_fail_open_count

    def _with_bounded_read(self, request: ToolCallRequest) -> ToolCallRequest:
        if request.tool_call.get("name") != "read_file":
            return request
        args = dict(request.tool_call.get("args", {}) or {})
        if not _discipline_path(args).startswith("/workspace/"):
            return request
        args["offset"] = _as_nonnegative_int(args.get("offset"), default=0)
        args["limit"] = min(_as_nonnegative_int(args.get("limit"), default=100), _MAX_READ_LIMIT)
        return request.override(tool_call={**request.tool_call, "args": args})

    def _track_probe(self, request: ToolCallRequest) -> str:
        """Count a read-only probe and return the stall nudge when due.

        Runs after the discipline's validation, so a blocked call never
        counts (its own block message already steers the model) and a probe
        that later errors in the tool still counts (it burned a round trip
        for nothing - the serial-4 storm opened with Windows-path rejections
        before settling into no-evidence greps). The count is per normalized
        target; a grep without a path searches the whole workspace and shares
        one bucket. Returns the nudge text exactly once per zero-write
        window, at the call that crosses :data:`MAX_PROBES_PER_TARGET`.
        """

        name = str(request.tool_call.get("name", ""))
        if name in _WRITE_FAMILY_TOOLS:
            # The window closes in _record_result, on a write that succeeded.
            return ""
        if name not in _PROBE_TOOLS:
            return ""
        args = request.tool_call.get("args", {}) or {}
        target = normalize_manifest_path(str(args.get("file_path") or args.get("path") or ""))
        count = self._probe_counts.get(target, 0) + 1
        self._probe_counts[target] = count
        if self._probe_nudge_fired or count < _MAX_PROBES_PER_TARGET:
            return ""
        self._probe_nudge_fired = True
        logging.getLogger(__name__).info(
            "probe-stall nudge injected: %d read-only lookups on %s with zero writes",
            count,
            target or "the whole workspace",
        )
        return probe_stall_hint(target=target, target_probes=count)

    @staticmethod
    def _append_probe_stall(result: ToolMessage | Any, nudge: str) -> ToolMessage | Any:
        """Ride the convergence nudge on the probe's own tool result.

        The nudge must be the last thing the model reads before it decides
        what to do next, exactly like the DESIGN pending-contract notice.
        A result that is not a string ToolMessage is returned untouched -
        the annotation is an optimization, never a failure.
        """

        if not nudge or not isinstance(result, ToolMessage) or not isinstance(result.content, str):
            return result
        result.content = f"{result.content}\n[{nudge}]"
        return result

    def _close_probe_window(self) -> None:
        """Close the zero-write window: a write landed, so probing that
        target again starts a fresh count - and a fresh storm is nudged
        again rather than suppressed by an earlier window's nudge."""

        self._probe_counts.clear()
        self._probe_nudge_fired = False

    def _record_result(self, request: ToolCallRequest, result: ToolMessage | Any) -> None:
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args", {}) or {}
        path = _discipline_path(args)
        if name in _VALIDATION_TOOLS:
            if _tool_result_failed(result, tool=name):
                self._validation_failed = True
                self._failed_paths.update(self._written_paths)
            return
        if _tool_result_failed(result, tool=name):
            if path:
                self._failed_paths.add(path)
                # A failed write consumed budget at validation time; release
                # the reservation so a retry after the error is not charged
                # twice for the same path. A path that already materialized
                # successfully keeps its reservation — its failed *retry*
                # must not re-free the slot it already occupies.
                if self._stage == "interface_design" and path not in self._written_paths:
                    self._design_write_reservations.discard(path)
                # The same release for the repair ledger (issue #240): a
                # failed repair edit (e.g. a stale anchor) must not burn a
                # unit; the failure itself unlocks the path anyway.
                if name == "edit_file":
                    self._repair_reservations.pop(str(request.tool_call.get("id", "")), None)
            return
        if name == "delete" and path:
            # The file is gone, so its write lock, read ranges, repeat-read
            # budget and failure record describe content that no longer
            # exists; dropping them is what makes delete-then-rewrite a real
            # exit instead of one that depends on an accidental later failure
            # to unlock. The repair count is the one exception: it exists
            # precisely to observe how often that exit repeats, so it
            # survives the delete.
            was_written = path in self._written_paths
            self._written_paths.discard(path)
            self._failed_paths.discard(path)
            self._read_ranges.pop(path, None)
            self._repeated_read_counts.pop(path, None)
            if self._write_block_counts is not None:
                self._write_block_counts.pop(path, None)
            if was_written:
                self._repair_counts[path] = self._repair_counts.get(path, 0) + 1
            self._write_events.append(path)
            self._discard_written_path(request, path)
            self._close_probe_window()
            return
        if name == "read_file" and path:
            offset = _as_nonnegative_int(args.get("offset"), default=0)
            limit = _as_nonnegative_int(args.get("limit"), default=100)
            previous = self._read_ranges.get(path, [])
            if (
                previous
                and not self._path_unlocked(path)
                and any(_ranges_overlap(offset, offset + limit, start, end) for start, end in previous)
            ):
                # Only a read that actually returned content consumes the
                # fresh re-read budget; failed reads must not.
                self._repeated_read_counts[path] = self._repeated_read_counts.get(path, 0) + 1
            self._read_ranges.setdefault(path, []).append((offset, offset + limit))
            self._cache_read_summary(request, path, offset, limit, result)
        if (name in _FILE_WRITE_TOOLS or name in _ADDITIVE_FILE_WRITE_TOOLS) and path:
            self._written_paths.add(path)
            self._write_events.append(path)
            if self._write_block_counts is not None:
                self._write_block_counts.pop(path, None)
            if name == "edit_file":
                # Settle the repair channel's reservation (issue #240): the
                # successful budgeted edit converts its reserved unit into a
                # consumed one. A reservation is always present here — the
                # validator reserved before the handler ran — but settling
                # defensively keeps the ledger exact even if a future check
                # order change drops that invariant.
                reserved_path = self._repair_reservations.pop(str(request.tool_call.get("id", "")), None)
                if reserved_path is not None:
                    self._repair_counts[reserved_path] = self._repair_counts.get(reserved_path, 0) + 1
            self._cache_written_path(request, path)
            self._close_probe_window()

    def materialized_paths(self) -> list[str]:
        """Paths successfully written by this stage run, sorted.

        This is the discipline's ground truth for "the agent actually
        materialized files" — unlike the model's own ``files_written`` answer,
        it cannot be empty when writes succeeded. Paths deleted afterwards are
        excluded: only the interface_design stage consumes this (``delete`` is
        disabled there), so in practice it never sees deleted paths.
        """

        return sorted(self._written_paths)

    def write_events(self) -> list[str]:
        """Successful write/edit/append/delete paths in call order, repeats kept.

        Where :meth:`materialized_paths` answers "what exists that this
        session wrote", this answers "what did the session do": a re-edit of
        the same file appears once per edit, and a delete appears even though
        it removes the path from the materialized set. Failed attempts are
        absent (only successful operations count as edits).
        """

        return list(self._write_events)

    def _annotate_pending_contract(self, request: ToolCallRequest, result: ToolMessage | Any) -> ToolMessage | Any:
        """Re-state the serialization obligation on a successful design write.

        The empty-``interfaces`` repair exists because the model treats the
        final structured array as redundant labor after the files are
        written. For every contract-embodied write this appends the derived
        pending contract ids to the write's own tool result, so the last
        things the model reads before composing its response are the exact
        records it still owes. The skeleton-guided repair in
        ``InterfaceDesigner`` remains the fallback when the response comes
        back empty anyway.
        """

        if self._stage != "interface_design" or self._pending_contract_registry is None:
            return result
        name = str(request.tool_call.get("name", ""))
        if name not in _FILE_WRITE_TOOLS and name not in _ADDITIVE_FILE_WRITE_TOOLS:
            return result
        if _tool_result_failed(result, tool=name):
            return result
        path = _discipline_path(request.tool_call.get("args", {}) or {})
        if not path or not isinstance(result, ToolMessage) or not isinstance(result.content, str):
            return result
        try:
            new_ids = self._pending_contract_registry.register_materialized_file(path)
        except Exception:
            # The notice is an optimization, not a gate: a derivation problem
            # must never fail the write that produced the file.
            return result
        if not new_ids:
            return result
        listed = ", ".join(new_ids)
        result.content = (
            f"{result.content}\n"
            f"[ARC pending contract: {listed} — your final response's `interfaces` array "
            "must contain one record for each of these interface_id values.]"
        )
        return result

    def _path_unlocked(self, path: str) -> bool:
        return self._validation_failed or path in self._failed_paths

    @staticmethod
    def _cache_read_summary(request: ToolCallRequest, path: str, offset: int, limit: int, result: ToolMessage | Any) -> None:
        if not isinstance(request.state, dict):
            return
        content = str(getattr(result, "content", "") or "")
        line_count = content.count("\n") + (1 if content else 0)
        request.state.setdefault("arc_read_summaries", {})[path] = (
            f"lines {offset}-{offset + limit - 1}; received {line_count} line(s), {len(content)} character(s)"
        )

    @staticmethod
    def _cache_written_path(request: ToolCallRequest, path: str) -> None:
        if isinstance(request.state, dict):
            written = request.state.setdefault("arc_written_paths", [])
            if path not in written:
                written.append(path)

    @staticmethod
    def _discard_written_path(request: ToolCallRequest, path: str) -> None:
        if isinstance(request.state, dict):
            written = request.state.get("arc_written_paths")
            if isinstance(written, list) and path in written:
                written.remove(path)

    @staticmethod
    def _blocked(request: ToolCallRequest, message: str) -> ToolMessage:
        return ToolMessage(
            content=f"{BLOCKED_RESULT_PREFIX} {message}",
            name=str(request.tool_call.get("name", "tool")),
            tool_call_id=str(request.tool_call.get("id", "")),
            status="error",
        )


def _discipline_path(args: dict[str, Any]) -> str:
    raw = str(args.get("file_path", "") or "").replace("\\", "/").strip()
    return raw if raw.startswith("/") else f"/{raw}" if raw else ""


def _as_nonnegative_int(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _ranges_overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


#: Tools whose result text is content the call fetched (file bodies, search
#: matches, directory listings). deepagents renders these tools' own failures
#: as ``ToolMessage(status="error")``, so their text is never a failure
#: signal: "Exit Code: 1" inside a read of a build log says the *file*
#: contains the marker, not that the read failed (arc-output-serial-4 REQ-1
#: recorded exactly that read as a failed round-trip).
_CONTENT_FETCH_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})

#: Tools whose rendered result text is an execution verdict the tool itself
#: computed (process exit-code transcripts). The exit-code scan in
#: ``_tool_result_failed`` applies only to these — for any other tool the
#: same text would be content or receipt, not a verdict.
_EXECUTION_VERDICT_TOOLS = frozenset({"run_build", "run_tests", "install_dependencies"})


def _tool_result_failed(result: ToolMessage | Any, *, tool: str = "") -> bool:
    """Whether a tool result represents a failed operation.

    ``tool`` names the tool that produced the result; callers with a tool
    name must pass it so the scan is narrowed by what the result text means
    (fetched content vs. computed verdict). The empty default keeps the
    conservative scan for callers without a name.
    """

    if isinstance(result, ToolMessage) and result.status == "error":
        return True
    content = str(getattr(result, "content", "") or "")
    if tool in _CONTENT_FETCH_TOOLS:
        return False
    if tool in _EXECUTION_VERDICT_TOOLS and "Exit Code:" in content:
        # One tool result can carry several exit-code segments (run_build
        # renders the frontend and backend builds back to back), so "any
        # Exit Code: 0 present" let a failed half hide behind a passing one.
        # Judge the text by the test-run contract's aggregate rule: a leading
        # aggregate line wins, otherwise every nested segment must be 0.
        # Deferred import: this module sits at the bottom of the bootstrap
        # order and must not pull in the app_type_handler package eagerly.
        from app_type_handler.test_results import extract_overall_exit_code

        return extract_overall_exit_code(content) != 0
    return content.lstrip().startswith("Error:")

"""Tool-level guardrails for ARC's staged agent workflow."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from agents.runtime.capabilities import capability_for, is_test_file_path, normalize_manifest_path
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
_MAX_SKELETON_LINES = 160
MAX_SKELETON_LINES = _MAX_SKELETON_LINES
MAX_APPEND_LINES = 80
MAX_APPENDS_PER_FILE = 3
_MAX_READ_LIMIT = 200
# Fresh overlapping re-reads allowed per path before the block returns. The
# hard block exists for the run7 loop (50 consecutive offset probes), but the
# fbd4a73b TDD run showed the other edge: an agent whose legitimate re-read is
# refused does not stop wanting the content — it burned 10 consecutive greps
# rebuilding one file, at a higher round-trip cost than the re-read itself.
# A small per-path budget serves the legitimate case and still caps the loop.
_MAX_REPEATED_READS_PER_PATH = 2
# Successful delete-then-rewrite cycles allowed per test-file path in one
# test_generation or implementation pass. The delete release exists so a
# legitimate fix does not wait for an accidental failure to unlock; the
# 2026-09-19 arc-output3 run showed its unbounded edge: a TestGenerator that
# *believed* its writes had been truncated (they had not — no truncation error
# ever occurred) re-ran the delete+write cycle 5-7 times per file, ~10M input
# tokens, until the step budget crashed the whole DESIGN task. Counting
# cycles on the delete keeps the last written version on disk when the cap
# trips.
_MAX_DELETE_REWRITES_PER_PATH = 2
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
        "To change it: delete this test asset first and then write it back "
        "(at most two delete-rewrite cycles per path); if the test assets are "
        "ready, stop editing and return the updated manifest."
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
    "Unlock condition: only a failed file operation on this path or a failing "
    "`run_build`/`run_tests` validation unlocks it. `delete` is disabled in "
    "DESIGN; do not invoke validation merely to unlock it. Do not retry this "
    "path; keep the skeleton and put its contract in the final response's "
    "`interfaces` array."
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
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._repeated_read_counts: dict[str, int] = {}
        self._written_paths: set[str] = set()
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
        self._rewrite_counts: dict[str, int] = {}
        self._write_block_counts: dict[str, int] | None = {} if stage == "interface_design" else None

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        blocked = self._validate_tool_call(request)
        if blocked:
            return self._blocked(request, blocked)
        request = self._with_bounded_read(request)
        result = handler(request)
        self._record_result(request, result)
        return self._annotate_pending_contract(request, result)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        blocked = self._validate_tool_call(request)
        if blocked:
            return self._blocked(request, blocked)
        request = self._with_bounded_read(request)
        result = await handler(request)
        self._record_result(request, result)
        return self._annotate_pending_contract(request, result)

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
            return self._validate_write(args)
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
        pre-flight in ``_validate_tool_call``). The filesystem tool checks
        the real file length; this middleware keeps ownership and per-pass
        policy here so appending cannot bypass claims, write-count limits,
        or the observability used by InterfaceDesigner.
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
            return (
                f"append_file accepts at most {MAX_APPEND_LINES} lines per chunk; received {line_count}. "
                "Split the next cohesive skeleton section into another append."
            )
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
        ``_MAX_DELETE_REWRITES_PER_PATH`` full delete-rewrite cycles, the next
        delete is refused and the last written version stands. A failed file
        operation on the path keeps its generic unlock (``_path_unlocked``):
        the budget limits self-review churn, not repair after an error.
        """

        path = _discipline_path(args)
        if not path or self._path_unlocked(path):
            return None
        if self._rewrite_counts.get(path, 0) < _MAX_DELETE_REWRITES_PER_PATH:
            return None
        fully_written = self._manifest_fully_written()
        if fully_written is not None:
            declared = ", ".join(fully_written)
            return (
                f"Rewrite budget blocked: {path} has already gone through "
                f"{_MAX_DELETE_REWRITES_PER_PATH} delete-rewrite cycles in this pass, and every "
                f"declared manifest file is written ({declared}). The current files are final for "
                "this stage: stop editing and return your manifest response now."
            )
        return (
            f"Rewrite budget blocked: {path} has already gone through "
            f"{_MAX_DELETE_REWRITES_PER_PATH} delete-rewrite cycles in this pass; the version on "
            "disk stands and the content you wrote is in your context. Continue with your "
            "remaining declared files and return the manifest instead of polishing this one."
        )

    def _manifest_fully_written(self) -> list[str] | None:
        """Declared manifest paths when every one of them is materialized.

        ``None`` when there is no locked manifest or at least one declared
        file was never written — the caller then falls back to the generic
        rewrite-budget wording.
        """

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

    def _validate_write(self, args: dict[str, Any]) -> str | None:
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
        here is the runtime-state ladder: repeated-write lock, manifest
        declaration, DESIGN content/budget, file-claim gate.
        """

        path = _discipline_path(args)
        if not path:
            return None
        if path in self._written_paths and not self._path_unlocked(path):
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
        if self._stage == "test_generation":
            # Whether the path is a test asset at all was already answered by
            # the capability table (pre-flight); the manifest gate below adds
            # the declaration requirement on top of it.
            blocked = self._validate_test_manifest_path(args, operation="write")
            if blocked:
                return blocked
        if self._stage == "interface_design":
            content = str(args.get("content", args.get("new_string", "")) or "")
            if violation := self._validate_design_content(content):
                return violation
            if content.count("\n") + 1 > _MAX_SKELETON_LINES:
                return (
                    f"InterfaceDesigner may only materialize small skeletons (at most {_MAX_SKELETON_LINES} lines per write). "
                    "Record the complete business contract for TDD instead of implementing it now."
                )
            if budget_block := self._reserve_design_write(path):
                return budget_block
        if self._file_claim_gate is not None:
            # Cross-node ownership of new files (parallel worktrees): claim
            # the path for this node or reject a sibling's claimed path. The
            # claim is recorded only for writes the discipline allows above,
            # so a skeleton-limit rejection never claims a path.
            return self._file_claim_gate.check_and_claim(path)
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
                "an apparent persistence or business mutation. Keep signatures, routes, types, "
                "and explicit TODO/unsupported boundaries in DESIGN, and leave complete behavior "
                "to TestDrivenDeveloper."
            )
        return None

    def _with_bounded_read(self, request: ToolCallRequest) -> ToolCallRequest:
        if request.tool_call.get("name") != "read_file":
            return request
        args = dict(request.tool_call.get("args", {}) or {})
        if not _discipline_path(args).startswith("/workspace/"):
            return request
        args["offset"] = _as_nonnegative_int(args.get("offset"), default=0)
        args["limit"] = min(_as_nonnegative_int(args.get("limit"), default=100), _MAX_READ_LIMIT)
        return request.override(tool_call={**request.tool_call, "args": args})

    def _record_result(self, request: ToolCallRequest, result: ToolMessage | Any) -> None:
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args", {}) or {}
        path = _discipline_path(args)
        if name in _VALIDATION_TOOLS:
            if _tool_result_failed(result):
                self._validation_failed = True
                self._failed_paths.update(self._written_paths)
            return
        if _tool_result_failed(result):
            if path:
                self._failed_paths.add(path)
                # A failed write consumed budget at validation time; release
                # the reservation so a retry after the error is not charged
                # twice for the same path. A path that already materialized
                # successfully keeps its reservation — its failed *retry*
                # must not re-free the slot it already occupies.
                if self._stage == "interface_design" and path not in self._written_paths:
                    self._design_write_reservations.discard(path)
            return
        if name == "delete" and path:
            # The file is gone, so its write lock, read ranges, repeat-read
            # budget and failure record describe content that no longer
            # exists; dropping them is what makes delete-then-rewrite a real
            # exit instead of one that depends on an accidental later failure
            # to unlock. The rewrite-cycle count is the one exception: it
            # exists precisely to observe how often that exit repeats, so it
            # survives the delete.
            was_written = path in self._written_paths
            self._written_paths.discard(path)
            self._failed_paths.discard(path)
            self._read_ranges.pop(path, None)
            self._repeated_read_counts.pop(path, None)
            if self._write_block_counts is not None:
                self._write_block_counts.pop(path, None)
            if was_written:
                self._rewrite_counts[path] = self._rewrite_counts.get(path, 0) + 1
            self._discard_written_path(request, path)
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
            if self._write_block_counts is not None:
                self._write_block_counts.pop(path, None)
            self._cache_written_path(request, path)

    def materialized_paths(self) -> list[str]:
        """Paths successfully written by this stage run, sorted.

        This is the discipline's ground truth for "the agent actually
        materialized files" — unlike the model's own ``files_written`` answer,
        it cannot be empty when writes succeeded. Paths deleted afterwards are
        excluded: only the interface_design stage consumes this (``delete`` is
        disabled there), so in practice it never sees deleted paths.
        """

        return sorted(self._written_paths)

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
        if _tool_result_failed(result):
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


def _tool_result_failed(result: ToolMessage | Any) -> bool:
    if isinstance(result, ToolMessage) and result.status == "error":
        return True
    content = str(getattr(result, "content", "") or "")
    return "Exit Code: 0" not in content and ("Exit Code:" in content or content.lstrip().startswith("Error:"))

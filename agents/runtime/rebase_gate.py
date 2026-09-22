"""Demand-pull mid-phase replay at file-tool boundaries (issue #127 / ADR 0003).

When a sibling merge lands while a task is executing, the workflow attaches
the merge's changed-file set to the task's worktree as a ``PendingMerge``.
Nothing happens until the task's agent actually touches one of those paths;
at that tool-call quiescent point this gate runs the mechanical replay
(``NodeWorktreeManager.replay_pending_merges``: WIP commit + rebase onto the
new integration HEAD) *before* the call is served, so the agent reads and
edits the fresh tree instead of a stale one. A replay that lands with
conflict markers hands the conflicted paths back to the resolving agent in
the tool result; the agent resolves them with its ordinary file tools and
the next boundary completes the rebase.

The whole feature is gated by ``ARC_REBASE_ON_MERGE`` (default off) and is
fail-open at every mechanical step: a failed replay aborts, restores the
pre-replay state, and the call proceeds against the old tree - the overlap
stays owned by the existing merge rails (additive resolution / arbitration /
conflict requeue). No new terminal task state, no agent-facing git: the
agent only ever sees file tools.

Soft guard: after three consecutive conflict-carrying replays in one stage
pass, mid-phase replay is disabled for the rest of that pass (the stage
falls back to the merge rails), so a pathological overlap cannot burn the
pass in a resolve-conflict loop.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from core.worktree import (
    PendingMerge,
    ReplayOutcome,
    WorktreeHandle,
    normalize_repo_path,
    touches_pending_file,
)

if TYPE_CHECKING:
    from arcbench_agent_runtime.events import EventClient


# Env gate: default off; the replay only runs with an explicit truthy value.
REBASE_ON_MERGE_ENV = "ARC_REBASE_ON_MERGE"

# File tools whose paths participate in the touch check. grep/glob do not:
# ADR 0003 pins the trigger to tool calls with a single well-defined path
# (scans over stale content are explicitly out of scope).
_FILE_PATH_TOOLS = frozenset({"read_file", "edit_file", "write_file", "append_file", "delete"})

# After this many consecutive conflict-carrying replays in one pass, the
# mid-phase replay stands down for the rest of the pass (soft guard).
_MAX_CONFLICT_REPLAYS_PER_PASS = 3


def rebase_on_merge_enabled() -> bool:
    """Whether the mid-phase replay is enabled for this process.

    Like ``ARC_MERGE_ARBITRATION`` the value is read per call but expected
    to be stable within one run; the gate is read here so the middleware
    stack stays unconditionally cheap to construct when the feature is off.
    """

    return os.environ.get(REBASE_ON_MERGE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def cached_rebase_gate(adapter: Any) -> Any | None:
    """Build (or reuse) an adapter's mid-phase replay gate (issue #127).

    The gate is cached on the adapter so every agent build of one pass
    (including the repair/re-ask rebuilds) shares the pass's soft-guard
    state. The cache is never cleared within an adapter: adapters are
    per-task instances, so its lifetime is exactly one task's stage.
    """

    cached = getattr(adapter, "_current_rebase_gate", None)
    provider = getattr(adapter, "_rebase_gate_provider", None)
    if cached is None and provider is not None:
        cached = provider()
        adapter._current_rebase_gate = cached
    return cached


class RebaseOnMergeMiddleware(AgentMiddleware):
    """Replay pending sibling merges at the touching tool call."""

    def __init__(
        self,
        *,
        handle: WorktreeHandle,
        replay: Callable[[WorktreeHandle], ReplayOutcome],
        pending_files: Callable[[], list[PendingMerge]] | None = None,
        is_mid_rebase: Callable[[WorktreeHandle], bool] | None = None,
        continue_replay: Callable[[WorktreeHandle], ReplayOutcome] | None = None,
        on_replay: Callable[[ReplayOutcome], None] | None = None,
        on_replay_started: Callable[[], None] | None = None,
        abort_replay: Callable[[WorktreeHandle], None] | None = None,
        conflict_contract_cards: Callable[[list[str]], dict[str, Any]] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._handle = handle
        self._replay = replay
        self._pending_files = pending_files
        self._is_mid_rebase = is_mid_rebase
        self._continue_replay = continue_replay
        self._on_replay = on_replay
        # Optional pruned contract cards for the conflict notice (issue #127
        # item 4): the workflow injects the sibling side's cards through the
        # same collect_contract_cards pruner the merge arbiter uses, so the
        # resolving agent sees the other side's declared interfaces next to
        # the conflict markers.
        self._conflict_contract_cards = conflict_contract_cards
        # Audit hook fired when a replay is about to run (the ``started``
        # lifecycle event, issue #127 item 6).
        self._on_replay_started = on_replay_started
        # Guard-disarm hook: aborts a rebase left mid-replay when the soft
        # guard stands the pass down (the phase-end integrate must never
        # meet an unmerged index).
        self._abort_replay = abort_replay
        self._enabled = rebase_on_merge_enabled() if enabled is None else enabled
        # Soft-guard state: consecutive conflict-carrying replays this pass,
        # and whether the guard has tripped (mid-phase replay stands down).
        self._conflict_replays = 0
        self._disarmed = False
        # Optional claim gate whose tracked-set snapshot describes the
        # pre-replay tree; a successful replay invalidates it (see
        # ``attach_claim_gate``).
        self._claim_gate: Any | None = None

    def attach_claim_gate(self, claim_gate: Any) -> None:
        """Register the file-claim gate for post-replay snapshot invalidation.

        The claim gate snapshots ``git ls-files`` once per agent on the
        assumption the tracked set never changes while the agent runs - true
        until the mid-phase replay lands a sibling's tracked files. After a
        replayed (or conflict-completed) outcome the snapshot is dropped so
        the next claim check reloads it from the fresh tree.
        """

        self._claim_gate = claim_gate

    # -- middleware contract ------------------------------------------------------

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        notice = self._before_call(request)
        result = handler(request)
        return self._annotate(request, result, notice)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        notice = self._before_call(request)
        result = await handler(request)
        return self._annotate(request, result, notice)

    # -- replay orchestration -----------------------------------------------------

    def _before_call(self, request: ToolCallRequest) -> ReplayOutcome | None:
        """Run the replay before the call is served; returns the outcome.

        ``None`` means no replay ran (feature off, guard tripped, no touch,
        or a non-file tool) and the call proceeds untouched.
        """

        if not self._enabled or self._disarmed:
            return None
        name = str(request.tool_call.get("name", ""))
        if name not in _FILE_PATH_TOOLS:
            return None
        args = request.tool_call.get("args", {}) or {}
        rel_path = normalize_repo_path(args.get("file_path", ""))
        if not rel_path:
            return None
        # A worktree sitting mid-rebase from a previous conflicted replay:
        # advance the replay first - completing it (all markers resolved)
        # or reporting the next conflict round - regardless of which path
        # this call touches, so the rebase cannot dangle behind unrelated
        # calls. The replay consumes its own pending set here.
        if self._is_mid_rebase is not None and self._is_mid_rebase(self._handle):
            self._notify_started()
            outcome = self._continue_replay(self._handle) if self._continue_replay else None
            if outcome is not None and outcome.status != ReplayOutcome.SKIPPED:
                self._observe(outcome)
                if outcome.status == ReplayOutcome.REPLAYED:
                    # Report the completion on this call's result (the
                    # completed replay has no pending set of its own left).
                    return outcome
                if outcome.status == ReplayOutcome.CONFLICTS:
                    return outcome
                # ABORTED: fail-open, serve the call silently.
        pending = self._pending_files() if self._pending_files else []
        if not pending or not touches_pending_file(pending, rel_path):
            return None
        self._notify_started()
        try:
            outcome = self._replay(self._handle)
        except Exception:  # noqa: BLE001 - fail-open is the contract
            return None
        self._observe(outcome)
        if outcome.status == ReplayOutcome.SKIPPED:
            return None
        return outcome

    def _notify_started(self) -> None:
        """Fire the started audit hook (best effort)."""

        if self._on_replay_started is None:
            return
        try:
            self._on_replay_started()
        except Exception:  # noqa: BLE001 - audit must not break the tool call
            pass

    def _observe(self, outcome: ReplayOutcome) -> None:
        """Track the soft guard, refresh the claim snapshot, and audit.

        The guard counts only *attempted* conflict rounds (git state moved);
        the passive "markers still unresolved" observation a boundary call
        makes while the agent works elsewhere informs the notice but never
        disarms the pass - no replay, no count.
        """

        if outcome.status == ReplayOutcome.CONFLICTS and outcome.attempted:
            self._conflict_replays += 1
            if self._conflict_replays >= _MAX_CONFLICT_REPLAYS_PER_PASS:
                self._disarmed = True
                if self._abort_replay is not None and self._is_mid_rebase is not None:
                    if self._is_mid_rebase(self._handle):
                        # Standing down with a rebase mid-replay would leave
                        # the worktree in an unmerged state the phase-end
                        # integrate cannot survive: abort it now (restoring
                        # the pre-replay WIP state) so the merge rails own
                        # the overlap (issue #127 item 5).
                        try:
                            self._abort_replay(self._handle)
                        except Exception:  # noqa: BLE001 - fail-open is the contract
                            pass
        elif outcome.status == ReplayOutcome.REPLAYED and outcome.origin == "replay":
            # Only a conflict-free fresh replay breaks the consecutive run;
            # a continue's completion resolves a conflict the same replay
            # already carried, so the streak survives it (the guard counts
            # conflict-carrying episodes, not rounds).
            self._conflict_replays = 0
            if self._claim_gate is not None:
                try:
                    self._claim_gate.invalidate_tracked_snapshot()
                except Exception:  # noqa: BLE001 - claim refresh is best effort
                    pass
        elif outcome.status == ReplayOutcome.REPLAYED:
            # A continue-completed replay: the claim snapshot still moved.
            if self._claim_gate is not None:
                try:
                    self._claim_gate.invalidate_tracked_snapshot()
                except Exception:  # noqa: BLE001 - claim refresh is best effort
                    pass
        if self._on_replay is not None:
            try:
                self._on_replay(outcome)
            except Exception:  # noqa: BLE001 - audit must not break the tool call
                pass

    # -- result annotation ----------------------------------------------------------

    def _annotate(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Any,
        outcome: ReplayOutcome | None,
    ) -> ToolMessage | Any:
        """Attach the replay notice to the served call's tool result.

        Mirrors ``_annotate_pending_contract``'s injection shape: the notice
        rides the tool result the model reads next, so no extra round-trip
        is spent telling the agent its tree moved under it.
        """

        if outcome is None or not isinstance(result, ToolMessage):
            return result
        if not isinstance(result.content, str):
            return result
        notice = self._notice_text(outcome)
        if not notice:
            return result
        result.content = f"{result.content}\n{notice}"
        return result

    def _notice_text(self, outcome: ReplayOutcome) -> str:
        """The tool-result notice for one replay outcome (empty when silent)."""

        if outcome.status == ReplayOutcome.REPLAYED:
            if not outcome.files:
                return ""
            listed = ", ".join(outcome.files[:8])
            return (
                f"[ARC rebase-on-merge: a parallel sibling's merged changes to {listed} "
                "were applied to this workspace before this call. Re-read any file "
                "whose content you rely on; the call above was served against the "
                "updated tree.]"
            )
        if outcome.status == ReplayOutcome.CONFLICTS:
            listed = ", ".join(outcome.files[:8])
            notice = (
                f"[ARC rebase-on-merge: replaying the sibling merge left merge conflicts in: {listed}. "
                "Resolve them with edit_file/write_file (keep both sides' behavior where "
                "they are compatible, prefer your node's contract for what your "
                "requirement owns); other file work may continue while conflicts remain.]"
            )
            cards = self._conflict_cards(outcome.files)
            if cards:
                # The pruned opposite-side interface cards, in the same
                # payload shape the merge arbiter's input uses (node id ->
                # interfaces/node_contract rows), so the resolver knows what
                # the sibling's edits were contractually obligated to keep.
                import json

                notice += (
                    "\n[ARC rebase-on-merge: the other side's registered interface "
                    f"contracts: {json.dumps(cards, ensure_ascii=False, default=str)[:4000]}]"
                )
            return notice
        if outcome.status == ReplayOutcome.ABORTED:
            # Fail-open is silent by design: the call was served against the old
            # tree and the merge rails own the overlap; telling the agent would
            # only invite it to invent a git workflow it must not have.
            return ""
        return ""

    def _conflict_cards(self, conflict_paths: list[str]) -> dict[str, Any]:
        """Pruned opposite-side contract cards for the conflict notice."""

        if self._conflict_contract_cards is None or not conflict_paths:
            return {}
        try:
            cards = self._conflict_contract_cards(conflict_paths)
        except Exception:  # noqa: BLE001 - the notice is advisory, never a gate
            return {}
        return cards if isinstance(cards, dict) else {}

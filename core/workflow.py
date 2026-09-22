from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from agents.interface_designer import InterfaceDesigner
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from app_type_handler import create_app_type_handler, normalize_app_type
from agents.context.pipeline import context_pipeline
from core import sessions
from core.file_claims import get_file_claim_registry
from core.phases import WorkflowPhaseRunner
from core.queue_state import (
    NODE_BLOCKED_BY_DEPENDENCY,
    NODE_CONVERGED,
    NODE_CONVERGED_WITH_FAILED_CHILDREN,
    NODE_DESIGNED,
    NODE_DESIGNING,
    NODE_FAILED,
    NODE_IMPLEMENTING,
    NODE_PASSED,
    NODE_UNSEEN,
    PHASE_DESIGN,
    PHASE_IMPLEMENT,
    QUEUE_FILENAME,
    TASK_BLOCKED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    ResetPlan,
    apply_retry_plan,
    begin_task,
    break_dependency_cycles,
    build_affinity_map,
    build_dependencies_map,
    build_descendants_map,
    build_parents_map,
    complete_task,
    drop_ancestor_dependency_edges,
    drop_unschedulable_dependencies,
    fail_task,
    has_phase_tasks,
    design_status_of,
    implement_status_of,
    load_or_create_queue,
    node_state,
    propagate_dependency_blocks,
    recover_interrupted,
    release_dependency_blocks,
    reset_node_for_retry,
    save_queue,
    structural_precedence_edges,
    task_status,
)
from core.service import configure_runtime
from core.commits import build_commit_message
from core.config import load_project_env, set_app_type, set_web_port, set_workspace_root
from core.files import load_requirements, validate_requirement_tree
from core.logging import append_debug_log, write_terminal_log
from core.contract_drift import ContractDrift, detect_contract_drift
from core.merge_arbitration import (
    ArbitrationInput,
    MergeArbiter,
    TRIGGER_CONFLICT,
    TRIGGER_HEALTH_GATE,
    arbitration_enabled,
    collect_contract_cards,
    merge_arbitration_budget_key,
    read_workspace_file,
)
from core.path_safety import validate_clean_target
from core.tdd_retry import build_tdd_reprompt, scan_test_failures
from core.visual_analysis import precompute_visual_references, visual_precompute_enabled
from core.worktree import (
    ArbitrationHooks,
    MergeArbitrationError,
    MergeConflictError,
    NodeWorktreeManager,
    WorktreeError,
    WorktreeHandle,
    WorktreeOutcome,
    WorktreeTaskResult,
)


load_project_env()

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

QUEUE_FILENAME = "processing_queue.json"

# Per-node worktree parallelism is the default: each in-flight task gets its
# own git worktree, web port slot and worktree-local E2E database, so up to
# ARC_MAX_CONCURRENT_TASKS (default PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS,
# capped at MAX_PARALLEL_TASKS) tasks may run at once. Setting
# ARC_NODE_WORKTREES=0 restores the historical mode: every task runs against
# the one shared workspace in strict queue order, because stage agents, git
# checkpoints (`git add .`) and test runners (one web port, one E2E database)
# would otherwise interfere with each other. Tasks are scheduled with subtree
# affinity: consecutive tasks of one subtree reuse one worktree directory and
# run sequentially inside it, so siblings never race on shared files;
# different subtrees drain in parallel and a freed slot steals work from
# another free group. ARC_AFFINITY_DEPTH (default 1) sets the depth at which
# the grouping splits: a wide top-level subtree's child subtrees each become
# their own group, trading worktree sharing for parallelism - the sibling
# skeletons then meet only through the merge rails (file claims, additive
# resolution, health gate). Cross-subtree conflicts that survive (shared glue
# files) are resolved mechanically when every side only appended lines,
# guarded by a backend health check before the merge commit; anything else
# fails the node with an explicit reason and preserves its worktree for
# inspection. Declared requirement dependencies gate both phases: a node's
# DESIGN and IMPLEMENT wait for the IMPLEMENT of every node its requirement
# declares as a dependency. An IMPLEMENT completes only after its work is
# merged, so the dependent DESIGN runs against the dependency's real surfaces
# (routes, session helpers) and reuses them instead of designing a duplicate,
# and the dependent IMPLEMENT finds the runtime state (accounts, routes,
# orders) its scenarios read. Edges that cannot participate in scheduling -
# between an ancestor and a descendant (the parent-child rules already
# sequence those pairs; such an edge could only deadlock the drain) or
# closing a cycle - are dropped and reported. The affinity picker weighs a
# group by the pending work that depends on it so a small hub subtree is not
# starved behind larger independent subtrees.
DEFAULT_MAX_CONCURRENT_TASKS = 1
PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS = 3
MAX_PARALLEL_TASKS = 8


def _worktrees_enabled() -> bool:
    raw = os.environ.get("ARC_NODE_WORKTREES", "").strip().lower()
    # Parallel mode is the default; only an explicit falsy value disables it.
    return raw not in {"0", "false", "no", "off"}


def _affinity_depth() -> int:
    """Subtree depth at which the affinity grouping splits.

    Depth 1 (the default) keeps the historical top-level-subtree groups.
    Deeper values split a wide top-level subtree into one group per
    descendant subtree at that depth, so sibling feature subtrees under a
    common parent can drain in parallel. Values below 1 and unparsable
    input degrade to 1: an affinity map must always exist, and the fallback
    is the behaviour every saved queue was built under.

    Unlike ARC_MAX_CONCURRENT_TASKS there is deliberately no upper clamp:
    a depth past the tree's height is the sanctioned every-subtree-its-own-
    group mode (no worktree sharing), monotonic and literal.
    """
    raw = os.environ.get("ARC_AFFINITY_DEPTH", "").strip()
    try:
        depth = int(raw)
    except ValueError:
        return 1
    return max(depth, 1)


DESIGN_GATE_PIPELINE_ENV = "ARC_DESIGN_GATE_PIPELINE"


def _design_pipelining_enabled() -> bool:
    """Whether a dependent's DESIGN waits only for its dependencies' DESIGNs.

    Default off: the dependent's DESIGN waits for every declared dependency's
    IMPLEMENT (the run8 serial semantics PR #38 installed, which eliminates
    run7's parallel duplicate implementations). With the gate open the wait
    relaxes to the dependency's DESIGN completing and merging - its
    registered interface cards, which carry the ``implemented`` flag, are
    then readable and the dependent designs against them incrementally. The
    semantic conflicts this re-exposes are owned by the merge rails
    (additive resolution + health gate + arbitration, #81) and by the
    contract drift check this ticket adds; the dependent's IMPLEMENT still
    waits for the dependency's IMPLEMENT (its scenarios read runtime state
    only the landed implementation creates).
    """

    return os.environ.get(DESIGN_GATE_PIPELINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _rebase_on_merge_enabled() -> bool:
    """Whether landed sibling merges replay onto in-flight tasks on demand.

    Default off (ADR 0003): with the gate closed a sibling merge never
    touches an executing task - the overlap surfaces at the task's own
    integrate through the merge rails, exactly like main. With the gate
    open, the merge attaches its changed-file set to each in-flight task
    and the task's file tools replay it at the boundary where they first
    touch one of those paths. The env read itself lives with the middleware
    (``agents.runtime.rebase_gate.rebase_on_merge_enabled``) so there is one
    definition of the flag's truthy set.
    """

    from agents.runtime.rebase_gate import rebase_on_merge_enabled

    return rebase_on_merge_enabled()


@dataclass
class _TaskWorkspace:
    """An in-flight task's isolated resources (worktree mode only)."""

    node_id: str
    handle: WorktreeHandle
    slot: int
    web_port: int | None
    phase_runner: WorkflowPhaseRunner


class ARCWorkflowManager:
    """Manage the ARC requirement-tree compilation queue."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str = "",
        app_type: str = "web",
        web_port: int = 3301,
        log_cb: LogCallback | None = None,
    ) -> None:
        self.workspace_path = str(Path(workspace_path).expanduser().resolve())
        self.requirement_path = str(Path(requirement_path).expanduser().resolve()) if requirement_path else ""
        self.app_type = normalize_app_type(app_type)
        self.web_port = int(web_port)
        set_workspace_root(self.workspace_path)
        self.log_cb = log_cb or _default_log_cb

        self.arc_dir = os.path.join(self.workspace_path, ".arc")
        self.queue_path = os.path.join(self.arc_dir, QUEUE_FILENAME)
        self.runtime = None

        # Per-node worktree parallelism (default on; ARC_NODE_WORKTREES=0
        # restores the shared-workspace serial mode).
        self._parallel_mode = _worktrees_enabled()
        # Captured once per process like ARC_NODE_WORKTREES: one CLI run, one
        # grouping; a mid-run flip would put queued tasks in two groups' files.
        self._affinity_depth = _affinity_depth()
        self._worktree_manager = (
            NodeWorktreeManager(self.workspace_path) if self._parallel_mode else None
        )
        # Nothing is in flight when a compile starts (fresh or resumed), so
        # any claims left by a previous process are stale by definition:
        # landed files are tracked in git and un-landed work re-runs.
        get_file_claim_registry(self.workspace_path).reset()
        self._merge_lock = asyncio.Lock()
        self._port_slots: dict[int, str] = {}
        self._port_slot_count = 1
        # In-flight task workspaces by node id (parallel mode). The merge
        # path consults this to attach pending merges for the demand-pull
        # replay (issue #127); entries live for the task's duration only.
        self._inflight: dict[str, _TaskWorkspace] = {}
        # Demand-pull mid-phase replay gate (issue #127, ARC_REBASE_ON_MERGE,
        # default off): off keeps the merge rails byte-for-byte identical.
        self._rebase_on_merge = _rebase_on_merge_enabled()

        set_web_port(self.web_port)
        self.interface_designer = InterfaceDesigner(
            self.log_cb,
            workspace_root=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
        )
        self.test_generator = TestGenerator(
            self.log_cb,
            workspace_root=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
        )
        self.test_driven_developer = TestDrivenDeveloper(
            self.log_cb,
            workspace_root=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
        )
        self.phase_runner = WorkflowPhaseRunner(
            workspace_path=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=self.interface_designer,
            test_generator=self.test_generator,
            test_driven_developer=self.test_driven_developer,
            log_cb=self.log_cb,
        )

    async def cleanup_workspace(self) -> bool:
        await self._log("Compiler", "Clear-and-recompile requested. Cleaning workspace...")
        try:
            # The overlap guard below refuses layouts where the requirement
            # directory lives inside the workspace, even though the deletion
            # loop preserves a `requirements` entry by name. That is
            # deliberate: the gate also protects requirement assets stored
            # under any other name inside the workspace. The CLI's --clean
            # performs its own validate_clean_target check against the
            # caller-supplied requirement directory before rmtree, and never
            # routes through here (clear_all is only set by direct API use).
            clean_error = validate_clean_target(
                self.workspace_path,
                str(Path(self.requirement_path).parent),
                repo_root=Path(__file__).resolve().parent.parent,
            )
            if clean_error:
                await self._log("Compiler", f"Refusing to clean workspace: {clean_error}", "error")
                return False
            Path(self.workspace_path).mkdir(parents=True, exist_ok=True)
            for item in os.listdir(self.workspace_path):
                if item == "requirements":
                    continue
                item_path = os.path.join(self.workspace_path, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    os.remove(item_path)
            self._remove_worktree_root()
            return True
        except Exception as exc:
            await self._log("Compiler", f"Failed to clean workspace: {exc}", "error")
            return False

    async def load_requirement_tree(self) -> dict[str, Any] | None:
        await self._log("RequirementLoader", f"Reading requirements file: {self.requirement_path}")
        try:
            return load_requirements(self.requirement_path)
        except Exception as exc:
            await self._log("RequirementLoader", f"Error while reading requirements file: {exc}", "error")
            return None

    async def initialize_project(self) -> bool:
        await self._log("System", f"Initializing project environment in {self.workspace_path}...")
        Path(self.arc_dir).mkdir(parents=True, exist_ok=True)
        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)
        set_web_port(self.web_port)

        traceability_dir = os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or os.path.join(
            self.arc_dir,
            "traceability",
        )
        self.runtime = configure_runtime(
            project_dir=self.workspace_path,
            traceability_dir=traceability_dir,
            app_type=self.app_type,
            web_port=self.web_port,
        )
        self.runtime.traceability.init_store(reset=False)

        app_handler = create_app_type_handler(
            workspace_path=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=self.interface_designer,
            log_cb=self.log_cb,
        )
        init_ok = await app_handler.initialize_workspace()
        if not init_ok:
            return False

        await self._log("System", "Initializing Git repository...")
        self.runtime.git.ensure_repo(create_initial_commit=True)
        self._prune_worktrees()
        return True

    async def prepare_resume_context(self) -> None:
        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)
        set_web_port(self.web_port)
        traceability_dir = os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or os.path.join(
            self.arc_dir,
            "traceability",
        )
        self.runtime = configure_runtime(
            project_dir=self.workspace_path,
            traceability_dir=traceability_dir,
            app_type=self.app_type,
            web_port=self.web_port,
        )
        self.runtime.traceability.init_store(reset=False)
        self.runtime.events.mark_run_resumed("ARC compilation resumed from processing queue.")
        self._prune_worktrees()

    async def start_compilation(
        self,
        *,
        clear_all: bool = False,
        resume_from_queue: bool = False,
        retry_failed: bool = False,
        retry_node_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        await self._log("Compiler", "ARC compilation started.")
        if clear_all:
            cleaned = await self.cleanup_workspace()
            if not cleaned:
                return {"ok": False, "failed_nodes": []}

        requirement_tree = await self.load_requirement_tree()
        if not requirement_tree:
            return {"ok": False, "failed_nodes": []}

        if resume_from_queue:
            await self._log("Compiler", f"Resuming from existing queue: {self.queue_path}")
            await self.prepare_resume_context()
        else:
            init_ok = await self.initialize_project()
            if not init_ok:
                await self._log("Compiler", "Project initialization failed.", "error")
                return {"ok": False, "failed_nodes": []}
            self.runtime.events.mark_run_started("ARC compilation run started.")

        result = await self.compile_requirement_tree(
            requirement_tree,
            resume_from_queue=resume_from_queue,
            retry_failed=retry_failed,
            retry_node_ids=retry_node_ids,
        )
        if result.get("ok"):
            self.runtime.events.mark_run_completed("ARC compilation completed.")
            await self._log("Compiler", "Compilation finished successfully.")
        else:
            self.runtime.events.mark_run_failed("ARC compilation finished with failures.")
            failed_nodes = result.get("failed_nodes", [])
            blocked_nodes = result.get("blocked_nodes", [])
            unvalidated_tasks = result.get("unvalidated_tasks", [])
            details = [f"failed: {', '.join(failed_nodes)}"] if failed_nodes else []
            if blocked_nodes:
                details.append(f"blocked: {', '.join(blocked_nodes)}")
            if unvalidated_tasks:
                details.append(f"unvalidated tasks: {', '.join(unvalidated_tasks)}")
            await self._log(
                "Compiler",
                "Compilation finished without an accepted result (" + "; ".join(details) + ")",
                "error",
            )
        return result

    async def compile_requirement_tree(
        self,
        requirement_tree: dict[str, Any],
        *,
        resume_from_queue: bool = False,
        retry_failed: bool = False,
        retry_node_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        root_id = str(requirement_tree.get("id") or "").strip()
        if not root_id:
            await self._log("Compiler", "Requirement root node id is missing.", "error")
            return {"ok": False, "failed_nodes": []}
        try:
            validate_requirement_tree(requirement_tree)
        except ValueError as exc:
            await self._log("Compiler", f"Invalid requirement tree: {exc}", "error")
            return {"ok": False, "failed_nodes": []}

        self.runtime.traceability.store_requirement_tree(requirement_tree)
        retry_requested = retry_failed or bool(retry_node_ids)
        try:
            queue_state = self._load_or_create_processing_queue(
                requirement_tree,
                require_compatible_existing_queue=resume_from_queue or retry_requested,
            )
        except ValueError as exc:
            await self._log("Compiler", str(exc), "error")
            return {"ok": False, "failed_nodes": []}
        self._sync_queue_node_states(queue_state)
        recovered_tasks = self._recover_interrupted_queue(queue_state)
        retry_plan = self._apply_retry_plan(
            queue_state,
            retry_failed=retry_failed,
            retry_node_ids=retry_node_ids,
        )
        if retry_plan:
            # A reset un-fails the retried node; dependents that propagation
            # blocked through it must return to schedulable state or the run
            # ends "blocked" even after every retry succeeds.
            await self._release_dependency_blocks(queue_state)
        self._save_processing_queue(queue_state)
        for recovered in recovered_tasks:
            await self._log(
                "Compiler",
                (
                    f"Recovered interrupted {recovered['phase']} task for node {recovered['node_id']}; "
                    "preserving existing workspace and traceability artifacts for the resumed agent."
                ),
                status="warning",
                node_id=recovered["node_id"],
            )
        for node_id in retry_plan:
            await self._log(
                "Compiler",
                f"Queued node {node_id} for retry using the existing workspace and traceability artifacts.",
                status="warning",
                node_id=node_id,
            )
        for dependent_id, dependency_id, reason in queue_state.get("dropped_dependency_edges") or []:
            if reason == "cycle":
                detail = "it would close a dependency cycle (with the parent-child scheduling rules)"
            elif reason == "ancestor-descendant":
                detail = "it links an ancestor with its own descendant, whom the parent-child rules already sequence"
            elif reason == "no-implement-task":
                detail = "this queue has no IMPLEMENT task for it"
            else:
                detail = "its edge list is malformed"
            await self._log(
                "Compiler",
                (
                    f"Declared dependency {dependent_id} -> {dependency_id or '(unknown)'} is ignored for "
                    f"scheduling: {detail}. The dependent proceeds without waiting for it."
                ),
                status="warning",
                node_id=dependent_id,
            )
        await self._log(
            "Compiler",
            f"Loaded processing queue with {len(queue_state['tasks'])} task(s) for root node {root_id}.",
        )

        await self._precompute_visual_references(requirement_tree)

        await self._drain_runnable_tasks(queue_state)

        # Post-run auto TDD re-prompt: after a full pass over the queue, scan the
        # runner events the agents emitted for `test/failed` requirement states and
        # re-prompt each unique still-failing node once with a TDD-first follow-up,
        # then drain the queue a second time. This mirrors the reference agent's
        # post-run re-prompt, adapted to ARC's per-node implement-phase retry. It
        # runs at most once per compilation; disable with ARC_AUTO_TDD_RETRY=0.
        if self._auto_tdd_retry_enabled():
            retry_node_ids = await self._prepare_auto_tdd_retry(queue_state)
            if retry_node_ids:
                await self._drain_runnable_tasks(queue_state)

        return self._build_compile_result(queue_state)

    async def _precompute_visual_references(self, requirement_tree: dict[str, Any]) -> None:
        """Analyze all reference images concurrently before the queue drains.

        Each image-bearing node otherwise blocks its DESIGN phase on a serial
        vision call; the persisted analysis makes this pass a cache hit for
        every later design phase. On ``--resume`` persisted analysis is reused
        directly, and images lacking it are re-attached from the per-image
        cache without another vision API call.
        """

        if not visual_precompute_enabled() or self.runtime is None:
            return
        nodes: list[tuple[str, dict[str, Any]]] = []

        def walk(node: dict[str, Any]) -> None:
            node_id = str(node.get("id") or "").strip()
            if node_id:
                nodes.append((node_id, self.runtime.traceability.get_requirement(node_id) or node))
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    walk(child)

        walk(requirement_tree)
        if not nodes:
            return
        requirements_dir = str(Path(self.requirement_path).expanduser().resolve().parent)
        count = await precompute_visual_references(
            workspace_path=self.workspace_path,
            requirements_dir=requirements_dir,
            requirement_nodes=nodes,
            log_cb=self._log,
        )
        if count:
            await self._log(
                "Compiler",
                f"Visual precompute finished; {count} node(s) analyzed before the node loop.",
            )

    async def _drain_runnable_tasks(self, queue_state: dict[str, Any]) -> None:
        """Run every PENDING task until none is left.

        Without per-node worktrees the drain is the historical strictly-serial
        loop: the stage agents, git checkpoints and test runners all share one
        workspace and one web port. With worktrees enabled, up to
        ARC_MAX_CONCURRENT_TASKS tasks run at once, each against its own
        worktree, port slot and E2E database. Ordering is still honoured: a
        node's DESIGN precedes its IMPLEMENT, a node's DESIGN waits for its
        parent's DESIGN (children design against the parent's merged shell)
        and for the IMPLEMENT of every declared dependency (an IMPLEMENT
        completes only after merging, so a dependent designs against the
        dependency's real surfaces instead of duplicating them), and an
        IMPLEMENT waits for every descendant node's IMPLEMENT (children
        before their parent) and for the IMPLEMENT of every declared
        dependency. Tasks are picked with subtree affinity (one in-flight task
        per top-level subtree, longest-remaining group - counted with the
        groups that depend on it - first), so a subtree's tasks stay sequential
        inside their shared worktree while different subtrees overlap.
        """

        # A resumed/interrupted drain cannot have live in-flight tasks in
        # this process; stale registrations would misdirect pending merges.
        self._inflight.clear()
        max_concurrency = self._max_concurrent_tasks()
        if max_concurrency <= 1:
            while True:
                await self._propagate_dependency_blocks(queue_state)
                task = self._next_runnable_task(queue_state)
                if task is None:
                    break
                self._begin_task(task, queue_state)
                await self._execute_task(task, queue_state)
            return

        self._port_slot_count = max_concurrency
        await self._log(
            "Compiler",
            f"Parallel drain enabled: up to {max_concurrency} task(s) in flight, "
            f"one worktree and web port per task (base port {self.web_port}).",
        )
        in_flight: dict[asyncio.Task[None], dict[str, Any]] = {}
        try:
            while True:
                while len(in_flight) < max_concurrency:
                    # Propagation runs before every pick because a task that
                    # just finished may have failed and blocked its dependents.
                    # It cannot race the in-flight executions: the marking
                    # section has no await, it never rewrites a RUNNING task,
                    # and an already-blocked node has no pending task left to
                    # re-mark. The scan is in-memory and only a state change
                    # saves the queue.
                    await self._propagate_dependency_blocks(queue_state)
                    task = self._next_affinity_task(queue_state, in_flight.values())
                    if task is None:
                        break
                    self._begin_task(task, queue_state)
                    in_flight[asyncio.create_task(self._execute_task(task, queue_state))] = task
                if not in_flight:
                    break
                done, _pending = await asyncio.wait(set(in_flight), return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    in_flight.pop(finished, None)
                    # _execute_task turns phase failures into task state, so an
                    # exception escaping here can only be a scheduler bug.
                    finished.result()
                # Loop back to refill the free slots (finishing tasks may have
                # unblocked new work); cancellation delivered at the next await
                # propagates through the finally below.
        finally:
            # Cancellation or an escaping scheduler exception must not leave
            # child tasks mutating shared queue state after the drain exits.
            for pending in in_flight:
                if not pending.done():
                    pending.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
        await self._propagate_dependency_blocks(queue_state)
        # Normal completion only (cancellation re-raises through the finally):
        # the reusable subtree worktrees are no longer needed this run.
        await self._cleanup_reusable_worktrees()

    @staticmethod
    def _next_affinity_task(
        queue_state: dict[str, Any],
        in_flight: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any] | None:
        """Pick the next runnable task honouring subtree affinity.

        Tasks group by their affinity-group subtree (top-level by default;
        ARC_AFFINITY_DEPTH splits deeper), and each group owns one reusable
        worktree, so at most one task per group may run at
        once. A freed slot prefers the free group with the most pending work
        (longest-remaining first), counted together with the pending work of
        the other groups that depend on it: a small hub subtree that many
        groups wait on (declared dependencies gate both phases) must not
        be starved behind larger independent subtrees. When a group runs dry
        the slot steals work from another free group. Without an affinity map
        (queues saved before subtree affinity) every node is its own group and
        the pick degenerates to the historical flat order.
        """

        affinity = queue_state.get("affinity") or {}
        in_flight_tasks = list(in_flight)
        busy_nodes = {str(task.get("node_id", "")) for task in in_flight_tasks}
        busy_groups = {affinity.get(node, node) for node in busy_nodes}

        pending_weight: dict[str, int] = {}
        for other in queue_state["tasks"]:
            if task_status(queue_state, other) != TASK_PENDING:
                continue
            node_id = str(other.get("node_id", ""))
            if node_id in busy_nodes:
                continue
            group = str(affinity.get(node_id, node_id))
            pending_weight[group] = pending_weight.get(group, 0) + 1

        dependent_groups: dict[str, set[str]] = {}
        for dependent_id, dependency_ids in (queue_state.get("dependencies") or {}).items():
            dependent_group = str(affinity.get(dependent_id, dependent_id))
            for dependency_id in dependency_ids:
                dependency_group = str(affinity.get(dependency_id, dependency_id))
                if dependency_group != dependent_group:
                    dependent_groups.setdefault(dependency_group, set()).add(dependent_group)

        best_task: dict[str, Any] | None = None
        best_weight = -1
        for task in queue_state["tasks"]:
            if task_status(queue_state, task) != TASK_PENDING:
                continue
            node_id = str(task.get("node_id", ""))
            if node_id in busy_nodes:
                continue
            group = str(affinity.get(node_id, node_id))
            if group in busy_groups:
                continue
            if not ARCWorkflowManager._task_dependencies_met(queue_state, task):
                continue
            weight = pending_weight.get(group, 0) + sum(
                pending_weight.get(dependent_group, 0)
                for dependent_group in dependent_groups.get(group, ())
            )
            if weight > best_weight:
                best_task, best_weight = task, weight
        return best_task

    async def _cleanup_reusable_worktrees(self) -> None:
        if self._worktree_manager is None:
            return
        try:
            removed = await asyncio.to_thread(self._worktree_manager.cleanup_reusable_worktrees)
        except Exception as exc:
            await self._log(
                "Compiler",
                f"Reusable worktree cleanup failed: {type(exc).__name__}: {exc}",
                "warning",
            )
            return
        if removed:
            await self._log(
                "Compiler",
                f"Removed {len(removed)} reusable worktree(s) after the drain.",
            )

    def _max_concurrent_tasks(self) -> int:
        if not self._parallel_mode:
            return DEFAULT_MAX_CONCURRENT_TASKS
        raw = os.environ.get("ARC_MAX_CONCURRENT_TASKS", "").strip()
        try:
            value = int(raw)
        except ValueError:
            return PARALLEL_DEFAULT_MAX_CONCURRENT_TASKS
        return min(max(1, value), MAX_PARALLEL_TASKS)

    async def _propagate_dependency_blocks(self, queue_state: dict[str, Any]) -> None:
        """Mark pending dependents blocked when a prerequisite has failed.

        A failed prerequisite must not be treated as a satisfied scheduling
        edge. Independent nodes continue draining, while direct and
        transitive dependents become explicit ``BLOCKED`` tasks instead of
        remaining ambiguous ``PENDING`` work at the end of the run. The
        state math lives in ``core.queue_state``; this wrapper persists and
        logs the changes.
        """

        changed = propagate_dependency_blocks(queue_state, on_state_change=self._upsert_node_state)
        if not changed:
            return
        self._save_processing_queue(queue_state)
        for node_id, dependency_ids in changed:
            await self._log(
                "Compiler",
                (
                    f"Blocked node {node_id}: prerequisite node(s) failed or were blocked "
                    f"({', '.join(dependency_ids)}). Its tasks will not be treated as successful; "
                    "independent nodes may continue for diagnostics."
                ),
                "warning",
                node_id,
            )

    async def _release_dependency_blocks(self, queue_state: dict[str, Any]) -> list[str]:
        """Return ``BLOCKED`` nodes to schedulable state after a retry reset.

        ``_propagate_dependency_blocks`` is one-way: a failed prerequisite
        blocks its dependents immediately, but nothing ever un-blocks them.
        When a retry resets a previously failed node, its dependents' BLOCKED
        tasks would stay frozen forever — the 2026-09-20 test1 run passed
        REQ-1's retry yet finished "blocked: REQ-2, ROOT" precisely because of
        this asymmetry. This mirror runs after every retry reset: a node whose
        failed prerequisites were all reset goes back to PENDING, and the
        drain's per-pick propagation re-blocks anything whose prerequisites
        fail again, so releasing is never unsafe. The fixpoint walk lives in
        ``core.queue_state``; this wrapper persists and logs the releases.
        """
        released = release_dependency_blocks(queue_state, on_state_change=self._upsert_node_state)
        if released:
            self._save_processing_queue(queue_state)
        for node_id in released:
            await self._log(
                "Compiler",
                (
                    f"Unblocked node {node_id}: its prerequisite node(s) were reset for retry, "
                    "so its tasks are schedulable again."
                ),
                node_id=node_id,
            )
        return released

    def _upsert_node_state(self, node_id: str, state: str) -> None:
        """Keep the traceability ``node_states`` table current on every write."""

        if self.runtime is not None:
            self.runtime.traceability.upsert_node_state(node_id, state)

    def _begin_task(self, task: dict[str, Any], queue_state: dict[str, Any]) -> None:
        begin_task(queue_state, task, on_state_change=self._upsert_node_state)
        if task["phase"] == PHASE_DESIGN:
            self.runtime.events.mark_design_started(task["node_id"])
        else:
            self.runtime.events.mark_implementation_started(task["node_id"])
        self._save_processing_queue(queue_state)

    async def _execute_task(self, task: dict[str, Any], queue_state: dict[str, Any]) -> None:
        node_id = task["node_id"]
        phase = task["phase"]
        requirement_data = self.runtime.traceability.get_requirement(node_id) or {}

        await self._log("Compiler", f"Running {phase} for node {node_id}...", node_id=node_id)

        ctx: _TaskWorkspace | None = None
        if self._parallel_mode:
            try:
                ctx = await self._open_task_workspace(task, queue_state)
                # In-flight registration for the demand-pull replay (issue
                # #127): sibling merges attach their changed files here.
                self._inflight[node_id] = ctx
            except Exception as exc:
                await self._log(
                    "Compiler",
                    f"Failed to prepare the isolated workspace for {node_id}: "
                    f"{type(exc).__name__}: {exc}",
                    "error",
                    node_id,
                )

        task_ok = False
        if ctx is not None or not self._parallel_mode:
            try:
                task_ok = await self._run_task(task, ctx)
            except Exception as exc:
                await self._log(
                    "Compiler",
                    f"{phase} task for node {node_id} crashed: {type(exc).__name__}: {exc}",
                    "error",
                    node_id,
                )
                task_ok = False

        merged = True
        merge_conflict: list[str] = []
        if ctx is not None:
            if not task_ok:
                # A failed phase may leave useful diagnostics in its isolated
                # worktree, but it must never be merged into the accepted
                # integration HEAD. Merging a ``DESIGN-FAILED`` checkpoint
                # makes downstream nodes observe an unverified shell and was
                # the source of false-successful continuation in failed runs.
                merged = False
                await self._log(
                    "Compiler",
                    f"Skipping integration for failed {phase} task of node {node_id}; preserving the isolated worktree for inspection/retry.",
                    "warning",
                    node_id,
                )
            else:
                # Commit the worktree and merge its branch back; a merge
                # conflict fails the node even when its phase succeeded,
                # because the work never reached the integration workspace.
                # One narrow exception per phase: the first DESIGN conflict
                # re-queues the node's DESIGN once, and the first IMPLEMENT
                # conflict re-queues the node's IMPLEMENT once - both with
                # the conflicting paths as guidance, so a parallel sibling
                # that won the file does not cost the whole node.
                merged, _detail, merge_conflict = await self._integrate_task_workspace(
                    ctx,
                    node_id,
                    phase,
                    requirement_data,
                )
                if not merged:
                    if (
                        merge_conflict
                        and phase == PHASE_DESIGN
                        and self._merge_conflict_requeue_available(node_id, PHASE_DESIGN)
                    ):
                        if await self._requeue_design_after_merge_conflict(
                            ctx, queue_state, node_id, merge_conflict
                        ):
                            # The requeue settled the worktree through the
                            # manager (reset + reuse-or-remove) and released
                            # the task's claims and port slot.
                            return
                    elif (
                        merge_conflict
                        and phase == PHASE_IMPLEMENT
                        and self._merge_conflict_requeue_available(node_id, PHASE_IMPLEMENT)
                    ):
                        if await self._requeue_implement_after_merge_conflict(
                            ctx, queue_state, node_id, merge_conflict
                        ):
                            return
                    # Requeue declined (this phase's budget already spent, no
                    # requeue path for the phase, or the requeue itself
                    # failed): fall through to the failure branch. The method
                    # tail still settles ctx as FAILED there, so a declined
                    # requeue never leaks the worktree - it is preserved for
                    # --retry exactly like any other failed merge.
                    task_ok = False

        if task_ok:
            sessions.merge_node_session(node_id, {"resume_context": {}})
            complete_task(queue_state, node_id, phase, on_state_change=self._upsert_node_state)
            self._save_processing_queue(queue_state)
            if phase == PHASE_DESIGN:
                self.runtime.events.mark_design_done(node_id)
            else:
                self.runtime.events.mark_implementation_done(node_id)
                self.runtime.events.mark_test_passed(node_id)
            if ctx is None:
                await self._commit_phase_checkpoint(node_id, phase, requirement_data)
            await self._log("Compiler", f"{phase} completed for node {node_id}.", node_id=node_id)
        else:
            fail_task(queue_state, node_id, on_state_change=self._upsert_node_state)
            self._save_processing_queue(queue_state)
            if phase == PHASE_DESIGN:
                self.runtime.events.mark_design_failed(node_id)
                # Audit trail for the parent-serial gate: a failed parent
                # unblocks its children, so record that they will design
                # against the integration state without this shell.
                descendants = queue_state.get("descendants", {}).get(node_id) or []
                if descendants:
                    await self._log(
                        "Compiler",
                        f"DESIGN failed for node {node_id}; {len(descendants)} descendant node(s) "
                        f"({', '.join(descendants)}) will design against the integration state "
                        "without this node's shell.",
                        "warning",
                        node_id,
                    )
            else:
                self.runtime.events.mark_implementation_failed(node_id)
                # The auto TDD retry reads this event's message into its
                # re-prompt; without it the retried session starts from
                # "(no detail provided)" and re-explores a failure the
                # previous round already diagnosed. The node session's
                # failure summary is the freshest evidence at this point.
                failure_detail = str(
                    sessions.load_node_session(node_id).get("recent_failure_summary", "") or ""
                ).strip()
                self.runtime.events.mark_test_failed(node_id, message=failure_detail or None)
            if ctx is None:
                await self._commit_phase_checkpoint(node_id, f"{phase}-FAILED", requirement_data)
            await self._log("Compiler", f"{phase} failed for node {node_id}.", "error", node_id)

        if ctx is not None:
            await self._settle_task_workspace(
                ctx, WorktreeTaskResult.MERGED if merged else WorktreeTaskResult.FAILED
            )

    async def _open_task_workspace(self, task: dict[str, Any], queue_state: dict[str, Any]) -> _TaskWorkspace:
        """Create the task's isolated worktree, port slot and phase runner.

        The worktree directory is keyed by the task's affinity-group subtree
        (top-level by default; ARC_AFFINITY_DEPTH splits deeper) so
        consecutive tasks of one group reuse it;
        ``prepare`` falls back to a node-keyed directory when the group
        directory is dirty or quarantined.
        """

        node_id = task["node_id"]
        slot = self._acquire_port_slot(node_id)
        try:
            group_key = self._task_affinity(node_id, queue_state)
            handle = await asyncio.to_thread(self._worktree_manager.prepare, node_id, group_key)
        except Exception:
            self._release_port_slot(slot)
            raise
        web_port = self._slot_port(slot)
        runner = self._build_task_phase_runner(handle.path, web_port, handle=handle)
        await self._log(
            "Compiler",
            f"Isolated workspace for {node_id}: {handle.path}"
            + (f" (web port {web_port})" if web_port is not None else ""),
            node_id=node_id,
        )
        return _TaskWorkspace(
            node_id=node_id,
            handle=handle,
            slot=slot,
            web_port=web_port,
            phase_runner=runner,
        )

    def _build_task_phase_runner(
        self,
        workspace_path: str,
        web_port: int | None,
        handle: Any | None = None,
    ) -> WorkflowPhaseRunner:
        """Build adapters and an app handler rooted at the task's worktree.

        The adapters are per-task instances because they carry per-run state
        (TDD budget/verifier bookkeeping) that parallel tasks must not share.
        Traceability, node sessions and the context pipeline stay rooted in the
        main workspace via context_workspace_root/context_workspace_path.
        ``handle`` (the task's worktree handle) additionally wires the
        mid-phase replay gate when ARC_REBASE_ON_MERGE is on.
        """

        common = dict(
            log_cb=self.log_cb,
            workspace_root=workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            context_workspace_root=self.workspace_path,
        )
        # The mid-phase replay gate (issue #127) is per task: its handle is
        # this task's worktree. ``None`` (serial mode / gate off) leaves the
        # adapters without a provider, unchanged from main.
        rebase_provider = None
        if handle is not None and self._rebase_on_merge:
            def rebase_provider() -> Any:
                return self._build_task_rebase_gate(handle.node_id, handle)
        return WorkflowPhaseRunner(
            workspace_path=workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=InterfaceDesigner(rebase_gate_provider=rebase_provider, **common),
            test_generator=TestGenerator(rebase_gate_provider=rebase_provider, **common),
            test_driven_developer=TestDrivenDeveloper(rebase_gate_provider=rebase_provider, **common),
            log_cb=self._log,
            web_port=web_port,
            context_workspace_path=self.workspace_path,
        )

    async def _integrate_task_workspace(
        self,
        ctx: _TaskWorkspace,
        node_id: str,
        phase: str,
        requirement_data: dict[str, Any],
    ) -> tuple[bool, str, list[str]]:
        """Merge the task's branch back; report conflicting paths if any.

        Returns ``(merged, detail, conflict_paths)``; ``conflict_paths`` is
        non-empty only when the merge aborted on a conflict, and carries the
        unmerged files for the conflict-aware DESIGN retry.
        """

        commit_message = build_commit_message(node_id, phase, requirement_data)
        verify = self._build_merge_health_gate() if self.app_type == "web" else None
        arbiter = self._build_merge_arbitration_hooks(ctx, node_id, phase)
        async with self._merge_lock:
            pre_merge_head = self._integration_head_sha()
            try:
                committed, detail = await asyncio.to_thread(
                    self._worktree_manager.integrate,
                    ctx.handle,
                    commit_message,
                    verify=verify,
                    arbiter=arbiter,
                )
            except MergeConflictError as exc:
                await self._log("Compiler", str(exc), "error", node_id)
                return False, str(exc), list(getattr(exc, "files", []) or [])
            except WorktreeError as exc:
                await self._log("Compiler", f"Integration of {node_id} failed: {exc}", "error", node_id)
                return False, str(exc), []
            # The merge is done and the integration branch moved (a no-change
            # merge leaves it in place and the diff inside comes back empty,
            # making the attach a no-op).
            self._attach_pending_merge(node_id, pre_merge_head)
        if not committed:
            await self._log("Compiler", "No file changes detected for this checkpoint.", node_id=node_id)
        await self._log("Compiler", f"Integrated {node_id}: {detail}.", node_id=node_id)
        if phase == PHASE_IMPLEMENT:
            await self._check_contract_drift(node_id)
        return True, detail, []

    def _integration_head_sha(self) -> str:
        """Current integration HEAD sha (empty when unavailable)."""

        if self._worktree_manager is None:
            return ""
        try:
            return self._worktree_manager.integration_head_sha()
        except Exception:  # noqa: BLE001 - a pending-merge attach is best effort
            return ""

    def _attach_pending_merge(self, source_node_id: str, pre_merge_head: str) -> None:
        """Attach the just-landed merge to every other in-flight task.

        Demand-pull bookkeeping (issue #127): the changed-file set
        (pre-merge HEAD..HEAD) lands on each in-flight task's worktree as a
        ``PendingMerge``; nothing replays until the task's agent touches one
        of those paths. Skipped entirely when the gate is off or no other
        task is in flight. Best effort: a git failure here must not fail the
        merge that already succeeded.
        """

        if not self._rebase_on_merge or not self._inflight:
            return
        try:
            changed_files = self._worktree_manager.read_integration_diff(pre_merge_head)
        except Exception as exc:  # noqa: BLE001 - the merge already landed
            append_debug_log(
                "Compiler",
                f"pending-merge attach for {source_node_id} failed: {type(exc).__name__}: {exc}",
                workspace_root=self.workspace_path,
            )
            return
        if not changed_files:
            return
        head_sha = self._integration_head_sha()
        for other_id, ctx in self._inflight.items():
            if other_id == source_node_id:
                continue
            self._worktree_manager.record_pending_merge(
                ctx.handle, source_node_id, head_sha, changed_files
            )

    def _build_task_rebase_gate(self, node_id: str, handle: Any) -> Any | None:
        """The per-task mid-phase replay middleware (issue #127).

        ``None`` keeps the agent stack unchanged: serial mode (no worktree),
        or the feature gate closed. The middleware receives the manager's
        replay entry points and an audit hook that writes the
        ``rebase_replay`` runner events; the file-claim gate's tracked-set
        snapshot is invalidated after each successful replay (the factory
        wires ``RebaseOnMergeMiddleware.attach_claim_gate`` to the claim gate
        it builds) so a sibling's newly-tracked file is claim-checked
        against the fresh tree.
        """

        if not self._rebase_on_merge or self._worktree_manager is None:
            return None
        from agents.runtime.rebase_gate import RebaseOnMergeMiddleware

        manager = self._worktree_manager

        def on_replay_started() -> None:
            self._emit_rebase_replay_event(node_id, "started", [], "pending merge touched")

        def on_replay(outcome: Any) -> None:
            status = str(getattr(outcome, "status", "") or "")
            # Issue #127's lifecycle vocabulary: a landed replay (clean or
            # conflict-completed) reports ``resolved``; a conflict round and
            # a fail-open abort keep their own statuses.
            if status == "replayed":
                status = "resolved"
            self._emit_rebase_replay_event(
                node_id,
                status,
                [str(path) for path in (getattr(outcome, "files", None) or [])],
                str(getattr(outcome, "detail", "") or "") or None,
            )

        def conflict_contract_cards(conflict_paths: list[str]) -> dict[str, Any]:
            # The same pruned both-sides cards the merge arbiter's input
            # uses; a traceability read that fails simply yields no cards
            # (the notice is advisory).
            try:
                return self._arbitration_contract_cards(node_id, conflict_paths)
            except Exception:  # noqa: BLE001 - advisory only
                return {}

        return RebaseOnMergeMiddleware(
            handle=handle,
            replay=manager.replay_pending_merges,
            pending_files=lambda: manager.pending_merges_for(handle),
            is_mid_rebase=manager.is_mid_rebase,
            continue_replay=manager.continue_replay,
            abort_replay=manager.abort_replay,
            on_replay=on_replay,
            on_replay_started=on_replay_started,
            conflict_contract_cards=conflict_contract_cards,
        )

    def _emit_rebase_replay_event(self, node_id: str, outcome: Any) -> None:
        """Persist one ``rebase_replay`` runner event (best effort)."""

        try:
            self.runtime.events.record_rebase_replay(
                node_id=node_id,
                status=str(getattr(outcome, "status", "") or ""),
                files=[str(path) for path in (getattr(outcome, "files", None) or [])],
                message=str(getattr(outcome, "detail", "") or "") or None,
            )
        except Exception as exc:  # noqa: BLE001 - audit must never break the tool call
            append_debug_log(
                "RebaseReplay",
                f"rebase replay audit emit failed: {type(exc).__name__}: {exc}",
                workspace_root=self.workspace_path,
            )

    async def _check_contract_drift(self, node_id: str) -> None:
        """Validate the node's registered contracts against the merged tree.

        Under DESIGN gate pipelining a dependent designs against this node's
        registered interface cards while this node is still implementing, so
        the card's anchor (``file_path`` + ``first_line``) is the contract the
        dependent relies on. When this IMPLEMENT merges, the landed tree must
        still honor every anchor; an implementation that moved or reshaped a
        registered surface is drift.

        The check is a guard, not a gate: with ``ARC_MERGE_ARBITRATION`` on
        and the node's arbitration budget unspent, the drift escalates through
        the health-gate arbitration path (the #81 contract: the arbiter may
        restore the drifted anchors inside the drift file set, one attempt,
        re-verified); otherwise - gate off, budget spent, or arbitration
        declined - the drift is recorded as a ``contract_drift`` runner event
        and a warning without blocking the merge, and downstream TDD red
        lights remain the final backstop (issue #83's contract: 仲裁未合入或
        关闭时记告警不阻塞).

        A store that cannot be read here (an older queue being resumed, a
        store without the interfaces surface) means no registered contracts
        to check: skip rather than fail the merge - same best-effort contract
        as the audit emit below.
        """

        try:
            registered = self.runtime.traceability.list_interfaces(req_id=node_id)
        except Exception as exc:  # noqa: BLE001 - the guard must never break the merge
            await self._log(
                "Compiler",
                (
                    f"Contract drift check for {node_id} skipped: the traceability "
                    f"store could not be read ({type(exc).__name__}: {exc})."
                ),
                "warning",
                node_id,
            )
            return
        if not registered:
            return
        drift = detect_contract_drift(registered, workspace_root=self.workspace_path)
        if not drift:
            return

        drift_paths = sorted({item.file_path for item in drift})
        await self._emit_contract_drift_event(
            {
                "type": "contract_drift",
                "node_id": node_id,
                "drift": [item.to_payload() for item in drift],
                "arbitration": arbitration_enabled(),
            }
        )
        await self._log(
            "Compiler",
            (
                f"Contract drift after merging IMPLEMENT of {node_id}: "
                + "; ".join(item.describe() for item in drift)
                + "."
            ),
            "warning",
            node_id,
        )

        if not arbitration_enabled():
            return
        if sessions.load_node_session(node_id).get(merge_arbitration_budget_key()):
            await self._log(
                "Compiler",
                f"Contract drift arbitration for {node_id} skipped: the node's single arbitration budget is already spent.",
                "warning",
                node_id,
            )
            return

        repair = await self._arbitrate_contract_drift(node_id, drift, drift_paths)
        if repair:
            await self._log(
                "Compiler",
                f"Contract drift arbitration for {node_id} restored the registered anchors: {', '.join(drift_paths)}.",
                node_id,
            )
            await self._emit_contract_drift_event(
                {
                    "type": "contract_drift",
                    "node_id": node_id,
                    "drift": [item.to_payload() for item in drift],
                    "arbitration": True,
                    "outcome": "repaired",
                }
            )
        else:
            await self._log(
                "Compiler",
                (
                    f"Contract drift arbitration for {node_id} did not restore the anchors; "
                    "the merge stands and downstream TDD red lights are the backstop."
                ),
                "warning",
                node_id,
            )

    async def _arbitrate_contract_drift(
        self,
        node_id: str,
        drift: list[ContractDrift],
        drift_paths: list[str],
    ) -> bool:
        """One arbitration attempt to restore the drifted anchors.

        Reuses the #81 machinery (pruned input, narrow edit rights inside the
        drift file set, budget marked spent before the model call, audit
        events) with the drift trigger framed like a health-gate failure: the
        arbiter sees the currently landed content (what drifted) plus the
        registered contracts it must honor, and rewrites only the drift
        files. The result is re-verified by the same anchor check; a failed
        attempt leaves the merge as it stands. Unlike the mid-merge hooks
        this runs after the merge commit, so a successful repair is committed
        as its own follow-up on the integration branch.
        """

        # Mark the budget spent *before* the model call: a crashed arbitration
        # must not buy a second attempt (same contract as the merge hooks).
        sessions.merge_node_session(node_id, {merge_arbitration_budget_key(): True})
        files: dict[str, dict[str, str | None]] = {}
        for path in drift_paths:
            files[path] = {
                "resolved": read_workspace_file(self.workspace_path, path),
                "base": None,
                "ours": None,
                "theirs": None,
            }
        registered = {
            str(item.get("interface_id") or "").strip(): item
            for item in self.runtime.traceability.list_interfaces(req_id=node_id)
        }
        contract_cards = {
            node_id: {
                "interfaces": [
                    registered.get(item.interface_id, {"interface_id": item.interface_id})
                    for item in drift
                ],
                "registered_anchors": [item.to_payload() for item in drift],
            }
        }
        arbitration_input = ArbitrationInput(
            trigger=TRIGGER_HEALTH_GATE,
            ours_label=self._integration_side_label(node_id),
            theirs_label=node_id,
            files=files,
            contract_cards=contract_cards,
            gate_failure=(
                "post-merge contract drift: the implementation no longer honors the "
                "anchors its DESIGN registered; dependents designed against these "
                "registered contracts, so each anchor (file_path + first_line) must "
                "be addressable again in the drifted files"
            ),
        )
        arbiter = MergeArbiter(
            model=self._build_arbitration_model(),
            workspace_path=self.workspace_path,
            emit_event=self._emit_merge_arbitration_event,
        )
        result = await arbiter.arbitrate(
            "",
            self._integration_side_label(node_id),
            arbitration_input,
            node_id=node_id,
            phase=PHASE_IMPLEMENT,
        )
        if not result.accepted:
            return False
        self._worktree_manager.stage_paths(result.applied)
        remaining = detect_contract_drift(
            self.runtime.traceability.list_interfaces(req_id=node_id),
            workspace_root=self.workspace_path,
        )
        if remaining:
            return False
        # An accepted arbitration whose rewrite is byte-identical to what is
        # already on disk (the anchors were somehow already honored) leaves
        # nothing to commit: that is a successful repair, not a failure, so
        # "nothing to commit" (commit_integration returning False) counts as
        # repaired. A real commit failure is not.
        try:
            self._worktree_manager.commit_integration(
                f"contract drift arbitration: restore registered anchors of {node_id}"
            )
        except WorktreeError:
            return False
        return True

    async def _emit_contract_drift_event(self, payload: dict[str, Any]) -> None:
        """Persist one contract-drift audit record (best effort)."""

        try:
            from arcbench_agent_runtime.events import utc_timestamp

            payload = {"timestamp": utc_timestamp(), **payload}
            from arcbench_agent_runtime.jsonio import append_jsonl

            append_jsonl(self.runtime.paths.runner_events_path, payload)
        except Exception as exc:  # noqa: BLE001 - audit must never break the merge
            append_debug_log(
                "ContractDrift",
                f"contract drift audit emit failed: {type(exc).__name__}: {exc}",
                workspace_root=self.workspace_path,
            )

    def _build_merge_arbitration_hooks(
        self,
        ctx: _TaskWorkspace,
        node_id: str,
        phase: str,
    ) -> ArbitrationHooks | None:
        """LLM escalation hooks for this task's merge (issue #81).

        Gated by ``ARC_MERGE_ARBITRATION`` (default off): with the gate closed
        the hooks are ``None`` and ``integrate`` behaves exactly like main.
        The budget is one arbitration per node, stored in the node session and
        marked spent *before* the model call so a crashed arbitration cannot
        buy a second attempt. Both hooks run inside the merge worker thread;
        the model call itself is executed synchronously through the arbiter's
        ``arbitrate`` (the stage adapters' model classes expose sync-less
        async only, so the hooks wrap the coroutine with ``asyncio.run`` on
        the worker thread, the same pattern the health gate uses).
        """

        if not arbitration_enabled():
            return None

        def collect_input(
            conflict_paths: list[str],
            trigger: str,
            gate_failure: str = "",
        ) -> ArbitrationInput | None:
            if sessions.load_node_session(node_id).get(merge_arbitration_budget_key()):
                return None
            stages = self._worktree_manager.read_conflict_stages(conflict_paths)
            if trigger == TRIGGER_HEALTH_GATE:
                # The mechanical resolution already staged the files, so the
                # merge index holds no conflict stages: the arbiter sees the
                # currently resolved content it must repair, plus whatever
                # stages the index still exposes.
                for path, entry in stages.items():
                    entry["resolved"] = read_workspace_file(self.workspace_path, path)
            return ArbitrationInput(
                trigger=trigger,
                ours_label=self._integration_side_label(node_id),
                theirs_label=node_id,
                files=stages,
                contract_cards=self._arbitration_contract_cards(node_id, conflict_paths),
                gate_failure=gate_failure,
            )

        def run(
            arbitration_input: ArbitrationInput,
            conflict_paths: list[str],
            trigger: str,
        ) -> str | None:
            if sessions.load_node_session(node_id).get(merge_arbitration_budget_key()):
                return "The node's single arbitration budget is already spent."
            sessions.merge_node_session(node_id, {merge_arbitration_budget_key(): True})
            arbiter = MergeArbiter(
                model=self._build_arbitration_model(),
                workspace_path=self.workspace_path,
                emit_event=self._emit_merge_arbitration_event,
            )
            result = asyncio.run(
                arbiter.arbitrate(
                    ctx.handle.path,
                    self._integration_side_label(node_id),
                    arbitration_input,
                    node_id=node_id,
                    phase=phase,
                )
            )
            if not result.accepted:
                return result.detail
            self._worktree_manager.stage_paths(result.applied)
            return None

        def on_reverified(gate_result: str | None) -> None:
            """Persist the post-repair re-verification result (audit trail)."""

            self._emit_merge_arbitration_event(
                {
                    "type": "merge_arbitration",
                    "node_id": node_id,
                    "phase": phase,
                    "trigger": TRIGGER_HEALTH_GATE,
                    "outcome": "reverified-passed" if not gate_result else "reverified-failed",
                    "detail": gate_result or "",
                }
            )

        return ArbitrationHooks(
            collect_input=collect_input,
            run=run,
            on_reverified=on_reverified,
        )

    def _integration_side_label(self, node_id: str) -> str:
        """How the integration side is named in the arbitration prompt.

        The integration branch holds every already-merged sibling, so it is
        presented as one side ("integration branch") rather than attributed to
        a single node; git cannot cheaply say which sibling authored the
        ``ours`` stage of a conflicted file.
        """

        return f"integration branch (all merged nodes except {node_id})"

    def _arbitration_contract_cards(self, node_id: str, conflict_paths: list[str]) -> dict[str, Any]:
        """Both sides' contract cards, filtered to the conflicting files.

        A card belongs in the arbitration input only when its interfaces
        point at a conflicted path (the incoming node's side) or it is the
        incoming node itself; unrelated nodes' cards - including siblings
        that never touched the conflict - are pruned, keeping the context
        cost gate honest on wide trees.
        """

        conflict_set = set(conflict_paths)
        try:
            others = [
                str(row.get("req_id") or "").strip()
                for row in self.runtime.traceability.list_node_contracts()
                if isinstance(row, dict)
            ]
        except Exception:
            others = []
        node_ids = [req_id for req_id in others if req_id and req_id != node_id]
        node_ids.append(node_id)
        cards = collect_contract_cards(self.runtime.traceability, node_ids)
        # Keep a foreign card only when one of its interfaces lives in a
        # conflicted file; the incoming node's own card always stays.
        pruned: dict[str, Any] = {}
        for card_node_id, card in cards.items():
            if card_node_id == node_id:
                pruned[card_node_id] = card
                continue
            interfaces = card.get("interfaces") if isinstance(card, dict) else None
            if any(
                isinstance(row, dict) and str(row.get("file_path") or "") in conflict_set
                for row in (interfaces or [])
            ):
                pruned[card_node_id] = card
        return pruned

    def _build_arbitration_model(self) -> Any:
        """The arbitration model: the run's configured main model.

        Issue #81 pins arbitration to the main model (no cheaper arbiter).
        The model instance is built per arbitration so a test-provided fake
        monkeypatched into the adapter layer is always honored.
        """

        from agents.model.factory import create_arc_chat_model

        model_name = os.environ.get("MODEL", "openai:gpt-5.4")
        return create_arc_chat_model(model_name)

    def _emit_merge_arbitration_event(self, payload: dict[str, Any]) -> None:
        """Persist one audit record as a runner event (best effort)."""

        try:
            from arcbench_agent_runtime.jsonio import append_jsonl

            append_jsonl(self.runtime.paths.runner_events_path, payload)
        except Exception as exc:  # noqa: BLE001 - audit must never break the merge
            append_debug_log(
                "MergeArbiter",
                f"merge arbitration audit emit failed: {type(exc).__name__}: {exc}",
                workspace_root=self.workspace_path,
            )

    def _apply_reset_side_effects(self, node_id: str, plan: ResetPlan) -> None:
        """Apply a reset's runtime effects (the plan comes from queue_state)."""

        if plan.clear_design_artifacts:
            self.runtime.traceability.clear_node_design_artifacts(node_id)
        if plan.reset_test_pass:
            self.runtime.traceability.reset_test_pass_statuses_for_requirement(node_id)
        if plan.invalidate_file_layers:
            context_pipeline.cache.invalidate_file_layers(node_id)
        if plan.invalidate_db_layers:
            context_pipeline.cache.invalidate_db_layers(node_id)

    async def _requeue_design_after_merge_conflict(
        self,
        ctx: _TaskWorkspace,
        queue_state: dict[str, Any],
        node_id: str,
        conflict_paths: list[str],
    ) -> bool:
        """Re-queue a node's DESIGN once after a merge conflict.

        The conflicting files stay owned by the winning sibling (its branch
        is already merged), so the node's retry must run from the current
        integration state - which already contains the winning sibling's
        files. The manager owns the retry reset: ``settle`` with
        ``RESET_FOR_RETRY`` resets the node's branch to the integration HEAD,
        un-quarantines the worktree directory, and reuses or removes the
        directory per its reusability. The conflicting paths are stored in
        the node session for the DESIGN prompt; a second conflict (or an
        IMPLEMENT conflict) fails the node as before.

        Every decline path leaves the task workspace exactly as a regular
        conflict failure left it: quarantined, branch intact, preserved for
        ``--retry``. The queue is validated *before* the reset so a decline
        never discards the node's conflicted commits.
        """

        if not has_phase_tasks(queue_state, node_id):
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed: its queue tasks are incomplete.",
                "error",
                node_id,
            )
            return False

        # The manager's reset-and-settle runs before the queue is mutated: a
        # failed reset leaves the worktree untouched, the requeue declines,
        # and the node fails with its conflicted commits intact.
        if not await self._settle_for_conflict_requeue(ctx, node_id):
            return False

        try:
            plan = reset_node_for_retry(
                queue_state,
                node_id,
                phase=PHASE_DESIGN,
                conflict_paths=conflict_paths,
                on_state_change=self._upsert_node_state,
            )
        except ValueError as exc:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed: its queue tasks are incomplete ({exc}).",
                "error",
                node_id,
            )
            return False
        self._apply_reset_side_effects(node_id, plan)
        self._save_processing_queue(queue_state)
        await self._log(
            "Compiler",
            "Merge conflict on: "
            + ", ".join(conflict_paths[:8])
            + f". Re-queued {node_id} DESIGN once; the retry starts from the merged integration "
            "state and must avoid the sibling-owned paths.",
            "warning",
            node_id,
        )
        return True

    @staticmethod
    def _merge_conflict_requeue_available(node_id: str, phase: str) -> bool:
        """Whether this phase still has its one-shot conflict requeue left.

        The budget is per phase: a node whose DESIGN retry already consumed
        the DESIGN budget keeps a full IMPLEMENT budget (and vice versa), so
        each phase gets exactly one requeue. The phase key is the recorded
        ``merge_conflict_context.phase`` - the flag written by a DESIGN
        requeue does not cost the node its IMPLEMENT requeue. A legacy
        ``retry_used`` flag with no readable context is treated as spent for
        safety (it can only come from a pre-phase-keying requeue).
        """

        session = sessions.load_node_session(node_id)
        if not session.get("merge_conflict_retry_used"):
            return True
        context = session.get("merge_conflict_context")
        if not isinstance(context, dict):
            return False
        recorded_phase = str(context.get("phase") or "").strip().lower()
        return recorded_phase != str(phase).strip().lower()

    async def _requeue_implement_after_merge_conflict(
        self,
        ctx: _TaskWorkspace,
        queue_state: dict[str, Any],
        node_id: str,
        conflict_paths: list[str],
    ) -> bool:
        """Re-queue a node's IMPLEMENT once after a merge conflict.

        The DESIGN-side counterpart of
        ``_requeue_design_after_merge_conflict``: the conflicting files stay
        owned by the winning sibling (its branch is already merged), so the
        node's retry must run from the current integration state - the
        manager's ``settle`` with ``RESET_FOR_RETRY`` resets the node's
        branch to the integration HEAD, un-quarantines the worktree
        directory, and reuses or removes the directory per its reusability.
        DESIGN artifacts are NOT cleared: the design already merged cleanly
        and is part of the integration HEAD the retry starts from. The
        conflicting paths are stored in the node session for the TDD prompt;
        a second conflict fails the node as before.

        Every decline path leaves the task workspace exactly as a regular
        conflict failure left it: quarantined, branch intact, preserved for
        ``--retry``. The queue is validated *before* the reset so a decline
        never discards the node's conflicted commits.
        """

        if not has_phase_tasks(queue_state, node_id):
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed: its queue tasks are incomplete.",
                "error",
                node_id,
            )
            return False
        # The requeue is implement-only by construction (the caller saw an
        # IMPLEMENT conflict), so the DESIGN task must have settled; guard
        # against an unexpected queue shape instead of resetting a completed
        # DESIGN and re-running its agent for nothing.
        if design_status_of(queue_state, node_id) != TASK_COMPLETED:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed: its DESIGN task is not completed.",
                "error",
                node_id,
            )
            return False

        # The manager's reset-and-settle runs before the queue is mutated: a
        # failed reset leaves the worktree untouched, the requeue declines,
        # and the node fails with its conflicted commits intact.
        if not await self._settle_for_conflict_requeue(ctx, node_id):
            return False

        try:
            plan = reset_node_for_retry(
                queue_state,
                node_id,
                phase=PHASE_IMPLEMENT,
                conflict_paths=conflict_paths,
                on_state_change=self._upsert_node_state,
            )
        except ValueError as exc:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed: its queue tasks are incomplete ({exc}).",
                "error",
                node_id,
            )
            return False
        self._apply_reset_side_effects(node_id, plan)
        self._save_processing_queue(queue_state)
        await self._log(
            "Compiler",
            "Merge conflict on: "
            + ", ".join(conflict_paths[:8])
            + f". Re-queued {node_id} IMPLEMENT once; the retry starts from the merged integration "
            "state and must avoid the sibling-owned paths.",
            "warning",
            node_id,
        )
        return True

    def _build_merge_health_gate(self) -> Callable[[], str | None]:
        """Build the sync verification callback for additively resolved merges.

        The callback runs inside the merge (worker thread) while the resolved
        tree is staged but not yet committed, so a failed probe aborts the
        merge without ever landing a broken registration on the integration
        branch. It boots the merged workspace's backend on a throwaway port
        and checks ``/api/health``.
        """

        def verify() -> str | None:
            import socket

            from app_type_handler.web import probe_backend_health

            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                probe_port = int(sock.getsockname()[1])
            return asyncio.run(probe_backend_health(self.workspace_path, probe_port))

        return verify

    async def _settle_task_workspace(
        self, ctx: _TaskWorkspace, result: WorktreeTaskResult
    ) -> WorktreeOutcome | None:
        """Settle the task's worktree through the manager's lifecycle decision.

        The scheduler reports the task's result (``WorktreeTaskResult``); the
        manager returns the one outcome (preserve / reuse / delete). Returns
        ``None`` when the manager could not settle the worktree (logged as a
        warning). Scheduler-owned resources - the node's file claims and the
        port slot - are always released here; the worktree itself is entirely
        the manager's.
        """

        try:
            outcome = await asyncio.to_thread(
                self._worktree_manager.settle, ctx.handle, result=result
            )
        except Exception as exc:
            await self._log(
                "Compiler",
                f"Worktree cleanup for {ctx.node_id} failed: {type(exc).__name__}: {exc}",
                "warning",
                ctx.node_id,
            )
            return None
        finally:
            # The task is no longer in flight: drop the demand-pull replay
            # registration and release the node's new-file claims (after a
            # successful merge the files are tracked in git - claims are
            # moot; after a terminal failure the paths must be free for
            # other nodes).
            self._inflight.pop(ctx.node_id, None)
            get_file_claim_registry(self.workspace_path).release_node(ctx.node_id)
            self._release_port_slot(ctx.slot)
        return outcome

    async def _settle_for_conflict_requeue(self, ctx: _TaskWorkspace, node_id: str) -> bool:
        """Settle the task workspace for a conflict requeue.

        The manager's reset-and-settle runs before the queue is mutated: a
        failed reset leaves the worktree untouched, the requeue declines, and
        the node fails with its conflicted commits intact. Returns ``False``
        when the requeue must decline; the settle failure's cause is already
        logged as a warning by ``_settle_task_workspace``.
        """

        settled = await self._settle_task_workspace(ctx, WorktreeTaskResult.RESET_FOR_RETRY)
        if settled is None:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed; the node fails instead.",
                "error",
                node_id,
            )
            return False
        return True

    def _acquire_port_slot(self, node_id: str) -> int:
        for slot in range(self._port_slot_count):
            if slot not in self._port_slots:
                self._port_slots[slot] = node_id
                return slot
        # The drain caps in-flight tasks at the slot count, so exhaustion can
        # only mean a scheduler bug that leaked a slot. Fail loudly instead of
        # silently widening the port range into unrelated services.
        raise RuntimeError(
            f"No free port slot for {node_id}: all {self._port_slot_count} slot(s) "
            f"are in flight ({sorted(self._port_slots.items())})."
        )

    def _release_port_slot(self, slot: int) -> None:
        self._port_slots.pop(slot, None)

    def _slot_port(self, slot: int) -> int | None:
        if slot < 0:
            return None
        return self.web_port + 1 + slot

    def _prune_worktrees(self) -> None:
        if self._worktree_manager is None:
            return
        with suppress(Exception):
            self._worktree_manager.prune()

    def _remove_worktree_root(self) -> None:
        if self._worktree_manager is None:
            return
        shutil.rmtree(self._worktree_manager.worktrees_root, ignore_errors=True)

    async def _prepare_auto_tdd_retry(self, queue_state: dict[str, Any]) -> list[str]:
        """Queue a single TDD-first retry for every node that ended the run in FAILED.

        Reads ``.arc/runner-events.jsonl`` for ``test/failed`` requirement states,
        keeps only nodes whose current queue state is still ``FAILED``, resets them
        for an implement-only retry (design artefacts are preserved), and injects the
        TDD follow-up into each node session so ``TestDrivenDeveloper`` receives it as
        ``previous_failure_summary``. Returns the node ids queued for retry.
        """
        failures = scan_test_failures(self.runtime.paths.runner_events_path)
        eligible = [
            (node_id, message)
            for node_id, message in failures
            if node_state(queue_state, node_id) == NODE_FAILED
        ]
        if not eligible:
            return []

        retry_node_ids: list[str] = []
        for node_id, _message in eligible:
            try:
                plan = reset_node_for_retry(
                    queue_state, node_id, on_state_change=self._upsert_node_state
                )
            except ValueError as exc:
                await self._log(
                    "Compiler",
                    f"[tdd-retry] {node_id} stays failed without a retry: {exc}",
                    "warning",
                    node_id,
                )
                continue
            self._apply_reset_side_effects(node_id, plan)
            retry_node_ids.append(node_id)
        if not retry_node_ids:
            return []
        # The reset un-failed the retried nodes; dependents that propagation
        # blocked through them must return to schedulable state, or the second
        # drain ends "blocked" even after every retry succeeds (observed: the
        # 2026-09-20 test1 run passed REQ-1's retry yet REQ-2/ROOT stayed
        # BLOCKED_BY_DEPENDENCY and the run finished failed).
        await self._release_dependency_blocks(queue_state)
        self._save_processing_queue(queue_state)

        # Inject the follow-up AFTER the reset so it survives into the implement
        # phase, which reads recent_failure_summary as previous_failure_summary.
        for node_id, message in eligible:
            if node_id not in retry_node_ids:
                continue
            handoff = sessions.load_node_session(node_id).get("tdd_handoff") or {}
            reprompt = build_tdd_reprompt(node_id, message, handoff=handoff if isinstance(handoff, dict) else None)
            sessions.merge_node_session(
                node_id,
                {
                    "recent_failure_summary": reprompt,
                    "resume_context": {"tdd_reprompt": reprompt, "instruction": reprompt},
                },
            )
            await self._log(
                "Compiler",
                f"[tdd-retry] {node_id} reported test/failed during the run; re-prompting with a TDD-first follow-up.",
                status="warning",
                node_id=node_id,
            )
        return retry_node_ids

    @staticmethod
    def _auto_tdd_retry_enabled() -> bool:
        return os.environ.get("ARC_AUTO_TDD_RETRY", "1").strip().lower() not in {"0", "false", "no", "off"}

    def _load_or_create_processing_queue(
        self,
        requirement_tree: dict[str, Any],
        *,
        require_compatible_existing_queue: bool = False,
    ) -> dict[str, Any]:
        """Load (and migrate) or create the queue; see core.queue_state."""

        return load_or_create_queue(
            self.queue_path,
            requirement_tree,
            affinity_depth=self._affinity_depth,
            require_compatible_existing_queue=require_compatible_existing_queue,
        )

    @staticmethod
    def _drop_unschedulable_dependencies(
        dependencies: Any,
        queue_state: dict[str, Any],
    ) -> tuple[dict[str, list[str]], list[tuple[str, str, str]]]:
        """Delegate kept for the scheduling-rule tests; see core.queue_state."""

        return drop_unschedulable_dependencies(dependencies, queue_state)

    # Queue-shape builders and validators delegate to core.queue_state;
    # the delegates exist for the scheduling-rule tests and read naturally
    # on the manager.

    @staticmethod
    def _build_dependencies_map(root_node: dict[str, Any]) -> dict[str, list[str]]:
        return build_dependencies_map(root_node)

    @staticmethod
    def _structural_precedence_edges(
        parents: dict[str, str],
        node_ids: list[str],
    ) -> dict[str, set[str]]:
        return structural_precedence_edges(parents, node_ids)

    @staticmethod
    def _drop_ancestor_dependency_edges(
        dependencies: dict[str, list[str]],
        parents: dict[str, str],
    ) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
        return drop_ancestor_dependency_edges(dependencies, parents)

    @staticmethod
    def _break_dependency_cycles(
        dependencies: dict[str, list[str]],
        structural_edges: dict[str, set[str]] | None = None,
    ) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
        return break_dependency_cycles(dependencies, structural_edges)

    @staticmethod
    def _build_descendants_map(root_node: dict[str, Any]) -> dict[str, list[str]]:
        return build_descendants_map(root_node)

    @staticmethod
    def _build_parents_map(root_node: dict[str, Any]) -> dict[str, str]:
        return build_parents_map(root_node)

    @staticmethod
    def _build_affinity_map(root_node: dict[str, Any], split_depth: int = 1) -> dict[str, str]:
        return build_affinity_map(root_node, split_depth)

    @staticmethod
    def _task_affinity(node_id: str, queue_state: dict[str, Any]) -> str:
        affinity = queue_state.get("affinity") or {}
        return str(affinity.get(node_id, node_id))

    def _recover_interrupted_queue(self, queue_state: dict[str, Any]) -> list[dict[str, str]]:
        git_status = ""
        if self.runtime is not None:
            try:
                git_status = self.runtime.git.status_porcelain().strip()
            except Exception:
                git_status = ""
        return recover_interrupted(
            queue_state,
            git_status.splitlines(),
            on_state_change=self._upsert_node_state,
        )

    @staticmethod
    def _next_runnable_task(
        queue_state: dict[str, Any],
        in_flight: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any] | None:
        """Return the first PENDING task the queue's ordering allows to start.

        The flat order built by _build_processing_tasks encodes: a node's
        DESIGN precedes its IMPLEMENT, and children IMPLEMENT before their
        parent. With parallel draining, a task may additionally never start
        while another task for the same node is in flight, and (enforced in
        _task_dependencies_met) a DESIGN waits for its parent's DESIGN and
        for declared dependencies' IMPLEMENTs, and an IMPLEMENT waits for
        its descendants.
        """

        busy_nodes = {str(task.get("node_id", "")) for task in in_flight}
        for task in queue_state["tasks"]:
            if task_status(queue_state, task) != TASK_PENDING:
                continue
            if str(task.get("node_id", "")) in busy_nodes:
                continue
            if not ARCWorkflowManager._task_dependencies_met(queue_state, task):
                continue
            return task
        return None

    @staticmethod
    def _task_dependencies_met(queue_state: dict[str, Any], task: dict[str, Any]) -> bool:
        """Guard the ordering the queue relies on but never encoded as edges.

        A node's DESIGN waits for its parent's DESIGN (children design against
        the parent's merged shell, so a parent's rewrite of shared surfaces can
        never conflict with a child's additive edits in flight; a failed parent
        does not block its children, matching the failed-descendant rule
        below) and for the declared dependencies: by default the dependency's
        IMPLEMENT (an IMPLEMENT task completes only after its work is merged,
        so the dependent designs against the dependency's real surfaces
        (routes, session helpers) and reuses them instead of designing a
        duplicate - run7's parallel run had the login node write its own auth
        routes precisely because its design ran before the registration node's
        routes existed); with ``ARC_DESIGN_GATE_PIPELINE`` on, only the
        dependency's DESIGN (merged, so its registered interface cards are
        readable and the dependent designs incrementally against them; the
        drift this exposes is owned by the merge rails plus the contract
        drift check). A node's IMPLEMENT waits for its own DESIGN, for every
        descendant node's IMPLEMENT (children before their parent; a failed
        descendant keeps the parent IMPLEMENT from claiming completion over an
        incomplete subtree), and for the IMPLEMENT of every declared
        dependency: the node's scenarios routinely read runtime state
        (accounts, routes, orders) that only those nodes create, so
        implementing earlier turns a missing prerequisite into a false test
        failure. A failed dependency blocks both phases and is propagated as
        ``BLOCKED_BY_DEPENDENCY``; only a completed dependency exposes a
        verified reusable surface. Independent subtrees still drain in
        parallel. Sibling subtrees otherwise impose no order on each other,
        which is what makes parallel draining sound.
        """

        node_id = task["node_id"]
        phase = task["phase"]
        if phase == PHASE_DESIGN:
            parent_id = str((queue_state.get("parents") or {}).get(node_id, "") or "")
            if parent_id:
                parent_design_status = design_status_of(queue_state, parent_id)
                # A parents entry without a matching DESIGN task means the
                # queue is inconsistent with its own map (tasks are built
                # from the same tree, so this should be unreachable): block
                # instead of designing against an unknown baseline.
                if parent_design_status not in {TASK_COMPLETED, TASK_FAILED}:
                    return False
            return ARCWorkflowManager._declared_dependencies_satisfied(
                queue_state, node_id, phase=PHASE_DESIGN
            )
        if phase == PHASE_IMPLEMENT:
            own_design_status = design_status_of(queue_state, node_id)
            if own_design_status is not None and own_design_status != TASK_COMPLETED:
                return False
            if not ARCWorkflowManager._declared_dependencies_satisfied(queue_state, node_id):
                return False
            descendants = set(queue_state.get("descendants", {}).get(node_id, []))
            if not descendants:
                return True
            descendant_tasks = [
                other
                for other in queue_state["tasks"]
                if other["phase"] == PHASE_IMPLEMENT and other["node_id"] in descendants
            ]
            return all(task_status(queue_state, other) == TASK_COMPLETED for other in descendant_tasks)
        return True

    @staticmethod
    def _declared_dependencies_satisfied(
        queue_state: dict[str, Any],
        node_id: str,
        *,
        phase: str = PHASE_IMPLEMENT,
    ) -> bool:
        """True when every declared dependency has passed the required phase.

        Default (gate closed, and for IMPLEMENT tasks in every mode): the
        dependency's IMPLEMENT must have passed. An IMPLEMENT completes only
        after its work is merged, so satisfied dependencies mean the
        dependency's surfaces are already on the integration HEAD the task
        starts from. A failed dependency is not satisfied; the scheduler
        propagates an explicit blocked state to the dependent instead of
        designing or implementing against an unverified surface. A dependency
        with no IMPLEMENT task means the queue is inconsistent with the tree
        it was built from (restored maps are validated against this): block,
        matching the unknown-parent rule.

        Pipeline mode (``ARC_DESIGN_GATE_PIPELINE``, DESIGN tasks only): the
        dependency's DESIGN must have passed - it merged, so the registered
        interface cards are readable and the dependent designs against them.
        The same failed/unknown rules apply (a failed DESIGN never exposed a
        usable contract baseline; a dependency without a DESIGN task is an
        inconsistent queue). IMPLEMENT tasks keep the default rule.
        """

        pipelined = phase == PHASE_DESIGN and _design_pipelining_enabled()
        for dependency_id in (queue_state.get("dependencies") or {}).get(node_id, []):
            if pipelined:
                dependency_status = design_status_of(queue_state, dependency_id)
            else:
                dependency_status = implement_status_of(queue_state, dependency_id)
            if dependency_status != TASK_COMPLETED:
                return False
        return True

    def _apply_retry_plan(
        self,
        queue_state: dict[str, Any],
        *,
        retry_failed: bool = False,
        retry_node_ids: list[str] | None = None,
    ) -> list[str]:
        """Reset the requested nodes; returns the node ids queued for retry.

        The reset payloads and the retry-kind choice live in
        ``core.queue_state.reset_node_for_retry`` (issue #106: one
        implementation); this wrapper applies the runtime side effects the
        plans carry.
        """

        resets = apply_retry_plan(
            queue_state,
            retry_failed=retry_failed,
            retry_node_ids=retry_node_ids,
            on_state_change=self._upsert_node_state,
        )
        for node_id, plan in resets:
            self._apply_reset_side_effects(node_id, plan)
        return [node_id for node_id, _plan in resets]

    async def _run_task(self, task: dict[str, Any], ctx: "_TaskWorkspace | None" = None) -> bool:
        node_id = task["node_id"]
        requirement_data = self.runtime.traceability.get_requirement(node_id)
        if not requirement_data:
            await self._log("System", f"Requirement node {node_id} not found in database.", "error", node_id)
            return False
        runner = ctx.phase_runner if ctx is not None else self.phase_runner
        if task["phase"] == PHASE_DESIGN:
            return await runner.run_design_phase(node_id, requirement_data)
        return await runner.run_implement_phase(node_id, requirement_data)

    async def _commit_phase_checkpoint(self, node_id: str, phase: str, requirement_data: dict[str, Any]) -> None:
        commit_message = build_commit_message(node_id, phase, requirement_data)
        await self._log("Compiler", f"Running git checkpoint for {phase} on node {node_id}...", node_id=node_id)
        committed = self.runtime.git.commit(commit_message)
        if not committed:
            await self._log("Compiler", "No file changes detected for this checkpoint.", node_id=node_id)

    def _sync_queue_node_states(self, queue_state: dict[str, Any]) -> None:
        for node_id, state in queue_state.get("node_states", {}).items():
            normalized_state = str(state or NODE_UNSEEN).strip().upper() or NODE_UNSEEN
            self.runtime.traceability.upsert_node_state(node_id, normalized_state)

    @staticmethod
    def _build_compile_result(queue_state: dict[str, Any]) -> dict[str, Any]:
        failed_nodes = sorted(
            node_id for node_id, state in queue_state["node_states"].items() if state == NODE_FAILED
        )
        blocked_nodes = sorted(
            node_id
            for node_id, state in queue_state["node_states"].items()
            if state == NODE_BLOCKED_BY_DEPENDENCY
        )
        completed_tasks = [
            task["task_id"] for task in queue_state["tasks"] if task_status(queue_state, task) == TASK_COMPLETED
        ]
        all_completed = all(task_status(queue_state, task) == TASK_COMPLETED for task in queue_state["tasks"])
        pending_tasks = [
            task["task_id"]
            for task in queue_state["tasks"]
            if task_status(queue_state, task) not in {TASK_COMPLETED, TASK_FAILED, TASK_BLOCKED}
        ]
        accepted = all_completed and not failed_nodes and not blocked_nodes
        return {
            "ok": accepted,
            "status": "PASS" if accepted else "FAIL",
            "failed_nodes": failed_nodes,
            "blocked_nodes": blocked_nodes,
            "unvalidated_tasks": pending_tasks,
            "visit_order": completed_tasks,
            "states": dict(queue_state["node_states"]),
        }

    def _save_processing_queue(self, queue_state: dict[str, Any]) -> None:
        save_queue(queue_state, self.queue_path)

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        result = self.log_cb(agent_name, message, status, node_id)
        if hasattr(result, "__await__"):
            await result


class _CompletedLogAwaitable:
    def __await__(self):
        return iter(())


def _default_log_cb(
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> _CompletedLogAwaitable:
    append_debug_log(agent_name, message, status=status, node_id=node_id)
    write_terminal_log(agent_name, message, status=status, node_id=node_id)
    return _CompletedLogAwaitable()

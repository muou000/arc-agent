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
from core.service import configure_runtime
from core.commits import build_commit_message
from core.config import load_project_env, set_app_type, set_web_port, set_workspace_root
from core.files import load_requirements, read_json_file, validate_requirement_tree, write_json_file
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
    read_conflict_stages,
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

PHASE_DESIGN = "DESIGN"
PHASE_IMPLEMENT = "IMPLEMENT"

TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_COMPLETED = "COMPLETED"
TASK_FAILED = "FAILED"
TASK_BLOCKED = "BLOCKED"

NODE_UNSEEN = "UNSEEN"
NODE_DESIGNING = "DESIGNING"
NODE_DESIGNED = "DESIGNED"
NODE_IMPLEMENTING = "IMPLEMENTING"
NODE_PASSED = "PASSED"
NODE_CONVERGED = "CONVERGED"
NODE_CONVERGED_WITH_FAILED_CHILDREN = "CONVERGED_WITH_FAILED_CHILDREN"
NODE_FAILED = "FAILED"
NODE_BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"


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
            if other["status"] != TASK_PENDING:
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
            if task["status"] != TASK_PENDING:
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
        remaining ambiguous ``PENDING`` work at the end of the run.

        Only never-started ``PENDING`` work is marked, and a node with a
        ``RUNNING`` task is skipped until that task ends: rewriting the
        status of work that is already executing cannot stop it and would
        only corrupt the record. The invariant still holds at drain end,
        where nothing runs and every dependent of a failure is marked.
        """

        changed: list[tuple[str, list[str]]] = []
        while True:
            progress = False
            for node_id in list(queue_state.get("node_states", {})):
                blocked_by = self._failed_prerequisite_ids(queue_state, node_id)
                if not blocked_by:
                    continue
                node_tasks = [
                    task
                    for task in queue_state.get("tasks", [])
                    if str(task.get("node_id", "")) == node_id
                ]
                if any(task.get("status") == TASK_RUNNING for task in node_tasks):
                    continue
                pending_tasks = [
                    task for task in node_tasks if task.get("status") == TASK_PENDING
                ]
                if not pending_tasks:
                    continue
                for task in pending_tasks:
                    task["status"] = TASK_BLOCKED
                self._set_node_state(
                    queue_state["node_states"],
                    node_id,
                    NODE_BLOCKED_BY_DEPENDENCY,
                )
                changed.append((node_id, blocked_by))
                progress = True
            if not progress:
                break

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

    @staticmethod
    def _failed_prerequisite_ids(queue_state: dict[str, Any], node_id: str) -> list[str]:
        """Return failed declared dependencies and failed child work."""

        failed: list[str] = []
        for dependency_id in (queue_state.get("dependencies") or {}).get(node_id, []):
            dependency_state = ARCWorkflowManager._implement_status(queue_state, dependency_id)
            if dependency_state in {TASK_FAILED, TASK_BLOCKED}:
                failed.append(str(dependency_id))
        for descendant_id in (queue_state.get("descendants") or {}).get(node_id, []):
            descendant_state = ARCWorkflowManager._implement_status(queue_state, descendant_id)
            if descendant_state in {TASK_FAILED, TASK_BLOCKED}:
                failed.append(str(descendant_id))
        return failed

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
        fail again, so releasing is never unsafe. Like propagation, releasing
        iterates to a fixpoint because BLOCKED is transitive (A fails -> B
        blocked -> C blocked through B).
        """
        released: list[str] = []
        while True:
            progress = False
            for node_id in list(queue_state.get("node_states", {})):
                if (
                    str(queue_state["node_states"].get(node_id, "")).strip().upper()
                    != NODE_BLOCKED_BY_DEPENDENCY
                ):
                    continue
                if self._failed_prerequisite_ids(queue_state, node_id):
                    continue
                design_completed = any(
                    task.get("phase") == PHASE_DESIGN
                    and str(task.get("node_id", "")) == node_id
                    and task.get("status") == TASK_COMPLETED
                    for task in queue_state.get("tasks", [])
                )
                for task in queue_state.get("tasks", []):
                    if str(task.get("node_id", "")) == node_id and task.get("status") == TASK_BLOCKED:
                        task["status"] = TASK_PENDING
                self._set_node_state(
                    queue_state["node_states"],
                    node_id,
                    NODE_DESIGNED if design_completed else NODE_UNSEEN,
                )
                released.append(node_id)
                progress = True
            if not progress:
                break
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

    def _begin_task(self, task: dict[str, Any], queue_state: dict[str, Any]) -> None:
        node_id = task["node_id"]
        phase = task["phase"]
        task["status"] = TASK_RUNNING
        self._mark_task_running(queue_state["node_states"], node_id, phase)
        queue_state["last_task_id"] = task["task_id"]
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
                            await self._close_task_workspace(ctx)
                            return
                    elif (
                        merge_conflict
                        and phase == PHASE_IMPLEMENT
                        and self._merge_conflict_requeue_available(node_id, PHASE_IMPLEMENT)
                    ):
                        if await self._requeue_implement_after_merge_conflict(
                            ctx, queue_state, node_id, merge_conflict
                        ):
                            await self._close_task_workspace(ctx)
                            return
                    # Requeue declined (this phase's budget already spent, no
                    # requeue path for the phase, or the requeue itself
                    # failed): fall through to the failure branch. The method
                    # tail still closes ctx with preserve=True there, so a
                    # declined requeue never leaks the worktree - it is
                    # preserved for --retry exactly like any other failed
                    # merge.
                    task_ok = False

        if task_ok:
            task["status"] = TASK_COMPLETED
            sessions.merge_node_session(node_id, {"resume_context": {}})
            new_state = self._resolve_completed_node_state(node_id, phase)
            self._set_node_state(queue_state["node_states"], node_id, new_state)
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
            task["status"] = TASK_FAILED
            self._set_node_state(queue_state["node_states"], node_id, NODE_FAILED)
            self._mark_remaining_node_tasks_failed(queue_state, node_id)
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
            await self._close_task_workspace(ctx, preserve=not merged)

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
        runner = self._build_task_phase_runner(handle.path, web_port)
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

    def _build_task_phase_runner(self, workspace_path: str, web_port: int | None) -> WorkflowPhaseRunner:
        """Build adapters and an app handler rooted at the task's worktree.

        The adapters are per-task instances because they carry per-run state
        (TDD budget/verifier bookkeeping) that parallel tasks must not share.
        Traceability, node sessions and the context pipeline stay rooted in the
        main workspace via context_workspace_root/context_workspace_path.
        """

        common = dict(
            log_cb=self.log_cb,
            workspace_root=workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            context_workspace_root=self.workspace_path,
        )
        return WorkflowPhaseRunner(
            workspace_path=workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=InterfaceDesigner(**common),
            test_generator=TestGenerator(**common),
            test_driven_developer=TestDrivenDeveloper(**common),
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
        if not committed:
            await self._log("Compiler", "No file changes detected for this checkpoint.", node_id=node_id)
        await self._log("Compiler", f"Integrated {node_id}: {detail}.", node_id=node_id)
        if phase == PHASE_IMPLEMENT:
            await self._check_contract_drift(node_id)
        return True, detail, []

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
        for path in result.applied:
            self._worktree_manager._git(
                ["add", "--", path], cwd=self.workspace_path, check=False
            )
        remaining = detect_contract_drift(
            self.runtime.traceability.list_interfaces(req_id=node_id),
            workspace_root=self.workspace_path,
        )
        if remaining:
            return False
        # An accepted arbitration whose rewrite is byte-identical to what is
        # already on disk (the anchors were somehow already honored) leaves
        # nothing to commit: that is a successful repair, not a failure, so a
        # "nothing to commit" outcome counts as repaired.
        commit = self._worktree_manager._git(
            [
                "commit",
                "-m",
                f"contract drift arbitration: restore registered anchors of {node_id}",
            ],
            cwd=self.workspace_path,
            check=False,
        )
        if commit.returncode == 0:
            return True
        return "nothing to commit" in (commit.stdout + commit.stderr).lower()

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
            git = self._worktree_manager._git
            stages = read_conflict_stages(
                lambda args: git(args, cwd=self.workspace_path, check=False),
                conflict_paths,
            )
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
            for path in result.applied:
                self._worktree_manager._git(
                    ["add", "--", path], cwd=self.workspace_path, check=False
                )
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

    async def _requeue_design_after_merge_conflict(
        self,
        ctx: _TaskWorkspace,
        queue_state: dict[str, Any],
        node_id: str,
        conflict_paths: list[str],
    ) -> bool:
        """Re-queue a node's DESIGN once after a merge conflict.

        The conflicting files stay owned by the winning sibling (its branch
        is already merged), so the node's branch is reset to the current
        integration HEAD - the retry sees the sibling's files on disk and
        designs around them. The conflicting paths are stored in the node
        session for the DESIGN prompt; a second conflict (or an IMPLEMENT
        conflict) fails the node as before.

        Every decline path leaves the task workspace exactly as a regular
        conflict failure left it: quarantined, branch intact, preserved for
        ``--retry``. The queue is validated *before* the branch reset so a
        decline never discards the node's conflicted commits.
        """

        design_task = None
        implement_task = None
        for task in queue_state["tasks"]:
            if task["node_id"] != node_id:
                continue
            if task["phase"] == PHASE_DESIGN:
                design_task = task
            elif task["phase"] == PHASE_IMPLEMENT:
                implement_task = task
        if design_task is None or implement_task is None:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed: its queue tasks are incomplete.",
                "error",
                node_id,
            )
            return False

        try:
            await asyncio.to_thread(self._worktree_manager.reset_branch_to_integration, ctx.handle)
        except WorktreeError as exc:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed; the node fails instead: {exc}",
                "error",
                node_id,
            )
            return False

        design_task["status"] = TASK_PENDING
        implement_task["status"] = TASK_PENDING
        self.runtime.traceability.clear_node_design_artifacts(node_id)
        self.runtime.traceability.reset_test_pass_statuses_for_requirement(node_id)
        self._set_node_state(queue_state["node_states"], node_id, NODE_UNSEEN)
        sessions.merge_node_session(
            node_id,
            {
                "interfaces": [],
                "materialized_files": [],
                "test_artifacts": [],
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "resume_context": {},
                "result_state": "",
                "merge_conflict_context": {"paths": list(conflict_paths), "phase": "design"},
                "merge_conflict_retry_used": True,
            },
        )
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
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
        node's branch is reset to the current integration HEAD - the retry
        sees the sibling's files on disk and implements around them. DESIGN
        artifacts are NOT cleared: the design already merged cleanly and is
        part of the integration HEAD the retry starts from. The conflicting
        paths are stored in the node session for the TDD prompt; a second
        conflict fails the node as before.

        Every decline path leaves the task workspace exactly as a regular
        conflict failure left it: quarantined, branch intact, preserved for
        ``--retry``. The queue is validated *before* the branch reset so a
        decline never discards the node's conflicted commits.
        """

        design_task = None
        implement_task = None
        for task in queue_state["tasks"]:
            if task["node_id"] != node_id:
                continue
            if task["phase"] == PHASE_DESIGN:
                design_task = task
            elif task["phase"] == PHASE_IMPLEMENT:
                implement_task = task
        if design_task is None or implement_task is None:
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
        if design_task["status"] != TASK_COMPLETED:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed: its DESIGN task is not completed.",
                "error",
                node_id,
            )
            return False

        try:
            await asyncio.to_thread(self._worktree_manager.reset_branch_to_integration, ctx.handle)
        except WorktreeError as exc:
            await self._log(
                "Compiler",
                f"Re-queueing {node_id} after its merge conflict failed; the node fails instead: {exc}",
                "error",
                node_id,
            )
            return False

        implement_task["status"] = TASK_PENDING
        self.runtime.traceability.reset_test_pass_statuses_for_requirement(node_id)
        self._set_node_state(queue_state["node_states"], node_id, NODE_DESIGNED)
        sessions.merge_node_session(
            node_id,
            {
                "phase_status": {"implement": "pending"},
                "resume_context": {},
                "result_state": "",
                # The workspace now contains the winning sibling's files; the
                # DESIGN baseline states are stale for this pass, so
                # IMPLEMENT must re-baseline from scratch (same as a manual
                # implement retry).
                "design_baseline": {},
                "recent_failure_summary": "",
                "merge_conflict_context": {"paths": list(conflict_paths), "phase": "implement"},
                "merge_conflict_retry_used": True,
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)
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

    async def _close_task_workspace(self, ctx: _TaskWorkspace, *, preserve: bool = False) -> None:
        try:
            # Reusable (subtree) worktrees always survive their task: the next
            # task of the subtree reuses the directory, and the end-of-drain
            # cleanup removes it once the run no longer needs it.
            await asyncio.to_thread(
                self._worktree_manager.discard,
                ctx.handle,
                preserve=preserve or ctx.handle.reusable,
            )
        except Exception as exc:
            await self._log(
                "Compiler",
                f"Worktree cleanup for {ctx.node_id} failed: {type(exc).__name__}: {exc}",
                "warning",
                ctx.node_id,
            )
        finally:
            # Release the node's new-file claims: after a successful merge the
            # files are tracked in git (claims are moot), and after a terminal
            # failure the paths must be free for other nodes.
            get_file_claim_registry(self.workspace_path).release_node(ctx.node_id)
            self._release_port_slot(ctx.slot)

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
            if str(queue_state.get("node_states", {}).get(node_id, "") or "").strip().upper() == NODE_FAILED
        ]
        if not eligible:
            return []

        retry_node_ids: list[str] = []
        for node_id, _message in eligible:
            try:
                self._reset_node_for_retry(queue_state, node_id)
            except ValueError:
                continue
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
        os.makedirs(self.arc_dir, exist_ok=True)
        root_id = str(requirement_tree.get("id", ""))
        expected_tasks = self._build_processing_tasks(requirement_tree)
        expected_task_ids = [task["task_id"] for task in expected_tasks]
        node_ids = self._collect_node_ids(expected_tasks)
        descendants = self._build_descendants_map(requirement_tree)
        parents = self._build_parents_map(requirement_tree)
        affinity = self._build_affinity_map(requirement_tree, self._affinity_depth)
        declared = self._build_dependencies_map(requirement_tree)
        dependencies, ancestor_dropped = self._drop_ancestor_dependency_edges(declared, parents)
        dependencies, cycle_dropped = self._break_dependency_cycles(
            dependencies,
            self._structural_precedence_edges(parents, node_ids),
        )
        dropped_dependency_edges = [
            (dependent_id, dependency_id, "ancestor-descendant")
            for dependent_id, dependency_id in ancestor_dropped
        ] + [
            (dependent_id, dependency_id, "cycle")
            for dependent_id, dependency_id in cycle_dropped
        ]
        existing_queue = read_json_file(self.queue_path)
        if self._is_compatible_queue(existing_queue, root_id, expected_task_ids):
            queue_state = existing_queue
            queue_state.setdefault("node_states", {})
            for node_id in node_ids:
                queue_state["node_states"].setdefault(node_id, NODE_UNSEEN)
            # Queues saved before per-node worktree parallelism lack the map.
            queue_state.setdefault("descendants", descendants)
            # Queues saved before parent-serial DESIGN lack the map.
            queue_state.setdefault("parents", parents)
            # Queues saved before affinity-depth split lack a finer map; like
            # the dependencies map, a restored map is the durable contract -
            # in-flight work was grouped under it, and regrouping mid-run
            # would put one subtree's tasks in two groups' worktree files.
            queue_state.setdefault("affinity", affinity)
            # Queues saved before dependency gating lack the map. A restored map
            # is the durable contract, but it is not trusted blindly: an edge
            # that references a node this queue cannot schedule (a hand-edited
            # or foreign queue file) would block its dependent's IMPLEMENT
            # forever, and a foreign cycle would stall the drain; both are
            # dropped and reported instead.
            if "dependencies" in queue_state:
                restored, unschedulable = self._drop_unschedulable_dependencies(
                    queue_state["dependencies"], queue_state
                )
                structural = self._structural_precedence_edges(parents, node_ids)
                restored, restored_ancestors = self._drop_ancestor_dependency_edges(restored, parents)
                restored, restored_cycles = self._break_dependency_cycles(restored, structural)
                queue_state["dependencies"] = restored
                queue_state["dropped_dependency_edges"] = list(unschedulable) + [
                    (dependent_id, dependency_id, "ancestor-descendant")
                    for dependent_id, dependency_id in restored_ancestors
                ] + [
                    (dependent_id, dependency_id, "cycle")
                    for dependent_id, dependency_id in restored_cycles
                ]
            else:
                queue_state.setdefault("dependencies", dependencies)
                queue_state["dropped_dependency_edges"] = dropped_dependency_edges
            self._apply_saved_states_to_tasks(queue_state)
            return queue_state
        if require_compatible_existing_queue:
            raise ValueError(
                "Resume or retry requested, but the existing processing queue is missing or "
                "incompatible with the current requirement tree."
            )
        queue_state = {
            "root_id": root_id,
            "tasks": expected_tasks,
            "node_states": {node_id: NODE_UNSEEN for node_id in node_ids},
            "descendants": descendants,
            "parents": parents,
            "affinity": affinity,
            "dependencies": dependencies,
            "dropped_dependency_edges": dropped_dependency_edges,
            "last_task_id": None,
        }
        self._apply_saved_states_to_tasks(queue_state)
        return queue_state

    @staticmethod
    def _drop_unschedulable_dependencies(
        dependencies: Any,
        queue_state: dict[str, Any],
    ) -> tuple[dict[str, list[str]], list[tuple[str, str, str]]]:
        """Drop dependency edges this queue cannot schedule, with a reason.

        The IMPLEMENT gate blocks on an edge whose dependency has no IMPLEMENT
        task at all (matching the unknown-parent rule), so such an edge would
        leave the dependent PENDING forever. Edges are therefore validated
        against the queue's own task set: both endpoints must have an IMPLEMENT
        task here. The map is rebuilt rather than reused so a malformed value
        (null, wrong types, unknown ids) can only ever degrade to "no
        dependencies", never to a stalled drain.
        """

        implement_nodes = {
            str(task.get("node_id", ""))
            for task in queue_state.get("tasks", [])
            if task.get("phase") == PHASE_IMPLEMENT
        }
        if not isinstance(dependencies, dict):
            return {}, []
        kept: dict[str, list[str]] = {}
        dropped: list[tuple[str, str, str]] = []
        for dependent_id, dependency_ids in dependencies.items():
            dependent_id = str(dependent_id)
            if dependent_id not in implement_nodes:
                dropped.append((dependent_id, "", "no-implement-task"))
                continue
            if not isinstance(dependency_ids, list):
                dropped.append((dependent_id, "", "malformed-edges"))
                continue
            for dependency_id in dependency_ids:
                dependency_id = str(dependency_id)
                if dependency_id not in implement_nodes:
                    dropped.append((dependent_id, dependency_id, "no-implement-task"))
                    continue
                kept.setdefault(dependent_id, []).append(dependency_id)
        return kept, dropped

    @staticmethod
    def _build_dependencies_map(root_node: dict[str, Any]) -> dict[str, list[str]]:
        """Map every node id to the requirements it declares as dependencies.

        The declared ``dependencies`` list is authored data (ARC-Bench trees
        use it to model runtime prerequisites: a login node depends on the
        registration node that creates the account its scenarios use). Only
        edges that can participate in scheduling survive: ids must exist in
        this tree and self-references are dropped, so a malformed edge can
        never stall the drain on a node that is not in the queue.
        """

        declared_by_node: dict[str, list[str]] = {}

        def collect(node: dict[str, Any]) -> None:
            node_id = str(node.get("id") or "").strip()
            if node_id:
                declared = node.get("dependencies")
                values = [
                    str(item or "").strip()
                    for item in (declared if isinstance(declared, list) else [])
                ]
                declared_by_node[node_id] = [value for value in values if value]
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    collect(child)

        collect(root_node)
        dependencies: dict[str, list[str]] = {}
        for node_id, declared in declared_by_node.items():
            kept = [
                dependency_id
                for dependency_id in declared
                if dependency_id != node_id and dependency_id in declared_by_node
            ]
            kept = list(dict.fromkeys(kept))
            if kept:
                dependencies[node_id] = kept
        return dependencies

    @staticmethod
    def _structural_precedence_edges(
        parents: dict[str, str],
        node_ids: list[str],
    ) -> dict[str, set[str]]:
        """The ordering the queue already enforces, as phase-vertex edges.

        Vertices are ``D:<node>`` and ``I:<node>`` (a node's DESIGN and
        IMPLEMENT tasks). The queue always runs a node's DESIGN before its
        IMPLEMENT, a child's DESIGN after its parent's DESIGN, and a parent's
        IMPLEMENT after its descendants' IMPLEMENTs; those rules are edges
        here so declared dependencies can be checked against them.
        """

        edges: dict[str, set[str]] = {}
        for node_id in node_ids:
            edges.setdefault(f"D:{node_id}", set()).add(f"I:{node_id}")
        for child_id, parent_id in parents.items():
            edges.setdefault(f"D:{parent_id}", set()).add(f"D:{child_id}")
            edges.setdefault(f"I:{child_id}", set()).add(f"I:{parent_id}")
        return edges

    @staticmethod
    def _drop_ancestor_dependency_edges(
        dependencies: dict[str, list[str]],
        parents: dict[str, str],
    ) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
        """Drop declared edges between an ancestor and its own descendant.

        The parent-child rules already sequence such a pair (the child's
        DESIGN waits for the ancestor's DESIGN, the ancestor's IMPLEMENT
        waits for the descendant's), and the dependency gate adds the
        reverse wait, so either direction of the edge makes the pair wait on
        itself and deadlocks the drain. The edge schedules nothing beyond
        those rules, so it is dropped here with its own reason instead of
        surfacing as an anonymous cycle later.

        The ancestry is derived here from ``parents`` (immediate parent per
        node, the shape _build_parents_map guarantees) rather than accepted
        as a precomputed descendants map: walking the parent chain per node
        cannot misclassify a grandparent<->grandchild edge even if a future
        map shape changes, so the classification is structural instead of a
        convention callers must uphold.
        """

        def has_ancestor(node_id: str, candidate_id: str) -> bool:
            parent_id = str((parents or {}).get(node_id, "") or "")
            while parent_id:
                if parent_id == candidate_id:
                    return True
                parent_id = str((parents or {}).get(parent_id, "") or "")
            return False

        kept: dict[str, list[str]] = {}
        dropped: list[tuple[str, str]] = []
        for dependent_id, dependency_ids in dependencies.items():
            for dependency_id in dependency_ids:
                if has_ancestor(dependent_id, dependency_id) or has_ancestor(dependency_id, dependent_id):
                    dropped.append((dependent_id, dependency_id))
                    continue
                kept.setdefault(dependent_id, []).append(dependency_id)
        return kept, dropped

    @staticmethod
    def _break_dependency_cycles(
        dependencies: dict[str, list[str]],
        structural_edges: dict[str, set[str]] | None = None,
    ) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
        """Drop dependency edges that close a cycle, keeping an acyclic graph.

        The gate blocks a node's DESIGN and IMPLEMENT until its dependencies'
        IMPLEMENTs end, so a cycle would leave every node in it permanently
        unrunnable and the drain would end with PENDING tasks instead of a
        reported failure. A declared edge means "the dependency's IMPLEMENT
        precedes the dependent's DESIGN" (``I:<dependency>`` before
        ``D:<dependent>``); ``structural_edges`` (from
        _structural_precedence_edges) adds the precedence the queue enforces
        on its own, so an edge that closes a cycle *through those rules* -
        e.g. a node depending on a sibling that depends on one of its
        children - is caught too, not just pure declared cycles. Without
        structural edges the check degenerates to the historical node-level
        graph over the declared edges alone.

        Edges are visited one at a time in map order (which follows the tree
        walk) and an edge is dropped when its target can already reach its
        source through the edges accepted so far. Both properties that matter
        hold at every visit, including forward edges whose cycle is only
        completed by later edges: (1) a dropped edge always closes a cycle in
        the original graph, because the accepted edges are a subset of it, and
        (2) every cycle loses an edge, because its last edge in visit order
        finds all its other edges accepted. The structural edges are acyclic
        by construction (design vertices precede implement vertices, ancestors
        design first, descendants implement first), so every cycle contains a
        declared edge and the pass above is enough. Without ``structural_edges``
        the pass still seeds each map node's inherent ``D:N -> I:N`` edge, which
        makes the phase graph equivalent to the historical node-level graph: a
        phase cycle must alternate declared ``I:dep -> D:dependent`` edges with
        ``D:N -> I:N`` edges, so the two cycle notions coincide. Which edge of
        a cycle is dropped follows the tree order and is reported to the
        caller; the result is therefore deterministic for a given tree, never
        partially applied.
        """

        adjacency: dict[str, set[str]] = {}
        for source, targets in (structural_edges or {}).items():
            adjacency.setdefault(source, set()).update(targets)
        if structural_edges is None:
            # Degenerate mode (no tree context): seed only the inherent
            # design-before-implement edges so declared cycles still close.
            for node_id in {str(key) for key in dependencies} | {
                str(value)
                for values in dependencies.values()
                for value in values
            }:
                adjacency.setdefault(f"D:{node_id}", set()).add(f"I:{node_id}")

        def reaches(start: str, goal: str, seen: set[str]) -> bool:
            if start == goal:
                return True
            if start in seen:
                return False
            seen.add(start)
            for next_id in adjacency.get(start, ()):
                if reaches(next_id, goal, seen):
                    return True
            return False

        kept: dict[str, list[str]] = {}
        dropped: list[tuple[str, str]] = []
        for dependent_id, dependency_ids in dependencies.items():
            for dependency_id in dependency_ids:
                source, target = f"I:{dependency_id}", f"D:{dependent_id}"
                if reaches(target, source, set()):
                    dropped.append((dependent_id, dependency_id))
                    continue
                kept.setdefault(dependent_id, []).append(dependency_id)
                adjacency.setdefault(source, set()).add(target)
        return kept, dropped

    @staticmethod
    def _build_descendants_map(root_node: dict[str, Any]) -> dict[str, list[str]]:
        """Map every node id to all of its transitive child ids."""

        descendants: dict[str, list[str]] = {}

        def walk(node: dict[str, Any], ancestors: list[str]) -> None:
            node_id = str(node.get("id", "")).strip()
            if not node_id:
                return
            for ancestor in ancestors:
                descendants.setdefault(ancestor, []).append(node_id)
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    walk(child, [*ancestors, node_id])

        walk(root_node, [])
        return descendants

    @staticmethod
    def _build_parents_map(root_node: dict[str, Any]) -> dict[str, str]:
        """Map every node id to its immediate parent id (the root has none)."""

        parents: dict[str, str] = {}

        def walk(node: dict[str, Any], parent_id: str) -> None:
            node_id = str(node.get("id", "")).strip()
            if not node_id:
                return
            if parent_id:
                parents[node_id] = parent_id
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    walk(child, node_id)

        walk(root_node, "")
        return parents

    @staticmethod
    def _build_affinity_map(root_node: dict[str, Any], split_depth: int = 1) -> dict[str, str]:
        """Map every node id to the subtree group it shares a worktree with.

        Tasks of one group run sequentially in the group's reusable worktree,
        so a parent's and its children's design phases never race on shared
        skeleton files; different groups drain in parallel. The root itself
        forms its own group.

        ``split_depth`` bounds how deep a top-level subtree stays one group:
        depth 1 is the historical top-level-subtree grouping; a deeper split
        gives each descendant subtree at that depth (e.g. feature subtrees
        under a wide parent) its own group so siblings can drain in parallel.
        Nodes deeper than ``split_depth`` inherit their ancestor's group, so a
        group boundary is always a whole subtree, never a node subset.
        """

        affinity: dict[str, str] = {}
        root_id = str(root_node.get("id", "")).strip()
        if root_id:
            affinity[root_id] = root_id

        def walk(node: dict[str, Any], group: str, depth: int) -> None:
            node_id = str(node.get("id", "")).strip()
            if not node_id:
                return
            # A node at depth <= split_depth heads its own group; deeper nodes
            # inherit the boundary ancestor's group, so a group is always a
            # whole subtree, never a node subset.
            node_group = node_id if depth <= split_depth else group
            affinity[node_id] = node_group
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    walk(child, node_group, depth + 1)

        for child in root_node.get("children", []) or []:
            if isinstance(child, dict):
                child_id = str(child.get("id", "")).strip()
                if child_id:
                    walk(child, child_id, 1)
        return affinity

    @staticmethod
    def _task_affinity(node_id: str, queue_state: dict[str, Any]) -> str:
        affinity = queue_state.get("affinity") or {}
        return str(affinity.get(node_id, node_id))

    def _build_processing_tasks(self, root_node: dict[str, Any]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []

        def walk(node: dict[str, Any]) -> None:
            node_id = str(node.get("id", "")).strip()
            if not node_id:
                return
            tasks.append(self._make_task(node_id, PHASE_DESIGN, len(tasks)))
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    walk(child)
            tasks.append(self._make_task(node_id, PHASE_IMPLEMENT, len(tasks)))

        walk(root_node)
        return tasks

    def _make_task(self, node_id: str, phase: str, order: int) -> dict[str, Any]:
        return {
            "task_id": f"{node_id}:{phase}",
            "node_id": node_id,
            "phase": phase,
            "order": order,
            "status": TASK_PENDING,
        }

    @staticmethod
    def _collect_node_ids(tasks: list[dict[str, Any]]) -> list[str]:
        seen: list[str] = []
        for task in tasks:
            node_id = task["node_id"]
            if node_id not in seen:
                seen.append(node_id)
        return seen

    @staticmethod
    def _is_compatible_queue(queue_state: dict[str, Any] | None, root_id: str, expected_task_ids: list[str]) -> bool:
        if not queue_state or queue_state.get("root_id") != root_id:
            return False
        return [task.get("task_id") for task in queue_state.get("tasks", [])] == expected_task_ids

    @staticmethod
    def _apply_saved_states_to_tasks(queue_state: dict[str, Any]) -> None:
        for task in queue_state["tasks"]:
            node_state = queue_state["node_states"].get(task["node_id"], NODE_UNSEEN)
            if node_state in {NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN} or (
                node_state == NODE_DESIGNED and task["phase"] == PHASE_DESIGN
            ):
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_FAILED:
                task["status"] = TASK_FAILED
            elif node_state == NODE_BLOCKED_BY_DEPENDENCY:
                task["status"] = TASK_BLOCKED

    def _recover_interrupted_queue(self, queue_state: dict[str, Any]) -> list[dict[str, str]]:
        recovered: list[dict[str, str]] = []
        git_status = ""
        if self.runtime is not None:
            try:
                git_status = self.runtime.git.status_porcelain().strip()
            except Exception:
                git_status = ""
        for task in queue_state["tasks"]:
            if task["status"] == TASK_RUNNING:
                node_id = str(task.get("node_id", "") or "").strip()
                phase = str(task.get("phase", "") or "").strip()
                previous_state = str(queue_state.get("node_states", {}).get(node_id, NODE_UNSEEN) or NODE_UNSEEN)
                task["status"] = TASK_PENDING
                fallback_state = NODE_DESIGNED if phase == PHASE_IMPLEMENT else NODE_UNSEEN
                if node_id:
                    queue_state["node_states"][node_id] = fallback_state
                    if self.runtime is not None:
                        self.runtime.traceability.upsert_node_state(node_id, fallback_state)
                    sessions.merge_node_session(
                        node_id,
                        {
                            "resume_context": {
                                "interrupted": True,
                                "task_id": str(task.get("task_id", "") or "").strip(),
                                "phase": phase,
                                "previous_node_state": previous_state,
                                "recovered_node_state": fallback_state,
                                "git_status": git_status.splitlines()[:80],
                                "instruction": (
                                    "This node is resuming after an interrupted agent stage. "
                                    "Preserve useful existing source, test, and traceability artifacts; inspect the listed dirty files "
                                    "and current-node records before regenerating or overwriting work."
                                ),
                            },
                            "phase_status": {phase.lower(): "interrupted"} if phase else {},
                        },
                    )
                recovered.append({"node_id": node_id, "phase": phase, "task_id": str(task.get("task_id", ""))})
        queue_state["recovered_interrupted_tasks"] = recovered
        return recovered

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
            if task["status"] != TASK_PENDING:
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
                parent_design_status: str | None = None
                for other in queue_state["tasks"]:
                    if other["phase"] == PHASE_DESIGN and other["node_id"] == parent_id:
                        parent_design_status = str(other.get("status", ""))
                        break
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
            for other in queue_state["tasks"]:
                if other["phase"] == PHASE_DESIGN and other["node_id"] == node_id:
                    if other["status"] != TASK_COMPLETED:
                        return False
                    break
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
            return all(other["status"] == TASK_COMPLETED for other in descendant_tasks)
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
                dependency_status = ARCWorkflowManager._design_status(queue_state, dependency_id)
            else:
                dependency_status = ARCWorkflowManager._implement_status(queue_state, dependency_id)
            if dependency_status != TASK_COMPLETED:
                return False
        return True

    @staticmethod
    def _design_status(queue_state: dict[str, Any], node_id: str) -> str | None:
        """Status of a node's DESIGN task, or None when the queue has none."""

        for other in queue_state["tasks"]:
            if other["phase"] == PHASE_DESIGN and other["node_id"] == node_id:
                return str(other.get("status", ""))
        return None

    @staticmethod
    def _implement_status(queue_state: dict[str, Any], node_id: str) -> str | None:
        """Status of a node's IMPLEMENT task, or None when the queue has none."""

        for other in queue_state["tasks"]:
            if other["phase"] == PHASE_IMPLEMENT and other["node_id"] == node_id:
                return str(other.get("status", ""))
        return None

    @staticmethod
    def _mark_remaining_node_tasks_failed(queue_state: dict[str, Any], node_id: str) -> None:
        for task in queue_state["tasks"]:
            if task["node_id"] == node_id and task["status"] in {TASK_PENDING, TASK_RUNNING}:
                task["status"] = TASK_FAILED

    def _apply_retry_plan(
        self,
        queue_state: dict[str, Any],
        *,
        retry_failed: bool = False,
        retry_node_ids: list[str] | None = None,
    ) -> list[str]:
        requested_ids: list[str] = []
        if retry_failed:
            requested_ids = [
                node_id
                for node_id, state in queue_state.get("node_states", {}).items()
                if str(state or "").strip().upper() in {NODE_FAILED, NODE_BLOCKED_BY_DEPENDENCY}
            ]
        elif retry_node_ids:
            requested_ids = [str(node_id).strip() for node_id in retry_node_ids if str(node_id).strip()]

        requested_ids = list(dict.fromkeys(requested_ids))
        if not requested_ids:
            return []

        known_ids = set(queue_state.get("node_states", {}).keys())
        unknown_ids = [node_id for node_id in requested_ids if node_id not in known_ids]
        if unknown_ids:
            raise ValueError(f"Retry requested for unknown node id(s): {', '.join(unknown_ids)}")

        for node_id in requested_ids:
            self._reset_node_for_retry(queue_state, node_id)

        queue_state["last_task_id"] = None
        queue_state["retry_plan"] = {"requested_node_ids": requested_ids, "retry_failed": bool(retry_failed)}
        return requested_ids

    def _reset_node_for_retry(self, queue_state: dict[str, Any], node_id: str) -> str:
        design_task = None
        implement_task = None
        for task in queue_state["tasks"]:
            if task["node_id"] != node_id:
                continue
            if task["phase"] == PHASE_DESIGN:
                design_task = task
            elif task["phase"] == PHASE_IMPLEMENT:
                implement_task = task
        if design_task is None or implement_task is None:
            raise ValueError(f"Retry requested for node {node_id}, but its queue tasks are incomplete.")

        design_status = str(design_task.get("status") or "").strip().upper()
        implement_status = str(implement_task.get("status") or "").strip().upper()
        design_failed = design_status == TASK_FAILED
        implement_failed = implement_status in {TASK_FAILED, TASK_BLOCKED}

        if design_failed or design_status != TASK_COMPLETED:
            self._reset_node_from_design_retry(queue_state, node_id, design_task, implement_task)
            return "design"

        if implement_failed:
            self._reset_node_from_implement_retry(queue_state, node_id, design_task, implement_task)
            return "implement"

        self._reset_node_for_full_retry(queue_state, node_id, design_task, implement_task)
        return "design"

    def _reset_node_from_design_retry(
        self,
        queue_state: dict[str, Any],
        node_id: str,
        design_task: dict[str, Any],
        implement_task: dict[str, Any],
    ) -> None:
        design_task["status"] = TASK_PENDING
        implement_task["status"] = TASK_PENDING
        self.runtime.traceability.clear_node_design_artifacts(node_id)
        self.runtime.traceability.reset_test_pass_statuses_for_requirement(node_id)
        self._set_node_state(queue_state["node_states"], node_id, NODE_UNSEEN)
        sessions.merge_node_session(
            node_id,
            {
                "interfaces": [],
                "materialized_files": [],
                "test_artifacts": [],
                "recent_failure_summary": "",
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "resume_context": {},
                "result_state": "",
                # Fresh DESIGN pass: the baseline gate will rebuild the
                # per-file states from the new manifest.
                "design_baseline": {},
                # A manual retry is a fresh DESIGN pass: restore the node's
                # one-shot conflict retry budget and drop stale conflict
                # paths so the prompt is not misdirected (None replaces the
                # dict wholesale; deep-merge would keep a {} patch intact).
                "merge_conflict_context": None,
                "merge_conflict_retry_used": False,
            },
        )
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)

    def _reset_node_from_implement_retry(
        self,
        queue_state: dict[str, Any],
        node_id: str,
        design_task: dict[str, Any],
        implement_task: dict[str, Any],
    ) -> None:
        design_task["status"] = TASK_COMPLETED
        implement_task["status"] = TASK_PENDING
        self.runtime.traceability.reset_test_pass_statuses_for_requirement(node_id)
        self._set_node_state(queue_state["node_states"], node_id, NODE_DESIGNED)
        sessions.merge_node_session(
            node_id,
            {
                "phase_status": {"implement": "pending"},
                "resume_context": {},
                "result_state": "",
                # The workspace now contains the node's own landed
                # implementation; the DESIGN baseline states are stale for
                # this pass, so IMPLEMENT must re-baseline from scratch.
                "design_baseline": {},
                # A manual retry is a fresh IMPLEMENT pass: restore the
                # one-shot conflict retry budget and drop stale conflict
                # paths so the prompt is not misdirected (same contract as
                # the DESIGN-side reset; None replaces the dict wholesale).
                "merge_conflict_context": None,
                "merge_conflict_retry_used": False,
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)

    def _reset_node_for_full_retry(
        self,
        queue_state: dict[str, Any],
        node_id: str,
        design_task: dict[str, Any],
        implement_task: dict[str, Any],
    ) -> None:
        design_task["status"] = TASK_PENDING
        implement_task["status"] = TASK_PENDING
        self._set_node_state(queue_state["node_states"], node_id, NODE_UNSEEN)
        sessions.merge_node_session(
            node_id,
            {
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "resume_context": {},
                "result_state": "",
                "recent_failure_summary": "",
                # Fresh DESIGN pass: the baseline gate will rebuild the
                # per-file states from the new manifest.
                "design_baseline": {},
                # Fresh DESIGN pass: restore the conflict-retry budget and
                # drop stale conflict paths (see _reset_node_from_design_retry).
                "merge_conflict_context": None,
                "merge_conflict_retry_used": False,
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)

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

    def _resolve_completed_node_state(self, node_id: str, phase: str) -> str:
        if phase == PHASE_DESIGN:
            return NODE_DESIGNED
        session = read_json_file(os.path.join(self.arc_dir, "node_sessions", f"{node_id}.json")) or {}
        result_state = str(session.get("result_state", "")).strip().upper()
        if result_state == NODE_CONVERGED_WITH_FAILED_CHILDREN:
            return NODE_CONVERGED_WITH_FAILED_CHILDREN
        if result_state == NODE_CONVERGED:
            return NODE_CONVERGED
        return NODE_PASSED

    def _set_node_state(self, node_states: dict[str, str], node_id: str, state: str) -> None:
        node_states[node_id] = state
        self.runtime.traceability.upsert_node_state(node_id, state)

    def _sync_queue_node_states(self, queue_state: dict[str, Any]) -> None:
        for node_id, state in queue_state.get("node_states", {}).items():
            normalized_state = str(state or NODE_UNSEEN).strip().upper() or NODE_UNSEEN
            self.runtime.traceability.upsert_node_state(node_id, normalized_state)

    def _mark_task_running(self, node_states: dict[str, str], node_id: str, phase: str) -> None:
        if phase == PHASE_DESIGN:
            self._set_node_state(node_states, node_id, NODE_DESIGNING)
            self.runtime.events.mark_design_started(node_id)
            return
        self._set_node_state(node_states, node_id, NODE_IMPLEMENTING)
        self.runtime.events.mark_implementation_started(node_id)

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
        completed_tasks = [task["task_id"] for task in queue_state["tasks"] if task["status"] == TASK_COMPLETED]
        all_completed = all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])
        pending_tasks = [
            task["task_id"]
            for task in queue_state["tasks"]
            if task["status"] not in {TASK_COMPLETED, TASK_FAILED, TASK_BLOCKED}
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
        write_json_file(self.queue_path, queue_state)

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

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from agents.interface_designer import InterfaceDesigner
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from app_type_handler import create_app_type_handler, normalize_app_type
from agents.context.pipeline import context_pipeline
from core import commits, config, files, sessions
from core.phases import WorkflowPhaseRunner
from core.service import configure_runtime
from core.commits import build_commit_message
from core.config import load_project_env, set_app_type, set_web_port, set_workspace_root
from core.files import load_requirements, read_json_file, validate_requirement_tree, write_json_file
from core.logging import append_debug_log, write_terminal_log
from core.path_safety import validate_clean_target
from core.tdd_retry import build_tdd_reprompt, scan_test_failures
from core.visual_analysis import precompute_visual_references, visual_precompute_enabled
from core.worktree import MergeConflictError, NodeWorktreeManager, WorktreeError, WorktreeHandle


load_project_env()

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

QUEUE_FILENAME = "processing_queue.json"

# Historical mode: every task runs against the one shared workspace in queue
# order, because stage agents, git checkpoints (`git add .`) and test runners
# (one web port, one E2E database) would otherwise interfere with each other.
# With ARC_NODE_WORKTREES=1 each in-flight task instead gets its own git
# worktree, web port slot and worktree-local E2E database, so up to
# ARC_MAX_CONCURRENT_TASKS (capped at MAX_PARALLEL_TASKS) tasks may run at
# once. Sibling nodes normally touch disjoint files, so their branches merge
# back cleanly; a merge conflict fails the node with an explicit reason and
# preserves its worktree for inspection.
DEFAULT_MAX_CONCURRENT_TASKS = 1
MAX_PARALLEL_TASKS = 8

PHASE_DESIGN = "DESIGN"
PHASE_IMPLEMENT = "IMPLEMENT"

TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_COMPLETED = "COMPLETED"
TASK_FAILED = "FAILED"

NODE_UNSEEN = "UNSEEN"
NODE_DESIGNING = "DESIGNING"
NODE_DESIGNED = "DESIGNED"
NODE_IMPLEMENTING = "IMPLEMENTING"
NODE_PASSED = "PASSED"
NODE_CONVERGED = "CONVERGED"
NODE_CONVERGED_WITH_FAILED_CHILDREN = "CONVERGED_WITH_FAILED_CHILDREN"
NODE_FAILED = "FAILED"


def _worktrees_enabled() -> bool:
    raw = os.environ.get("ARC_NODE_WORKTREES", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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

        # Per-node worktree parallelism (opt-in via ARC_NODE_WORKTREES=1).
        self._parallel_mode = _worktrees_enabled()
        self._worktree_manager = (
            NodeWorktreeManager(self.workspace_path) if self._parallel_mode else None
        )
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
            await self._log(
                "Compiler",
                f"Compilation finished with {len(failed_nodes)} failed node(s): {', '.join(failed_nodes)}",
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
        node's DESIGN precedes its IMPLEMENT, and an IMPLEMENT waits for every
        descendant node's IMPLEMENT (children before their parent); sibling
        subtrees are independent and may overlap.
        """

        max_concurrency = self._max_concurrent_tasks()
        if max_concurrency <= 1:
            while True:
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
                    task = self._next_runnable_task(queue_state, in_flight.values())
                    if task is None:
                        break
                    self._begin_task(task, queue_state)
                    in_flight[asyncio.create_task(self._execute_task(task, queue_state))] = task
                if not in_flight:
                    break
                await asyncio.wait(set(in_flight), return_when=asyncio.FIRST_COMPLETED)
                for finished in [pending for pending in in_flight if pending.done()]:
                    in_flight.pop(finished, None)
                    # _execute_task turns phase failures into task state, so an
                    # exception escaping here can only be a scheduler bug.
                    finished.result()
        finally:
            # Cancellation or an escaping scheduler exception must not leave
            # child tasks mutating shared queue state after the drain exits.
            for pending in in_flight:
                if not pending.done():
                    pending.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    def _max_concurrent_tasks(self) -> int:
        if not self._parallel_mode:
            return DEFAULT_MAX_CONCURRENT_TASKS
        raw = os.environ.get("ARC_MAX_CONCURRENT_TASKS", "").strip()
        try:
            value = int(raw)
        except ValueError:
            return 1
        return min(max(1, value), MAX_PARALLEL_TASKS)

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
                ctx = await self._open_task_workspace(task)
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
        if ctx is not None:
            # Commit the worktree and merge its branch back; a merge conflict
            # fails the node even when its phase succeeded, because the work
            # never reached the integration workspace.
            merged, _detail = await self._integrate_task_workspace(
                ctx,
                node_id,
                phase if task_ok else f"{phase}-FAILED",
                requirement_data,
            )
            if task_ok and not merged:
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
            else:
                self.runtime.events.mark_implementation_failed(node_id)
                self.runtime.events.mark_test_failed(node_id)
            if ctx is None:
                await self._commit_phase_checkpoint(node_id, f"{phase}-FAILED", requirement_data)
            await self._log("Compiler", f"{phase} failed for node {node_id}.", "error", node_id)

        if ctx is not None:
            await self._close_task_workspace(ctx, preserve=not merged)

    async def _open_task_workspace(self, task: dict[str, Any]) -> _TaskWorkspace:
        """Create the task's isolated worktree, port slot and phase runner."""

        node_id = task["node_id"]
        slot = self._acquire_port_slot(node_id)
        try:
            handle = await asyncio.to_thread(self._worktree_manager.prepare, node_id)
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
        runner = WorkflowPhaseRunner(
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
        return runner

    async def _integrate_task_workspace(
        self,
        ctx: _TaskWorkspace,
        node_id: str,
        phase: str,
        requirement_data: dict[str, Any],
    ) -> tuple[bool, str]:
        commit_message = build_commit_message(node_id, phase, requirement_data)
        async with self._merge_lock:
            try:
                committed, detail = await asyncio.to_thread(
                    self._worktree_manager.integrate,
                    ctx.handle,
                    commit_message,
                )
            except MergeConflictError as exc:
                await self._log("Compiler", str(exc), "error", node_id)
                return False, str(exc)
            except WorktreeError as exc:
                await self._log("Compiler", f"Integration of {node_id} failed: {exc}", "error", node_id)
                return False, str(exc)
        if not committed:
            await self._log("Compiler", "No file changes detected for this checkpoint.", node_id=node_id)
        await self._log("Compiler", f"Integrated {node_id}: {detail}.", node_id=node_id)
        return True, detail

    async def _close_task_workspace(self, ctx: _TaskWorkspace, *, preserve: bool = False) -> None:
        try:
            await asyncio.to_thread(self._worktree_manager.discard, ctx.handle, preserve=preserve)
        except Exception as exc:
            await self._log(
                "Compiler",
                f"Worktree cleanup for {ctx.node_id} failed: {type(exc).__name__}: {exc}",
                "warning",
                ctx.node_id,
            )
        finally:
            self._release_port_slot(ctx.slot)

    def _acquire_port_slot(self, node_id: str) -> int:
        for slot in range(self._port_slot_count):
            if slot not in self._port_slots:
                self._port_slots[slot] = node_id
                return slot
        # Should not happen: the drain caps in-flight tasks at the slot count.
        self._port_slots[max(self._port_slots, default=-1) + 1] = node_id
        return max(self._port_slots) if self._port_slots else -1

    def _release_port_slot(self, slot: int) -> None:
        self._port_slots.pop(slot, None)

    def _slot_port(self, slot: int) -> int | None:
        if slot < 0:
            return None
        return self.web_port + 1 + slot

    def _prune_worktrees(self) -> None:
        if self._worktree_manager is None:
            return
        try:
            self._worktree_manager.prune()
        except Exception:
            # Pruning is advisory; a stale registration only makes prepare()
            # fall back to removing the leftover directory itself.
            pass

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
        self._save_processing_queue(queue_state)

        # Inject the follow-up AFTER the reset so it survives into the implement
        # phase, which reads recent_failure_summary as previous_failure_summary.
        for node_id, message in eligible:
            if node_id not in retry_node_ids:
                continue
            reprompt = build_tdd_reprompt(node_id, message)
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
        existing_queue = read_json_file(self.queue_path)
        if self._is_compatible_queue(existing_queue, root_id, expected_task_ids):
            queue_state = existing_queue
            queue_state.setdefault("node_states", {})
            for node_id in node_ids:
                queue_state["node_states"].setdefault(node_id, NODE_UNSEEN)
            # Queues saved before per-node worktree parallelism lack the map.
            queue_state.setdefault("descendants", descendants)
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
            "last_task_id": None,
        }
        self._apply_saved_states_to_tasks(queue_state)
        return queue_state

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
            if node_state in {NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN}:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_DESIGNED and task["phase"] == PHASE_DESIGN:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_FAILED:
                task["status"] = TASK_FAILED

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
        _task_dependencies_met) an IMPLEMENT waits for its descendants.
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

        A node's IMPLEMENT waits for its own DESIGN and for every descendant
        node's IMPLEMENT (children before their parent; a failed descendant
        does not block its parent, matching the historical rule that an
        earlier failed IMPLEMENT does not either). Sibling subtrees impose no
        order on each other, which is what makes parallel draining sound.
        """

        if task["phase"] != PHASE_IMPLEMENT:
            return True
        node_id = task["node_id"]
        for other in queue_state["tasks"]:
            if other["phase"] == PHASE_DESIGN and other["node_id"] == node_id:
                if other["status"] != TASK_COMPLETED:
                    return False
                break
        descendants = set(queue_state.get("descendants", {}).get(node_id, []))
        if not descendants:
            return True
        descendant_tasks = [
            other
            for other in queue_state["tasks"]
            if other["phase"] == PHASE_IMPLEMENT and other["node_id"] in descendants
        ]
        return all(
            other["status"] in {TASK_COMPLETED, TASK_FAILED}
            for other in descendant_tasks
        )

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
                if str(state or "").strip().upper() == NODE_FAILED
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
        implement_failed = implement_status == TASK_FAILED

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
        completed_tasks = [task["task_id"] for task in queue_state["tasks"] if task["status"] == TASK_COMPLETED]
        all_completed = all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])
        return {
            "ok": all_completed and not failed_nodes,
            "failed_nodes": failed_nodes,
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
        if False:
            yield None
        return None


def _default_log_cb(
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> _CompletedLogAwaitable:
    append_debug_log(agent_name, message, status=status, node_id=node_id)
    write_terminal_log(agent_name, message, status=status, node_id=node_id)
    return _CompletedLogAwaitable()

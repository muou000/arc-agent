from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

from app_type_handler import create_app_type_handler
from agents.context.pipeline import context_pipeline
from agents.tools.test_contract_check import (
    build_satisfiability_universe,
    classify_test_hooks,
    collect_manifest_hooks,
)
from core import sessions
from core.service import get_runtime
from core.path_compat import normalize_workspace_relative_path
from core.test_executor import (
    TDD_BATCH_ORDER,
    TDD_RUN_TESTS_BUDGET,
    TDD_STALL_THRESHOLD,
    TddTestExecutor,
    collect_test_files,
)
# Re-exported for the executor contract tests (canonical layer vocabulary).
from core.test_types import canonical_test_type  # noqa: F401
from core.visual_analysis import analyze_and_attach_visual_references
from app_type_handler.test_results import TestRunResult
from agents.runtime.capabilities import is_test_file_path
from agents.tools.test_manifest import normalize_coverage_scope


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
ALLOWED_INTERFACE_TYPES = {"UI", "API", "FUNC", "DB"}
#: Rejection rounds a TestGenerator pass gets to clear its green baseline
#: files (delete or rework) before the DESIGN phase hard-fails.
DESIGN_BASELINE_MAX_REJECTIONS = 2
#: Cap on run-output log files retained per node under ``.arc/tdd_runs``.
TDD_RUN_LOG_RETENTION = 20

class WorkflowPhaseRunner:
    """Run ARC DESIGN and IMPLEMENT phases using the agent adapters."""

    def __init__(
        self,
        *,
        workspace_path: str,
        requirement_path: str,
        app_type: str,
        interface_designer: Any,
        test_generator: Any,
        test_driven_developer: Any,
        log_cb: LogCallback | None = None,
        web_port: int | None = None,
        context_workspace_path: str | None = None,
    ) -> None:
        self.workspace_path = str(Path(workspace_path).expanduser().resolve())
        self.requirement_path = requirement_path
        self.app_type = app_type
        self.interface_designer = interface_designer
        self.test_generator = test_generator
        self.test_driven_developer = test_driven_developer
        self.log_cb = log_cb
        # Per-task test port override (per-node worktree parallelism); None
        # keeps the process-wide configured port.
        self.web_port = web_port
        # Shared-workspace root for traceability-adjacent reads (visual cache),
        # distinct from workspace_path when this runner works in a worktree.
        self.context_workspace_path = context_workspace_path or self.workspace_path
        self.app_handler = create_app_type_handler(
            app_type=app_type,
            workspace_path=self.workspace_path,
            requirement_path=requirement_path,
            interface_designer=interface_designer,
            log_cb=self._log,
        )
        self.test_driven_developer.app_handler = self.app_handler

    @property
    def traceability(self):
        return get_runtime().traceability

    @property
    def events(self):
        return get_runtime().events

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        requirement_data = await analyze_and_attach_visual_references(
            workspace_path=self.context_workspace_path,
            requirements_dir=str(Path(self.requirement_path).expanduser().resolve().parent),
            requirement_data=requirement_data,
            log_cb=self._log,
        )
        requirement_data = self.traceability.get_requirement(node_id) or requirement_data
        # The leaf/non-leaf split must use the traceability record's
        # children_ids, not a caller's possibly-partial requirement snapshot.
        is_non_leaf = bool(requirement_data.get("children_ids"))
        visual_reference = requirement_data.get("visual_reference") or []
        self._update_node_session(
            node_id,
            {
                "node_id": node_id,
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "requirement_snapshot": {
                    "name": requirement_data.get("name", ""),
                    "description": requirement_data.get("description", ""),
                    "visual_reference": requirement_data.get("visual_reference") or [],
                    "children_ids": requirement_data.get("children_ids") or [],
                    "dependencies": requirement_data.get("dependencies") or [],
                },
                "recent_failure_summary": "",
            },
        )

        if is_non_leaf and not visual_reference:
            self.traceability.clear_node_design_artifacts(node_id)
            context_pipeline.cache.invalidate_file_layers(node_id)
            context_pipeline.cache.invalidate_db_layers(node_id)
            self._update_node_session(
                node_id,
                {
                    "interfaces": [],
                    "materialized_files": [],
                    "test_artifacts": [],
                    "phase_status": {"design": "skipped", "test": "skipped"},
                },
            )
            await self._log(
                "InterfaceDesigner",
                "Skipping non-leaf DESIGN because this node has no visual reference; no UI/API/FUNC/DB interfaces are owned here.",
                status="info",
                node_id=node_id,
            )
            await self._log(
                "TestGenerator",
                "Skipping test generation for non-leaf node.",
                status="info",
                node_id=node_id,
            )
            return True

        await self._log("InterfaceDesigner", "Running interface design.", node_id=node_id)
        interface_result = await self.interface_designer.run(
            node_id=node_id,
            requirement_data=requirement_data,
        )
        interfaces = []
        for item in interface_result.get("interfaces", []):
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized["file_path"] = normalize_workspace_relative_path(normalized.get("file_path"), self.workspace_path)
            interfaces.append(normalized)
        files_written = []
        for path in interface_result.get("files_written") or []:
            normalized_path = normalize_workspace_relative_path(path, self.workspace_path)
            if normalized_path:
                files_written.append(normalized_path)
        if not interfaces:
            materialized_paths = [
                str(path).strip()
                for path in interface_result.get("materialized_paths") or []
                if str(path).strip()
            ]
            if materialized_paths:
                # Hard gate: the design pass materialized skeleton files but
                # recorded no interface contracts. An empty traceability
                # interface registry leaves TestGenerator and TDD blind to the
                # design (and has historically deadlocked them), so fail the
                # DESIGN phase instead of proceeding silently.
                await self._log(
                    "InterfaceDesigner",
                    "DESIGN failed: "
                    + f"{len(materialized_paths)} skeleton file(s) were materialized (e.g. {materialized_paths[0]}) "
                    + "but the response recorded no interface contracts; downstream stages would be blind to the design.",
                    status="error",
                    node_id=node_id,
                )
                return False
            if not is_non_leaf:
                # Hard gate: a leaf DESIGN pass that records no interface
                # contracts and materializes no files has produced nothing
                # downstream stages can anchor to. The observed failure shape
                # is the reuse shortcut - the pass claims parent/dependency
                # contracts in `summary` prose and returns an empty
                # `interfaces` array - which only surfaced one stage later as
                # a confusing TestGenerator ownership failure after the whole
                # tree had waited on this node. The way out is to return the
                # reused interfaces with their original interface_id.
                await self._log(
                    "InterfaceDesigner",
                    "DESIGN failed: the leaf node recorded no interface contracts and "
                    "materialized no files, so TestGenerator and TDD have no contract to "
                    "anchor to. Reused parent/dependency interfaces must still be returned "
                    "in `interfaces` with their original interface_id; summary prose alone "
                    "does not attach the node to a contract.",
                    status="error",
                    node_id=node_id,
                )
                return False
            await self._log(
                "InterfaceDesigner",
                "Interface design returned no current-node owned interface definitions.",
                status="warning",
                node_id=node_id,
            )

        try:
            prepared_interfaces = self._prepare_interfaces(node_id, interfaces)
        except ValueError as exc:
            await self._log("InterfaceDesigner", str(exc), status="error", node_id=node_id)
            return False
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "materialized_files": files_written,
                "phase_status": {"design": "prepared"},
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)

        stored_tests: list[dict[str, Any]] = []
        if is_non_leaf:
            self.traceability.clear_node_design_artifacts(node_id)
            self._store_prepared_interfaces(node_id, prepared_interfaces)
            context_pipeline.cache.invalidate_file_layers(node_id)
            context_pipeline.cache.invalidate_db_layers(node_id)
            self._update_node_session(
                node_id,
                {
                    "interfaces": prepared_interfaces,
                    "test_artifacts": [],
                    "phase_status": {"design": "completed", "test": "skipped"},
                },
            )
            await self._log(
                "InterfaceDesigner",
                f"Stored {len(prepared_interfaces)} interface definition(s) into traceability DB.",
                node_id=node_id,
            )
            await self._log(
                "InterfaceDesigner",
                f"Interface artifact summary: {json.dumps(summarize_interface_artifacts(prepared_interfaces), ensure_ascii=False)}",
                node_id=node_id,
            )
            await self._log(
                "TestGenerator",
                "Skipping test generation for non-leaf node; composition nodes only define interfaces.",
                status="info",
                node_id=node_id,
            )
            return True

        await self._log(
            "InterfaceDesigner",
            f"Prepared {len(prepared_interfaces)} interface definition(s) for traceability storage.",
            node_id=node_id,
        )
        await self._log(
            "InterfaceDesigner",
            f"Interface artifact summary: {json.dumps(summarize_interface_artifacts(prepared_interfaces), ensure_ascii=False)}",
            node_id=node_id,
        )

        await self._log("TestGenerator", "Generating tests from agent-selected coverage strategy.", node_id=node_id)
        tests, _ = await self.test_generator.run(
            node_id=node_id,
            requirement_data=requirement_data,
        )
        if tests is None:
            await self._log(
                "TestGenerator",
                "DESIGN test generation did not return a valid test manifest.",
                status="error",
                node_id=node_id,
            )
            return False

        try:
            stored_tests = self._prepare_tests(node_id=node_id, tests=tests)
        except ValueError as exc:
            await self._log("TestGenerator", str(exc), status="error", node_id=node_id)
            return False

        owned_interface_ids = {
            str(item.get("interface_id") or "").strip()
            for item in prepared_interfaces
            if str(item.get("interface_id") or "").strip()
        }
        foreign_owned_tests = [
            str(item.get("file_path") or "").strip()
            for item in stored_tests
            if normalize_coverage_scope(item.get("coverage_scope")) == "owned"
            and normalize_string_list(item.get("interface_ids"))
            and not (
                set(normalize_string_list(item.get("interface_ids"))) & owned_interface_ids
            )
        ]
        if foreign_owned_tests:
            await self._log(
                "TestGenerator",
                (
                    "DESIGN failed: `owned` test coverage does not point to any "
                    "interface owned by the current node: "
                    + ", ".join(foreign_owned_tests)
                    + ". If the current node's DESIGN recorded no owned interface "
                    "contracts, inspect the interface design output first."
                ),
                status="error",
                node_id=node_id,
            )
            return False

        # Static satisfiability check, before any baseline run spends real
        # test executions: extract the observable hooks the E2E/Integration
        # tests drive and classify them against the requirement + interface
        # specs. Hooks the sources never name become declared test-contract
        # hooks (node session -> TDD context); this is what turns the REQ-1
        # dual-blind selector mismatch into an explicit handoff. Fail-open:
        # a checker error logs and continues rather than failing DESIGN.
        try:
            test_contract_hooks = await self._check_test_contract_satisfiability(
                node_id=node_id,
                requirement_data=requirement_data,
                tests=stored_tests,
            )
        except Exception as exc:
            test_contract_hooks = []
            await self._log(
                "TestGenerator",
                f"Static satisfiability check failed ({exc}); continuing without test-contract hooks.",
                status="warning",
                node_id=node_id,
            )
        # Always (re)write the hook list: a retry design pass with zero
        # test-contract hooks must not leave the previous run's stale hooks
        # in the session.
        self._update_node_session(node_id, {"test_contract_hooks": test_contract_hooks})

        baseline = await self._enforce_design_baseline_red(
            node_id=node_id,
            requirement_data=requirement_data,
            prepared_tests=stored_tests,
            owned_interface_ids=owned_interface_ids,
        )
        # The E2E baseline runs may have started the session-scoped backend
        # runtime; DESIGN must not leave it holding the task's port (the
        # same lifecycle rule run_implement_phase applies at its end).
        await self.app_handler.shutdown_e2e_runtime()
        if baseline is None:
            # The gate hard-failed (green files survived every rejection
            # round, or the repair pass broke the manifest); run_design_phase
            # must not store the rejected artifacts.
            return False
        if baseline.get("revised_tests") is not None:
            stored_tests = baseline["revised_tests"]

        self.traceability.clear_node_design_artifacts(node_id)
        self._store_prepared_interfaces(node_id, prepared_interfaces)
        self._store_prepared_tests(stored_tests)
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "test_artifacts": stored_tests,
                "phase_status": {"design": "completed", "test": "completed"},
                "design_baseline": baseline["file_state"],
            },
        )
        await self._log(
            "InterfaceDesigner",
            f"Stored {len(prepared_interfaces)} interface definition(s) into traceability DB.",
            node_id=node_id,
        )
        await self._log(
            "TestGenerator",
            f"Stored {len(stored_tests)} test mapping item(s) into traceability DB.",
            node_id=node_id,
        )
        await self._log(
            "TestGenerator",
            f"Test artifact summary: {json.dumps(summarize_test_artifacts(stored_tests), ensure_ascii=False)}",
            node_id=node_id,
        )
        return True

    async def run_implement_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_non_leaf = bool(requirement_data.get("children_ids"))
        if is_non_leaf:
            interfaces = self.traceability.list_interfaces(req_id=node_id)
            self._mark_interfaces_implemented(interfaces)
            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "completed"},
                    "result_state": "CONVERGED",
                },
            )
            await self._log(
                "TestDrivenDeveloper",
                "Non-leaf node completed directly after interface materialization; no TDD batch was scheduled.",
                node_id=node_id,
            )
            return True

        del requirement_data
        self._update_node_session(node_id, {"phase_status": {"implement": "in_progress"}})
        interfaces = self.traceability.list_interfaces(req_id=node_id)
        tests = self.traceability.list_tests(req_id=node_id)
        if not tests:
            await self._log(
                "TestDrivenDeveloper",
                "No node-local tests were registered; skipping TDD implementation for this node.",
                node_id=node_id,
            )
            self._mark_interfaces_implemented(interfaces)
            self._update_node_session(node_id, {"phase_status": {"implement": "completed"}})
            return True

        final_ok = False
        try:
            final_ok = await self._run_tdd_for_node(
                node_id=node_id,
                tests=tests,
            )
        finally:
            # The session-scoped E2E runtime (web) deliberately outlives single
            # run_tests calls; the end of the IMPLEMENT phase is the point it
            # must not outlive, so the per-task port is freed for reuse.
            await self.app_handler.shutdown_e2e_runtime()
        if final_ok:
            self._mark_interfaces_implemented(interfaces)
            await self._sweep_undeclared_test_files(node_id, tests)
        self._update_node_session(
            node_id,
            {"phase_status": {"implement": "completed" if final_ok else "failed"}},
        )
        return final_ok

    async def _sweep_undeclared_test_files(self, node_id: str, tests: list[dict[str, Any]]) -> None:
        """Move undeclared test files out of the delivery tree before the checkpoint.

        The simple-ticketing arc-output1 run showed why the IMPLEMENT
        checkpoint needs this sweep in addition to the discipline's
        budgeted delete: a TDD agent that could not delete its render-probe
        diagnostics (``frontend/tests/diag.test.tsx``,
        ``RegisterPage.diag.test.tsx``, ``backend/test-express-wildcard.js``)
        left them in the tree, and the ``git add -A`` checkpoint shipped them
        into the delivery commit. The discipline now allows the agent to
        clean them up itself; this sweep is the mechanical backstop for the
        files it still leaves behind.

        An undeclared test file is one that lives under a test directory or
        carries a test name, is untracked in git (so an edit to a *registered*
        test file, or a pre-existing sibling test the manifest never owned,
        never matches), and is not in the current node's test manifest. On
        match the file is moved under ``.arc/diagnostics/<node>/`` — ignored
        by the managed gitignore, kept for inspection — instead of deleted.
        Movement failures (locks, permission errors) log a warning and leave
        the file in place: a dirty delivery commit is recoverable, a crashed
        IMPLEMENT phase is not.
        """

        try:
            candidates = collect_undeclared_test_files(
                self.workspace_path,
                declared_paths=collect_test_files(tests),
            )
            if not candidates:
                return
            safe_node = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "node"
            diagnostics_dir = Path(self.workspace_path) / ".arc" / "diagnostics" / safe_node
            moved: list[str] = []
            move_error: Exception | None = None
            for relative in candidates:
                source = Path(self.workspace_path) / relative
                target = diagnostics_dir / relative
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                    moved.append(relative)
                except OSError as exc:
                    # Record the partial sweep and keep the remaining files in
                    # place; a dirty delivery commit is recoverable, a crash
                    # here must not fail the phase.
                    move_error = exc
                    break
            if moved:
                self._update_node_session(node_id, {"swept_diagnostic_files": sorted(moved)})
                await self._log(
                    "TestDrivenDeveloper",
                    (
                        f"Moved {len(moved)} undeclared test file(s) out of the delivery tree for "
                        f"{node_id}: {', '.join(moved)}. They are preserved under "
                        "`.arc/diagnostics/` (ignored by checkpoints) for inspection."
                        + (
                            f" (Stopped after a move failure: {move_error}; any remaining "
                            "diagnostic files will stay in the test tree.)"
                            if move_error is not None
                            else ""
                        )
                    ),
                    status="warning",
                    node_id=node_id,
                )
        except Exception as exc:
            await self._log(
                "TestDrivenDeveloper",
                (
                    f"Undeclared-test-file sweep failed for {node_id} ({type(exc).__name__}: {exc}); "
                    "the checkpoint will include any diagnostic files left in the test tree."
                ),
                status="warning",
                node_id=node_id,
            )

    async def _check_test_contract_satisfiability(
        self,
        *,
        node_id: str,
        requirement_data: dict[str, Any],
        tests: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Classify the E2E/Integration tests' observable hooks (mechanical).

        Returns the ``test_contract`` hooks — values the tests drive that
        neither the requirement text nor the interface specs name. They are
        stored in the node session and surface to TestDrivenDeveloper as a
        context block; the implementer aligns to them instead of discovering
        them one Playwright timeout at a time. Grounded hooks need no
        handoff: the implementer already receives those source texts.
        """

        interfaces = sessions.load_node_session(node_id).get("interfaces") or []
        hooks = collect_manifest_hooks(self.workspace_path, tests)
        if not hooks:
            return []
        universe = build_satisfiability_universe(requirement_data, interfaces)
        result = classify_test_hooks(hooks, universe)
        test_contract = result["test_contract"]
        if test_contract:
            summary = ", ".join(
                f"{hook['kind']}=`{hook['value']}`" for hook in test_contract[:8]
            )
            suffix = f" (+{len(test_contract) - 8} more)" if len(test_contract) > 8 else ""
            await self._log(
                "TestGenerator",
                (
                    f"Static satisfiability check: {len(result['grounded'])}/{len(hooks)} hook(s) grounded "
                    f"in the requirement/interface specs; {len(test_contract)} are test-defined contract "
                    f"hooks handed to the implementation: {summary}{suffix}."
                ),
                status="warning",
                node_id=node_id,
            )
        else:
            await self._log(
                "TestGenerator",
                f"Static satisfiability check: all {len(result['grounded'])}/{len(hooks)} hook(s) grounded in the requirement/interface specs.",
                node_id=node_id,
            )
        return test_contract

    async def _enforce_design_baseline_red(
        self,
        *,
        node_id: str,
        requirement_data: dict[str, Any],
        prepared_tests: list[dict[str, Any]],
        owned_interface_ids: set[str],
    ) -> dict[str, Any] | None:
        """System-run baseline gate over the freshly generated manifest.

        Current-node behavior is governed by an ownership witness: at least
        one ``owned`` test must be genuinely RED before IMPLEMENT. Tests
        explicitly marked ``dependency`` or ``shared`` are regression checks;
        they may already be green and are recorded as exempt evidence instead
        of forcing a rejection round. This keeps baseline validation strict
        for new behavior without requiring inherited/shared coverage to be
        artificially broken.

        Returns ``None`` when the gate hard-fails, otherwise a dict with:
        - ``file_state``: ``{file_path: "red" | "green" | None}`` seeds for
          the IMPLEMENT baseline (``None`` = environmental failure);
        - ``revised_tests``: the final manifest when a rejection round
          changed it, else ``None``.

        Two legitimate-green situations do not reject: an empty manifest
        (nothing to gate) and a node whose git history already contains an
        ``implement`` checkpoint (a retry designing over its own landed
        implementation; recording the state without rejection is the only
        truthful signal, and the IMPLEMENT tautology fast path is the
        intended outcome there).
        """
        del requirement_data
        # DESIGN gate as an executor adapter: baseline runs and the
        # red/green/unverified classification flow through the same
        # TddTestExecutor primitives the IMPLEMENT loop uses (the gate never
        # opens agent sessions, so budgets/advancement stay untouched here).
        executor = TddTestExecutor(
            node_id=node_id,
            workspace_path=self.workspace_path,
            run_group=self._run_test_group,
        )
        layer_files: list[tuple[str, str, str]] = []
        for test_type in TDD_BATCH_ORDER:
            layer_items = [
                item
                for item in prepared_tests
                if str(item.get("type", "")).strip().lower() == test_type.lower()
            ]
            items_by_path = {
                str(item.get("file_path", "") or "").strip(): item
                for item in layer_items
                if str(item.get("file_path", "") or "").strip()
            }
            for path in collect_test_files(layer_items):
                item = items_by_path.get(path, {})
                layer_files.append(
                    (test_type, path, normalize_coverage_scope(item.get("coverage_scope")) or "owned")
                )
        if not layer_files:
            return {"file_state": {}, "revised_tests": None, "skipped": "no tests"}

        # The anchor relies on core.commits.build_commit_message, which
        # prefixes every checkpoint with "<node_id> (<phase>):" — a coupling
        # tests/test_workflow/test_design_baseline_red_gate.py locks in.
        prior_implementation = False
        try:
            git_log = get_runtime().git.run(["log", "--oneline", "--all", "-i", "--grep", "(implement", "--"], check=False)
            # --oneline lines are "<short-sha> <commit message>"; the commit
            # builder starts every message with the node id, so anchor the
            # match to the message start to keep REQ-1 from matching REQ-10.
            message_prefix = f"{node_id} (implement"
            prior_implementation = any(
                line.split(" ", 1)[-1].startswith(message_prefix) if " " in line else False
                for line in (git_log.stdout or "").splitlines()
            )
        except Exception as exc:
            # A git failure must not silently degrade a full-retry node's
            # legitimate-green path into a rejection spiral; make the
            # degraded mode visible in the run log.
            prior_implementation = False
            await self._log(
                "TestGenerator",
                (
                    f"Prior-implementation git check failed ({exc}); assuming no prior "
                    "implement checkpoint for this node and applying the green-baseline "
                    "rejection rules."
                ),
                status="warning",
                node_id=node_id,
            )

        current_tests = prepared_tests
        manifest_revised = False
        file_state: dict[str, str | None] = {}
        green_evidence: list[dict[str, Any]] = []
        exempt_green_evidence: list[dict[str, Any]] = []
        owned_paths = {
            path
            for _test_type, path, scope in layer_files
            if scope == "owned"
        }

        for test_type, path, scope in layer_files:
            baseline = await executor.run_baseline_file(test_type, path)
            if baseline.state == "green":
                file_state[path] = "green"
                evidence = {
                    "file_path": path,
                    "type": test_type,
                    "coverage_scope": scope,
                    "output_summary": summarize_batch_output(baseline.output, max_lines=4),
                }
                (green_evidence if scope == "owned" else exempt_green_evidence).append(evidence)
                continue
            baseline_env = baseline.environment_failure
            file_state[path] = baseline.state
            if baseline_env:
                # The file could not be verified either way (broken workspace,
                # missing runner). The DESIGN gate only rejects verified-green
                # files; environmental failures are handed to IMPLEMENT, whose
                # per-layer baseline re-runs every None-state file and applies
                # the repair-and-revalidate contract there.
                await self._log(
                    "TestGenerator",
                    (
                        f"Baseline RED check `{test_type}` {path} could not run for an "
                        f"environmental reason ({baseline_env}); leaving it unverified - "
                        "the IMPLEMENT baseline will re-run it under the environment "
                        "repair contract."
                    ),
                    status="warning",
                    node_id=node_id,
                )

        if not green_evidence and any(state is None for state in file_state.values()):
            # Nothing was verified green, but not everything is provably red
            # either: the manifest leaves DESIGN with unverified files. Make
            # that visible; the gate itself must not fail the node over an
            # environment problem.
            await self._log(
                "TestGenerator",
                (
                    "Baseline RED check ended with no verified-green files, but "
                    f"{sum(1 for state in file_state.values() if state is None)} file(s) "
                    "could not be verified for environmental reasons; they stay unverified "
                    "and the IMPLEMENT baseline owns them."
                ),
                status="warning",
                node_id=node_id,
            )

        if exempt_green_evidence:
            await self._log(
                "TestGenerator",
                (
                    f"Baseline recorded {len(exempt_green_evidence)} pre-existing green "
                    "dependency/shared test file(s) as exempt coverage: "
                    + ", ".join(item["file_path"] for item in exempt_green_evidence)
                ),
                status="info",
                node_id=node_id,
            )

        if owned_interface_ids and not owned_paths:
            await self._log(
                "TestGenerator",
                (
                    "DESIGN failed: the node owns interface contract(s) but the test "
                    "manifest contains no `owned` coverage witness. Dependency/shared "
                    "regression tests cannot substitute for current-node behavior."
                ),
                status="error",
                node_id=node_id,
            )
            return None

        if prior_implementation:
            await self._log(
                "TestGenerator",
                (
                    f"Baseline RED check skipped rejection: git history contains an implement "
                    f"checkpoint for {node_id}; {len(green_evidence)} green file(s) recorded as "
                    "legitimate (behavior already landed by a previous run)."
                    if green_evidence
                    else f"Baseline RED check complete; no green files (implement checkpoint present for {node_id})."
                ),
                status="warning",
                node_id=node_id,
            )
            return {"file_state": file_state, "revised_tests": None, "prior_implementation": True}

        rejection_round = 0
        while green_evidence:
            rejection_round += 1
            if rejection_round > DESIGN_BASELINE_MAX_REJECTIONS:
                green_paths = ", ".join(item["file_path"] for item in green_evidence)
                await self._log(
                    "TestGenerator",
                    (
                        f"DESIGN failed: {len(green_evidence)} test file(s) still pass the baseline "
                        f"after {DESIGN_BASELINE_MAX_REJECTIONS} rejection round(s): {green_paths}. "
                        "Green tests against an unimplemented node cannot verify the requirement."
                    ),
                    status="error",
                    node_id=node_id,
                )
                return None
            await self._log(
                "TestGenerator",
                (
                    f"Green baseline rejection round {rejection_round}/{DESIGN_BASELINE_MAX_REJECTIONS}: "
                    f"{len(green_evidence)} test file(s) pass before implementation "
                    f"({', '.join(item['file_path'] for item in green_evidence)})."
                ),
                status="warning",
                node_id=node_id,
            )
            revised_tests, _ = await self.test_generator.repair_green_baseline(
                node_id,
                {"name": "", "description": ""},
                green_evidence=green_evidence,
                previous_manifest=current_tests,
            )
            if revised_tests is None:
                await self._log(
                    "TestGenerator",
                    "Green baseline rework did not return a valid test manifest.",
                    status="error",
                    node_id=node_id,
                )
                return None
            try:
                current_tests = self._prepare_tests(node_id=node_id, tests=revised_tests)
                manifest_revised = True
            except ValueError as exc:
                await self._log("TestGenerator", str(exc), status="error", node_id=node_id)
                return None
            if not revised_tests and not current_tests:
                # The repair explicitly returned an empty manifest: every
                # test was tautological and got deleted. An empty manifest
                # is a valid DESIGN result (the node owns no local tests).
                break
            if not current_tests:
                # The repair claimed tests but every item was dropped by
                # manifest validation; treating that as a legitimate empty
                # manifest would silently strip the node's coverage.
                await self._log(
                    "TestGenerator",
                    "Green baseline rework returned only invalid manifest item(s).",
                    status="error",
                    node_id=node_id,
                )
                return None

            # Re-baseline everything that must prove itself RED this round:
            # the files that carried the green evidence into this repair
            # round AND any file the repair newly introduced. The second set
            # closes the rename escape: a repair that deletes the green path
            # and re-adds the same tautology under a new path must not slip
            # through just because the old evidence path left the manifest.
            survived: set[str] = set()
            for evidence in green_evidence:
                if any(item.get("file_path") == evidence["file_path"] for item in current_tests):
                    survived.add(evidence["file_path"])
            original_paths = {str(item.get("file_path", "") or "").strip() for item in prepared_tests}
            for item in current_tests:
                path = str(item.get("file_path", "") or "").strip()
                if path and path not in original_paths:
                    survived.add(path)
                    file_state.pop(path, None)
            recheck_paths = sorted(survived)
            if recheck_paths:
                await self._log(
                    "TestGenerator",
                    (
                        f"Re-baselining {len(recheck_paths)} file(s) after the rework round: "
                        f"{', '.join(recheck_paths)}."
                    ),
                    node_id=node_id,
                )
            green_evidence = []
            for path in recheck_paths:
                test_type = next(
                    str(item.get("type", "")).strip()
                    for item in current_tests
                    if item.get("file_path") == path
                )
                scope = next(
                    normalize_coverage_scope(item.get("coverage_scope")) or "owned"
                    for item in current_tests
                    if item.get("file_path") == path
                )
                baseline = await executor.run_baseline_file(test_type, path)
                if baseline.state == "green":
                    file_state[path] = "green"
                    evidence = {
                        "file_path": path,
                        "type": test_type,
                        "coverage_scope": scope,
                        "output_summary": summarize_batch_output(baseline.output, max_lines=4),
                    }
                    (green_evidence if scope == "owned" else exempt_green_evidence).append(evidence)
                else:
                    # Same semantics as the first pass: an environmental
                    # failure leaves the file unverified for IMPLEMENT's
                    # baseline instead of asserting a RED it cannot prove.
                    file_state[path] = baseline.state

        # Drop states for files a repair round removed from the manifest;
        # they are no longer this node's tests, and a stale "green" would
        # poison nothing but the log (the IMPLEMENT seeding only reads
        # registered files), while a stale entry lying around the session
        # invites confusion on later reads.
        final_paths = {str(item.get("file_path", "") or "").strip() for item in current_tests}
        file_state = {path: state for path, state in file_state.items() if path in final_paths}
        final_owned_paths = {
            str(item.get("file_path", "") or "").strip()
            for item in current_tests
            if normalize_coverage_scope(item.get("coverage_scope")) == "owned"
        }
        if owned_interface_ids and not final_owned_paths:
            await self._log(
                "TestGenerator",
                (
                    "DESIGN failed: green-baseline repair removed every owned "
                    "coverage witness; dependency/shared tests cannot validate this node."
                ),
                status="error",
                node_id=node_id,
            )
            return None
        await self._log(
            "TestGenerator",
            (
                f"Baseline RED verification complete: {len(current_tests)} test item(s), "
                + ", ".join(f"{path}: {state or 'not verified (environment)'}" for path, state in sorted(file_state.items()))
            ),
            node_id=node_id,
        )
        return {
            "file_state": file_state,
            "revised_tests": current_tests if manifest_revised else None,
        }

    async def _run_test_group(self, test_type: str, file_paths: list[str]) -> TestRunResult:
        """Single choke point over the app handler's batch runner.

        Both executor instances (the TDD loop's and the DESIGN gate's
        adapter) run every batch through here so the per-task port override
        applies uniformly.
        """

        return await self.app_handler.run_test_group(
            test_type,
            file_paths,
            web_port=self.web_port,
        )

    async def _run_tdd_for_node(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> bool:
        executor = TddTestExecutor(
            node_id=node_id,
            workspace_path=self.workspace_path,
            run_group=self._run_test_group,
            log_cb=self._log,
        )
        ordered_types = executor.register_tests(tests)
        if not ordered_types:
            return True
        # Baseline RED verification: per-file state seeded before the first
        # agent session of each layer. ``red`` files failed the baseline run
        # (legitimate failing tests, the RED evidence for that file), ``green``
        # files already passed (tautology fast path), and ``None`` means no
        # verified state yet (never run, or the baseline stopped at an
        # environment failure).
        # The DESIGN phase already ran each file once right after generation
        # (the green-baseline rejection gate); its per-file state is reused
        # here so IMPLEMENT does not pay the same runs again. Files the DESIGN
        # baseline never saw (manifests from before that gate, or new files
        # from a repair pass) keep ``None`` and are baseline-run below.
        design_baseline = sessions.load_node_session(node_id).get("design_baseline") or {}
        executor.seed_file_states(design_baseline if isinstance(design_baseline, dict) else {})
        await self._log(
            "TestDrivenDeveloper",
            "Running leaf TDD sessions in ordered layers with independent budgets: " + " -> ".join(ordered_types) + ".",
            node_id=node_id,
        )

        output = ""
        session_count = 0
        max_sessions = max(1, TDD_RUN_TESTS_BUDGET * len(ordered_types))
        # Files edited across every agent session of this TDD pass (the
        # adapter exposes only the latest session's writes).
        modified_files_round: list[str] = []
        for ordered_type in ordered_types:
            if executor.environment_failure:
                # The workspace is broken; every remaining layer would fail the
                # same way. Do not spend their budgets too.
                await self._log(
                    "TestDrivenDeveloper",
                    f"Skipping `{ordered_type}`: the workspace failed for environmental reasons "
                    f"({executor.environment_failure}).",
                    status="error",
                    node_id=node_id,
                )
                break
            if executor.layer_passed(ordered_type):
                # The layer was already closed inside an earlier session's
                # in-session advance (a passing full-layer run): no baseline,
                # no regression, no second session.
                continue
            # ---- Baseline RED verification (system-run, no agent budget). ----
            # Run each file of the layer once before the first agent session:
            # a green baseline closes the file (tautology fast path - the
            # agent must not "fix" tests that already pass); an environmental
            # baseline failure is handed to the first agent session under the
            # same repair-and-revalidate contract as an in-session one; a red
            # baseline is the per-file RED evidence injected into the first
            # session. Files already verified green/red by an earlier layer's
            # in-session advance keep their state and skip the baseline run.
            layer_files = executor.layer_files(ordered_type)
            unverified_files = [
                path for path in layer_files if executor.file_states(ordered_type).get(path) is None
            ]
            baseline_red_evidence: list[str] = []
            baseline_env_failure: str | None = None
            for baseline_file in unverified_files:
                baseline = await executor.run_baseline_file(ordered_type, baseline_file)
                baseline_output = baseline.output
                baseline_exit = baseline.exit_code
                await self._log(
                    "TestDrivenDeveloper",
                    (
                        f"Baseline RED check `{ordered_type}` {baseline_file}: "
                        f"{'green (already passing)' if baseline_exit == 0 else 'red (failing)'} "
                        f"with Exit Code: {baseline_exit}."
                    ),
                    status="ok" if baseline_exit == 0 else "warning",
                    node_id=node_id,
                )
                if baseline_exit == 0:
                    continue
                # The run did happen and failed: record it as red even when
                # the failure is environmental (run_baseline_file records the
                # shared unverified state; this layer hands the env failure to
                # the first session below, so its per-file state stays red).
                executor.record_file_state(ordered_type, baseline_file, "red")
                baseline_env = baseline.environment_failure
                if baseline_env:
                    # Broken workspace before any agent budget is spent: hand
                    # the failure to the first session instead of burning its
                    # run_tests calls discovering it.
                    baseline_env_failure = baseline_env
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Baseline RED check `{ordered_type}` {baseline_file} failed for an "
                            f"environmental reason ({baseline_env}); handing the repair contract "
                            "to the first agent session."
                        ),
                        status="error",
                        node_id=node_id,
                    )
                    break
                baseline_red_evidence.append(
                    f"{baseline_file}:\n{summarize_batch_output(baseline_output, max_lines=12)}"
                )
            baseline_red_summary = ""
            if baseline_red_evidence:
                baseline_red_summary = (
                    "### Baseline RED Evidence (system-verified before this session)\n"
                    "The following test files were run by the system and verifiably fail RIGHT NOW. "
                    "This is the RED state for this layer: implement/repair until each listed file turns green.\n"
                    + "\n\n".join(baseline_red_evidence)
                )
            if baseline_env_failure is not None:
                baseline_red_summary = (
                    "### Baseline RED Evidence (system-verified before this session)\n"
                    f"The baseline run failed for an environmental reason: {baseline_env_failure}.\n"
                    "The same repair-and-revalidate contract as an in-session environment failure "
                    "applies: make one file edit that fixes the root cause (create a missing local "
                    "module, correct a wrong relative import, add a missing npm script) and call "
                    "run_tests once to re-validate. If the failure names a package that must be "
                    "installed, end your turn with a short report naming the missing dependency.\n"
                )
            elif baseline_env_failure is None and all(
                state == "green" for state in executor.file_states(ordered_type).values()
            ) and executor.file_states(ordered_type):
                # Tautology fast path: every file of this layer already passed
                # either its baseline run or an in-session run. The system runs
                # one full-layer regression itself - an agent session that only
                # re-runs passing tests buys nothing.
                regression = await executor.run_full_layer(ordered_type)
                if regression.state == "green":
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Baseline RED check: all `{ordered_type}` files already pass; "
                            "system-run full-layer regression passed (tautology fast path, no agent session)."
                        ),
                        node_id=node_id,
                    )
                    continue
                await self._log(
                    "TestDrivenDeveloper",
                    (
                        f"Baseline RED check: `{ordered_type}` files passed individually but the "
                        "full-layer regression failed; opening an agent session for the combined failure."
                    ),
                    status="warning",
                    node_id=node_id,
                )
                baseline_red_summary = (
                    "### Baseline RED Evidence (system-verified before this session)\n"
                    "Every test file in this layer passed its individual run, but the full-layer run "
                    "failed when the files execute together. The combined failure output:\n"
                    f"{summarize_batch_output(regression.output, max_lines=20)}"
                )
            # Between sessions the outer loop owns the layer transitions: it
            # re-pins the active layer and visits each layer exactly once, so
            # a layer the executor closed or advanced past in-session never
            # gets a second session (the while below only runs for layers
            # that are still failing and not yet out of budget).
            executor.pin_active_layer(ordered_type)
            previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
            while not executor.layer_passed(ordered_type):
                if executor.environment_failure:
                    break
                # All files green but the layer was never closed by a full run
                # (e.g. the agent verified each file individually and ended
                # its turn): close it with a system-run regression instead of
                # opening a fresh agent session just to run one command.
                layer_states = executor.file_states(ordered_type)
                if layer_states and all(state == "green" for state in layer_states.values()):
                    regression = await executor.run_full_layer(ordered_type)
                    if regression.state == "green":
                        await self._log(
                            "TestDrivenDeveloper",
                            (
                                f"All `{ordered_type}` files are green; system-run full-layer "
                                "regression passed, closing the layer without a new agent session."
                            ),
                            node_id=node_id,
                        )
                        break
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"All `{ordered_type}` files are green individually but the system-run "
                            "full-layer regression failed; opening an agent session for the combined failure."
                        ),
                        status="warning",
                        node_id=node_id,
                    )
                    baseline_red_summary = (
                        "### Baseline RED Evidence (system-verified before this session)\n"
                        "Every test file in this layer passed its individual run, but the full-layer run "
                        "failed when the files execute together. The combined failure output:\n"
                        f"{summarize_batch_output(regression.output, max_lines=20)}"
                    )
                used_before = executor.usage(ordered_type)
                if executor.budget_exhausted(ordered_type):
                    break
                if session_count >= max_sessions:
                    await self._log(
                        "TestDrivenDeveloper",
                        f"TDD stopped after {session_count} agent session(s); continuing layer summary with collected results.",
                        status="error",
                        node_id=node_id,
                    )
                    break

                session_count += 1
                if used_before > 0:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Resuming TDD agent session {session_count} for `{ordered_type}`; "
                            f"run_tests usage is {used_before}/{TDD_RUN_TESTS_BUDGET}."
                        ),
                        node_id=node_id,
                    )
                # First session of the layer carries the baseline RED evidence
                # so the agent starts from verified failures; follow-up
                # sessions carry the freshest failure evidence instead.
                session_failure_context = (
                    baseline_red_summary
                    if used_before == 0
                    else previous_failure_summary
                )
                stall_context = ""
                recent = executor.fingerprints(ordered_type)[-TDD_STALL_THRESHOLD:]
                if executor.is_stalled(ordered_type):
                    stall_context = (
                        "\n\n### Stall Governance Handoff\n"
                        f"The last {len(recent)} run_tests failures in this layer share the same fingerprint. "
                        "Do not continue the previous session's approach: re-classify the failure, list the "
                        "hypotheses already tried, and edit a different layer of the UI/API/FUNC/DB chain "
                        "(or a different fix approach within the same layer) before the next run_tests call.\n"
                        f"Repeated fingerprint: {recent[-1]}"
                    )
                output = await self.test_driven_developer.run(
                    node_id=node_id,
                    test_files=collect_test_files(tests),
                    test_type=ordered_type,
                    node_tests=tests,
                    previous_failure_summary=(session_failure_context + stall_context).strip(),
                    run_tests_budget=None,
                    run_tests_usage=None,
                    run_tests_executor=executor.run_requested,
                )
                # Union across sessions: the adapter's per-session list resets
                # on every run(), so accumulate here for the round-level
                # tdd_handoff written at the end of this TDD pass.
                for path in self.test_driven_developer.get_last_modified_files():
                    if path and path not in modified_files_round:
                        modified_files_round.append(path)

                latest_result = executor.layer_result(ordered_type)
                # Three-part cross-session handoff: the structured per-test
                # digest (locations + expected/received), a diff hint of what
                # the previous session edited, and the pointer to the persisted
                # raw output. The digest is preferred over the raw verifier
                # report tail; the report stays as the fallback when the run
                # output carried no recognizable per-test structure.
                previous_failure_summary = (
                    self._build_session_handoff(
                        self.test_driven_developer.get_last_failure_digest()
                        or self.test_driven_developer.get_last_verifier_report()
                        or summarize_batch_output((latest_result.output if latest_result else "") or output),
                        modified_files=self.test_driven_developer.get_last_modified_files(),
                        fingerprint_history=executor.fingerprints(ordered_type),
                    )
                )
                used_after = executor.usage(ordered_type)
                if executor.layer_passed(ordered_type):
                    break
                if executor.budget_exhausted(ordered_type):
                    break
                if used_after == used_before:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"TDD agent session ended without calling run_tests for `{ordered_type}`; "
                            "moving to the next scheduled layer with a fresh budget."
                        ),
                        status="error",
                        node_id=node_id,
                    )
                    break
            executor.unpin_active_layer()
            if (
                not executor.layer_passed(ordered_type)
                and not executor.environment_failure
            ):
                await self._log(
                    "TestDrivenDeveloper",
                    f"Advancing past `{ordered_type}` without a passing result; the next scheduled layer will start with its own budget.",
                    status="warning",
                    node_id=node_id,
                )

        final_ok = True
        failure_summaries: list[str] = []
        failed_types: list[str] = []
        for test_type in ordered_types:
            if not executor.layer_passed(test_type):
                await self._reverify_budget_exhausted_layer(
                    node_id=node_id,
                    test_type=test_type,
                    executor=executor,
                )
            latest_result = executor.layer_result(test_type)
            group_passed = executor.layer_passed(test_type)
            status_by_test_id = {
                str(test.get("test_id", "")).strip(): group_passed
                for test in executor.layer_items(test_type)
                if str(test.get("test_id", "")).strip()
            }
            self.traceability.set_test_pass_statuses(status_by_test_id)
            if group_passed:
                await self._log(
                    "TestDrivenDeveloper",
                    f"TDD batch `{test_type}` passed after {executor.usage(test_type)}/{TDD_RUN_TESTS_BUDGET} run_tests call(s).",
                    node_id=node_id,
                )
                continue
            final_ok = False
            failed_types.append(test_type)
            if executor.environment_failure and not latest_result:
                # This layer was never attempted: the workspace was already known
                # to be broken, so reporting stale verifier output here would be
                # misleading.
                failure_summaries.append(
                    f"{test_type}: not attempted - the workspace failed for environmental "
                    f"reasons ({executor.environment_failure})."
                )
            else:
                failure_summary = (
                    summarize_batch_output(latest_result.output)
                    if latest_result
                    else self.test_driven_developer.get_last_verifier_report()
                    or summarize_batch_output(output)
                )
                if executor.environment_failure:
                    failure_summary = (
                        f"[environment failure] {executor.environment_failure}\n{failure_summary}"
                    )
                failure_summaries.append(f"{test_type}: {failure_summary}")
            used = executor.usage(test_type)
            if executor.environment_failure:
                detail = f"environment failure ({executor.environment_failure})"
            elif used >= TDD_RUN_TESTS_BUDGET:
                detail = "budget exhausted"
            else:
                detail = "agent session ended before this layer passed"
            await self._log(
                "TestDrivenDeveloper",
                f"TDD batch `{test_type}` did not pass after {used}/{TDD_RUN_TESTS_BUDGET} run_tests call(s); {detail}.",
                status="error",
                node_id=node_id,
            )

        context_pipeline.cache.invalidate_db_layers(node_id)
        context_pipeline.cache.invalidate_file_layers(node_id)
        failure_summary = "\n\n".join(failure_summaries)
        self._update_node_session(
            node_id,
            {
                "recent_failure_summary": failure_summary,
                "tdd_handoff": {
                    "last_test_type": failed_types[-1] if failed_types else ordered_types[-1],
                    "last_failed_output_summary": failure_summary,
                    # Diff hint for the next TDD round (post-run retry or
                    # --retry-failed): what the sessions of this pass actually
                    # edited, from the stage discipline rather than the
                    # model's own self-report.
                    "modified_files": sorted(modified_files_round),
                    # Evidence for the next TDD round's prompt: failing
                    # layers' run_tests usage and fingerprint history. A
                    # post-run auto retry starts with fresh budgets, and
                    # without this history the retried session re-derives -
                    # or worse, repeats - hypotheses the previous round
                    # already burned its budget on.
                    "layer_usage": {
                        test_type: executor.usage(test_type)
                        for test_type in ordered_types
                        if executor.usage(test_type)
                    },
                    "fingerprint_history": {
                        test_type: executor.fingerprints(test_type)[-5:]
                        for test_type in ordered_types
                        if executor.fingerprints(test_type)
                    },
                },
            },
        )
        if not final_ok:
            return False

        unexpected_types = executor.unsupported_layers()
        if unexpected_types:
            await self._log(
                "TestDrivenDeveloper",
                f"Ignoring unsupported test batch type(s): {', '.join(unexpected_types)}.",
                status="warning",
                node_id=node_id,
            )

        return True

    async def _reverify_budget_exhausted_layer(
        self,
        *,
        node_id: str,
        test_type: str,
        executor: TddTestExecutor,
    ) -> None:
        """Limited late-fix re-verification before a layer is failed (#116).

        A layer can fail purely because its ``run_tests`` budget ran out while
        the fix was still landing: the agent keeps editing shared code from
        the next layer's session, so by the time a later layer passes, the
        failed layer's tests may already be green. Without a re-run the
        verdict lands between "fix landed" and "fix verified" and fails a
        node whose code is correct.

        A layer qualifies when its budget is spent (the condition the failure
        detail reports as "budget exhausted") with no unresolved environment
        failure at verdict time, and some later layer is green: a layer that
        ran after this one closed went green, so the fix may have landed
        after this layer's budget died. A still-red layer in between does not
        block - the green run still proves late edits landed, and layer
        verdicts stay independent, so nothing red is masked. (A budget spent
        on a since-repaired environment failure qualifies the same way: the
        repair was itself a late fix.) An unresolved environment failure
        blocks the channel: the re-run would only re-hit the broken
        workspace.

        The re-run covers exactly the layer's manifest files, runs once,
        after the agent sessions and outside the budget counters. A pass
        closes the layer; a failure falls through to the ordinary failure
        bookkeeping. Layer ordering, budgets and manifest lock semantics are
        untouched.
        """

        used = executor.usage(test_type)
        if executor.environment_failure or used < TDD_RUN_TESTS_BUDGET:
            return
        ordered_types = executor.ordered_layers
        successor_index = ordered_types.index(test_type) + 1
        if not any(executor.layer_passed(later) for later in ordered_types[successor_index:]):
            return
        layer_files = executor.layer_files(test_type)

        def record_reverify(status: str, message: str | None = None) -> None:
            self.events.record_layer_reverify(
                node_id=node_id,
                layer=test_type,
                status=status,
                files=layer_files,
                used=used,
                message=message,
            )

        await self._log(
            "TestDrivenDeveloper",
            (
                f"`{test_type}` failed with its budget exhausted while a later layer is green; "
                f"re-verifying the layer once against its {len(layer_files)} manifest file(s)."
            ),
            node_id=node_id,
        )
        record_reverify("triggered")
        # The re-run goes through the executor's full-layer primitive: it
        # records the result for the failure summaries below, closes the
        # layer on a pass, and leaves budgets untouched.
        reverify = await executor.run_full_layer(test_type)
        if reverify.state == "green":
            await self._log(
                "TestDrivenDeveloper",
                (
                    f"Late-fix re-verification passed for `{test_type}` "
                    f"(Exit Code: 0); treating the layer as passed."
                ),
                status="ok",
                node_id=node_id,
            )
            record_reverify("passed")
            return
        await self._log(
            "TestDrivenDeveloper",
            (
                f"Late-fix re-verification failed for `{test_type}` "
                f"(Exit Code: {reverify.exit_code}); keeping the failure."
            ),
            status="error",
            node_id=node_id,
        )
        record_reverify("failed", summarize_batch_output(reverify.output))

    def _prepare_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue
            existing = self.traceability.get_interface(interface_id)
            if existing:
                try:
                    existing_content = json.loads(str(existing.get("content") or "{}"))
                except json.JSONDecodeError:
                    existing_content = {}
                if isinstance(existing_content, dict):
                    interface = {**existing_content, **interface}
            interface_type = str(interface.get("type") or (existing or {}).get("type") or "").strip().upper()
            if interface_type not in ALLOWED_INTERFACE_TYPES:
                raise ValueError(
                    f"Generated interface `{interface_id}` has invalid `type` {interface.get('type')!r}. "
                    "Interface type must be one of UI, API, FUNC, or DB."
                )
            normalized = {
                **interface,
                "interface_id": interface_id,
                "req_id": node_id,
                "type": interface_type,
                "file_path": (
                    normalize_workspace_relative_path(interface.get("file_path"), self.workspace_path)
                    or ((existing or {}).get("file_path") if existing else "")
                ),
                "first_line": str(interface.get("first_line") or (existing or {}).get("first_line") or "").strip(),
                "callers": normalize_string_list(interface.get("callers")) or normalize_string_list((existing or {}).get("callers")),
                "callees": normalize_string_list(interface.get("callees")) or normalize_string_list((existing or {}).get("callees")),
                "_existing_req_ids": list(existing.get("req_ids", [])) if existing else [],
                "_existing_implemented": bool(existing.get("implemented")) if existing else False,
            }
            prepared.append(normalized)
        return prepared

    def _store_prepared_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> None:
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue
            req_ids = normalize_string_list(interface.get("_existing_req_ids"))
            if node_id not in req_ids:
                req_ids.append(node_id)
            self.traceability.upsert_interface(
                interface_id=interface_id,
                req_ids=req_ids,
                type=str(interface.get("type", "") or "").strip().upper(),
                content=json.dumps(_strip_internal_fields(interface), ensure_ascii=False),
                file_path=str(interface.get("file_path", "") or "").strip() or None,
                first_line=str(interface.get("first_line", "") or "").strip() or None,
                implemented=bool(interface.get("_existing_implemented")),
                callers=normalize_string_list(interface.get("callers")),
                callees=normalize_string_list(interface.get("callees")),
            )
            self._register_interface_edges(node_id, interface_id, interface)

    def _prepare_tests(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        generated_ids: set[str] = set()
        for test in tests:
            if not isinstance(test, dict):
                continue
            raw_test_id = str(test.get("test_id", "")).strip()
            if not raw_test_id:
                continue
            file_path = normalize_workspace_relative_path(test.get("file_path"), self.workspace_path)
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `type`.")
            if not file_path:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `file_path`.")
            validation_error = self.app_handler.validate_test_path(test_type, file_path)
            if validation_error:
                raise ValueError(f"Generated test `{raw_test_id}` has an invalid path. {validation_error}")
            coverage_scope = normalize_coverage_scope(test.get("coverage_scope"))
            if not coverage_scope:
                raise ValueError(
                    f"Generated test `{raw_test_id}` has invalid `coverage_scope`; "
                    "expected owned, dependency, or shared."
                )
            if raw_test_id in generated_ids:
                raise ValueError(f"Generated duplicate test id `{raw_test_id}`.")
            generated_ids.add(raw_test_id)
            stored_item = {
                **test,
                "test_id": raw_test_id,
                "req_id": node_id,
                "type": test_type,
                "file_path": file_path,
                "coverage_scope": coverage_scope,
                "interface_ids": normalize_string_list(test.get("interface_ids")),
                "first_line": str(test.get("first_line", "")).strip(),
            }
            stored.append(stored_item)
        return stored

    def _store_prepared_tests(self, tests: list[dict[str, Any]]) -> None:
        for test in tests:
            self.traceability.upsert_test(
                test_id=str(test.get("test_id", "") or "").strip(),
                req_id=str(test.get("req_id", "") or "").strip(),
                interface_ids=normalize_string_list(test.get("interface_ids")),
                type=str(test.get("type", "") or "").strip(),
                file_path=str(test.get("file_path", "") or "").strip() or None,
                first_line=str(test.get("first_line", "") or "").strip() or None,
                passed=None,
            )

    def _register_interface_edges(self, node_id: str, interface_id: str, interface: dict[str, Any]) -> None:
        for caller_id in normalize_string_list(interface.get("callers")):
            caller = self.traceability.get_interface(caller_id)
            if not caller:
                continue
            for source_req_id in caller.get("req_ids", []):
                if source_req_id and source_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=source_req_id,
                        target_req_id=node_id,
                        from_interface_id=caller_id,
                        to_interface_id=interface_id,
                        edge_type="cross_req",
                    )
        for callee_id in normalize_string_list(interface.get("callees")):
            callee = self.traceability.get_interface(callee_id)
            if not callee:
                continue
            for target_req_id in callee.get("req_ids", []):
                if target_req_id and target_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=node_id,
                        target_req_id=target_req_id,
                        from_interface_id=interface_id,
                        to_interface_id=callee_id,
                        edge_type="cross_req",
                    )

    def _mark_interfaces_implemented(self, interfaces: list[dict[str, Any]]) -> None:
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if interface_id:
                self.traceability.set_interface_implemented(interface_id, True)

    @staticmethod
    def _build_session_handoff(
        failure_evidence: str,
        *,
        modified_files: list[str],
        fingerprint_history: list[str],
    ) -> str:
        """Compose the cross-session TDD handoff text.

        Parts: the failure evidence (structured digest preferred, verifier
        report tail as fallback), then a diff hint naming the files the
        previous session edited and how the failure fingerprint moved — the
        next session uses this to know what was already tried without
        re-reading files or re-running tests.
        """

        evidence = str(failure_evidence or "").strip()
        hint_parts: list[str] = []
        if modified_files:
            hint_parts.append(
                "Files edited by the previous session (stage-discipline ground truth): "
                + ", ".join(f"`{path}`" for path in modified_files)
            )
        if len(fingerprint_history) >= 2:
            moved = (
                "changed"
                if fingerprint_history[-1] != fingerprint_history[-2]
                else "DID NOT change — the previous session's edits did not move this failure; "
                "rotate your hypothesis before editing the same files again"
            )
            hint_parts.append(
                f"Failure fingerprint across this layer's runs: {fingerprint_history[-1]} "
                f"(vs. previous run: {moved})"
            )
        if not hint_parts:
            return evidence
        return (
            evidence
            + "\n\n### Previous Session Diff Hint\n"
            + "\n".join(f"- {part}" for part in hint_parts)
        )

    def _update_node_session(self, node_id: str, patch: dict[str, Any]) -> None:
        sessions.merge_node_session(node_id, patch)
        context_pipeline.cache.invalidate_db_layers(node_id)

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(agent_name, message, status, node_id)
        if hasattr(result, "__await__"):
            await result


#: Workspace subtrees an undeclared-test sweep never enters: ignored runtime
#: state, dependency installs, and generated build output never belong to a
#: delivery commit decision in the first place.
_SWEEP_SKIPPED_PARTS = frozenset({".arc", ".git", "node_modules", "dist", "dist-ssr", "build", "coverage", ".vite"})


def _is_test_like_relative_path(path: str) -> bool:
    """Whether a workspace-relative path looks like a test file.

    The manifest predicate (``is_test_file_path``) governs what a TestGenerator
    may declare; the sweep deliberately accepts more, because it protects the
    delivery commit, not the declaration channel: an IMPLEMENT-stage probe
    with a bare ``test-*`` name (``backend/test-express-wildcard.js``) dodges
    the manifest's stricter naming and must not sail into the delivery commit
    on that technicality.
    """

    if is_test_file_path(path):
        return True
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem == "test" or stem.startswith("test-") or stem.endswith("-test")


def _load_untracked_paths(workspace_root: str) -> set[str]:
    """Git-untracked paths of ``workspace_root`` (repo-relative, POSIX style).

    ``--exclude-standard`` keeps ignored state (``.arc``, ``node_modules``,
    lockfiles) out of the candidate set, so the sweep only ever considers
    files git would actually stage. Failures return an empty set: the sweep
    is a best-effort backstop, not a gate, and a git hiccup must not fail
    the IMPLEMENT phase. The raw ``subprocess.run`` (rather than the runtime
    SDK's ``GitClient.run``) is deliberate: the client hardcodes its own
    ``project_dir`` as cwd and cannot target a task worktree, which is
    exactly where the parallel-mode sweep must run.
    """

    try:
        completed = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode != 0:
        return set()
    return {
        line.replace("\\", "/").strip()
        for line in completed.stdout.splitlines()
        if line.strip()
    }


def collect_undeclared_test_files(
    workspace_root: str,
    *,
    declared_paths: list[str] | set[str],
) -> list[str]:
    """Untracked test-named files under ``workspace_root`` outside the manifest.

    This is the IMPLEMENT checkpoint's backstop against agent-authored
    diagnostic files leaking into the delivery commit (issue #89): the file
    set is the intersection of

    - untracked paths in git (so tracked/committed files never match), and
    - test-named paths the node's test manifest does not declare.

    The git-untracked filter is what keeps the sweep surgical: a *modified*
    registered test file, a pre-existing sibling test from an earlier node,
    and files another node's manifest owns are all committed state and stay
    untouched. Only genuinely new, unregistered test-shaped files — the
    diagnostics an agent wrote to localize a failure — are collected.

    Returns workspace-relative POSIX-style paths, sorted.
    """

    root = Path(workspace_root).expanduser().resolve()
    declared = {
        normalize_workspace_relative_path(path, str(root))
        for path in declared_paths
        if str(path or "").strip()
    }
    matches: list[str] = []
    for path in sorted(_load_untracked_paths(str(root))):
        parts = path.split("/")
        if any(part in _SWEEP_SKIPPED_PARTS for part in parts[:-1]):
            continue
        if path in declared or normalize_workspace_relative_path(path, str(root)) in declared:
            continue
        if _is_test_like_relative_path(path):
            matches.append(path)
    return sorted(matches)


def summarize_interface_artifacts(interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(interfaces),
        "items": [
            {
                "id": str(item.get("interface_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "responsibility": str(item.get("responsibility", "") or item.get("name", "") or "").strip(),
            }
            for item in interfaces
            if isinstance(item, dict)
        ],
    }


def summarize_test_artifacts(tests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(tests),
        "items": [
            {
                "id": str(item.get("test_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "interfaces": normalize_string_list(item.get("interface_ids")),
            }
            for item in tests
            if isinstance(item, dict)
        ],
    }


def summarize_batch_output(batch_output: str, max_lines: int = 30) -> str:
    lines = [line for line in (batch_output or "").splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = ["...[truncated]", *lines[-max_lines:]]
    return "\n".join(lines)


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _strip_internal_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}

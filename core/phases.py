from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from app_type_handler import create_app_type_handler
from agents.context.pipeline import context_pipeline
from agents.skills.planning import plan_and_store_stage_skills
from core import sessions
from core.service import get_runtime
from core.path_compat import normalize_windows_extended_prefix_text
from core.visual_analysis import analyze_and_attach_visual_references
from app_type_handler.test_results import classify_test_failure, failure_fingerprint, parse_test_results


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
TDD_RUN_TESTS_BUDGET = 10
#: Consecutive identical failure fingerprints before stall governance fires.
TDD_STALL_THRESHOLD = 3
ALLOWED_INTERFACE_TYPES = {"UI", "API", "FUNC", "DB"}
TDD_BATCH_ORDER = ("Unit", "Integration", "E2E")

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

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_non_leaf = bool(requirement_data.get("children_ids"))
        requirement_data = await analyze_and_attach_visual_references(
            workspace_path=self.context_workspace_path,
            requirements_dir=str(Path(self.requirement_path).expanduser().resolve().parent),
            requirement_data=requirement_data,
            log_cb=self._log,
        )
        requirement_data = self.traceability.get_requirement(node_id) or requirement_data
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

        await plan_and_store_stage_skills(
            node_id=node_id,
            requirement_data=requirement_data,
            log_cb=self._log,
        )

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
        self._update_node_session(
            node_id,
            {"phase_status": {"implement": "completed" if final_ok else "failed"}},
        )
        return final_ok

    async def _run_tdd_for_node(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> bool:
        previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
        groups: dict[str, list[dict[str, Any]]] = {}
        for test in tests:
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                continue
            normalized_type = test_type.lower()
            groups.setdefault(normalized_type, []).append(test)

        ordered_types = [test_type for test_type in TDD_BATCH_ORDER if groups.get(test_type.lower())]
        if not ordered_types:
            return True

        usage_by_type = {test_type: 0 for test_type in ordered_types}
        result_by_type: dict[str, str] = {}
        environment_failure: str | None = None
        # Baseline RED verification: per-file state seeded before the first
        # agent session of each layer. ``red`` files failed the baseline run
        # (legitimate failing tests, the RED evidence for that file), ``green``
        # files already passed (tautology fast path), and ``None`` means no
        # verified state yet (never run, or the baseline stopped at an
        # environment failure).
        file_state_by_type: dict[str, dict[str, str | None]] = {
            test_type: {path: None for path in collect_test_files(groups[test_type.lower()])}
            for test_type in ordered_types
        }
        # A layer passes only through a passing run that covered EVERY
        # registered file of the layer (an agent full-layer run or the
        # system-run regression below). A passing subset run no longer closes
        # the layer: every file must be verified green and confirmed together.
        full_layer_passed = {test_type: False for test_type in ordered_types}
        # Failure-fingerprint history per layer for stall governance: each
        # failed run_tests call appends its fingerprint; three identical
        # consecutive fingerprints force a hypothesis-rotation directive.
        fingerprint_history: dict[str, list[str]] = {test_type: [] for test_type in ordered_types}
        await self._log(
            "TestDrivenDeveloper",
            "Running leaf TDD sessions in ordered layers with independent budgets: " + " -> ".join(ordered_types) + ".",
            node_id=node_id,
        )
        active_test_type: str | None = None

        async def run_requested_tests(requested_type: str | None = None, requested_files: list[str] | None = None) -> str:
            nonlocal environment_failure, active_test_type
            requested = str(requested_type or "").strip()
            if active_test_type is None:
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    "No active TDD test layer is currently scheduled.\n"
                )
            if requested.lower() in {"", "all", "current", "next"}:
                selected_type = active_test_type
            else:
                selected_type = canonical_test_type(requested)
                if selected_type is None or selected_type not in ordered_types:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"Unsupported current-node test_type={requested!r}. "
                        f"Available ordered layers: {', '.join(ordered_types)}.\n"
                    )
                if selected_type != active_test_type:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"The active TDD layer is `{active_test_type}`, but run_tests requested `{selected_type}`. "
                        "The system attempts layers in Unit -> Integration -> E2E order with independent budgets.\n"
                    )

            selected_files = [
                path
                for value in (requested_files or collect_test_files(groups[selected_type.lower()]))
                if (path := normalize_workspace_relative_path(value, self.workspace_path))
            ]
            registered_files = {
                str(item.get("file_path", "") or "").strip()
                for item in groups[selected_type.lower()]
                if str(item.get("file_path", "") or "").strip()
            }
            unknown = [path for path in selected_files if path not in registered_files]
            if unknown:
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"run_tests({selected_type}) may only execute registered {selected_type} tests for the current node. "
                    f"Unknown files: {', '.join(unknown)}\n"
                )
            used = usage_by_type[selected_type]
            if used >= TDD_RUN_TESTS_BUDGET:
                await self._log(
                    "TestDrivenDeveloper",
                    f"`run_tests` {selected_type} budget exhausted at {used}/{TDD_RUN_TESTS_BUDGET}.",
                    status="error",
                    node_id=node_id,
                )
                if environment_failure is not None:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"run_tests budget exhausted for {selected_type}: {used}/{TDD_RUN_TESTS_BUDGET}.\n"
                        "The workspace failed for an environmental reason. Do not call run_tests again; "
                        "return your report now.\n"
                    )
                closed_next_index = ordered_types.index(selected_type) + 1
                closed_next_type = ordered_types[closed_next_index] if closed_next_index < len(ordered_types) else None
                if closed_next_type is not None:
                    # Advance the active layer the moment its budget is gone. The
                    # prompt promises "the system moves to later layers even if an
                    # earlier layer fails or exhausts its budget"; without this
                    # in-session advance the model stays locked to the closed
                    # layer (next-layer requests are rejected) and deadloops on
                    # budget-exhausted responses (observed on the 12306
                    # benchmark: ~5 minutes of pure model turns per node). The
                    # advance cannot make the outer scheduler reopen this layer:
                    # it visits each layer once and its session loop breaks on
                    # the budget check, so a closed layer never runs again.
                    active_test_type = closed_next_type
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"The {selected_type} layer is closed: run_tests budget exhausted at "
                        f"{used}/{TDD_RUN_TESTS_BUDGET}.\n"
                        f"The system has advanced the active layer to `{closed_next_type}`; "
                        f"`run_tests(test_type='{closed_next_type}')` now targets it.\n"
                        f"Apply a concrete repair before spending the {closed_next_type} budget, or end your turn "
                        f"with a concise summary of the failing {selected_type} tests and the next edit target.\n"
                    )
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"The {selected_type} layer is closed: run_tests budget exhausted at "
                    f"{used}/{TDD_RUN_TESTS_BUDGET}. This was the last scheduled layer.\n"
                    "End your turn now with a concise summary of the failing tests and the next edit target. "
                    "Do not call run_tests again.\n"
                )
            usage_by_type[selected_type] = used + 1
            await self._log(
                "TestDrivenDeveloper",
                f"`run_tests` {selected_type} usage {usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}.",
                node_id=node_id,
            )
            output = await self.app_handler.run_test_group(
                selected_type,
                selected_files,
                web_port=self.web_port,
            )
            await self._log(
                "TestDrivenDeveloper",
                (
                    "run_tests raw output\n"
                    f"test_type={selected_type}\n"
                    f"attempt={usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}\n"
                    f"test_files={json.dumps(selected_files, ensure_ascii=False)}\n"
                    "----- BEGIN RAW TEST OUTPUT -----\n"
                    f"{output.rstrip()}\n"
                    "----- END RAW TEST OUTPUT -----"
                ),
                status="debug",
                node_id=node_id,
            )
            parsed_result = parse_test_results(output)
            exit_code = int(parsed_result.get("exit_code", -1))
            passed = exit_code == 0
            await self._log(
                "TestDrivenDeveloper",
                (
                    f"`run_tests` {selected_type} {'passed' if passed else 'failed'} "
                    f"with Exit Code: {exit_code} "
                    f"on attempt {usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}: "
                    f"{', '.join(selected_files)}"
                ),
                status="ok" if passed else "error",
                node_id=node_id,
            )
            result_by_type[selected_type] = output
            if passed:
                for path in selected_files:
                    file_state_by_type[selected_type][path] = "green"
                # A layer passes only through a passing run that covered every
                # registered file; a passing subset run keeps the layer open.
                registered_layer_files = {
                    str(item.get("file_path", "") or "").strip()
                    for item in groups[selected_type.lower()]
                    if str(item.get("file_path", "") or "").strip()
                }
                if registered_layer_files and set(selected_files) >= registered_layer_files:
                    full_layer_passed[selected_type] = True
                if environment_failure is not None:
                    # The reported environment failure was repaired (e.g. the
                    # agent created a missing local module or fixed an import);
                    # the normal layer flow resumes.
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"`run_tests` {selected_type} passed after the reported environment "
                            f"failure ({environment_failure}) was repaired; resuming normal TDD flow."
                        ),
                        node_id=node_id,
                    )
                    environment_failure = None
            else:
                for path in selected_files:
                    if file_state_by_type[selected_type].get(path) != "green":
                        file_state_by_type[selected_type][path] = "red"
                fingerprint_history[selected_type].append(failure_fingerprint(output))
                failure_now = classify_test_failure(output) or None
                if environment_failure is None:
                    if failure_now:
                        environment_failure = failure_now
                        await self._log(
                            "TestDrivenDeveloper",
                            (
                                f"`run_tests` {selected_type} failed for an environmental reason "
                                f"({environment_failure}); the workspace is broken, not the "
                                "implementation. Allowing one repair-and-revalidate attempt."
                            ),
                            status="error",
                            node_id=node_id,
                        )
                elif failure_now:
                    # Still environmental after the one re-validation attempt:
                    # spend the rest of this layer's budget up front so the
                    # outer loop breaks instead of re-running a doomed command.
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"`run_tests` {selected_type} still fails for an environmental reason "
                            f"({failure_now}); stopping the TDD loop instead of retrying."
                        ),
                        status="error",
                        node_id=node_id,
                    )
                    usage_by_type[selected_type] = TDD_RUN_TESTS_BUDGET
                else:
                    # The run now fails on assertions, not the environment: the
                    # earlier environment failure was repaired, so the normal
                    # retry loop resumes.
                    environment_failure = None
            next_index = ordered_types.index(selected_type) + 1
            next_type = ordered_types[next_index] if next_index < len(ordered_types) else None
            # Micro-loop status: per-file red/green state the agent sees on
            # every run_tests result, so each file's red -> green transition is
            # an explicit, verifiable step rather than batch soup.
            layer_file_states = file_state_by_type[selected_type]
            still_red = sorted(path for path, state in layer_file_states.items() if state != "green")
            output += (
                "\n\nARC_TEST_FILE_STATUS:\n"
                f"- Layer `{selected_type}` per-file state:\n"
                + "\n".join(
                    f"  - {path}: {state or 'not yet run'}"
                    for path, state in layer_file_states.items()
                )
                + "\n"
            )
            # Stall governance: three consecutive identical failure fingerprints
            # mean the last repairs did not change the failure - the agent is
            # stuck on one hypothesis. Force an explicit rotation.
            recent = fingerprint_history[selected_type][-TDD_STALL_THRESHOLD:]
            stalled = (
                not passed
                and len(fingerprint_history[selected_type]) >= TDD_STALL_THRESHOLD
                and len(set(recent)) == 1
            )
            if stalled:
                await self._log(
                    "TestDrivenDeveloper",
                    (
                        f"`run_tests` {selected_type} failure fingerprint stalled for "
                        f"{len(recent)} consecutive attempts; requiring hypothesis rotation."
                    ),
                    status="error",
                    node_id=node_id,
                )
                output += (
                    "- STALL DETECTED: the same failure fingerprint has repeated "
                    f"{len(recent)} times in a row. Your recent edits are not changing the failure. "
                    "Before the next run_tests call, you MUST rotate your hypothesis: "
                    "(1) re-classify the failure (implementation logic, boundary wiring, selector/render "
                    "state, persistence/test database, framework/config, or test content); "
                    "(2) list the hypotheses you have already tried; "
                    "(3) pick a DIFFERENT layer of the UI/API/FUNC/DB chain to edit, or a different "
                    "fix approach within the same layer; "
                    "(4) only then make the repair and re-run.\n"
                )
            if passed and still_red:
                # A passing run on a subset of files: the still-red files keep
                # their red state and stay the repair target.
                output += (
                    f"- {len(still_red)} file(s) in this layer are still red: {', '.join(still_red)}. "
                    "The layer passes only when every file is green and a final full-layer run passes.\n"
                )
            if full_layer_passed[selected_type] and next_type:
                # Advance immediately instead of waiting for the session to
                # end. Otherwise a model that keeps polling `run_tests` after a
                # pass re-runs the passing layer until its budget is gone and
                # then deadloops on rejected next-layer requests (observed on
                # the 12306 benchmark: attempts 6-10 re-ran an already-passing
                # batch, then the session burned minutes on "the system says it
                # will advance but never does").
                active_test_type = next_type
                # The next layer's files were never baseline-verified in this
                # session; the outer scheduler will baseline them before their
                # first agent session (in-session advances pin the active
                # layer, and the baseline loop only runs per layer).
                output += (
                    "\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed (full layer).\n"
                    f"- The system has advanced the active layer to `{next_type}`; "
                    f"`run_tests(test_type='{next_type}')` now targets it.\n"
                    f"- The {selected_type} layer is closed: further `{selected_type}` calls are rejected "
                    "without consuming budget.\n"
                    "- Do not return IMPLEMENTED until all scheduled layers have been attempted and passed.\n"
                )
            elif full_layer_passed[selected_type]:
                output += (
                    "\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed (full layer).\n"
                    "- This is the last scheduled test layer. You may return IMPLEMENTED only if all earlier scheduled layers also passed.\n"
                )
            elif environment_failure and usage_by_type[selected_type] >= TDD_RUN_TESTS_BUDGET:
                output += (
                    "\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} is still failing for an environmental reason "
                    f"({environment_failure}).\n"
                    "- This layer is closed: the one repair-and-revalidate attempt has been used.\n"
                    "- Do not call run_tests again; return a short report naming the missing "
                    "dependency instead.\n"
                )
            elif environment_failure:
                output += (
                    "\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} could not run: {environment_failure}.\n"
                    "- This is your one repair-and-revalidate attempt for this environment failure.\n"
                    "- First decide the root cause. If it is fixable with a file edit (create a "
                    "missing local module, correct a wrong relative import, add a missing npm "
                    "script), make that edit and call run_tests once more to re-validate.\n"
                    "- If instead it names a package that must be installed, you cannot fix it "
                    "mid-run: end your turn with a short report naming the missing dependency "
                    "and do not call run_tests again.\n"
                )
            return output

        output = ""
        session_count = 0
        max_sessions = max(1, TDD_RUN_TESTS_BUDGET * len(ordered_types))
        for ordered_type in ordered_types:
            if environment_failure:
                # The workspace is broken; every remaining layer would fail the
                # same way. Do not spend their budgets too.
                await self._log(
                    "TestDrivenDeveloper",
                    f"Skipping `{ordered_type}`: the workspace failed for environmental reasons "
                    f"({environment_failure}).",
                    status="error",
                    node_id=node_id,
                )
                break
            if full_layer_passed[ordered_type]:
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
            layer_files = collect_test_files(groups[ordered_type.lower()])
            unverified_files = [path for path in layer_files if file_state_by_type[ordered_type].get(path) is None]
            baseline_red_evidence: list[str] = []
            baseline_env_failure: str | None = None
            for baseline_file in unverified_files:
                baseline_output = await self.app_handler.run_test_group(
                    ordered_type,
                    [baseline_file],
                    web_port=self.web_port,
                )
                baseline_exit = int(parse_test_results(baseline_output).get("exit_code", -1))
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
                    file_state_by_type[ordered_type][baseline_file] = "green"
                    continue
                file_state_by_type[ordered_type][baseline_file] = "red"
                baseline_env = classify_test_failure(baseline_output)
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
                state == "green" for state in file_state_by_type[ordered_type].values()
            ) and file_state_by_type[ordered_type]:
                # Tautology fast path: every file of this layer already passed
                # either its baseline run or an in-session run. The system runs
                # one full-layer regression itself - an agent session that only
                # re-runs passing tests buys nothing.
                regression_output = await self.app_handler.run_test_group(
                    ordered_type,
                    layer_files,
                    web_port=self.web_port,
                )
                result_by_type[ordered_type] = regression_output
                if int(parse_test_results(regression_output).get("exit_code", -1)) == 0:
                    full_layer_passed[ordered_type] = True
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
                for path in layer_files:
                    if file_state_by_type[ordered_type].get(path) != "green":
                        file_state_by_type[ordered_type][path] = "red"
                baseline_red_summary = (
                    "### Baseline RED Evidence (system-verified before this session)\n"
                    "Every test file in this layer passed its individual run, but the full-layer run "
                    "failed when the files execute together. The combined failure output:\n"
                    f"{summarize_batch_output(regression_output, max_lines=20)}"
                )
            # Between sessions the outer loop owns the layer transitions: it
            # re-pins the active layer and visits each layer exactly once, so
            # a layer the executor closed or advanced past in-session never
            # gets a second session (the while below only runs for layers
            # that are still failing and not yet out of budget).
            active_test_type = ordered_type
            previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
            while not full_layer_passed[ordered_type]:
                if environment_failure:
                    break
                used_before = usage_by_type.get(ordered_type, 0)
                if used_before >= TDD_RUN_TESTS_BUDGET:
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
                recent = fingerprint_history[ordered_type][-TDD_STALL_THRESHOLD:]
                if len(fingerprint_history[ordered_type]) >= TDD_STALL_THRESHOLD and len(set(recent)) == 1:
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
                    run_tests_executor=run_requested_tests,
                )

                latest_result = result_by_type.get(ordered_type, "")
                previous_failure_summary = (
                    self.test_driven_developer.get_last_verifier_report()
                    or summarize_batch_output(latest_result or output)
                )
                used_after = usage_by_type.get(ordered_type, 0)
                if full_layer_passed[ordered_type]:
                    break
                if used_after >= TDD_RUN_TESTS_BUDGET:
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
            active_test_type = None
            if (
                not full_layer_passed[ordered_type]
                and not environment_failure
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
            latest_result = result_by_type.get(test_type, "")
            group_passed = full_layer_passed[test_type]
            status_by_test_id = {
                str(test.get("test_id", "")).strip(): group_passed
                for test in groups[test_type.lower()]
                if str(test.get("test_id", "")).strip()
            }
            self.traceability.set_test_pass_statuses(status_by_test_id)
            if group_passed:
                await self._log(
                    "TestDrivenDeveloper",
                    f"TDD batch `{test_type}` passed after {usage_by_type.get(test_type, 0)}/{TDD_RUN_TESTS_BUDGET} run_tests call(s).",
                    node_id=node_id,
                )
                continue
            final_ok = False
            failed_types.append(test_type)
            if environment_failure and not latest_result:
                # This layer was never attempted: the workspace was already known
                # to be broken, so reporting stale verifier output here would be
                # misleading.
                failure_summaries.append(
                    f"{test_type}: not attempted - the workspace failed for environmental "
                    f"reasons ({environment_failure})."
                )
            else:
                failure_summary = (
                    summarize_batch_output(latest_result)
                    if latest_result
                    else self.test_driven_developer.get_last_verifier_report()
                    or summarize_batch_output(output)
                )
                if environment_failure:
                    failure_summary = (
                        f"[environment failure] {environment_failure}\n{failure_summary}"
                    )
                failure_summaries.append(f"{test_type}: {failure_summary}")
            used = usage_by_type.get(test_type, 0)
            if environment_failure:
                detail = f"environment failure ({environment_failure})"
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
                    "modified_files": [],
                },
            },
        )
        if not final_ok:
            return False

        unexpected_types = sorted(set(groups) - {item.lower() for item in TDD_BATCH_ORDER})
        if unexpected_types:
            await self._log(
                "TestDrivenDeveloper",
                f"Ignoring unsupported test batch type(s): {', '.join(unexpected_types)}.",
                status="warning",
                node_id=node_id,
            )

        return True

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
            if raw_test_id in generated_ids:
                raise ValueError(f"Generated duplicate test id `{raw_test_id}`.")
            generated_ids.add(raw_test_id)
            stored_item = {
                **test,
                "test_id": raw_test_id,
                "req_id": node_id,
                "type": test_type,
                "file_path": file_path,
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
            interface_id = str(interface.get("interface_id", "") or "").strip()
            if interface_id:
                self.traceability.set_interface_implemented(interface_id, True)

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


def collect_test_files(tests: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for test in tests:
        file_path = str(test.get("file_path", "")).strip()
        if file_path and file_path not in seen:
            seen.append(file_path)
    return seen


def canonical_test_type(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    for test_type in TDD_BATCH_ORDER:
        if normalized == test_type.lower():
            return test_type
    return None


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


def normalize_workspace_relative_path(value: Any, workspace_path: str) -> str:
    path = normalize_windows_extended_prefix_text(value)
    if not path:
        return ""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path == "/workspace":
        return ""
    if path.startswith("/workspace/"):
        return path[len("/workspace/") :].lstrip("/")

    workspace = normalize_windows_extended_prefix_text(Path(workspace_path).expanduser().resolve()).rstrip("/")
    if path == workspace:
        return ""
    if path.startswith(workspace + "/"):
        return path[len(workspace) + 1 :].lstrip("/")
    return path.lstrip("/")


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

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Awaitable, Callable

from agents.context.pipeline import context_pipeline
from agents.context.prompts.common import stage_skill_activation_policy
from agents.context.prompts.test_driven_developer import get_system_prompt, get_user_prompt
from agents.runtime.capabilities import is_test_file_path, normalize_manifest_path
from agents.runtime.factory import StageAgentBuild
from agents.runtime.stage_session import DEFAULT_STAGE_MODEL, StageSession
from agents.runtime.rebase_gate import cached_rebase_gate
from agents.skills.selection import SKILLS_SOURCE, implementation_skills
from agents.tools.build import build_install_dependencies_tool
from agents.tools.build import build_run_build_tool as build_system_run_build_tool
from agents.tools.test_failure_digest import (
    build_failure_digest,
    build_test_edit_stall_hint,
    format_failure_digest,
)
from agents.tools.test_manifest import DeclaredTestFile, TestManifestLock
from agents.tools.traceability import build_traceability_tools
from app_type_handler.test_results import TestRunResult
from core.test_types import canonical_test_type


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class TestDrivenDeveloper:
    """Deep-agents based TDD implementation adapter."""

    agent_name = "TestDrivenDeveloper"

    def __init__(
        self,
        log_cb: LogCallback | None = None,
        *,
        model: str | object | None = None,
        workspace_root: str | None = None,
        requirement_path: str | None = None,
        app_type: str | None = None,
        app_handler: Any | None = None,
        context_workspace_root: str | None = None,
        rebase_gate_provider: Callable[[], Any | None] | None = None,
    ) -> None:
        self.log_cb = log_cb
        self.model = model or os.environ.get("MODEL", DEFAULT_STAGE_MODEL)
        self.workspace_root = workspace_root
        self.requirement_path = requirement_path or ""
        self.app_type = app_type
        self.app_handler = app_handler
        # Context/session root: stays on the main workspace when the agent's
        # filesystem root is an isolated per-node worktree.
        self.context_workspace_root = context_workspace_root
        # Optional per-pass mid-phase replay gate (issue #127), shared by
        # every TDD-layer agent build of this adapter.
        self._rebase_gate_provider = rebase_gate_provider
        self._last_run_tests_result: str | None = None
        self._last_run_tests_exit_code: int | None = None
        self._last_verifier_report_text = ""
        self._last_failure_digest_text = ""
        self._last_modified_files: list[str] = []
        self._test_budget_exhausted = False
        self._current_test_files: list[str] = []
        self._current_test_type = ""
        # Per-run test-edit stall chain: per test layer, the last failure's
        # fingerprint plus the write-event position at that failure, so the
        # next same-fingerprint failure can ask "what was edited in between?".
        self._stage_build: StageAgentBuild | None = None
        # Per-run test-edit stall chain: per test layer, the last failure's
        # fingerprint plus the write-event position at that failure, so the
        # next same-fingerprint failure can ask "what was edited in between?".
        self._stall_chains: dict[str, tuple[str, int]] = {}

    def _rebase_gate(self) -> Any | None:
        return cached_rebase_gate(self)

    def _build_import_manifest_lock(self, node_tests: list[dict[str, Any]]) -> TestManifestLock | None:
        """Build read-only import-check metadata from the current test manifest.

        IMPLEMENT must repair the same test files TestGenerator declared, but
        its capability table intentionally allows broader product/test edits
        and must not inherit the generation-stage manifest write lock. The
        lock passed to ``build_stage_agent`` is therefore metadata only: the
        discipline consults it for import validation while capability and
        ownership gates continue to decide which paths TDD may edit.

        An empty/legacy node manifest returns ``None`` (fail-open), preserving
        diagnostic-probe and older callers that do not carry test rows.
        """

        rows: list[DeclaredTestFile] = []
        seen: set[str] = set()
        for item in node_tests:
            raw_path = str(item.get("file_path", "") or "").strip()
            path = normalize_manifest_path(raw_path)
            if not path or path in seen or not is_test_file_path(path):
                continue
            seen.add(path)
            rows.append(
                DeclaredTestFile(
                    file_path=path,
                    test_type=str(item.get("type", "") or "Unit"),
                    interface_ids=[
                        str(value).strip()
                        for value in item.get("interface_ids", []) or []
                        if str(value or "").strip()
                    ],
                    coverage_scope=str(item.get("coverage_scope", "owned") or "owned"),
                )
            )
        if not rows:
            return None
        lock = TestManifestLock()
        lock.declare(rows)
        return lock

    async def run(
        self,
        *,
        node_id: str,
        test_files: list[str],
        test_type: str,
        node_tests: list[dict[str, Any]] | None = None,
        preloaded_source: str | None = None,
        previous_failure_summary: str = "",
        run_tests_budget: int | None = None,
        run_tests_usage: dict[str, int] | None = None,
        stop_on_test_budget_exhausted: bool = True,
        run_tests_executor: Callable[[str | None, list[str] | None], Awaitable["TestRunResult"]] | None = None,
    ) -> str:
        self._last_run_tests_result = None
        self._last_run_tests_exit_code = None
        self._last_verifier_report_text = ""
        self._last_failure_digest_text = ""
        self._last_modified_files = []
        self._test_budget_exhausted = False
        self._stage_build = None
        self._stall_chains = {}
        self._current_test_files = [str(path or "").strip() for path in test_files if str(path or "").strip()]
        self._current_test_type = test_type
        current_node_tests = [item for item in (node_tests or []) if isinstance(item, dict)]
        manifest_lock = self._build_import_manifest_lock(current_node_tests)
        session = StageSession(
            agent_name=self.agent_name,
            node_id=node_id,
            phase="IMPLEMENT",
            model=self.model,
            log_cb=self.log_cb,
            workspace_root=self.workspace_root,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            context_workspace_root=self.context_workspace_root,
            thread_suffix=self._current_test_type or "batch",
            test_type=self._current_test_type,
        )

        def normalize_requested_path(value: Any) -> str:
            path = str(value or "").strip().replace("\\", "/")
            if path.startswith("/workspace/"):
                return path[len("/workspace/") :].lstrip("/")
            if path == "/workspace":
                return ""
            return path.lstrip("./")

        context_text = session.build_context(
            preloaded_source=preloaded_source,
            target_test_files=self._current_test_files,
        )
        interface_contract = context_pipeline.get_interface_contract_context(node_id)
        required_skill_names = implementation_skills(
            interface_contract=interface_contract,
            previous_failure_summary=previous_failure_summary,
        )

        async def run_tests(test_type: str | None = None, test_files: list[str] | None = None) -> str:
            """Run current-node tests. Optionally pass a test_type or exact test_files from the manifest."""

            requested_type = str(test_type or self._current_test_type).strip()
            requested_files = [
                path
                for value in (test_files or [])
                if (path := normalize_requested_path(value))
            ]
            usage = run_tests_usage if run_tests_usage is not None else None
            if usage is not None or run_tests_budget is not None:
                usage = usage if usage is not None else {}
                used = int(usage.get("run_tests", 0))
                if run_tests_budget is not None and used >= run_tests_budget:
                    self._test_budget_exhausted = True
                    await self._log(
                        f"`run_tests` budget exhausted at {used}/{run_tests_budget}.",
                        status="error",
                        node_id=node_id,
                    )
                    return (
                        f"Tool budget exhausted for `run_tests` ({used}/{run_tests_budget}). "
                        "Stop and summarize the current status."
                    )
                usage["run_tests"] = used + 1
                await self._log(
                    f"`run_tests` usage {usage['run_tests']}" + (f"/{run_tests_budget}" if run_tests_budget is not None else "") + ".",
                    node_id=node_id,
                )
            if requested_files and test_type is None:
                file_types = {
                    str(item.get("type", "") or "").strip()
                    for item in current_node_tests
                    if str(item.get("file_path", "") or "").strip() in requested_files
                    and str(item.get("type", "") or "").strip()
                }
                if len(file_types) == 1:
                    requested_type = next(iter(file_types))
                elif len(file_types) > 1:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        "run_tests(test_files=[...]) received files from multiple test types; pass one type at a time.\n"
                    )
            if not requested_files and test_type:
                requested_files = [
                    str(item.get("file_path", "") or "").strip()
                    for item in current_node_tests
                    if str(item.get("type", "") or "").strip() == requested_type
                    and str(item.get("file_path", "") or "").strip()
                ]
                if not requested_files:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"No current-node tests are registered for test_type={requested_type!r}.\n"
                    )
            result = (
                TestRunResult(
                    exit_code=1,
                    output=(
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        "System test runner is not configured for this TDD session.\n"
                    ),
                )
                if run_tests_executor is None
                else await run_tests_executor(requested_type, requested_files or None)
            )
            # One call computes the stall hint from the PRE-advance chain and
            # then advances it, so the ordering contract ("hint first, then
            # chain update") cannot drift apart. The chain key is the
            # canonical layer name, which always matches the key core.phases
            # uses for its in-session digest lookup: bare run_tests calls
            # resolve to _current_test_type (the layer this run() serves),
            # aliases never carry a runnable type through this closure (the
            # file-resolution gate rejects them first), and after an in-session
            # layer advance the stale layer name is rejected by the executor
            # before any run - so only agreed-upon keys ever enter the chain.
            chain_type = canonical_test_type(requested_type) or requested_type
            stall_hint = self._advance_stall_chain(chain_type, result)
            self._last_run_tests_result = result.output
            self._last_run_tests_exit_code = result.exit_code
            self._record_failure_state(result, test_edit_hint=stall_hint)
            return result.output

        if self.app_handler is None:
            async def run_build() -> str:
                """Run system-defined build verification when an app handler is configured."""

                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    "System build runner is not configured for this TDD session.\n"
                )
        else:
            run_build = build_system_run_build_tool(app_handler=self.app_handler, node_id=node_id, log_cb=self.log_cb)

        if self.app_handler is None:
            async def install_dependencies(package: str, target: str = "backend") -> str:
                """Install one npm package into the workspace when an app handler is configured."""

                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    "Package installation is not configured for this TDD session.\n"
                )
        else:
            install_dependencies = build_install_dependencies_tool(
                app_handler=self.app_handler, node_id=node_id, log_cb=self.log_cb
            )

        traceability_tools = build_traceability_tools(node_id=node_id, log_cb=self.log_cb)
        built = session.build_agent(
            name="test_driven_developer",
            stage="implementation",
            system_prompt="\n\n".join(
                [get_system_prompt(), stage_skill_activation_policy(required_skill_names)]
            ),
            response_format=None,
            rebase_gate=self._rebase_gate(),
            tools=[run_tests, run_build, install_dependencies, *traceability_tools],
            skills=[SKILLS_SOURCE],
            test_manifest_lock=manifest_lock,
        )
        # Live handle for the stall hint: the discipline's write-event log is
        # queryable mid-session, while materialized_paths is read only at the
        # end for the diff hint below.
        self._stage_build = built
        message = get_user_prompt(
            node_id=node_id,
            dynamic_context=context_text,
            interface_contract=interface_contract,
            test_files=self._current_test_files,
            test_type=test_type,
            node_tests=current_node_tests,
            previous_failure_summary=previous_failure_summary,
            merge_conflict=self._load_merge_conflict_context(node_id),
        )
        await self._log(f"required-skills: {', '.join(required_skill_names) or 'none'}", node_id=node_id)
        await self._log("Invoking TDD implementation.", node_id=node_id)
        payload = await session.invoke(built, message=message)
        final_text = self._payload_to_final_text(payload)
        # Capture the session's writes (discipline ground truth, virtual
        # /workspace/ paths) for the cross-session diff hint. Deleted-after-
        # write paths are excluded by the discipline itself.
        self._last_modified_files = [
            normalize_manifest_path(path)
            for path in built.materialized_paths()
        ]
        if self._test_budget_exhausted and stop_on_test_budget_exhausted:
            return "BUDGET_EXHAUSTED"
        if "IMPLEMENTED" in final_text.upper() and self._last_run_tests_exit_code != 0:
            final_text = (
                "Error: latest run_tests result did not pass with Exit Code: 0, "
                "so IMPLEMENTED is not accepted for this batch."
            )
        await self._log("TDD session completed.", node_id=node_id)
        return final_text

    def get_last_run_tests_result(self) -> str | None:
        return self._last_run_tests_result

    def get_last_verifier_report(self) -> str:
        return self._last_verifier_report_text

    def get_last_failure_digest(self) -> str:
        """Structured per-test digest of the last failed in-session run.

        Empty when the last run passed or the session never ran tests. The
        workflow prefers this over ``get_last_verifier_report`` for the
        cross-session handoff: it carries per-test locations and
        expected/received detail instead of a raw tail, so the next session
        starts at the failure instead of re-localizing it.
        """

        return self._last_failure_digest_text

    def get_last_modified_files(self) -> list[str]:
        """Workspace-relative paths this session wrote (discipline ground truth).

        Populated only when the backing agent exposes the stage discipline;
        stays empty otherwise. Feeds the tdd_handoff diff hint so the next
        session knows what its predecessor edited without re-reading files.
        """

        return list(self._last_modified_files)

    def test_edit_stall_hint(self, test_type: str, fingerprint: str) -> str:
        """The test-edit stall hint for a just-returned failed ``run_tests`` result.

        Non-empty only when this failure repeats the previous failure's
        fingerprint for *test_type* and everything edited since that previous
        failure is a test file (manifest-declared or test-shaped). Read by
        ``core.phases`` while composing the in-session digest — at which point
        the chain state below is still the previous failure's — so one failure
        renders the identical hint on the in-session and cross-session
        surfaces.
        """

        chain = self._stall_chains.get(test_type)
        if not chain or chain[0] != fingerprint:
            return ""
        return build_test_edit_stall_hint(
            fingerprint=fingerprint,
            previous_fingerprint=chain[0],
            edited_paths=self._stage_write_events()[chain[1] :],
            manifest_test_files=self._current_test_files,
        )

    def _stage_write_events(self) -> list[str]:
        build = self._stage_build
        return build.write_events() if build is not None else []

    def _advance_stall_chain(self, test_type: str, result: TestRunResult) -> str:
        """Compute the stall hint for this result, then advance the chain.

        The hint must reflect the chain state BEFORE this failure is recorded
        (it compares against the previous failure), so both happen here in
        that order and the ``run_tests`` closure cannot get them reversed.
        Only real failures extend the chain: layer/budget gate rejections
        carry no error key line (``has_error_fingerprint``) and would
        otherwise break - or fabricate - the consecutive-repeat condition. A
        passing run clears the chain: the next same-fingerprint failure
        starts a fresh one.
        """

        stall_hint = self.test_edit_stall_hint(test_type, result.fingerprint)
        if result.passed_run:
            self._stall_chains.pop(test_type, None)
        elif result.has_error_fingerprint:
            self._stall_chains[test_type] = (result.fingerprint, len(self._stage_write_events()))
        return stall_hint

    @staticmethod
    def _load_merge_conflict_context(node_id: str) -> dict[str, Any] | None:
        """Conflict paths recorded when this node's IMPLEMENT merge conflicted.

        The workflow re-queues a conflicted IMPLEMENT once and stores the
        conflicting paths (owned by a parallel sibling) in the node session
        so this retry can steer edits away from them. The retry flag must be
        set and the recorded phase must be ``implement``: a fresh IMPLEMENT
        pass (manual retry, resume) must never be guided by a previous run's
        stale conflict paths, and a DESIGN-conflict record must not leak into
        the TDD prompt.
        """

        from core import sessions

        session = sessions.load_node_session(node_id)
        if not session.get("merge_conflict_retry_used"):
            return None
        context = session.get("merge_conflict_context")
        if not isinstance(context, dict) or context.get("phase") != "implement":
            return None
        paths = context.get("paths")
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            return None
        return {"paths": paths, "phase": "implement"}

    @staticmethod
    def _payload_to_final_text(payload: dict[str, Any]) -> str:
        summary = str(payload.get("summary", "") or "").strip()
        if summary:
            return summary
        for key in ("final", "result", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _record_failure_state(self, result: TestRunResult, *, test_edit_hint: str = "") -> None:
        """Record the failure evidence of the latest run for the next session.

        Reads the run's structured fields — the workflow already interpreted
        the transcription once when producing the :class:`TestRunResult`, so
        nothing here re-parses the rendered text.
        """

        if result.exit_code == 0:
            self._last_verifier_report_text = ""
            self._last_failure_digest_text = ""
            return
        digest = build_failure_digest(result.output)
        self._last_failure_digest_text = format_failure_digest(
            digest,
            test_type=self._current_test_type,
            raw_output_path=result.run_log_path or None,
            fingerprint=result.fingerprint,
            environment_failure=result.environment_failure,
            build=result.build_note,
            served=result.served_verdict,
            test_edit_hint=test_edit_hint,
        )
        lines = [line for line in (result.output or "").splitlines() if line.strip()]
        excerpt = "\n".join(lines[-40:])
        # Same fingerprint source as the digest above: the inline keyword scan
        # this replaced keyed on the E2E preamble's informational "Note:" line
        # ("This is expected when ...") and mislabeled every E2E failure.
        self._last_verifier_report_text = (
            "<failure_analysis>\n"
            f"fingerprint: {result.fingerprint}\n"
            "latest_test_output_excerpt:\n"
            f"{excerpt}\n"
            "</failure_analysis>"
        )

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(self.agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result

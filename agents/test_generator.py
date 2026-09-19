from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from core import sessions
from agents.context.pipeline import context_pipeline
from agents.context.prompts.common import stage_skill_activation_policy
from agents.context.prompts.test_generator import get_system_prompt, get_user_prompt
from agents.results import normalize_test_manifest_payload
from agents.runtime.checkpointer import get_project_thread_namespace
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from agents.skills.selection import SKILLS_SOURCE, test_generation_skills
from agents.tools.test_manifest import (
    DeclaredTestFile,
    TestManifestLock,
    build_declare_test_manifest_tool,
    canonical_test_type,
    normalize_coverage_scope,
    normalize_manifest_path,
    reconcile_declared_manifest,
)
from agents.tools.traceability import build_traceability_tools
from langgraph.errors import GraphRecursionError


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class TestManifestItem(BaseModel):
    test_id: str = Field(description="Stable test artifact id.")
    req_id: str = Field(description="Requirement node id covered by this test.")
    interface_ids: list[str] = Field(default_factory=list, description="Covered interface ids.")
    coverage_scope: str = Field(
        default="owned",
        description="owned for current-node behavior, dependency for dependency regression, shared for shared contract coverage.",
    )
    type: str = Field(description="Unit, Integration, or E2E.")
    file_path: str = Field(description="Workspace-relative test file path; must be a path that was declared via declare_test_manifest and actually written in this pass.")
    first_line: str = Field(default="", description="Exact first line in the written test file.")


class TestGenerationResponse(BaseModel):
    summary: str = Field(default="", description="Short test-design summary.")
    tests: list[TestManifestItem] = Field(default_factory=list, description="Generated test manifest.")
    files_written: list[str] = Field(default_factory=list, description="Workspace-relative files written or edited.")


class TestGenerator:
    """Deep-agents based test-generation stage adapter."""

    agent_name = "TestGenerator"

    def __init__(
        self,
        log_cb: LogCallback | None = None,
        *,
        model: str | object | None = None,
        workspace_root: str | None = None,
        requirement_path: str | None = None,
        app_type: str | None = None,
        context_workspace_root: str | None = None,
    ) -> None:
        self.log_cb = log_cb
        self.model = model or os.environ.get("MODEL", "openai:gpt-5.4")
        self.workspace_root = workspace_root
        self.requirement_path = requirement_path or ""
        self.app_type = app_type
        # Context/session root: stays on the main workspace when the agent's
        # filesystem root is an isolated per-node worktree.
        self.context_workspace_root = context_workspace_root

    async def run(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        *,
        preloaded_source: str | None = None,
    ) -> tuple[list[dict[str, Any]] | None, str]:
        workspace_root = str(Path(
            self.workspace_root
            or context_pipeline.config.workspace_dir
            or os.environ.get("ARC_WORKSPACE_ROOT")
            or os.getcwd()
        ).expanduser().resolve())
        app_type = (self.app_type or context_pipeline.config.app_type or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        required_skill_names = test_generation_skills(requirement_data)
        context_pipeline.configure(
            workspace_dir=self.context_workspace_root or workspace_root,
            app_type=app_type,
        )
        static_context, dynamic_context = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
            map_workspace_dir=workspace_root,
        )
        interface_contract = context_pipeline.get_interface_contract_context(node_id)
        context_text = "\n\n".join(part.strip() for part in (static_context, dynamic_context) if part.strip())
        current_interfaces = self._current_node_interfaces(node_id)
        current_interface_ids = self._current_interface_ids(node_id, current_interfaces)
        manifest_lock = TestManifestLock()
        agent = build_stage_agent(
            name="test_generator",
            stage="test_generation",
            model=self.model,
            system_prompt="\n\n".join(
                [get_system_prompt(), stage_skill_activation_policy(required_skill_names)]
            ),
            response_format=TestGenerationResponse,
            workspace_root=workspace_root,
            writable_roots=[workspace_root],
            skills=[SKILLS_SOURCE],
            memory=[],
            tools=[
                *build_traceability_tools(
                    node_id=node_id,
                    log_cb=self.log_cb,
                    current_interfaces=current_interfaces,
                ),
                build_declare_test_manifest_tool(
                    node_id=node_id,
                    manifest_lock=manifest_lock,
                    validate_test_path=self._make_path_validator(app_type, workspace_root),
                    log_cb=self.log_cb,
                    current_interface_ids=current_interface_ids,
                    require_interface_coverage=bool(current_interface_ids),
                ),
            ],
            node_id=node_id,
            claims_workspace_root=self.context_workspace_root or workspace_root,
            test_manifest_lock=manifest_lock,
        )

        message = get_user_prompt(
            node_id=node_id,
            requirement_data=requirement_data,
            dynamic_context=context_text,
            interface_contract=interface_contract,
        )
        await self._log(f"required-skills: {', '.join(required_skill_names) or 'none'}", node_id=node_id)
        await self._log("Invoking test generation.", node_id=node_id)
        try:
            raw_payload = await ainvoke_stage_agent(
                agent,
                message=message,
                context=AgentRuntimeContext(
                    node_id=node_id,
                    phase="DESIGN",
                    app_type=app_type,
                    workspace_root=workspace_root,
                    requirement_path=self.requirement_path,
                ),
                thread_id=f"{get_project_thread_namespace()}:{node_id}:DESIGN:TestGenerator",
                label=self.agent_name,
                log_cb=self.log_cb,
            )
        except GraphRecursionError as exc:
            raw_payload = await self._salvage_step_budget(
                node_id=node_id, manifest_lock=manifest_lock, agent=agent, exc=exc
            )
            if raw_payload is None:
                raise
        tests = normalize_test_manifest_payload(raw_payload)
        tests, output_text = await self._reconcile_first_pass(
            node_id=node_id,
            tests=tests,
            raw_payload=raw_payload,
            manifest_lock=manifest_lock,
            agent=agent,
        )
        if tests is None:
            return None, output_text
        await self._log(f"Test generation returned {len(tests)} test artifact(s).", node_id=node_id)
        return tests, output_text

    async def _salvage_step_budget(
        self,
        *,
        node_id: str,
        manifest_lock: TestManifestLock,
        agent: Any,
        exc: GraphRecursionError,
    ) -> dict[str, Any] | None:
        """Complete a first pass whose step budget ran out with all work done.

        The 2026-09-19 arc-output3 run crashed exactly here: the model wrote
        every declared test file, then looped on delete-rewrite cycles until
        LangGraph raised ``GraphRecursionError`` — failing the whole DESIGN
        task while the complete, on-disk test suite was usable. When the
        discipline's materialized paths cover the locked manifest, the
        declared rows are re-attached mechanically (the same reconciliation
        the normal path applies to a dropped row) and the pass completes
        with a warning. Anything less — no locked manifest, nothing
        materialized, or a declared file missing — returns ``None`` so the
        error propagates: a partial suite salvaged from a crashed session is
        a silent quality cut, not a rescue.
        """

        discipline = getattr(agent, "arc_stage_discipline", None)
        written_paths = discipline.materialized_paths() if discipline is not None else []
        if not manifest_lock.locked or not written_paths:
            return None
        written = {normalize_manifest_path(path) for path in written_paths}
        missing = sorted(path for path in manifest_lock.declared_files if path not in written)
        if missing:
            await self._log(
                "Test generation hit its step budget with declared file(s) never written: "
                f"{', '.join(missing)}; not salvaging, failing the stage.",
                status="warning",
                node_id=node_id,
            )
            return None
        tests = reconcile_declared_manifest(
            manifest_items=[],
            manifest_lock=manifest_lock,
            written_paths=written_paths,
            node_id=node_id,
        )["tests"]
        await self._log(
            "Test generation session hit its step budget after every declared manifest file "
            f"was written; salvaging {len(tests)} manifest row(s) mechanically instead of "
            "failing the node.",
            status="warning",
            node_id=node_id,
        )
        return {
            "summary": (
                "Step budget reached after all declared test files were written; manifest "
                "rows re-attached from the locked declaration."
            ),
            "tests": tests,
            "files_written": [normalize_manifest_path(path) for path in written_paths],
        }

    async def _reconcile_first_pass(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
        raw_payload: dict[str, Any] | None,
        manifest_lock: TestManifestLock,
        agent: Any,
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """Reconcile the returned manifest with the declaration and the disk.

        The reconciliation contract (see
        ``agents.tools.test_manifest.reconcile_declared_manifest``): entries
        for undeclared paths fail the pass (the model returned tests it was
        forbidden to write — or never wrote); declared-but-dropped rows are
        re-attached mechanically; declared-but-unwritten entries are removed
        with a diagnostic.
        """

        if not manifest_lock.locked:
            return tests, json.dumps(raw_payload or {"tests": tests}, ensure_ascii=False)

        discipline = getattr(agent, "arc_stage_discipline", None)
        written_paths = discipline.materialized_paths() if discipline is not None else []
        result = reconcile_declared_manifest(
            manifest_items=tests,
            manifest_lock=manifest_lock,
            written_paths=written_paths,
            node_id=node_id,
        )
        tests = result["tests"]
        output_text = json.dumps(raw_payload or {"tests": tests}, ensure_ascii=False)
        if result["undeclared_paths"]:
            # Hard contract violation: returning manifest entries that were
            # never declared (hence never writable) means either the model
            # fabricated rows or wrote files through an unlocked path. Either
            # way the pass is invalid; None fails the DESIGN phase loudly.
            await self._log(
                "Test generation returned manifest entries for paths that were never "
                f"declared: {', '.join(result['undeclared_paths'])}.",
                status="error",
                node_id=node_id,
            )
            return None, output_text
        for path in result["reattached_paths"]:
            await self._log(
                f"Test file `{path}` was written but dropped from the returned manifest; "
                "its entry was re-attached from the declaration.",
                status="warning",
                node_id=node_id,
            )
        for path in result["unwritten_paths"]:
            await self._log(
                f"Manifest entry `{path}` was declared but never written; dropping it "
                "from the stored manifest.",
                status="warning",
                node_id=node_id,
            )
        return tests, output_text

    def _make_path_validator(self, app_type: str, workspace_root: str) -> Callable[[str, str], str | None] | None:
        """App-type placement validator for the declare tool (early check).

        The handler instance is heavyweight (it may run template setup); only
        its pure ``validate_test_path`` is needed here, so a tiny adapter is
        returned instead of holding a handler for the whole stage. Any
        construction failure disables the early check — the workflow's own
        manifest validation stays authoritative.
        """

        try:
            from app_type_handler import create_app_type_handler

            handler = create_app_type_handler(
                app_type=app_type,
                workspace_path=workspace_root,
                requirement_path=self.requirement_path or "",
                interface_designer=None,
                log_cb=None,
            )
        except Exception:
            return None
        validate = getattr(handler, "validate_test_path", None)
        if not callable(validate):
            return None
        return lambda test_type, file_path: validate(test_type, file_path)

    async def repair_green_baseline(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        *,
        green_evidence: list[dict[str, Any]],
        previous_manifest: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """Re-ask the same thread to delete or rework tests that passed the
        system-run baseline before any implementation exists.

        ``green_evidence`` carries the file paths whose baseline runs already
        exited 0 (plus their test types and a short output summary). The
        generated suite must be RED against the DESIGN skeleton: a test that
        passes now verifies nothing about the node's own behavior and would be
        silently waved through by the tautology fast path at IMPLEMENT time.
        """
        workspace_root = str(Path(
            self.workspace_root
            or context_pipeline.config.workspace_dir
            or os.environ.get("ARC_WORKSPACE_ROOT")
            or os.getcwd()
        ).expanduser().resolve())
        app_type = (self.app_type or context_pipeline.config.app_type or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        current_interfaces = self._current_node_interfaces(node_id)
        current_interface_ids = self._current_interface_ids(node_id, current_interfaces)
        # Pre-seed the manifest lock with the previous manifest's paths: a
        # repair pass may only delete or rewrite existing test files, never
        # introduce a new path. This closes the rename escape (delete the
        # green file, re-add the same tautology under a fresh name) at the
        # write gate, before the re-baseline could ever see it.
        manifest_lock = TestManifestLock(
            declared_files={
                path: DeclaredTestFile(
                    file_path=path,
                    test_type=canonical_test_type(item.get("type")) or "Unit",
                    interface_ids=[str(i) for i in item.get("interface_ids") or [] if str(i or "").strip()],
                    coverage_scope=normalize_coverage_scope(item.get("coverage_scope")) or "owned",
                )
                for item in previous_manifest
                if (path := str(item.get("file_path", "") or "").strip())
            }
        )
        agent = build_stage_agent(
            name="test_generator",
            stage="test_generation",
            model=self.model,
            system_prompt=get_system_prompt(),
            response_format=TestGenerationResponse,
            workspace_root=workspace_root,
            writable_roots=[workspace_root],
            skills=[],
            memory=[],
            tools=[
                *build_traceability_tools(
                    node_id=node_id,
                    log_cb=self.log_cb,
                    current_interfaces=current_interfaces,
                ),
                build_declare_test_manifest_tool(
                    node_id=node_id,
                    manifest_lock=manifest_lock,
                    validate_test_path=self._make_path_validator(app_type, workspace_root),
                    log_cb=self.log_cb,
                    current_interface_ids=current_interface_ids,
                    require_interface_coverage=bool(current_interface_ids),
                ),
            ],
            node_id=node_id,
            claims_workspace_root=self.context_workspace_root or workspace_root,
            test_manifest_lock=manifest_lock,
        )
        message = self._green_rejection_message(
            node_id=node_id,
            green_evidence=green_evidence,
            previous_manifest=previous_manifest,
        )
        await self._log(
            "Green baseline rejection: requesting rework or removal of "
            f"{len(green_evidence)} passing test file(s).",
            status="warning",
            node_id=node_id,
        )
        raw_payload = await ainvoke_stage_agent(
            agent,
            message=message,
            context=AgentRuntimeContext(
                node_id=node_id,
                phase="DESIGN",
                app_type=app_type,
                workspace_root=workspace_root,
                requirement_path=self.requirement_path,
            ),
            thread_id=f"{get_project_thread_namespace()}:{node_id}:DESIGN:TestGenerator",
            label=self.agent_name,
            log_cb=self.log_cb,
        )
        tests = normalize_test_manifest_payload(raw_payload)
        output_text = json.dumps(raw_payload or {"tests": tests}, ensure_ascii=False)
        await self._log(f"Green baseline rework returned {len(tests)} test artifact(s).", node_id=node_id)
        return tests, output_text

    def _green_rejection_message(
        self,
        *,
        node_id: str,
        green_evidence: list[dict[str, Any]],
        previous_manifest: list[dict[str, Any]],
    ) -> str:
        evidence_lines: list[str] = []
        for item in green_evidence:
            path = str(item.get("file_path", "") or "").strip()
            test_type = str(item.get("type", "") or "").strip()
            summary = str(item.get("output_summary", "") or "").strip()
            line = f"- `{path}` ({test_type}): passed the system-run baseline with Exit Code: 0"
            if summary:
                line += f" — {summary}"
            evidence_lines.append(line)
        return (
            f"### Current Node\n`{node_id}`\n\n"
            "### System Rejection: GREEN Baseline Tests\n"
            "The system ran every test file you generated against the current workspace, "
            "BEFORE any implementation exists (only the interface design skeletons are in place). "
            "A generated test that already PASSES against an unimplemented node verifies nothing: "
            "it is either asserting placeholder/scaffold behavior, duplicating coverage that cannot "
            "fail, or testing a dependency's behavior instead of this node's owned outcome.\n\n"
            "The following test files PASSED the baseline run and are REJECTED:\n"
            + "\n".join(evidence_lines)
            + "\n\n### Required Repair\n"
            "For each rejected file, choose exactly one:\n"
            "1. **Delete** the test file (`delete` tool) if its coverage duplicates another "
            "current-node test or the scenario should not be node-local, and drop its manifest "
            "entries from the returned `tests` list.\n"
            "2. **Rewrite** the test so it drives the requirement's target behavior through the "
            "node's own interface contract and would verifiably FAIL against the current "
            "skeleton-only workspace.\n\n"
            "Rules:\n"
            "- Do not weaken or delete tests for files that are NOT listed above; their baseline "
            "runs verifiably failed (RED), which is the correct state.\n"
            "- Do not create new test files in this repair: the test-file manifest is locked to "
            "the existing files. Rework the content of a rejected file in place, or delete it.\n"
            "- Do not add setup/teardown guards, conditionals, or `skip` marks that make a test "
            "pass on the skeleton; the target behavior must be asserted unconditionally.\n"
            "- Do not run the tests yourself; the system re-runs the baseline after this pass.\n"
            "- Return the FULL updated manifest (`tests`) and `files_written`/deleted paths in "
            "`files_written` semantics of your final structured answer.\n\n"
            "### Previous Manifest (for reference)\n"
            f"```json\n{json.dumps(previous_manifest, ensure_ascii=False, indent=2, default=str)}\n```"
        )

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(self.agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _current_node_interfaces(node_id: str) -> list[dict[str, Any]]:
        session = sessions.load_node_session(node_id)
        interfaces = session.get("interfaces")
        if not isinstance(interfaces, list):
            return []
        return [dict(item) for item in interfaces if isinstance(item, dict)]

    @staticmethod
    def _current_interface_ids(
        node_id: str,
        interfaces: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        source = interfaces if interfaces is not None else TestGenerator._current_node_interfaces(node_id)
        return [
            str(item.get("interface_id") or "").strip()
            for item in source
            if str(item.get("interface_id") or "").strip()
        ]

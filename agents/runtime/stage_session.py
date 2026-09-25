"""Shared session interface for the three stage adapters.

DESIGN (``InterfaceDesigner``), test generation (``TestGenerator``) and TDD
(``TestDrivenDeveloper``) used to repeat the same preamble in every pass:
resolve the workspace root and app type, configure the context pipeline and
assemble the agent context, build the stage agent with the same shared
parameters, invoke it on the same thread-identity pattern, and recover
stage-discipline ground truth by probing an undeclared
``arc_stage_discipline`` attribute with ``getattr``. :class:`StageSession`
owns that preamble once; adapters keep only stage-specific decisions
(prompts, response formats, tools, and their own payload shaping).

A session binds one run of one adapter for one node: identity (``node_id``,
``phase``, thread suffix, test layer), configuration (model, roots, app type,
log callback). Repair or re-ask passes that re-invoke the same thread reuse
the same session.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.context.pipeline import context_pipeline
from agents.runtime.checkpointer import get_project_thread_namespace
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import StageAgentBuild, StageKind, build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

DEFAULT_STAGE_MODEL = "openai:gpt-5.4"


class StageSession:
    """One stage-agent session: config, context, thread, invocation, and the
    declared discipline accessor.

    Payload normalization enters here too: :meth:`invoke` returns the
    runner-normalized payload (``extract_payload``), and adapters layer their
    stage-specific shaping on top of it.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        node_id: str,
        phase: str,
        model: str | object | None = None,
        log_cb: LogCallback | None = None,
        workspace_root: str | None = None,
        requirement_path: str = "",
        app_type: str | None = None,
        context_workspace_root: str | None = None,
        thread_suffix: str = "",
        test_type: str = "",
    ) -> None:
        self.agent_name = agent_name
        self.node_id = node_id
        self.phase = phase
        self.model = model or os.environ.get("MODEL", DEFAULT_STAGE_MODEL)
        self.log_cb = log_cb
        self.requirement_path = requirement_path or ""
        self.workspace_root = self.resolve_workspace_root(workspace_root)
        self.app_type = self.resolve_app_type(app_type)
        # Where traceability-adjacent context (node sessions, the visual cache)
        # is read from. Equals workspace_root except when the agent works in an
        # isolated per-node worktree: sessions and caches stay in the main
        # workspace while the agent's filesystem root is the worktree.
        self.context_workspace_root = context_workspace_root
        self.thread_suffix = thread_suffix
        self.test_type = test_type

    # -- configuration resolution -------------------------------------------------

    @staticmethod
    def resolve_workspace_root(explicit: str | None) -> str:
        """Agent filesystem root: explicit > context pipeline > env > cwd."""

        return str(Path(
            explicit
            or context_pipeline.config.workspace_dir
            or os.environ.get("ARC_WORKSPACE_ROOT")
            or os.getcwd()
        ).expanduser().resolve())

    @staticmethod
    def resolve_app_type(explicit: str | None) -> str:
        """App type: explicit > context pipeline > env > ``web``."""

        return (
            explicit
            or context_pipeline.config.app_type
            or os.environ.get("ARC_APP_TYPE")
            or "web"
        ).strip().lower()

    @property
    def claims_workspace_root(self) -> str:
        """Integration workspace hosting the shared file-claim registry.

        The main workspace when the agent's filesystem root is an isolated
        per-node worktree, the one shared workspace in serial mode.
        """

        return self.context_workspace_root or self.workspace_root

    # -- context assembly -----------------------------------------------------------

    def build_context(self, **split_kwargs: Any) -> str:
        """Configure the context pipeline for this session and assemble the
        agent context: the static and dynamic blocks joined by blank lines."""

        context_pipeline.configure(
            workspace_dir=self.context_workspace_root or self.workspace_root,
            app_type=self.app_type,
        )
        static_context, dynamic_context = context_pipeline.build_agent_context_split(
            node_id=self.node_id,
            agent_type=self.agent_name,
            map_workspace_dir=self.workspace_root,
            **split_kwargs,
        )
        return "\n\n".join(part.strip() for part in (static_context, dynamic_context) if part.strip())

    # -- agent construction -----------------------------------------------------------

    def build_agent(
        self,
        *,
        name: str,
        stage: StageKind,
        system_prompt: str,
        response_format: object | None,
        tools: list[object] | None,
        skills: list[str] | None,
        test_manifest_lock: Any | None = None,
        stage_write_set_lock: Any | None = None,
        enforce_node_test_domain: bool = False,
        pending_contract_registry: Any | None = None,
        max_design_writes: int | None = None,
        rebase_gate: Any | None = None,
    ) -> StageAgentBuild:
        """Build one stage agent with the session's shared parameters; stage
        differences arrive only through the arguments."""

        return build_stage_agent(
            name=name,
            stage=stage,
            model=self.model,
            system_prompt=system_prompt,
            response_format=response_format,
            workspace_root=self.workspace_root,
            writable_roots=[self.workspace_root],
            skills=skills,
            memory=[],
            tools=tools,
            node_id=self.node_id,
            claims_workspace_root=self.claims_workspace_root,
            test_manifest_lock=test_manifest_lock,
            stage_write_set_lock=stage_write_set_lock,
            enforce_node_test_domain=enforce_node_test_domain,
            pending_contract_registry=pending_contract_registry,
            app_type=self.app_type,
            max_design_writes=max_design_writes,
            rebase_gate=rebase_gate,
        )

    # -- thread identity -----------------------------------------------------------

    def thread_id(self) -> str:
        """Stable conversation thread for this session:
        ``{namespace}:{node}:{PHASE}:{agent}[:suffix]``."""

        base = f"{get_project_thread_namespace()}:{self.node_id}:{self.phase}:{self.agent_name}"
        return f"{base}:{self.thread_suffix}" if self.thread_suffix else base

    def runtime_context(self) -> AgentRuntimeContext:
        """Per-run metadata handed to tools, logging, and middleware."""

        return AgentRuntimeContext(
            node_id=self.node_id,
            phase=self.phase,
            app_type=self.app_type,
            workspace_root=self.workspace_root,
            requirement_path=self.requirement_path,
            test_type=self.test_type,
        )

    # -- invocation --------------------------------------------------------------------

    async def invoke(self, built: StageAgentBuild, *, message: str) -> dict[str, Any]:
        """Invoke one stage-agent session on the session's thread.

        Returns the runner-normalized payload; usage attribution, streaming,
        step budget, and payload extraction stay in
        ``agents.runtime.runners.ainvoke_stage_agent``.
        """

        return await ainvoke_stage_agent(
            built.agent,
            message=message,
            context=self.runtime_context(),
            thread_id=self.thread_id(),
            label=self.agent_name,
            log_cb=self.log_cb,
        )

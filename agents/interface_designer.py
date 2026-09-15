from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from agents.context.pipeline import context_pipeline
from agents.context.prompts.common import stage_skill_activation_policy
from agents.context.prompts.interface_designer import get_system_prompt, get_user_prompt
from agents.runtime.checkpointer import get_project_thread_namespace
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from agents.skills.planning import load_skill_plan_extras
from agents.skills.selection import SKILLS_SOURCE, interface_design_skills
from agents.tools.traceability import build_traceability_tools


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class InterfaceDesignResponse(BaseModel):
    summary: str = Field(default="", description="Short design-stage summary.")
    interfaces: list[dict[str, Any]] = Field(default_factory=list, description="Interface contracts for the current node.")
    files_written: list[str] = Field(default_factory=list, description="Workspace-relative files written or edited.")


class InterfaceDesigner:
    """Deep-agents based interface-design stage adapter."""

    agent_name = "InterfaceDesigner"

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
        # Where traceability-adjacent context (node sessions, the visual cache)
        # is read from. Equals workspace_root except when the agent works in an
        # isolated per-node worktree: sessions and caches stay in the main
        # workspace while the agent's filesystem root is the worktree.
        self.context_workspace_root = context_workspace_root

    async def run(
        self,
        *,
        node_id: str,
        requirement_data: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_root = str(Path(
            self.workspace_root
            or context_pipeline.config.workspace_dir
            or os.environ.get("ARC_WORKSPACE_ROOT")
            or os.getcwd()
        ).expanduser().resolve())
        app_type = (self.app_type or context_pipeline.config.app_type or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        selected_skill_names = interface_design_skills(
            requirement_data,
            extra_skills=load_skill_plan_extras(node_id, "design"),
        )
        context_pipeline.configure(
            workspace_dir=self.context_workspace_root or workspace_root,
            app_type=app_type,
        )
        static_context, dynamic_context = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            map_workspace_dir=workspace_root,
        )
        context_text = "\n\n".join(part.strip() for part in (static_context, dynamic_context) if part.strip())

        agent = build_stage_agent(
            name="interface_designer",
            stage="interface_design",
            model=self.model,
            system_prompt="\n\n".join(
                [get_system_prompt(), stage_skill_activation_policy(selected_skill_names)]
            ),
            response_format=InterfaceDesignResponse,
            workspace_root=workspace_root,
            writable_roots=[workspace_root],
            skills=[SKILLS_SOURCE] if selected_skill_names else [],
            permitted_skill_names=selected_skill_names,
            memory=[],
            tools=build_traceability_tools(node_id=node_id, log_cb=self.log_cb),
            node_id=node_id,
            claims_workspace_root=self.context_workspace_root or workspace_root,
        )
        message = get_user_prompt(
            node_id=node_id,
            requirement_data=requirement_data,
            dynamic_context=context_text,
            merge_conflict=self._load_merge_conflict_context(node_id),
        )
        await self._log(f"skill-permitted: {', '.join(selected_skill_names) or 'none'}", node_id=node_id)
        await self._log("Invoking interface design.", node_id=node_id)
        agent_context = AgentRuntimeContext(
            node_id=node_id,
            phase="DESIGN",
            app_type=app_type,
            workspace_root=workspace_root,
            requirement_path=self.requirement_path,
        )
        payload = await ainvoke_stage_agent(
            agent,
            message=message,
            context=agent_context,
            thread_id=f"{get_project_thread_namespace()}:{node_id}:DESIGN:InterfaceDesigner",
            label=self.agent_name,
            log_cb=self.log_cb,
        )
        bundle = await self._normalize_with_recovery(payload, node_id=node_id)
        materialized_paths = self._stage_materialized_paths(agent)
        bundle["materialized_paths"] = materialized_paths
        if not bundle["interfaces"] and materialized_paths:
            # The final response serialized the design into `summary` prose (or
            # dropped the arrays entirely) while the discipline observed real
            # file writes. Without interface records the traceability store
            # stays empty and downstream stages go blind, so re-ask once on
            # the same thread before letting the workflow hard-fail the node.
            await self._log(
                f"Response recorded no interface contracts for {len(materialized_paths)} materialized file(s); "
                "requesting one-shot contract re-serialization.",
                status="warning",
                node_id=node_id,
            )
            repair_payload = await ainvoke_stage_agent(
                agent,
                message=self._repair_message(materialized_paths),
                context=agent_context,
                thread_id=f"{get_project_thread_namespace()}:{node_id}:DESIGN:InterfaceDesigner",
                label=self.agent_name,
                log_cb=self.log_cb,
            )
            repaired = await self._normalize_with_recovery(repair_payload, node_id=node_id)
            if repaired["interfaces"]:
                bundle["interfaces"] = repaired["interfaces"]
                if not bundle["files_written"]:
                    bundle["files_written"] = repaired["files_written"]
            else:
                await self._log(
                    "Contract re-serialization returned no interface records either.",
                    status="error",
                    node_id=node_id,
                )
        await self._log(
            f"Interface design returned {len(bundle.get('interfaces', []))} interface(s).",
            node_id=node_id,
        )
        return bundle

    _FENCED_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

    async def _normalize_with_recovery(self, payload: dict[str, Any], node_id: str) -> dict[str, Any]:
        """Normalize a design payload, lifting contracts buried in prose.

        The model sometimes serializes the whole response as a fenced JSON
        block nested inside the `summary` prose instead of the structured
        fields (observed live with deepseek-v4-flash on 2026-09-15: both leaf
        nodes did this on the contract re-serialization pass). Without this
        recovery the records are dropped and the workflow hard-fails a node
        whose design actually completed.
        """

        bundle = self._normalize_design_payload(payload)
        if not bundle["interfaces"] and bundle["summary"]:
            recovered = self._recover_fenced_payload(bundle["summary"])
            if recovered.get("interfaces"):
                await self._log(
                    f"Recovered {len(recovered['interfaces'])} interface record(s) from JSON embedded in `summary`.",
                    node_id=node_id,
                )
                bundle["interfaces"] = recovered["interfaces"]
                if not bundle["files_written"] and recovered.get("files_written"):
                    bundle["files_written"] = recovered["files_written"]
            elif bundle["summary"].lstrip().startswith("{"):
                await self._log(
                    "`summary` holds a JSON object that could not be parsed "
                    "(likely truncated by the max output token limit); no interface records recovered.",
                    status="warning",
                    node_id=node_id,
                )
        return bundle

    @classmethod
    def _recover_fenced_payload(cls, summary: str) -> dict[str, Any]:
        """Extract interface records embedded in `summary` prose.

        Handles fenced JSON blocks (```json ... ```) and bare JSON objects the
        model emitted instead of the structured tool call. Returns a dict that
        may carry `interfaces` and/or `files_written`; empty when no parseable
        block contains contract records.
        """

        for match in cls._FENCED_JSON_BLOCK_RE.finditer(summary or ""):
            recovered = cls._payload_from_json_text(match.group(1))
            if recovered:
                return recovered
        text = (summary or "").strip()
        if text.startswith("{"):
            return cls._payload_from_json_text(text)
        return {}

    @staticmethod
    def _payload_from_json_text(text: str) -> dict[str, Any]:
        candidate = text.strip()
        if not candidate.startswith("{"):
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start == -1 or end <= start:
                return {}
            candidate = candidate[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(parsed, list):
            return {"interfaces": [item for item in parsed if isinstance(item, dict)]}
        if isinstance(parsed, dict):
            recovered: dict[str, Any] = {}
            interfaces = parsed.get("interfaces")
            if isinstance(interfaces, list):
                recovered["interfaces"] = [item for item in interfaces if isinstance(item, dict)]
            files_written = parsed.get("files_written")
            if isinstance(files_written, list):
                recovered["files_written"] = [
                    str(path).strip() for path in files_written if str(path).strip()
                ]
            return recovered
        return {}

    @staticmethod
    def _stage_materialized_paths(agent: Any) -> list[str]:
        discipline = getattr(agent, "arc_stage_discipline", None)
        if discipline is None:
            return []
        try:
            return list(discipline.materialized_paths())
        except Exception:
            return []

    @staticmethod
    def _repair_message(materialized_paths: list[str]) -> str:
        listed = "\n".join(f"- {path}" for path in materialized_paths)
        return "\n".join(
            [
                "Your design pass materialized the file(s) below, but the final response recorded an empty `interfaces` array.",
                "None of these contracts reached the traceability store, so downstream stages cannot see the design; prose in `summary` is not a substitute for structured records.",
                "Return now a single `InterfaceDesignResponse` whose `interfaces` array contains one complete record for every contract embodied by these files (plus any reused interface this node depends on), using the schema fields from your original instructions.",
                "Return the structured fields themselves. Do NOT wrap the JSON in markdown code fences and do NOT nest the response JSON inside the `summary` string.",
                "Keep each record compact so the response fits within output limits: `specification` and `responsibility` at most ~200 characters each, and omit narrative fields that would only restate the requirement prose.",
                "Keep `interface_id` values stable and globally unique, and list exactly the materialized files in `files_written`.",
                "",
                "Materialized files:",
                listed,
            ]
        )

    @staticmethod
    def _load_merge_conflict_context(node_id: str) -> dict[str, Any] | None:
        """Conflict paths recorded when this node's DESIGN merge conflicted.

        The workflow re-queues a conflicted DESIGN once and stores the
        conflicting paths (owned by a parallel sibling) in the node session
        so this retry can steer new files away from them. The retry flag
        must be set: a fresh DESIGN pass (manual retry, resume) must never
        be guided by a previous run's stale conflict paths.
        """

        from core import sessions

        session = sessions.load_node_session(node_id)
        if not session.get("merge_conflict_retry_used"):
            return None
        context = session.get("merge_conflict_context")
        if not isinstance(context, dict):
            return None
        paths = context.get("paths")
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            return None
        return {"paths": paths, "phase": context.get("phase", "design")}

    def _normalize_design_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        interfaces = payload.get("interfaces")
        if interfaces is None and isinstance(payload.get("items"), list):
            interfaces = payload["items"]
        if not isinstance(interfaces, list):
            interfaces = []
        normalized_interfaces = [item for item in interfaces if isinstance(item, dict)]
        files_written = payload.get("files_written") or payload.get("files") or []
        if not isinstance(files_written, list):
            files_written = []
        return {
            "summary": str(payload.get("summary", "") or "").strip(),
            "interfaces": normalized_interfaces,
            "files_written": [str(path).strip() for path in files_written if str(path).strip()],
        }

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(self.agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result

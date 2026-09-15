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
from agents.runtime.runners import ainvoke_stage_agent, salvage_json_objects
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
        bundle = await self._normalize_with_recovery(payload, node_id=node_id, agent=agent)
        materialized_paths = bundle.get("materialized_paths") or []
        # Serialization-failure evidence: real writes observed by the
        # discipline, or the model's own files_written claim. A write-less
        # pass that only records reused parent/dependency interfaces must
        # still serialize them, so the self-reported evidence is enough to
        # re-ask; the workflow-level hard gate stays keyed on the
        # discipline's ground truth only.
        evidence_paths = materialized_paths or list(bundle["files_written"])
        if not bundle["interfaces"] and evidence_paths:
            # The structured response recorded no interface contracts while
            # the pass has recorded files. Without interface records the
            # traceability store stays empty and downstream stages go blind,
            # so re-ask once on the same thread before letting the workflow
            # hard-fail the node.
            await self._log(
                f"Response recorded no interface contracts for {len(evidence_paths)} file(s); "
                "requesting one-shot contract re-serialization.",
                status="warning",
                node_id=node_id,
            )
            repair_payload = await ainvoke_stage_agent(
                agent,
                message=self._repair_message(evidence_paths),
                context=agent_context,
                thread_id=f"{get_project_thread_namespace()}:{node_id}:DESIGN:InterfaceDesigner",
                label=self.agent_name,
                log_cb=self.log_cb,
            )
            repaired = await self._normalize_with_recovery(repair_payload, node_id=node_id, agent=agent)
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

    async def _normalize_with_recovery(
        self,
        payload: dict[str, Any],
        node_id: str,
        agent: Any = None,
    ) -> dict[str, Any]:
        """Normalize a design payload, lifting contracts buried in prose.

        Two recovery layers, ordered strict-first:

        1. Summary-embedded scan (only when StageDisciplineMiddleware
           observed real file writes): the structured tool call ran but
           serialized the design into the ``summary`` prose instead of the
           ``interfaces`` array. Ground truth from real writes keeps a
           deliberate empty structured response from being second-guessed.
        2. Raw-message scan (gated on the ``_raw_final_message`` marker the
           adapter preserves): a quote-aware salvage of contract-shaped
           objects from the final text, covering plain-text answers and
           damaged or truncated JSON the strict parse rejects. The marker is
           only set when the structured contract was never delivered, so a
           deliberate empty structured response is never second-guessed —
           even when the pass materialized no files.

        Both shapes were observed live with deepseek-v4-flash (2026-09-15).
        """

        bundle = self._normalize_design_payload(payload)
        materialized_paths = self._stage_materialized_paths(agent)
        bundle["materialized_paths"] = materialized_paths
        if bundle["interfaces"]:
            return bundle
        if materialized_paths and bundle["summary"]:
            recovered = self._recover_fenced_payload(bundle["summary"])
            if recovered.get("interfaces"):
                await self._log(
                    f"Recovered {len(recovered['interfaces'])} interface record(s) from JSON embedded in `summary`.",
                    node_id=node_id,
                )
                bundle["interfaces"] = recovered["interfaces"]
                if recovered.get("summary"):
                    # The recovered JSON carries the model's real summary;
                    # prefer it over the raw JSON blob parked in `summary`.
                    bundle["summary"] = recovered["summary"]
                if not bundle["files_written"] and recovered.get("files_written"):
                    bundle["files_written"] = recovered["files_written"]
                return bundle
            if bundle["summary"].lstrip().startswith("{"):
                await self._log(
                    "`summary` holds a JSON object that could not be parsed "
                    "(likely truncated by the max output token limit); trying the raw-message scan.",
                    node_id=node_id,
                )
        recovered_raw = self._recover_interfaces_from_raw(payload)
        if recovered_raw:
            await self._log(
                f"Recovered {len(recovered_raw)} interface(s) from the final message JSON.",
                node_id=node_id,
            )
            bundle["interfaces"] = recovered_raw
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
            summary = parsed.get("summary")
            if isinstance(summary, str) and summary.strip():
                recovered["summary"] = summary.strip()
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
    def _repair_message(recorded_paths: list[str]) -> str:
        listed = "\n".join(f"- {path}" for path in recorded_paths)
        return "\n".join(
            [
                "Your design pass recorded the file(s) below, but the final response recorded an empty `interfaces` array.",
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
    def _recover_interfaces_from_raw(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Fenced-JSON fallback for contracts the structured tool call missed.

        Models sometimes answer the DESIGN turn with prose plus a ```json```
        block instead of calling the structured-output tool (observed on the
        ticket-booking benchmark: both parallel leaves "returned 0 interfaces"
        while the full contract array sat inside the final message). When the
        structured result is empty and the raw final message was preserved,
        recover the JSON objects embedded in that text. The scanner is
        quote-aware and only keeps objects that still parse, so damaged prose
        never becomes a contract; entries without an ``interface_id`` are
        dropped later by the workflow's ``_prepare_interfaces``.
        """

        if not payload.get("_raw_final_message"):
            return []
        summary_text = str(payload.get("summary") or "")
        raw_text = str(payload.get("_raw_final_message") or "")
        # The fallback branch of extract_payload always puts the full final
        # text into ``summary``; unwrapping the raw debug dump too keeps the
        # scan source aligned with the marker this method gates on, so an
        # adapter that only populates ``_raw_final_message`` still recovers.
        final_text = summary_text if summary_text.strip() else InterfaceDesigner._text_from_raw_dump(raw_text)
        if not final_text.strip():
            return []
        return [
            item
            for item in salvage_json_objects(final_text)
            if any(key in item for key in ("interface_id", "file_path", "specification", "responsibility"))
        ]

    @staticmethod
    def _text_from_raw_dump(raw_text: str) -> str:
        """Unwrap the escaped message dump so the scanner can see its braces.

        ``_stringify_final_message`` stores a JSON-encoded debug dump; the
        fenced JSON inside it is escaped into a string value that the
        quote-aware scanner would skip. Decode it first and return the
        assistant content (string or text-block list) when possible.
        """

        try:
            dumped = json.loads(raw_text)
        except json.JSONDecodeError:
            return raw_text
        if not isinstance(dumped, dict):
            return raw_text
        content = dumped.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text)
                elif isinstance(block, str) and block.strip():
                    texts.append(block)
            if texts:
                return "\n".join(texts)
        return raw_text

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

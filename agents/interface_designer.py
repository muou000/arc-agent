from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, create_model

from langchain.agents.structured_output import ToolStrategy

from agents.context.pipeline import context_pipeline
from agents.context.prompts.common import stage_skill_activation_policy
from agents.context.prompts.interface_designer import get_system_prompt, get_user_prompt
from agents.design.contract_skeleton import ContractSkeleton, derive_contract_skeletons, merge_filled_contracts
from agents.model.openai_api_adapter import json_schema_structured_output_supported
from agents.runtime.checkpointer import get_project_thread_namespace
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent, salvage_json_objects
from agents.skills.planning import load_skill_plan_extras
from agents.skills.selection import SKILLS_SOURCE, interface_design_skills
from agents.tools.traceability import build_traceability_tools


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

# Validation retries the constrained repair schema may spend inside one agent
# session before giving up (langchain's ToolStrategy default retries forever,
# bounded only by the recursion limit — a stuck model would burn the whole
# step budget re-submitting the same blank sheet).
_REPAIR_VALIDATION_RETRIES = 2


class InterfaceDesignResponse(BaseModel):
    summary: str = Field(default="", description="Short design-stage summary.")
    interfaces: list[dict[str, Any]] = Field(default_factory=list, description="Interface contracts for the current node.")
    files_written: list[str] = Field(default_factory=list, description="Workspace-relative files written or edited.")


def _dynamic_repair_response_format(min_items: int) -> Any | None:
    """Repair response format with a semantic floor on the ``interfaces`` array.

    A pydantic schema whose ``interfaces`` array carries ``min_items`` (via
    ``min_length``) as its lower bound, wrapped in langchain's ``ToolStrategy``
    with a bounded validation-retry budget: a flash-class model that "legally
    hands in a blank sheet" (schema-valid ``[]``, observed 2026-09-16 on
    deepseek-v4-flash) violates the constraint and is re-asked up to
    ``_REPAIR_VALIDATION_RETRIES`` times before the payload is returned as-is
    for the caller's fallback layers. The floor also reaches
    provider-native constrained decoding when the endpoint supports
    ``response_format: json_schema, strict`` (``json_schema_structured_output_supported``,
    probed once per process): the constraint then holds at decode time and the
    blank sheet becomes impossible in the first place.
    """

    if min_items <= 0:
        return None
    if not json_schema_structured_output_supported(_designer_model_hint()):
        return None
    schema = create_model(
        "InterfaceDesignRepairResponse",
        __base__=BaseModel,
        summary=(str, Field(default="", description="Short design-stage summary.")),
        interfaces=(
            list[dict[str, Any]],
            Field(
                min_length=min_items,
                description=f"Interface contracts for the current node; at least {min_items} records are required.",
            ),
        ),
        files_written=(list[str], Field(default_factory=list, description="Workspace-relative files written or edited.")),
    )

    # ToolStrategy's default ``handle_errors=True`` retries validation errors
    # forever inside the agent loop (bounded only by the recursion limit); a
    # stuck model then burns the whole step budget on the same blank sheet.
    # ``handle_errors=False`` instead RAISES StructuredOutputValidationError
    # out of the agent session on the first violation, which ainvoke surfaces
    # as an exception; the repair pass catches it (see ``_fill_skeletons_pass``)
    # and moves on to the batched/mechanical layers. One in-session correction
    # round is kept by re-asking the model ourselves before that: the
    # whole-list pass and the batched retry each get their own fresh budget.
    schema = create_model(
        "InterfaceDesignRepairResponse",
        __base__=BaseModel,
        summary=(str, Field(default="", description="Short design-stage summary.")),
        interfaces=(
            list[dict[str, Any]],
            Field(
                min_length=min_items,
                description=f"Interface contracts for the current node; at least {min_items} records are required.",
            ),
        ),
        files_written=(list[str], Field(default_factory=list, description="Workspace-relative files written or edited.")),
    )
    return ToolStrategy(schema=schema, handle_errors=False)


def _designer_model_hint() -> str:
    """Model hint for the json_schema capability probe.

    The probe is endpoint+model scoped and cached, so resolving it from the
    designer's configured model (constructor arg or ``MODEL`` env) matches
    the model the stage agents will actually use.
    """

    return str(os.environ.get("MODEL", "") or "").strip() or "openai:gpt-5.4"


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

        agent = self._build_agent(
            node_id=node_id,
            workspace_root=workspace_root,
            app_type=app_type,
            selected_skill_names=selected_skill_names,
            response_format=InterfaceDesignResponse,
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
            repaired = await self._repair_empty_interfaces(
                agent,
                node_id=node_id,
                agent_context=agent_context,
                evidence_paths=evidence_paths,
                materialized_paths=materialized_paths,
                workspace_root=workspace_root,
                app_type=app_type,
                selected_skill_names=selected_skill_names,
            )
            if repaired.get("interfaces"):
                bundle["interfaces"] = repaired["interfaces"]
                if not bundle["files_written"] and repaired.get("files_written"):
                    bundle["files_written"] = repaired["files_written"]
                if repaired.get("summary"):
                    bundle["summary"] = repaired["summary"]
        await self._log(
            f"Interface design returned {len(bundle.get('interfaces', []))} interface(s).",
            node_id=node_id,
        )
        return bundle

    def _build_agent(
        self,
        *,
        node_id: str,
        workspace_root: str,
        app_type: str,
        selected_skill_names: list[str],
        response_format: Any,
    ) -> Any:
        """Build the InterfaceDesigner deep-agent with a given response format."""

        del app_type  # kept in the signature for parity with run()'s resolution
        return build_stage_agent(
            name="interface_designer",
            stage="interface_design",
            model=self.model,
            system_prompt="\n\n".join(
                [get_system_prompt(), stage_skill_activation_policy(selected_skill_names)]
            ),
            response_format=response_format,
            workspace_root=workspace_root,
            writable_roots=[workspace_root],
            skills=[SKILLS_SOURCE] if selected_skill_names else [],
            permitted_skill_names=selected_skill_names,
            memory=[],
            tools=build_traceability_tools(node_id=node_id, log_cb=self.log_cb),
            node_id=node_id,
            claims_workspace_root=self.context_workspace_root or workspace_root,
        )

    async def _repair_empty_interfaces(
        self,
        agent: Any,
        *,
        node_id: str,
        agent_context: AgentRuntimeContext,
        evidence_paths: list[str],
        materialized_paths: list[str],
        workspace_root: str = "",
        app_type: str = "",
        selected_skill_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Re-serialize contracts after a schema-valid but semantically empty response.

        Flash-class models complete the design files, then treat the large
        structured ``interfaces`` array as redundant labor and hand back a
        schema-valid ``[]`` (observed 2026-09-16: every DESIGN pass with 9-12
        materialized files on deepseek-v4-flash). The identity and count of
        the contracts are mechanically derivable from the files, so the repair
        no longer re-asks the same big question: it derives contract skeletons
        from the materialized files and asks the model only for the two
        semantic fields per pre-computed row — on an agent rebuilt with a
        ``minItems``-constrained schema when the endpoint supports provider-
        native json_schema output, turning the blank sheet into a constraint
        violation. When the files give no mechanical signal (non-template
        layouts, write-less passes claiming ``files_written``), the repair
        falls back to the open re-serialization question on the same thread.
        """

        skeletons = self._derive_skeletons(node_id=node_id, materialized_paths=materialized_paths, workspace_root=agent_context.workspace_root)
        if skeletons:
            await self._log(
                f"Response recorded no interface contracts for {len(evidence_paths)} file(s); "
                f"derived {len(skeletons)} contract skeleton(s) from the materialized files; "
                "requesting skeleton-guided contract serialization.",
                status="warning",
                node_id=node_id,
            )
            fill_agent = self._constrained_repair_agent(
                agent,
                node_id=node_id,
                workspace_root=workspace_root,
                app_type=app_type,
                selected_skill_names=selected_skill_names,
                min_items=len(skeletons),
            )
            merged = await self._fill_skeletons(
                fill_agent,
                node_id=node_id,
                agent_context=agent_context,
                skeletons=skeletons,
                evidence_paths=evidence_paths,
            )
            if merged:
                return {"interfaces": merged}

        await self._log(
            f"Response recorded no interface contracts for {len(evidence_paths)} file(s); "
            "requesting one-shot contract re-serialization.",
            status="warning",
            node_id=node_id,
        )
        repair_payload = await ainvoke_stage_agent(
            agent,
            message=self._repair_message(evidence_paths, expected_count=len(skeletons)),
            context=agent_context,
            thread_id=f"{get_project_thread_namespace()}:{node_id}:DESIGN:InterfaceDesigner",
            label=self.agent_name,
            log_cb=self.log_cb,
        )
        repaired = await self._normalize_with_recovery(repair_payload, node_id=node_id, agent=agent)
        if repaired["interfaces"]:
            return {"interfaces": repaired["interfaces"], "files_written": repaired["files_written"], "summary": repaired["summary"]}
        await self._log(
            "Contract re-serialization returned no interface records either.",
            status="error",
            node_id=node_id,
        )
        return {}

    def _constrained_repair_agent(
        self,
        agent: Any,
        *,
        node_id: str,
        workspace_root: str,
        app_type: str,
        selected_skill_names: list[str] | None,
        min_items: int,
    ) -> Any:
        """Rebuild the agent with a minItems-constrained schema when possible.

        The constrained schema only changes the final structured-output tool:
        same thread (checkpointer resumes the conversation), same tools, same
        discipline. When native json_schema is unavailable the original agent
        is returned unchanged and the repair relies on prompting + retries +
        the mechanical fallback.
        """

        if not workspace_root or selected_skill_names is None:
            return agent
        constrained = _dynamic_repair_response_format(min_items)
        if constrained is None:
            return agent
        try:
            rebuilt = self._build_agent(
                node_id=node_id,
                workspace_root=workspace_root,
                app_type=app_type,
                selected_skill_names=selected_skill_names,
                response_format=constrained,
            )
        except Exception:
            return agent
        # Carry the discipline over so materialized-write ground truth stays
        # observable to the normalization path on the rebuilt agent.
        discipline = getattr(agent, "arc_stage_discipline", None)
        if discipline is not None:
            rebuilt.arc_stage_discipline = discipline
        return rebuilt

    async def _fill_skeletons(
        self,
        agent: Any,
        *,
        node_id: str,
        agent_context: AgentRuntimeContext,
        skeletons: list[ContractSkeleton],
        evidence_paths: list[str],
    ) -> list[dict[str, Any]]:
        """Ask the model to fill the semantic fields of pre-computed skeletons.

        One whole-list pass, then at most one batched retry over the gaps
        (small batches of 3-4 rows), then a conservative mechanical fallback:
        records with code-derived responsibility/specification and a
        ``skeleton_derived`` marker. The fallback trades semantics for
        keeping the traceability chain connected — the workflow hard gate
        would otherwise kill nodes whose designs are fully materialized.
        """

        merged = await self._fill_skeletons_pass(
            agent, node_id=node_id, agent_context=agent_context, skeletons=skeletons,
            message=self._skeleton_fill_message(skeletons, evidence_paths, expected_count=len(skeletons)),
        )
        merged = self._drop_blank_records(merged)
        # Early exit only when every skeleton row is actually covered by a
        # filled record — a raw count comparison would let a pile of junk ids
        # (or duplicated reused interfaces) masquerade as a complete fill.
        gaps = [skeleton for skeleton in skeletons if not self._find_filled_record(merged, skeleton)]
        if merged and not gaps:
            return merged

        if gaps:
            # One retry round, batched small (3-4 rows per ask): the failure
            # mode being treated is "the array is too big to serialize", so a
            # single whole-list re-ask would run into the same wall.
            if merged:
                await self._log(
                    f"Skeleton fill returned {len(merged)}/{len(skeletons)} record(s); retrying {len(gaps)} gap(s) in batches.",
                    status="warning",
                    node_id=node_id,
                )
            else:
                await self._log(
                    "Skeleton fill returned no records; retrying in small batches.",
                    status="warning",
                    node_id=node_id,
                )
            for batch in self._batches(gaps):
                batch_merged = await self._fill_skeletons_pass(
                    agent, node_id=node_id, agent_context=agent_context, skeletons=batch,
                    message=self._skeleton_fill_message(batch, evidence_paths, expected_count=len(skeletons), batch_mode=True),
                )
                batch_merged = self._drop_blank_records(batch_merged)
                if batch_merged:
                    merged.extend(batch_merged)
            gaps = [skeleton for skeleton in skeletons if not self._find_filled_record(merged, skeleton)]

        if gaps and merged:
            # Batched retries still left gaps: keep what the model produced
            # and cover the rest with conservative mechanical records.
            await self._log(
                f"Skeleton fill left {len(gaps)} unfilled record(s); materializing conservative skeleton records for them.",
                status="warning",
                node_id=node_id,
            )
            merged.extend(self._mechanical_records(gaps))
        elif not merged:
            await self._log(
                "Skeleton fill produced no records; materializing conservative skeleton records.",
                status="warning",
                node_id=node_id,
            )
            merged = self._mechanical_records(skeletons)
        return merged

    async def _fill_skeletons_pass(
        self,
        agent: Any,
        *,
        node_id: str,
        agent_context: AgentRuntimeContext,
        skeletons: list[ContractSkeleton],
        message: str,
    ) -> list[dict[str, Any]]:
        try:
            payload = await ainvoke_stage_agent(
                agent,
                message=message,
                context=agent_context,
                thread_id=f"{get_project_thread_namespace()}:{node_id}:DESIGN:InterfaceDesigner",
                label=self.agent_name,
                log_cb=self.log_cb,
            )
        except Exception as exc:
            # A failed fill pass must not sink the whole repair — but the
            # constrained repair schema raises on minItems violations, and
            # the offending structured tool call often still carries valid
            # partial rows. Salvage those before giving up on this pass so a
            # "6 of 8 rows" answer is not thrown away with the exception.
            salvaged = self._salvage_structured_records(exc)
            if salvaged:
                await self._log(
                    f"Skeleton fill pass failed ({type(exc).__name__}) but salvaged {len(salvaged)} record(s) from the structured tool call.",
                    status="warning",
                    node_id=node_id,
                )
                return merge_filled_contracts(skeletons, salvaged)
            await self._log(
                f"Skeleton fill pass failed with {type(exc).__name__}; continuing with the remaining batches.",
                status="warning",
                node_id=node_id,
            )
            return []
        filled = await self._normalize_with_recovery(payload, node_id=node_id, agent=agent)
        records = filled.get("interfaces") or []
        if not records:
            return []
        return merge_filled_contracts(skeletons, records)

    @staticmethod
    def _salvage_structured_records(exc: Exception) -> list[dict[str, Any]]:
        """Pull valid interface rows out of a failed structured-output session.

        ``ToolStrategy(handle_errors=False)`` raises on schema violations; the
        raised error carries the offending ``AIMessage``, whose structured
        tool call holds the raw arguments (the violation is usually the count,
        not the row syntax). The rows that do carry a semantic field are kept
        — the caller's gap logic re-asks only what is missing.
        """

        ai_message = getattr(exc, "ai_message", None)
        for call in getattr(ai_message, "tool_calls", None) or []:
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
            if isinstance(args, dict) and isinstance(args.get("interfaces"), list):
                return [item for item in args["interfaces"] if isinstance(item, dict)]
        return []

    @staticmethod
    def _batches(skeletons: list[ContractSkeleton], size: int = 4) -> list[list[ContractSkeleton]]:
        return [skeletons[index : index + size] for index in range(0, len(skeletons), size)]

    @staticmethod
    def _find_filled_record(records: list[dict[str, Any]], skeleton: ContractSkeleton) -> dict[str, Any] | None:
        for record in records:
            if str(record.get("interface_id") or "").strip() == skeleton.interface_id:
                return record
            if str(record.get("file_path") or "").replace("\\", "/").lstrip("/") == skeleton.file_path:
                return record
        return None

    @staticmethod
    def _drop_blank_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop records the model returned without either semantic field.

        A row with empty responsibility AND specification carries no
        information beyond the mechanical skeleton; keeping it would let the
        model satisfy the row count without doing the fill-in work.
        """

        return [
            record
            for record in records
            if str(record.get("responsibility") or "").strip() or str(record.get("specification") or "").strip()
        ]

    @staticmethod
    def _mechanical_records(skeletons: list[ContractSkeleton]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for skeleton in skeletons:
            location = f"{skeleton.file_path}"
            if skeleton.method and skeleton.mount_path:
                location += f" ({skeleton.method.upper()} {skeleton.mount_path})"
            elif skeleton.table_name:
                location += f" (table {skeleton.table_name})"
            records.append(
                {
                    "interface_id": skeleton.interface_id,
                    "req_id": skeleton.req_id,
                    "type": skeleton.type,
                    "name": skeleton.name,
                    "file_path": skeleton.file_path,
                    "first_line": skeleton.first_line,
                    "responsibility": f"{skeleton.type} contract at {location} (recorded mechanically; semantic summary unavailable).",
                    "specification": f"Interface skeleton materialized in this DESIGN pass; see the file for the declared boundary ({skeleton.relation}).",
                    "callers": [],
                    "callees": [],
                    "relation": skeleton.relation,
                    "skeleton_derived": True,
                }
            )
        return records

    def _derive_skeletons(
        self,
        *,
        node_id: str,
        materialized_paths: list[str],
        workspace_root: str,
    ) -> list[ContractSkeleton]:
        if not materialized_paths:
            return []
        return derive_contract_skeletons(
            node_id=node_id,
            file_paths=materialized_paths,
            workspace_root=workspace_root,
            interface_ids_by_file=self._registered_interfaces_by_file(),
        )

    @staticmethod
    def _registered_interfaces_by_file() -> dict[str, set[str]]:
        """Map workspace-relative file paths to already registered interface ids.

        Shared integration surfaces (app entry, route registration, database
        bootstrap) are extended by this pass rather than newly owned; their
        edits become update relations of the existing contracts. When the
        runtime is unavailable the map is empty and every materialized file
        yields a new owned skeleton.
        """

        try:
            from core.service import get_runtime

            store = get_runtime().traceability
        except Exception:
            return {}
        by_file: dict[str, set[str]] = {}
        for record in store.list_interfaces():
            interface_id = str(record.get("interface_id") or "").strip()
            file_path = str(record.get("file_path") or "").strip().replace("\\", "/").lstrip("/")
            if interface_id and file_path:
                by_file.setdefault(file_path, set()).add(interface_id)
        return by_file

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
    def _repair_message(recorded_paths: list[str], *, expected_count: int = 0) -> str:
        listed = "\n".join(f"- {path}" for path in recorded_paths)
        gap = (
            f"The final response recorded 0 interface records while {len(recorded_paths)} file(s) were materialized; at least {expected_count} contract records are expected from this file set. "
            if expected_count
            else f"The final response recorded 0 interface records while {len(recorded_paths)} file(s) were materialized. "
        )
        return "\n".join(
            [
                "Your design pass recorded the file(s) below, but the final response recorded an empty `interfaces` array.",
                gap
                + "None of these contracts reached the traceability store, so downstream stages cannot see the design; prose in `summary` is not a substitute for structured records.",
                "Return now a single `InterfaceDesignResponse` whose `interfaces` array contains one complete record for every contract embodied by these files (plus any reused interface this node depends on), using the schema fields from your original instructions.",
                "You can mostly transcribe from the files you just wrote: each materialized file that declares a boundary (router, service, page component, table schema) is one interface record, and shared-surface edits extend the contracts already registered for that file.",
                "Return the structured fields themselves. Do NOT wrap the JSON in markdown code fences and do NOT nest the response JSON inside the `summary` string.",
                "Keep each record compact so the response fits within output limits: `specification` and `responsibility` at most ~200 characters each, and omit narrative fields that would only restate the requirement prose.",
                "Keep `interface_id` values stable and globally unique, and list exactly the materialized files in `files_written`.",
                "",
                "Materialized files:",
                listed,
            ]
        )

    @staticmethod
    def _skeleton_fill_message(
        skeletons: list[ContractSkeleton],
        recorded_paths: list[str],
        *,
        expected_count: int,
        batch_mode: bool = False,
    ) -> str:
        rows = "\n".join(skeleton.to_prompt_row() for skeleton in skeletons)
        return "\n".join(
            [
                "Your design pass materialized the files below, but the final response recorded an empty `interfaces` array, so no contract reached the traceability store.",
                f"{len(recorded_paths)} file(s) were materialized and at least {expected_count} contract records are expected; the contract identities below were derived mechanically from those files.",
                (
                    "Fill in the two semantic fields for each skeleton row below — `responsibility` (one sentence, at most ~200 characters) and `specification` (the contract a test can target, at most ~200 characters)."
                    if not batch_mode
                    else "The previous serialization pass missed the rows below. Fill in the two semantic fields for each of them — `responsibility` (one sentence, at most ~200 characters) and `specification` (the contract a test can target, at most ~200 characters)."
                ),
                "Keep every identity field exactly as given (interface_id, type, name, file_path, first_line, relation); you may add callers/callees/inputs/outputs/test_focus when you know them from your own design.",
                "Return one `InterfaceDesignResponse` whose `interfaces` array contains one record per skeleton row, plus any reused interface this node depends on that is not listed.",
                "Return the structured fields themselves. Do NOT wrap the JSON in markdown code fences and do NOT nest the response JSON inside the `summary` string.",
                "",
                "Contract skeletons:",
                rows,
                "",
                "Materialized files:",
                "\n".join(f"- {path}" for path in recorded_paths),
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

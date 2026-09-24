from __future__ import annotations

from pathlib import Path
from typing import Any

from arcbench_agent_runtime.runtime import AgentRuntime
from agents.context.pipeline import set_context_config, set_context_runtime
from agents.model.usage_capture import LLMUsageRecord, set_llm_usage_sink
from agents.runtime.tool_usage import ToolUsageRecord, set_tool_usage_sink


_runtime: AgentRuntime | None = None


def configure_runtime(
    *,
    project_dir: str,
    traceability_dir: str | None = None,
    runner_events_path: str | None = None,
    app_type: str | None = None,
    web_port: int | None = None,
    android_package: str | None = None,
) -> AgentRuntime:
    """Create and publish the process-wide ARC runtime."""

    global _runtime
    resolved_project_dir = str(Path(project_dir).expanduser().resolve())
    _runtime = AgentRuntime.from_env(
        project_dir=resolved_project_dir,
        runner_events_path=runner_events_path,
        traceability_dir=traceability_dir,
    )
    set_context_runtime(_runtime)
    set_llm_usage_sink(_make_llm_usage_sink(_runtime))
    set_tool_usage_sink(_make_tool_usage_sink(_runtime))
    set_context_config(
        workspace_dir=resolved_project_dir,
        app_type=app_type,
        web_port=web_port,
        android_package=android_package,
    )
    return _runtime


def _make_llm_usage_sink(runtime: AgentRuntime) -> Any:
    """Persist every captured model-call usage as an ``llm_usage`` runner event."""

    def sink(record: LLMUsageRecord) -> None:
        runtime.events.record_llm_usage(
            node_id=record.node_id,
            phase=record.phase,
            model=record.model,
            api_mode=record.api_mode,
            source=record.source,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cache_read_tokens=record.cache_read_tokens,
            cache_write_tokens=record.cache_write_tokens,
            cache_write_1h_tokens=record.cache_write_1h_tokens,
            reasoning_tokens=record.reasoning_tokens,
            total_tokens=record.total_tokens,
            cost=record.cost,
            duration_s=record.duration_s,
            transport=record.transport,
            attempts=record.attempts,
        )

    return sink


def _make_tool_usage_sink(runtime: AgentRuntime) -> Any:
    """Persist every observed tool round-trip as a ``tool_usage`` runner event."""

    def sink(record: ToolUsageRecord) -> None:
        runtime.events.record_tool_usage(
            node_id=record.node_id,
            phase=record.phase,
            tool=record.tool,
            status=record.status,
            path=record.path,
            offset=record.offset,
            limit=record.limit,
            result_chars=record.result_chars,
            requested_path=record.requested_path,
            path_classification=record.path_classification,
            execution_path=record.execution_path,
        )

    return sink


def get_runtime() -> AgentRuntime:
    if _runtime is None:
        raise RuntimeError("ARC runtime has not been configured.")
    return _runtime


def has_runtime() -> bool:
    return _runtime is not None


def reset_runtime_for_tests() -> None:
    global _runtime
    _runtime = None
    set_context_runtime(None)
    set_llm_usage_sink(None)
    set_tool_usage_sink(None)

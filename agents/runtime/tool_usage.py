"""Tool-round-trip observability for ARC stage agents.

Every stage-agent tool call passes through ``ToolUsageMiddleware`` (registered
as the outermost ``wrap_tool_call`` layer, so blocked discipline calls are
observed too). Each executed or blocked round-trip is attributed to the
stage's node/phase via ``llm_usage_context`` and dispatched to a process-wide
sink; ``core.service.configure_runtime`` registers a sink that persists each
observation as a ``tool_usage`` runner event. ``arcbench_agent_runtime.usage.aggregate_tool_usage``
folds those events into per-node tool-round-trip counts, which is how
whole-file reads (unpaged ``read_file``) and ineffective greps (empty
results) become measurable without touching tool implementations.

Capture is best-effort: it never raises and is a no-op when no runtime is
configured (e.g. faux-model unit tests).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from agents.model.usage_capture import current_usage_context
from agents.runtime.stage_discipline import (
    BLOCKED_RESULT_PREFIX,
    _discipline_path,
    _tool_result_failed,
)
from agents.runtime.virtual_paths import current_virtual_path_audit


logger = logging.getLogger(__name__)

UsageSink = Callable[["ToolUsageRecord"], None]

_sink: UsageSink | None = None
_sink_lock = threading.Lock()


@dataclass(frozen=True)
class ToolUsageRecord:
    """One agent tool round-trip, ready for ``EventClient.record_tool_usage``."""

    tool: str
    node_id: str
    phase: str
    status: str  # "ok" | "error" | "blocked"
    path: str | None
    offset: int | None
    limit: int | None
    result_chars: int
    requested_path: str | None = None
    path_classification: str = ""
    execution_path: str | None = None


def set_tool_usage_sink(sink: UsageSink | None) -> None:
    """Install the process-wide tool-usage sink (or remove it with ``None``)."""

    global _sink
    with _sink_lock:
        _sink = sink


def get_tool_usage_sink() -> UsageSink | None:
    with _sink_lock:
        return _sink


def record_tool_usage(
    *,
    tool: str,
    status: str,
    path: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    result_chars: int = 0,
    requested_path: str | None = None,
    path_classification: str = "",
    execution_path: str | None = None,
) -> None:
    """Best-effort tool-usage capture; never raises and needs no sink."""

    try:
        sink = get_tool_usage_sink()
        if sink is None:
            return
        node_id, phase = current_usage_context()
        sink(
            ToolUsageRecord(
                tool=str(tool or ""),
                node_id=node_id,
                phase=phase,
                status=str(status or ""),
                path=path,
                offset=offset,
                limit=limit,
                result_chars=int(result_chars or 0),
                requested_path=requested_path,
                path_classification=str(path_classification or ""),
                execution_path=execution_path,
            )
        )
    except Exception:
        logger.debug("Tool usage capture failed", exc_info=True)


class ToolUsageMiddleware(AgentMiddleware[Any, Any, Any]):
    """Record one ``tool_usage`` observation per executed or blocked tool call.

    Must run outermost so results returned by discipline middleware (blocked
    calls) are observed as well. It never modifies requests or results.
    """

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        result = handler(request)
        _record(request, result)
        return result

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        result = await handler(request)
        _record(request, result)
        return result


def _record(request: ToolCallRequest, result: ToolMessage | Any) -> None:
    tool_call = request.tool_call
    args = tool_call.get("args", {}) or {}
    tool = str(tool_call.get("name", ""))
    status = _result_status(result, tool=tool)
    is_read = tool == "read_file"
    audit = current_virtual_path_audit(str(tool_call.get("id") or ""))
    requested_path = audit.requested_path if audit is not None else _discipline_path(args) or None
    record_tool_usage(
        tool=tool,
        status=status,
        path=requested_path,
        offset=_optional_int(args.get("offset")) if is_read else None,
        limit=_optional_int(args.get("limit")) if is_read else None,
        result_chars=len(str(getattr(result, "content", "") or "")),
        requested_path=requested_path if audit is not None else None,
        path_classification=audit.classification if audit is not None else "",
        execution_path=audit.execution_path if audit is not None else None,
    )


def _result_status(result: ToolMessage | Any, *, tool: str = "") -> str:
    content = str(getattr(result, "content", "") or "")
    if content.startswith(BLOCKED_RESULT_PREFIX):
        return "blocked"
    # Results that self-report failure in their rendered text (an exit-code
    # segment the tool wrote itself) share the discipline's failure predicate,
    # so the observation cannot call a mixed-exit-code build "ok" while the
    # write lock treats it as failed. The predicate itself narrows the scan
    # by tool: a read whose *content* holds failure markers still read fine.
    if _tool_result_failed(result, tool=tool):
        return "error"
    return "ok"


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None

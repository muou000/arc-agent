from __future__ import annotations

import inspect
import json
import os
import re
import time
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from agents.model.openai_api_adapter import ARCModelAPIError
from agents.model.usage_capture import llm_usage_context
from agents.runtime.contracts import AgentRuntimeContext
from core.logging import format_json_for_log, log_to_logger
from langchain.agents.structured_output import StructuredOutputError
from langgraph.errors import GraphRecursionError


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

# Empirical ceiling for one stage-agent session (one ``ainvoke`` call). Healthy
# sessions on the 12306 benchmark stay under ~150 graph steps; a runaway repair
# loop (the model re-editing a file it just corrupted) blew past 450 steps and
# kept going under the previous limit of 5000, burning ~20 minutes of model
# calls on one node. When the limit trips, LangGraph raises GraphRecursionError;
# the workflow marks the node failed and the queue moves on, so the node stays
# recoverable via ``--resume``/``--retry``.
DEFAULT_RECURSION_LIMIT = 300
_MIN_RECURSION_LIMIT = 20


async def ainvoke_stage_agent(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    logger: Any | None = None,
    label: str = "",
    log_cb: LogCallback | None = None,
    stream: bool | None = None,
) -> dict[str, Any]:
    """Invoke one stage-agent session, attributing model token usage to its node/phase."""
    with llm_usage_context(node_id=context.node_id, phase=context.phase):
        return await _ainvoke_stage_agent(
            agent,
            message=message,
            context=context,
            thread_id=thread_id,
            logger=logger,
            label=label,
            log_cb=log_cb,
            stream=stream,
        )


async def _ainvoke_stage_agent(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    logger: Any | None = None,
    label: str = "",
    log_cb: LogCallback | None = None,
    stream: bool | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    run_label = label or getattr(agent, "name", "") or context.phase or "stage-agent"
    log_to_logger(logger, "AGENT_CALL_START", label=run_label, thread_id=thread_id, body=message)
    await _emit_log(
        log_cb,
        run_label,
        f"agent call start: thread_id={thread_id}",
        node_id=context.node_id,
    )

    if _should_stream(stream):
        stream_payload = await _try_astream_stage_agent(
            agent,
            message=message,
            context=context,
            thread_id=thread_id,
            run_label=run_label,
            log_cb=log_cb,
            logger=logger,
        )
        if stream_payload is not None:
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            log_to_logger(
                logger,
                "AGENT_CALL_END",
                label=run_label,
                thread_id=thread_id,
                body=f"duration_ms={duration_ms:.1f}\n{format_json_for_log(stream_payload)}",
            )
            await _emit_log(
                log_cb,
                run_label,
                f"agent call end: duration_ms={duration_ms:.1f}, payload={format_json_for_log(stream_payload)}",
                node_id=context.node_id,
            )
            return stream_payload

    config = build_agent_config(thread_id)
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config=config,
        )
    except GraphRecursionError as exc:
        raise GraphRecursionError(
            f"stage agent session hit its step budget (recursion_limit={config['recursion_limit']}). "
            "If this node legitimately needs more steps, raise ARC_AGENT_RECURSION_LIMIT for the run."
        ) from exc
    await _log_completed_tool_batches(log_cb, result, label=run_label, node_id=context.node_id)
    await _log_agent_trace(log_cb, result, label=run_label, thread_id=thread_id, node_id=context.node_id)
    payload = extract_payload(result)
    duration_ms = (time.perf_counter() - started_at) * 1000.0
    log_to_logger(
        logger,
        "AGENT_CALL_END",
        label=run_label,
        thread_id=thread_id,
        body=f"duration_ms={duration_ms:.1f}\n{format_json_for_log(payload)}",
    )
    await _emit_log(
        log_cb,
        run_label,
        f"agent call end: duration_ms={duration_ms:.1f}, payload={format_json_for_log(payload)}",
        node_id=context.node_id,
    )
    return payload


def extract_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structured_response") if isinstance(result, dict) else None
    normalized = _normalize_payload_value(structured)
    if normalized is not None:
        return normalized

    final_text = _extract_final_message_text(result)
    parsed = parse_json_payload(final_text)
    if parsed is not None:
        return parsed
    return {"summary": final_text, "_raw_final_message": _stringify_final_message(result)}


def parse_json_payload(text: str) -> dict[str, Any] | None:
    current = (text or "").strip()
    for _ in range(3):
        if not current:
            return None
        next_string: str | None = None
        for candidate in _json_candidates(current):
            outcome = _loads_candidate(candidate)
            if isinstance(outcome, dict):
                return outcome
            if isinstance(outcome, str) and outcome.strip():
                next_string = outcome.strip()
        if next_string is None:
            # Model-written JSON blobs (answering with prose plus a fenced
            # block instead of the structured-output tool call) often carry
            # syntax damage that strict parsing rejects wholesale. Try the
            # tolerant repairs before giving up on the whole payload.
            for candidate in _json_candidates(current):
                recovered = _loads_candidate_tolerant(candidate)
                if isinstance(recovered, dict):
                    return recovered
            return None
        current = next_string
    return None


def _loads_candidate(candidate: str) -> dict[str, Any] | str | None:
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    normalized = _normalize_payload_value(payload)
    if normalized is not None:
        return normalized
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return None


def _loads_candidate_tolerant(candidate: str) -> dict[str, Any] | None:
    repaired_variants = (
        _escape_control_chars_in_strings(candidate),
        _strip_trailing_commas(_escape_control_chars_in_strings(candidate)),
    )
    for repaired in repaired_variants:
        if repaired == candidate:
            continue
        try:
            payload = json.loads(repaired)
        except json.JSONDecodeError:
            continue
        normalized = _normalize_payload_value(payload)
        if normalized is not None:
            return normalized
    return None


def salvage_json_objects(text: str) -> list[dict[str, Any]]:
    """Recover complete JSON objects from damaged model-written payloads.

    A long fenced JSON answer cut off by the output-token limit fails every
    whole-document load even though every object written before the cut is
    intact. A quote-aware brace scanner slices out the root object and its
    direct children; only slices that still parse as JSON objects survive, so
    damaged prose can never turn into a contract.
    """

    repaired = _escape_control_chars_in_strings(text or "")
    results: list[dict[str, Any]] = []
    openings: list[int] = []
    in_string = False
    escaped = False
    for index, ch in enumerate(repaired):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            openings.append(index)
        elif ch == "}":
            if not openings:
                continue
            start = openings.pop()
            if len(openings) <= 1:
                try:
                    payload = json.loads(repaired[start : index + 1])
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    results.append(payload)
    return results


def _escape_control_chars_in_strings(text: str) -> str:
    if not any(ch in text for ch in ("\n", "\r", "\t")):
        return text
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ch == "\n":
                out.append("\\n")
                continue
            elif ch == "\r":
                out.append("\\r")
                continue
            elif ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for index, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                continue
        out.append(ch)
    return "".join(out)


def _normalize_payload_value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump()
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {"items": dumped}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    return None


def _extract_final_message_text(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        role = _message_role(message)
        if role in {"human", "tool"}:
            continue
        text = _message_content_text(message)
        if text:
            return text
    return ""


def _stringify_final_message(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        if _message_role(message) in {"human", "tool"}:
            continue
        return _truncate_text(_stringify_tool_args(_message_to_debug_payload(message)), max_chars=4000)
    return ""


def _message_to_debug_payload(message: Any) -> Any:
    if hasattr(message, "model_dump"):
        return message.model_dump()
    if isinstance(message, dict):
        return message
    return {
        "type": _message_role(message),
        "content": getattr(message, "content", None),
        "tool_calls": getattr(message, "tool_calls", None),
    }


async def _try_astream_stage_agent(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    run_label: str,
    log_cb: LogCallback | None,
    logger: Any | None,
) -> dict[str, Any] | None:
    if not hasattr(agent, "astream_events"):
        await _emit_log(log_cb, run_label, "agent streaming is unavailable; falling back to ainvoke.", node_id=context.node_id)
        return None

    await _emit_log(log_cb, run_label, "agent stream start.", node_id=context.node_id)
    final_state: dict[str, Any] | None = None
    try:
        event_stream = agent.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config=build_agent_config(thread_id),
            version=os.environ.get("ARC_AGENT_STREAM_VERSION", "v2"),
        )
        if inspect.isawaitable(event_stream):
            event_stream = await event_stream
        async for event in event_stream:
            maybe_state = await _log_stream_event(
                log_cb,
                event,
                label=run_label,
                node_id=context.node_id,
            )
            if isinstance(maybe_state, dict):
                final_state = maybe_state
    except GraphRecursionError:
        # The step budget is exhausted; a full ainvoke retry would burn the
        # same budget again on a fresh session. Let the failure propagate.
        raise
    except ARCModelAPIError as exc:
        # The model API call already exhausted its adapter-level retry chain
        # (or tripped the consecutive-failure budget). The ainvoke fallback
        # would replay the whole session — a second full retry chain against
        # the same dead or throttling endpoint — so surface the error to the
        # caller; the workflow marks the node failed and stays resumable.
        await _emit_log(
            log_cb,
            run_label,
            f"agent stream failed on a model API error; surfacing the error. error={exc}",
            status="error",
            node_id=context.node_id,
        )
        log_to_logger(logger, "AGENT_STREAM_MODEL_API_ERROR", label=run_label, thread_id=thread_id, body=str(exc))
        raise
    except StructuredOutputError as exc:
        # The structured-output contract failed validation and the strategy is
        # configured not to retry in-session. The ainvoke fallback would replay
        # the whole session — a second full model conversation whose only new
        # information is the same validation failure — so surface the error to
        # the caller (the designer's repair path salvages partial rows from it).
        await _emit_log(
            log_cb,
            run_label,
            f"agent stream failed on structured output validation; surfacing the error. error={exc}",
            status="warning",
            node_id=context.node_id,
        )
        log_to_logger(logger, "AGENT_STREAM_STRUCTURED_ERROR", label=run_label, thread_id=thread_id, body=str(exc))
        raise
    except Exception as exc:
        await _emit_log(
            log_cb,
            run_label,
            f"agent stream failed; falling back to ainvoke. error={exc}",
            status="warning",
            node_id=context.node_id,
        )
        log_to_logger(logger, "AGENT_STREAM_FALLBACK", label=run_label, thread_id=thread_id, body=str(exc))
        return None

    if final_state is None:
        await _emit_log(
            log_cb,
            run_label,
            "agent stream ended without final state; falling back to ainvoke.",
            status="warning",
            node_id=context.node_id,
        )
        return None

    await _log_agent_trace(log_cb, final_state, label=run_label, thread_id=thread_id, node_id=context.node_id)
    return extract_payload(final_state)


async def _log_stream_event(
    log_cb: LogCallback | None,
    event: Any,
    *,
    label: str,
    node_id: str,
) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        text = _typed_stream_item_text(event)
        if text:
            await _emit_log(log_cb, label, f"stream: {text}", node_id=node_id)
        return None

    event_name = str(event.get("event", "") or "")
    name = str(event.get("name", "") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if event_name in {"on_chat_model_stream", "on_llm_stream"}:
        # The model is configured for non-streaming output. Ignore any provider
        # chunks that still arrive and render the complete response on *_end.
        return None

    if event_name in {"on_chat_model_end", "on_llm_end"}:
        text = _message_content_text(data.get("output"))
        if text:
            await _emit_log(log_cb, label, f"model-final> {_truncate_text(text, max_chars=2400)}", node_id=node_id)
        return None

    if event_name == "on_tool_start":
        tool_input = data.get("input")
        await _emit_log(
            log_cb,
            label,
            f"tool-call> {name or 'unknown'} args={_truncate_text(_stringify_tool_args(tool_input), max_chars=1000)}",
            node_id=node_id,
        )
        skill_name = _skill_name_from_read_call(name, tool_input)
        if skill_name:
            await _emit_log(log_cb, label, f"skill-loaded: {skill_name}", node_id=node_id)
        return None

    if event_name == "on_tool_end":
        await _emit_log(
            log_cb,
            label,
            f"tool-result> {name or 'unknown'} result={_truncate_text(_stringify_tool_args(data.get('output')), max_chars=2000)}",
            node_id=node_id,
        )
        return None

    if event_name in {"on_chain_end", "on_graph_end"}:
        output = data.get("output")
        if isinstance(output, dict) and _is_agent_state_with_payload(output):
            return output
    return None


def _skill_name_from_read_call(tool_name: str, tool_input: Any) -> str:
    """Return the selected skill name when the model reads its instruction file."""

    if tool_name != "read_file" or not isinstance(tool_input, dict):
        return ""
    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip().replace("\\", "/")
    match = re.fullmatch(r"/skills/([^/]+)/SKILL\.md", file_path)
    return match.group(1) if match else ""


def _is_agent_state_with_payload(state: dict[str, Any]) -> bool:
    structured = state.get("structured_response")
    if structured is not None:
        return True
    messages = state.get("messages")
    return isinstance(messages, list) and any(_message_role(message) not in {"human", "tool"} for message in messages)


async def _log_agent_trace(
    log_cb: LogCallback | None,
    result: dict[str, Any],
    *,
    label: str,
    thread_id: str,
    node_id: str,
) -> None:
    if not _should_log_full_agent_trace():
        return
    formatted = _format_message_trace(result.get("messages", []) if isinstance(result, dict) else [])
    if not formatted:
        await _emit_log(log_cb, label, f"agent trace is empty: thread_id={thread_id}", node_id=node_id)
        return
    await _emit_log(log_cb, label, f"agent trace: thread_id={thread_id}\n{formatted}", node_id=node_id)


async def _log_completed_tool_batches(
    log_cb: LogCallback | None,
    result: dict[str, Any],
    *,
    label: str,
    node_id: str,
) -> None:
    """Emit each completed tool-call batch from a non-streaming agent result."""

    messages = result.get("messages", []) if isinstance(result, dict) else []
    pending: list[dict[str, str]] = []
    for message in messages if isinstance(messages, list) else []:
        calls = _extract_tool_calls(message)
        if calls:
            await _emit_tool_batch(log_cb, label, pending, node_id=node_id)
            pending = [
                {
                    "id": str(call.get("id", "") or call.get("tool_call_id", "")),
                    "name": str(call.get("name", "") or call.get("tool_name", "") or "unknown"),
                    "args": _truncate_text(_stringify_tool_args(call.get("args")), max_chars=1200),
                    "result": "",
                }
                for call in calls
            ]
            continue

        if _message_role(message) != "tool" or not pending:
            continue
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id is None and isinstance(message, dict):
            tool_call_id = message.get("tool_call_id")
        matched = next((item for item in pending if item["id"] and item["id"] == str(tool_call_id or "")), None)
        target = matched or next((item for item in pending if not item["result"]), None)
        if target is not None:
            target["result"] = _truncate_text(_message_content_text(message), max_chars=2000)
        if all(item["result"] for item in pending):
            await _emit_tool_batch(log_cb, label, pending, node_id=node_id)
            pending = []

    await _emit_tool_batch(log_cb, label, pending, node_id=node_id)


async def _emit_tool_batch(
    log_cb: LogCallback | None,
    label: str,
    tools: list[dict[str, str]],
    *,
    node_id: str,
) -> None:
    if not tools:
        return
    payload = {
        "tools": [
            {"name": item["name"], "args": item["args"], "result": item["result"] or "[no result returned]"}
            for item in tools
        ]
    }
    await _emit_log(log_cb, label, f"tool-batch> {json.dumps(payload, ensure_ascii=False)}", node_id=node_id)


def _should_log_full_agent_trace() -> bool:
    return str(os.environ.get("ARC_DEBUG_AGENT_TRACE", "")).strip().lower() in {"1", "true", "yes", "on"}


def _format_message_trace(messages: list[Any]) -> str:
    if not isinstance(messages, list) or not messages:
        return ""
    blocks: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = _message_role(message)
        if role == "human":
            continue
        lines = [f"[{index}] {role.upper()}"]
        tool_calls = _extract_tool_calls(message)
        if tool_calls:
            lines.append("tool_calls:")
            for tool_index, call in enumerate(tool_calls, start=1):
                tool_name = str(call.get("name", "") or call.get("tool_name", "") or "unknown")
                tool_id = str(call.get("id", "") or call.get("tool_call_id", "") or "-")
                tool_args = _truncate_text(_stringify_tool_args(call.get("args")), max_chars=1000)
                lines.append(f"  {tool_index}. {tool_name} id={tool_id}")
                if tool_args:
                    lines.append(f"     args: {tool_args}")
        tool_result_meta = _extract_tool_result_meta(message)
        if tool_result_meta:
            lines.append(tool_result_meta)
        content = _message_content_text(message)
        if content:
            lines.append("content:")
            lines.append(_indent_block(_truncate_text(content, max_chars=2400)))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _message_role(message: Any) -> str:
    role = getattr(message, "type", None) or getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("type") or message.get("role")
    if isinstance(role, str) and role:
        return role
    cls_name = message.__class__.__name__.lower()
    if "tool" in cls_name:
        return "tool"
    if "human" in cls_name:
        return "human"
    if "ai" in cls_name or "assistant" in cls_name:
        return "assistant"
    return cls_name or "message"


def _extract_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if isinstance(call, dict):
            normalized.append(call)
            continue
        normalized.append(
            {
                "name": getattr(call, "name", ""),
                "id": getattr(call, "id", ""),
                "args": getattr(call, "args", {}),
            }
        )
    return normalized


def _extract_tool_result_meta(message: Any) -> str:
    if _message_role(message) != "tool":
        return ""
    tool_name = getattr(message, "name", None)
    if tool_name is None and isinstance(message, dict):
        tool_name = message.get("name")
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id is None and isinstance(message, dict):
        tool_call_id = message.get("tool_call_id")
    parts = ["tool_result:"]
    if tool_name:
        parts.append(f"name={tool_name}")
    if tool_call_id:
        parts.append(f"id={tool_call_id}")
    return " ".join(parts)


def _message_content_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return _content_text(content)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [_content_text(item) for item in content]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(content, dict):
        for key in ("text", "content", "output_text"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        parsed = content.get("parsed")
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False)
        if isinstance(parsed, str) and parsed.strip():
            return parsed.strip()
        nested_parts = [_content_text(value) for value in content.values()]
        return "\n".join(part for part in nested_parts if part).strip()
    if content is None:
        return ""
    text = getattr(content, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    nested = getattr(content, "content", None)
    if nested is not None:
        return _content_text(nested)
    return str(content).strip()


def _typed_stream_item_text(item: Any) -> str:
    if isinstance(item, tuple) and item:
        parts = [_typed_stream_item_text(part) for part in item]
        return " ".join(part for part in parts if part).strip()
    text = getattr(item, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = _message_content_text(item)
    if content:
        return content
    name = getattr(item, "name", None)
    status = getattr(item, "status", None)
    if name or status:
        return f"name={name or '-'} status={status or '-'}"
    return ""


def _stringify_tool_args(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return repr(value)


def _truncate_text(text: str, *, max_chars: int) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 18].rstrip() + "\n...[truncated]"


def _indent_block(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines()) if text else prefix


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result


def _should_stream(stream: bool | None) -> bool:
    if stream is not None:
        return stream
    return True


def _resolve_recursion_limit() -> int:
    raw = os.environ.get("ARC_AGENT_RECURSION_LIMIT", "").strip()
    if not raw:
        return DEFAULT_RECURSION_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RECURSION_LIMIT
    return max(_MIN_RECURSION_LIMIT, value)


def build_agent_config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": _resolve_recursion_limit(),
    }


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []

    def add(candidate: str) -> None:
        stripped = candidate.strip()
        if stripped and stripped not in candidates:
            candidates.append(stripped)

    add(text)
    for match in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
        add(match)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            add(text[start : end + 1])
    return candidates

"""The stage-agent session step budget must be bounded and overridable.

A runaway agent session (the model repeatedly re-editing a file it just
corrupted) used to be bounded only by ``recursion_limit=5000`` - roughly hours
of model calls on a single node. The default now reflects the empirical 12306
benchmark distribution (healthy sessions <= ~150 steps, pathological > 450),
and operators can raise it for a run via ``ARC_AGENT_RECURSION_LIMIT``.
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.errors import GraphRecursionError

from agents.model.openai_api_adapter import ARCModelAPIError
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.runners import (
    DEFAULT_RECURSION_LIMIT,
    _MIN_RECURSION_LIMIT,
    StageAgentStreamError,
    StreamCompatibilityError,
    _resolve_recursion_limit,
    ainvoke_stage_agent,
    build_agent_config,
)


def test_default_limit_is_bounded_below_legacy_5000() -> None:
    assert DEFAULT_RECURSION_LIMIT < 5000
    assert build_agent_config("t")["recursion_limit"] == DEFAULT_RECURSION_LIMIT


def test_env_override_raises_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_AGENT_RECURSION_LIMIT", "1500")
    assert _resolve_recursion_limit() == 1500
    assert build_agent_config("t")["recursion_limit"] == 1500


def test_invalid_or_tiny_values_fall_back_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for raw in ("", "abc", "-5", "0"):
        monkeypatch.setenv("ARC_AGENT_RECURSION_LIMIT", raw)
        assert _resolve_recursion_limit() >= _MIN_RECURSION_LIMIT

    monkeypatch.setenv("ARC_AGENT_RECURSION_LIMIT", "5")
    assert _resolve_recursion_limit() == _MIN_RECURSION_LIMIT

    monkeypatch.delenv("ARC_AGENT_RECURSION_LIMIT")
    assert _resolve_recursion_limit() == DEFAULT_RECURSION_LIMIT


def test_thread_id_still_reaches_the_config() -> None:
    config = build_agent_config("proj:REQ-1.1:IMPLEMENT")
    assert config["configurable"]["thread_id"] == "proj:REQ-1.1:IMPLEMENT"


class _BudgetExhaustedAgent:
    name = "budget-probe"

    async def ainvoke(self, *_args, **_kwargs):
        raise GraphRecursionError("Recursion limit of 300 reached")


class _StreamBudgetExhaustedAgent:
    name = "budget-probe"

    async def astream_events(self, *_args, **_kwargs):
        raise GraphRecursionError("Recursion limit of 300 reached")

    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("a step-budget exhaustion must not be retried via ainvoke")


class _StreamGenericFailureAgent:
    name = "generic-stream-failure-probe"

    async def astream_events(self, *_args, **_kwargs):
        raise RuntimeError("stream parser exploded")

    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("a generic stream failure must not replay the agent session")


class _StreamCompatibilityFailureAgent:
    name = "stream-compatibility-probe"

    def __init__(self) -> None:
        self.ainvoke_calls = 0

    async def astream_events(self, *_args, **_kwargs):
        raise StreamCompatibilityError("stream transport is unsupported")

    async def ainvoke(self, *_args, **_kwargs):
        self.ainvoke_calls += 1
        return {"messages": [{"role": "assistant", "content": "{\"ok\": true}"}]}


class _StreamPostToolFailureAgent:
    name = "post-tool-stream-failure-probe"

    async def astream_events(self, *_args, **_kwargs):
        yield {
            "event": "on_tool_start",
            "name": "write_file",
            "data": {"input": {"file_path": "/workspace/app.js"}},
        }
        raise RuntimeError("tool-side stream failure")

    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("a stream failure after a tool event must not replay the session")


class _StreamPostFileFailureAgent:
    name = "post-file-stream-failure-probe"

    async def astream_events(self, *_args, **_kwargs):
        yield {
            "event": "on_file_write",
            "name": "write_file",
            "data": {"path": "/workspace/app.js"},
        }
        raise RuntimeError("file-side stream failure")

    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("a stream failure after a file event must not replay the session")


class _StreamPostToolCompatibilityFailureAgent:
    name = "post-tool-compatibility-failure-probe"

    async def astream_events(self, *_args, **_kwargs):
        yield {
            "event": "on_tool_start",
            "name": "write_file",
            "data": {"input": {"file_path": "/workspace/app.js"}},
        }
        raise StreamCompatibilityError("stream transport is unsupported")

    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("a compatibility failure after a tool event must not replay the session")


class _StreamWithoutFinalStateAgent:
    name = "missing-final-state-probe"

    async def astream_events(self, *_args, **_kwargs):
        yield {"event": "on_chat_model_end", "data": {"output": "done"}}

    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("a stream without final state must not replay the session")


def _context(workspace) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        node_id="REQ-BUDGET-1",
        phase="IMPLEMENT",
        app_type="web",
        workspace_root=str(workspace),
        requirement_path="",
    )


def _logs():
    entries = []

    async def log_cb(agent_name, message, status, node_id):
        entries.append((agent_name, message, status, node_id))

    return entries, log_cb


def test_recursion_error_message_names_the_env_override(tmp_path) -> None:
    """The workflow logs only ``type(exc).__name__: str(exc)`` on a node
    crash, so the message itself must point operators at the escape hatch."""

    with pytest.raises(GraphRecursionError) as excinfo:
        asyncio.run(
            ainvoke_stage_agent(
                _BudgetExhaustedAgent(),
                message="go",
                context=_context(tmp_path),
                thread_id="REQ-BUDGET-1:probe",
            )
        )

    message = str(excinfo.value)
    assert "step budget" in message
    assert "recursion_limit=300" in message
    assert "ARC_AGENT_RECURSION_LIMIT" in message


def test_stream_budget_exhaustion_does_not_fall_back_to_ainvoke(tmp_path) -> None:
    """Falling back would rerun the whole session on a fresh thread and burn
    the same budget a second time; the error must propagate instead."""

    logs, log_cb = _logs()
    with pytest.raises(GraphRecursionError):
        asyncio.run(
            ainvoke_stage_agent(
                _StreamBudgetExhaustedAgent(),
                message="go",
                context=_context(tmp_path),
                thread_id="REQ-BUDGET-1:probe",
                log_cb=log_cb,
            )
        )
    assert any(entry[2] == "error" and "step budget" in entry[1] for entry in logs)


def test_generic_stream_failure_before_any_event_is_terminal(tmp_path) -> None:
    logs, log_cb = _logs()

    with pytest.raises(RuntimeError, match="stream parser exploded"):
        asyncio.run(
            ainvoke_stage_agent(
                _StreamGenericFailureAgent(),
                message="go",
                context=_context(tmp_path),
                thread_id="REQ-BUDGET-1:generic-stream-failure",
                log_cb=log_cb,
            )
        )

    terminal = [entry for entry in logs if entry[2] == "error"]
    assert terminal
    assert "surfacing the terminal error" in terminal[-1][1]
    assert "event_seen=False" in terminal[-1][1]


def test_explicit_stream_compatibility_failure_falls_back_once(tmp_path) -> None:
    agent = _StreamCompatibilityFailureAgent()
    logs, log_cb = _logs()

    payload = asyncio.run(
        ainvoke_stage_agent(
            agent,
            message="go",
            context=_context(tmp_path),
            thread_id="REQ-BUDGET-1:compatibility-failure",
            log_cb=log_cb,
        )
    )

    assert payload == {"ok": True}
    assert agent.ainvoke_calls == 1
    assert any("compatibility failure" in entry[1] for entry in logs)
    assert not any(entry[2] == "error" for entry in logs)


@pytest.mark.parametrize(
    "agent_factory",
    [_StreamPostToolFailureAgent, _StreamPostFileFailureAgent],
)
def test_stream_failure_after_tool_or_file_event_is_terminal(tmp_path, agent_factory) -> None:
    logs, log_cb = _logs()

    with pytest.raises(RuntimeError):
        asyncio.run(
            ainvoke_stage_agent(
                agent_factory(),
                message="go",
                context=_context(tmp_path),
                thread_id="REQ-BUDGET-1:post-side-effect-failure",
                log_cb=log_cb,
            )
        )

    terminal = [entry for entry in logs if entry[2] == "error"]
    assert terminal
    assert "event_seen=True" in terminal[-1][1]
    assert "side_effect_seen=True" in terminal[-1][1]


def test_classified_compatibility_failure_after_tool_event_is_terminal(tmp_path) -> None:
    logs, log_cb = _logs()

    with pytest.raises(StreamCompatibilityError, match="stream transport is unsupported"):
        asyncio.run(
            ainvoke_stage_agent(
                _StreamPostToolCompatibilityFailureAgent(),
                message="go",
                context=_context(tmp_path),
                thread_id="REQ-BUDGET-1:post-tool-compatibility-failure",
                log_cb=log_cb,
            )
        )

    terminal = [entry for entry in logs if entry[2] == "error"]
    assert terminal
    assert "event_seen=True" in terminal[-1][1]
    assert "side_effect_seen=True" in terminal[-1][1]


def test_stream_without_final_state_is_terminal(tmp_path) -> None:
    logs, log_cb = _logs()

    with pytest.raises(StageAgentStreamError, match="without final state"):
        asyncio.run(
            ainvoke_stage_agent(
                _StreamWithoutFinalStateAgent(),
                message="go",
                context=_context(tmp_path),
                thread_id="REQ-BUDGET-1:missing-final-state",
                log_cb=log_cb,
            )
        )

    assert any(entry[2] == "error" and "without final state" in entry[1] for entry in logs)


class _StreamModelAPIErrorAgent:
    name = "api-error-probe"

    async def astream_events(self, *_args, **_kwargs):
        raise ARCModelAPIError(
            "Model API request failed using `chat_completions` mode; model=test-model",
            api_mode="chat_completions",
            model="test-model",
        )

    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("a model API failure must not be retried via ainvoke")


def test_stream_model_api_error_does_not_fall_back_to_ainvoke(tmp_path) -> None:
    """The adapter retry chain already ran (or tripped the consecutive-failure
    budget); the ainvoke fallback would replay the whole session as a second
    full retry chain against the same endpoint. The error must propagate."""

    logs, log_cb = _logs()
    with pytest.raises(ARCModelAPIError):
        asyncio.run(
            ainvoke_stage_agent(
                _StreamModelAPIErrorAgent(),
                message="go",
                context=_context(tmp_path),
                thread_id="REQ-BUDGET-1:api-error-probe",
                log_cb=log_cb,
            )
        )
    assert any(entry[2] == "error" and "model API error" in entry[1] for entry in logs)

"""Faux model + fake app handler: drive ARC's real agent loops without a real LLM.

Python port of pi's faux provider (``pi/packages/ai/src/providers/faux.ts``) and
the e2e harness idea (``pi/packages/coding-agent/test/suite/harness.ts``):

- ``FauxChatModel`` is a LangChain ``BaseChatModel`` that pops scripted
  ``AIMessage``s from a queue (``faux_text`` / ``faux_tool_call`` builders mirror
  pi's ``fauxText`` / ``fauxToolCall``; an ``Exception`` queue entry raises
  that call's error instead). Because it is a real chat model object,
  ``build_stage_agent``/``create_deep_agent`` drive the genuine agent loop:
  tool calls are executed by the real tool nodes, results flow back as
  ``ToolMessage``s, and middleware (``StageDisciplineMiddleware``,
  ``DisableToolsMiddleware``) intercepts for real.
- ``FakeAppHandler`` scripts ``run_test_group`` outputs so the TDD loop's
  "write code -> run_tests -> fail -> fix -> pass" cycle can be exercised
  without spawning npm/pytest.

Both raise loudly when a script is exhausted, so tests fail instead of looping.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

from app_type_handler.test_results import TestRunResult, parse_test_run


def faux_text(text: str) -> AIMessage:
    """A scripted assistant turn that ends the loop (no tool calls)."""

    return AIMessage(content=text)


def faux_tool_call(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> AIMessage:
    """A scripted assistant turn issuing exactly one tool call."""

    return faux_tool_calls((name, args, call_id), response_metadata=response_metadata)


def faux_tool_calls(*calls: Any, response_metadata: dict[str, Any] | None = None) -> AIMessage:
    """A scripted assistant turn issuing one or more tool calls.

    Each entry is either ``(name, args)`` or ``(name, args, call_id)``.
    ``response_metadata`` simulates provider metadata such as
    ``{"finish_reason": "length"}`` for truncated-output scenarios.
    """

    normalized = []
    for index, call in enumerate(calls):
        name, args = call[0], call[1]
        call_id = call[2] if len(call) > 2 and call[2] else f"faux-call-{index}"
        normalized.append({"name": name, "args": args, "id": call_id, "type": "tool_call"})
    return AIMessage(content="", tool_calls=normalized, response_metadata=response_metadata or {})


def tool_display_name(tool: Any) -> str:
    """The name a tool carries into ``bind_tools`` (BaseTool, dict, callable)."""

    if isinstance(tool, dict):
        inner = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = inner.get("name") if isinstance(inner, dict) else None
        return str(name) if name else ""
    name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
    return str(name) if name else str(tool)


class FauxChatModel(BaseChatModel):
    """Scripted chat model: each model call consumes the next queued response.

    Queue entries are normally scripted ``AIMessage``s; an ``Exception``
    entry makes that one model call raise instead, scripting provider
    failures (transient errors, malformed payloads) without a real LLM.
    """

    responses: list[BaseMessage | Exception] = Field(default_factory=list)
    _queue: deque = PrivateAttr(default_factory=deque)
    _calls: list = PrivateAttr(default_factory=list)
    _bound_tool_name_sets: list[list[str]] = PrivateAttr(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        self.set_responses(self.responses)

    # -- scripting API (mirrors pi's faux provider registration) ------------

    def set_responses(self, responses: list[BaseMessage | Exception]) -> None:
        self.responses = list(responses)
        self._queue = deque(self.responses)

    def append_responses(self, responses: list[BaseMessage]) -> None:
        self._queue.extend(responses)

    def get_pending_response_count(self) -> int:
        return len(self._queue)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def calls(self) -> list[list[BaseMessage]]:
        """Messages the agent sent to the model on every call."""

        return self._calls

    @property
    def bound_tool_name_sets(self) -> list[list[str]]:
        """Tool names bound on each model call, in bind order.

        ``bind_tools`` receives the post-middleware tool list — after the
        mount-time capability filter, the harness-profile tool exclusion and
        ``DisableToolsMiddleware`` — so this is the tool surface the model
        actually sees on every turn (issue #182 mount-surface pins).
        """

        return [list(names) for names in self._bound_tool_name_sets]

    # -- BaseChatModel plumbing ---------------------------------------------

    @property
    def _llm_type(self) -> str:
        return "faux-chat-model"

    def _get_ls_params(self, *, stop: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        # Pretend to be an unknown provider so structured-output selection
        # falls back to ToolStrategy (an ordinary tool call we can script).
        return {"ls_provider": "faux", "ls_model_name": "faux-1"}

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FauxChatModel":
        self._bound_tool_name_sets.append([tool_display_name(tool) for tool in (tools or [])])
        return self

    def bind(self, **kwargs: Any) -> "FauxChatModel":
        del kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        self._calls.append(list(messages))
        if not self._queue:
            raise RuntimeError(
                "FauxChatModel ran out of scripted responses "
                f"after {self.call_count} call(s). Extend the script or check the loop."
            )
        response = self._queue.popleft()
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


class FakeAppHandler:
    """Stand-in for ``AppTypeHandler`` with scripted ``run_test_group`` outputs.

    Scripts are plain runner texts (the shape real handlers render); each is
    converted through the single parser into the :class:`TestRunResult` the
    phase side consumes.
    """

    def __init__(self, results: list[str] | None = None) -> None:
        self._results: deque[str] = deque(results or [])
        self.calls: list[tuple[str, list[str]]] = []
        # Parallel to ``calls``: the failed-case filter each call carried
        # (None = unfiltered full run). Lets TDD-loop tests assert which
        # rounds re-ran only the digest's failed cases.
        self.case_filters: list[list[str] | None] = []
        self.shutdown_calls = 0
        self.install_calls: list[tuple[str, str]] = []
        self._install_results: deque[str] = deque()

    def queue_install(self, *results: str) -> "FakeAppHandler":
        self._install_results.extend(results)
        return self

    def queue(self, *results: str) -> "FakeAppHandler":
        self._results.extend(results)
        return self

    def _record_call(
        self,
        test_type: str,
        file_paths: list[str],
        failed_case_names: list[str] | None,
    ) -> None:
        """Append one call to the parallel ``calls``/``case_filters`` records."""

        self.calls.append((test_type, list(file_paths)))
        self.case_filters.append(list(failed_case_names) if failed_case_names else None)

    async def run_test_group(
        self,
        test_type: str,
        file_paths: list[str],
        web_port: int | None = None,
        failed_case_names: list[str] | None = None,
    ) -> TestRunResult:
        del web_port  # per-task port override; the fake records the call only
        self._record_call(test_type, file_paths, failed_case_names)
        if not self._results:
            raise RuntimeError(
                f"FakeAppHandler ran out of scripted results after {len(self.calls)} call(s)."
            )
        return parse_test_run(self._results.popleft())

    async def shutdown_e2e_runtime(self) -> None:
        self.shutdown_calls += 1

    async def run_build(self) -> str:
        return "Exit Code: 0\nSTDERR:\n(fake build ok)\n"

    async def install_package(self, package: str, target: str = "backend") -> str:
        """Scriptable stand-in for the TDD-stage package install."""

        self.install_calls.append((package, target))
        if self._install_results:
            return self._install_results.popleft()
        return (
            "Exit Code: 0\n"
            f"Installed '{package}' into {target}/node_modules (no-save; package.json and "
            "lockfile untouched). Re-run run_tests to validate the repair.\n"
        )

    def validate_test_path(self, test_type: str, file_path: str) -> str:
        del test_type, file_path
        return ""


def test_result(exit_code: int, detail: str = "") -> str:
    """Format a runner output the way ARC test runners report results."""

    lines = [f"Exit Code: {exit_code}", "STDERR:"]
    if detail:
        lines.append(detail)
    return "\n".join(lines) + "\n"


def failing_test_output(detail: str = "AssertionError: expected 2 got 1") -> str:
    return test_result(1, detail)


def passing_test_output(detail: str = "1 passed") -> str:
    return test_result(0, detail)


def drive_scripted_tool_turns(
    workspace_root: Path,
    turns: list[list[tuple]],
    *,
    stage: str = "implementation",
    node_id: str = "REQ-FS-PROBE",
    label: str = "FsBehaviorProbe",
) -> list[list[str]]:
    """Drive scripted tool-call turns through one real ``build_stage_agent`` agent.

    Builds the production stage agent (scripted by ``FauxChatModel``), issues
    each turn's tool calls on its own assistant turn, then ends the loop with
    a plain text turn. Returns, per turn, the ToolMessage contents the model
    received on the following turn — the same text a real provider would see,
    middleware chain included. Tests use this to assert filesystem behaviors
    through the build path instead of applying runtime patches themselves.

    Each turn entry is ``(name, args)`` or ``(name, args, call_id)``; call ids
    are auto-assigned (``faux-call-<turn>-<index>``) when omitted. Multiple
    calls within one turn may resume concurrently, so per-call middleware
    state (e.g. the grep no-match streak) is only deterministic across turns.
    Per-call ids make the returned contents stable even though every model
    call replays the full conversation.

    Stateless: the agent is built with ``checkpointer=None``.
    """

    import asyncio

    from agents.runtime.contracts import AgentRuntimeContext
    from agents.runtime.factory import build_stage_agent
    from agents.runtime.runners import ainvoke_stage_agent

    phase = {"implementation": "IMPLEMENT", "test_generation": "TEST_GENERATION"}.get(stage)
    if phase is None:
        raise ValueError(f"unsupported probe stage: {stage!r}")

    responses: list[BaseMessage] = []
    turn_call_ids: list[list[str]] = []
    for turn_index, entries in enumerate(turns):
        call_ids = []
        normalized = []
        for entry_index, entry in enumerate(entries):
            name, args = entry[0], entry[1]
            call_id = entry[2] if len(entry) > 2 and entry[2] else f"faux-call-{turn_index}-{entry_index}"
            call_ids.append(call_id)
            normalized.append((name, args, call_id))
        turn_call_ids.append(call_ids)
        responses.append(faux_tool_calls(*normalized))
    model = FauxChatModel(responses=[*responses, faux_text("DONE")])
    built = build_stage_agent(
        name=label.lower(),
        stage=stage,
        model=model,
        system_prompt="You are a test agent.",
        response_format=None,
        workspace_root=str(workspace_root),
        writable_roots=[str(workspace_root)],
        skills=[],
        memory=[],
        tools=[],
        checkpointer=None,
    )
    asyncio.run(
        ainvoke_stage_agent(
            built.agent,
            message="run the scripted tool calls",
            context=AgentRuntimeContext(
                node_id=node_id,
                phase=phase,
                app_type="web",
                workspace_root=str(workspace_root),
                requirement_path="",
            ),
            thread_id=f"{node_id}:{label.lower()}",
            label=label,
        )
    )
    if model.call_count < 2:
        raise AssertionError(
            f"expected the probe loop to reach a second model turn after the "
            f"{turns[0][0][0]} call(s) (call_count={model.call_count})"
        )
    contents_by_call_id: dict[str, str] = {}
    for turn_messages in model.calls:
        for message in turn_messages:
            if getattr(message, "type", "") == "tool":
                # Each model call replays the full conversation; the latest
                # content per tool_call_id is the executed result.
                contents_by_call_id[str(getattr(message, "tool_call_id", ""))] = str(message.content)
    per_turn: list[list[str]] = []
    for call_ids in turn_call_ids:
        missing = [call_id for call_id in call_ids if call_id not in contents_by_call_id]
        if missing:
            raise AssertionError(f"probe turn produced no tool result for call id(s): {missing}")
        per_turn.append([contents_by_call_id[call_id] for call_id in call_ids])
    return per_turn


def drive_scripted_tool_call(
    workspace_root: Path,
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    stage: str = "implementation",
    call_id: str = "call-probe-1",
) -> list[str]:
    """Drive exactly one scripted tool call through a real ``build_stage_agent`` agent.

    Single-turn convenience over :func:`drive_scripted_tool_turns`; see it for
    the harness contract.
    """

    (contents,) = drive_scripted_tool_turns(
        workspace_root,
        [[(tool_name, tool_args, call_id)]],
        stage=stage,
    )
    return contents

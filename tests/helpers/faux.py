"""Faux model + fake app handler: drive ARC's real agent loops without a real LLM.

Python port of pi's faux provider (``pi/packages/ai/src/providers/faux.ts``) and
the e2e harness idea (``pi/packages/coding-agent/test/suite/harness.ts``):

- ``FauxChatModel`` is a LangChain ``BaseChatModel`` that pops scripted
  ``AIMessage``s from a queue (``faux_text`` / ``faux_tool_call`` builders mirror
  pi's ``fauxText`` / ``fauxToolCall``). Because it is a real chat model object,
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
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


def faux_text(text: str) -> AIMessage:
    """A scripted assistant turn that ends the loop (no tool calls)."""

    return AIMessage(content=text)


def faux_tool_call(name: str, args: dict[str, Any], *, id: str | None = None) -> AIMessage:
    """A scripted assistant turn issuing exactly one tool call."""

    return faux_tool_calls((name, args, id))


def faux_tool_calls(*calls: Any) -> AIMessage:
    """A scripted assistant turn issuing one or more tool calls.

    Each entry is either ``(name, args)`` or ``(name, args, id)``.
    """

    normalized = []
    for index, call in enumerate(calls):
        name, args = call[0], call[1]
        call_id = call[2] if len(call) > 2 and call[2] else f"faux-call-{index}"
        normalized.append({"name": name, "args": args, "id": call_id, "type": "tool_call"})
    return AIMessage(content="", tool_calls=normalized)


class FauxChatModel(BaseChatModel):
    """Scripted chat model: each model call consumes the next queued response."""

    responses: list[BaseMessage] = []
    _queue: deque = PrivateAttr(default_factory=deque)
    _calls: list = PrivateAttr(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        self.set_responses(self.responses)

    # -- scripting API (mirrors pi's faux provider registration) ------------

    def set_responses(self, responses: list[BaseMessage]) -> None:
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

    # -- BaseChatModel plumbing ---------------------------------------------

    @property
    def _llm_type(self) -> str:
        return "faux-chat-model"

    def _get_ls_params(self, *, stop: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        # Pretend to be an unknown provider so structured-output selection
        # falls back to ToolStrategy (an ordinary tool call we can script).
        return {"ls_provider": "faux", "ls_model_name": "faux-1"}

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FauxChatModel":
        del tools, kwargs
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
        return ChatResult(generations=[ChatGeneration(message=response)])


class FakeAppHandler:
    """Stand-in for ``AppTypeHandler`` with scripted ``run_test_group`` outputs."""

    def __init__(self, results: list[str] | None = None) -> None:
        self._results: deque[str] = deque(results or [])
        self.calls: list[tuple[str, list[str]]] = []

    def queue(self, *results: str) -> "FakeAppHandler":
        self._results.extend(results)
        return self

    async def run_test_group(self, test_type: str, file_paths: list[str]) -> str:
        self.calls.append((test_type, list(file_paths)))
        if not self._results:
            raise RuntimeError(
                f"FakeAppHandler ran out of scripted results after {len(self.calls)} call(s)."
            )
        return self._results.popleft()

    async def run_build(self) -> str:
        return "Exit Code: 0\nSTDERR:\n(fake build ok)\n"

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

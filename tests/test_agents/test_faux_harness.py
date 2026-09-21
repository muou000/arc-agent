"""Sanity e2e for the faux harness itself (port of pi's ``test-harness.test.ts``).

A scripted ``FauxChatModel`` drives a *real* ``build_stage_agent`` deep agent:
the model issues tool calls, the actual tool nodes execute them (including
deep-agents filesystem writes), results flow back as ``ToolMessage``s, and the
loop terminates on a plain text response. No network, no tokens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent

from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call


def _invoke(agent, message: str, workspace: Path) -> dict:
    return asyncio.run(
        ainvoke_stage_agent(
            agent,
            message=message,
            context=AgentRuntimeContext(
                node_id="REQ-HARNESS",
                phase="IMPLEMENT",
                app_type="web",
                workspace_root=str(workspace),
                requirement_path="",
            ),
            thread_id="REQ-HARNESS:test",
            label="FauxHarness",
        )
    )


def test_scripted_tool_calls_drive_real_agent_loop(tmp_project_dir: Path) -> None:
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/hello.py", "content": "print('hi')\n"},
                call_id="call-1",
            ),
            faux_tool_call("echo", {"text": "loop-ok"}, call_id="call-2"),
            faux_text("DONE"),
        ]
    )

    def echo(text: str) -> str:
        """Echo the text back."""

        return text

    built = build_stage_agent(
        name="faux_harness",
        stage="implementation",
        model=model,
        system_prompt="You are a test agent.",
        response_format=None,
        workspace_root=str(tmp_project_dir),
        writable_roots=[str(tmp_project_dir)],
        skills=[],
        memory=[],
        tools=[echo],
    )

    payload = _invoke(built.agent, "run the script", tmp_project_dir)

    # The loop consumed the whole script and ended on the final text turn.
    assert model.call_count == 3
    assert model.get_pending_response_count() == 0
    assert payload["summary"] == "DONE"

    # The scripted write_file tool call really wrote into the workspace.
    written = tmp_project_dir / "src" / "hello.py"
    assert written.read_text(encoding="utf-8") == "print('hi')\n"

    # Tool results flowed back into the next model call as tool messages.
    second_call_messages = model.calls[1]
    tool_messages = [m for m in second_call_messages if getattr(m, "type", "") == "tool"]
    assert tool_messages, "expected ToolMessage results in the second model call"
    assert any("hello.py" in str(m.content) for m in tool_messages)
    third_call_messages = model.calls[2]
    assert any(
        getattr(m, "type", "") == "tool" and "loop-ok" in str(m.content) for m in third_call_messages
    )


def test_length_truncated_tool_call_is_failed_and_reissued(tmp_project_dir: Path) -> None:
    """A length-truncated tool call must not execute; the model re-issues it.

    Mirrors pi's ``failToolCallsFromTruncatedMessage`` contract: every call in a
    truncated message gets an error tool result instead of running, and the loop
    hands those errors back to the model for a clean re-issue.
    """

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/truncated.py", "content": "print('hi"},
                call_id="call-truncated",
                response_metadata={"finish_reason": "length"},
            ),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/hello.py", "content": "print('hi')\n"},
                call_id="call-reissued",
            ),
            faux_text("DONE"),
        ]
    )

    built = build_stage_agent(
        name="faux_harness",
        stage="implementation",
        model=model,
        system_prompt="You are a test agent.",
        response_format=None,
        workspace_root=str(tmp_project_dir),
        writable_roots=[str(tmp_project_dir)],
        skills=[],
        memory=[],
        tools=[],
    )

    payload = _invoke(built.agent, "run the script", tmp_project_dir)

    assert model.call_count == 3
    assert payload["summary"] == "DONE"

    # The truncated call never touched the workspace; the re-issued one did.
    assert not (tmp_project_dir / "src" / "truncated.py").exists()
    written = tmp_project_dir / "src" / "hello.py"
    assert written.read_text(encoding="utf-8") == "print('hi')\n"

    # The model was told explicitly why the truncated call did not run.
    second_call_messages = model.calls[1]
    errors = [
        m
        for m in second_call_messages
        if getattr(m, "type", "") == "tool" and m.tool_call_id == "call-truncated"
    ]
    assert errors, "the truncated call must be answered with an error tool result"
    assert "output token limit" in str(errors[0].content)

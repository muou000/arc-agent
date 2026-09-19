"""Regression tests for cross-invocation agent conversation reuse.

ARC rebuilds a stage agent for every ``run()`` call, so without a checkpointer
each invocation starts a cold conversation. ``agents.runtime.checkpointer``
shares one LangGraph saver across those rebuilds, keyed by the stable
``thread_id`` ARC already computes per ``(node, phase, stage, test layer)``.

These tests pin both halves of the contract:

- with reuse enabled, a second invocation on the same thread sees the first
  invocation's messages (the conversation resumed);
- with ``ARC_AGENT_CHECKPOINTER=0``, the same second invocation starts cold.

They also pin that a resumed run does not inherit a stale ``structured_response``
from the previous run, which would otherwise let ``extract_payload`` return an
old structured bundle instead of the new answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from agents.runtime.checkpointer import get_checkpointer, reset_checkpointer
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call


class ProbeResponse(BaseModel):
    summary: str = Field(default="")
    items: list[dict] = Field(default_factory=list)


def build_probe(workspace: Path, model: FauxChatModel, *, response_format: object | None):
    return build_stage_agent(
        name="probe",
        stage="implementation",
        model=model,
        system_prompt="probe",
        response_format=response_format,
        workspace_root=str(workspace),
        writable_roots=[str(workspace)],
        skills=[],
        memory=[],
        tools=[],
    )


def run_twice(workspace: Path, model: FauxChatModel, *, response_format: object | None = None):
    """Invoke two separately-built agents on the same thread, returning both payloads."""

    context = AgentRuntimeContext(
        node_id="REQ-REUSE-1",
        phase="DESIGN",
        app_type="web",
        workspace_root=str(workspace),
        requirement_path="",
    )

    async def scenario() -> tuple[dict, dict]:
        first = await ainvoke_stage_agent(
            build_probe(workspace, model, response_format=response_format),
            message="first invocation",
            context=context,
            thread_id="REQ-REUSE-1:probe",
            label="probe",
        )
        second = await ainvoke_stage_agent(
            build_probe(workspace, model, response_format=response_format),
            message="second invocation",
            context=context,
            thread_id="REQ-REUSE-1:probe",
            label="probe",
        )
        return first, second

    return asyncio.run(scenario())


def call_text(messages: list) -> str:
    """Flatten one model call's messages into searchable text."""

    return "\n".join(str(getattr(message, "content", "") or "") for message in messages)


def test_second_invocation_resumes_the_conversation(tmp_project_dir: Path) -> None:
    model = FauxChatModel(responses=[faux_text("first answer"), faux_text("second answer")])

    _, second = run_twice(tmp_project_dir, model)

    assert model.call_count == 2
    # The resumed run replays the first turn (its human message and answer) before
    # appending the new one, so its prompt carries strictly more history.
    assert len(model.calls[1]) > len(model.calls[0])
    assert "first invocation" in call_text(model.calls[1])
    assert "first answer" in call_text(model.calls[1])
    assert "first answer" not in call_text(model.calls[0])
    assert second["summary"] == "second answer"


def test_resume_does_not_leak_stale_structured_response(tmp_project_dir: Path) -> None:
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "ProbeResponse",
                {"summary": "structured from run one", "items": []},
                call_id="c1",
            ),
            faux_text("plain answer from run two"),
        ]
    )

    first, second = run_twice(tmp_project_dir, model, response_format=ProbeResponse)

    assert first["summary"] == "structured from run one"
    # The second run answered in plain text; it must not surface run one's bundle.
    assert second["summary"] == "plain answer from run two"


def test_reuse_can_be_disabled_with_env_var(tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_AGENT_CHECKPOINTER", "0")
    reset_checkpointer()
    assert get_checkpointer() is None

    model = FauxChatModel(responses=[faux_text("first answer"), faux_text("second answer")])

    _, second = run_twice(tmp_project_dir, model)

    # Cold start: the second invocation never sees the first turn's messages.
    assert len(model.calls[1]) == len(model.calls[0])
    assert "first answer" not in call_text(model.calls[1])
    assert second["summary"] == "second answer"

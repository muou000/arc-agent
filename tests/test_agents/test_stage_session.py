"""Pins for the shared stage-session interface (issue #103).

``StageSession`` owns the preamble the three stage adapters used to repeat:
configuration resolution (workspace root + app type), context-pipeline
configuration and assembly, shared agent-build parameters, thread identity,
and invocation. ``StageAgentBuild`` is the declared result of
``build_stage_agent`` and carries the stage-discipline accessor that replaced
the undeclared ``arc_stage_discipline`` attribute probes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agents.runtime.stage_session as stage_session_module
from agents.context.pipeline import context_pipeline
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import StageAgentBuild
from agents.runtime.stage_session import StageSession


def make_session(tmp_path: Path, **overrides: Any) -> StageSession:
    kwargs: dict[str, Any] = {
        "agent_name": "InterfaceDesigner",
        "node_id": "REQ-SESSION-1",
        "phase": "DESIGN",
        "workspace_root": str(tmp_path),
        "app_type": "web",
    }
    kwargs.update(overrides)
    return StageSession(**kwargs)


# -- configuration resolution ---------------------------------------------------


def test_explicit_workspace_root_and_app_type_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(tmp_path / "from-env"))
    monkeypatch.setenv("ARC_APP_TYPE", "cli")
    monkeypatch.setattr(context_pipeline.config, "workspace_dir", str(tmp_path / "from-pipeline"), raising=False)
    monkeypatch.setattr(context_pipeline.config, "app_type", "android", raising=False)

    session = make_session(tmp_path, app_type="Web ")

    assert Path(session.workspace_root).resolve() == tmp_path.resolve()
    assert session.app_type == "web"


def test_workspace_root_falls_through_pipeline_env_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_pipeline.config, "workspace_dir", str(tmp_path / "from-pipeline"), raising=False)

    resolved = StageSession.resolve_workspace_root(None)
    assert Path(resolved).resolve() == (tmp_path / "from-pipeline").resolve()

    monkeypatch.setattr(context_pipeline.config, "workspace_dir", None, raising=False)
    monkeypatch.setenv("ARC_WORKSPACE_ROOT", str(tmp_path / "from-env"))
    resolved = StageSession.resolve_workspace_root(None)
    assert Path(resolved).resolve() == (tmp_path / "from-env").resolve()

    monkeypatch.setenv("ARC_WORKSPACE_ROOT", "")
    resolved = StageSession.resolve_workspace_root(None)
    assert Path(resolved).resolve() == Path.cwd().resolve()


def test_app_type_falls_through_pipeline_env_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(context_pipeline.config, "app_type", "android", raising=False)
    assert StageSession.resolve_app_type(None) == "android"

    monkeypatch.setattr(context_pipeline.config, "app_type", None, raising=False)
    monkeypatch.setenv("ARC_APP_TYPE", "CLI")
    assert StageSession.resolve_app_type(None) == "cli"

    monkeypatch.setenv("ARC_APP_TYPE", "")
    assert StageSession.resolve_app_type(None) == "web"


def test_claims_root_prefers_the_context_root(tmp_path: Path) -> None:
    context_root = tmp_path / "main-workspace"
    session = make_session(tmp_path, context_workspace_root=str(context_root))
    assert session.claims_workspace_root == str(context_root)

    plain = make_session(tmp_path)
    assert plain.claims_workspace_root == plain.workspace_root


# -- context assembly -------------------------------------------------------------


def test_build_context_configures_pipeline_and_joins_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured: dict[str, Any] = {}
    split_calls: list[dict[str, Any]] = []

    def fake_configure(**kwargs: Any) -> None:
        configured.update(kwargs)

    def fake_split(**kwargs: Any) -> tuple[str, str]:
        split_calls.append(kwargs)
        return ("  static block  ", "  dynamic block  \n\n  ")

    monkeypatch.setattr(context_pipeline, "configure", fake_configure)
    monkeypatch.setattr(context_pipeline, "build_agent_context_split", fake_split)

    session = make_session(tmp_path)
    context_text = session.build_context(preloaded_source="seed")

    assert context_text == "static block\n\ndynamic block"
    assert configured == {
        "workspace_dir": session.workspace_root,
        "app_type": "web",
    }
    assert split_calls == [
        {
            "node_id": "REQ-SESSION-1",
            "agent_type": "InterfaceDesigner",
            "map_workspace_dir": session.workspace_root,
            "preloaded_source": "seed",
        }
    ]


def test_build_context_uses_the_context_root_for_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured: dict[str, Any] = {}
    monkeypatch.setattr(
        context_pipeline,
        "configure",
        lambda **kwargs: configured.update(kwargs),
    )
    monkeypatch.setattr(
        context_pipeline,
        "build_agent_context_split",
        lambda **kwargs: ("", ""),
    )

    context_root = tmp_path / "main-workspace"
    session = make_session(tmp_path, context_workspace_root=str(context_root))
    assert session.build_context() == ""
    assert configured["workspace_dir"] == str(context_root)


# -- agent construction ------------------------------------------------------------


def test_build_agent_forwards_session_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_stage_agent(**kwargs: Any) -> StageAgentBuild:
        captured.update(kwargs)
        return StageAgentBuild(agent=object(), stage_discipline=None)

    monkeypatch.setattr(stage_session_module, "build_stage_agent", fake_build_stage_agent)

    session = make_session(tmp_path)
    built = session.build_agent(
        name="interface_designer",
        stage="interface_design",
        system_prompt="probe prompt",
        response_format=None,
        tools=[],
        skills=["/skills/"],
        max_design_writes=12,
    )

    assert isinstance(built, StageAgentBuild)
    assert captured["name"] == "interface_designer"
    assert captured["stage"] == "interface_design"
    assert captured["system_prompt"] == "probe prompt"
    assert captured["skills"] == ["/skills/"]
    assert captured["workspace_root"] == session.workspace_root
    assert captured["writable_roots"] == [session.workspace_root]
    assert captured["node_id"] == "REQ-SESSION-1"
    assert captured["claims_workspace_root"] == session.workspace_root
    assert captured["app_type"] == "web"
    assert captured["max_design_writes"] == 12
    assert "permitted_skill_names" not in captured


def test_build_agent_claims_root_follows_the_context_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        stage_session_module,
        "build_stage_agent",
        lambda **kwargs: captured.update(kwargs) or StageAgentBuild(agent=object(), stage_discipline=None),
    )

    context_root = tmp_path / "main-workspace"
    session = make_session(tmp_path, context_workspace_root=str(context_root))
    session.build_agent(
        name="probe",
        stage="implementation",
        system_prompt="p",
        response_format=None,
        tools=[],
        skills=[],
    )

    assert captured["claims_workspace_root"] == str(context_root)


# -- thread identity --------------------------------------------------------------


def test_thread_identity_and_suffix(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    assert session.thread_id().endswith(":REQ-SESSION-1:DESIGN:InterfaceDesigner")

    tdd = make_session(
        tmp_path,
        agent_name="TestDrivenDeveloper",
        node_id="REQ-SESSION-2",
        phase="IMPLEMENT",
        thread_suffix="Unit",
    )
    assert tdd.thread_id().endswith(":REQ-SESSION-2:IMPLEMENT:TestDrivenDeveloper:Unit")

    batch = make_session(
        tmp_path,
        agent_name="TestDrivenDeveloper",
        node_id="REQ-SESSION-2",
        phase="IMPLEMENT",
        thread_suffix="",
    )
    assert batch.thread_id().endswith(":REQ-SESSION-2:IMPLEMENT:TestDrivenDeveloper")
    assert not batch.thread_id().endswith(":")


def test_runtime_context_carries_session_identity(tmp_path: Path) -> None:
    session = make_session(
        tmp_path,
        requirement_path=str(tmp_path / "requirements" / "req.md"),
        test_type="Unit",
    )

    context = session.runtime_context()
    assert isinstance(context, AgentRuntimeContext)
    assert context.node_id == "REQ-SESSION-1"
    assert context.phase == "DESIGN"
    assert context.app_type == "web"
    assert context.workspace_root == session.workspace_root
    assert context.requirement_path == str(tmp_path / "requirements" / "req.md")
    assert context.test_type == "Unit"


# -- invocation -----------------------------------------------------------------------


def test_invoke_uses_the_session_thread_context_and_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_ainvoke(agent: Any, **kwargs: Any) -> dict[str, Any]:
        captured["agent"] = agent
        captured.update(kwargs)
        return {"summary": "ok"}

    monkeypatch.setattr(stage_session_module, "ainvoke_stage_agent", fake_ainvoke)

    session = make_session(tmp_path)
    built = StageAgentBuild(agent=object(), stage_discipline=None)
    payload = asyncio.run(session.invoke(built, message="run the stage"))

    assert payload == {"summary": "ok"}
    assert captured["agent"] is built.agent
    assert captured["message"] == "run the stage"
    assert captured["context"] == session.runtime_context()
    assert captured["thread_id"] == session.thread_id()
    assert captured["label"] == "InterfaceDesigner"
    assert captured["log_cb"] is None


def test_invoke_repeats_the_same_thread_across_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[str] = []

    async def fake_ainvoke(agent: Any, **kwargs: Any) -> dict[str, Any]:
        threads.append(kwargs["thread_id"])
        return {"summary": "ok"}

    monkeypatch.setattr(stage_session_module, "ainvoke_stage_agent", fake_ainvoke)

    session = make_session(tmp_path)
    built = StageAgentBuild(agent=object(), stage_discipline=None)
    asyncio.run(session.invoke(built, message="first"))
    asyncio.run(session.invoke(built, message="repair re-ask"))

    assert threads == [session.thread_id(), session.thread_id()]


# -- declared discipline accessor ------------------------------------------------------


def test_materialized_paths_accessor_reads_the_declared_discipline() -> None:
    built = StageAgentBuild(
        agent=SimpleNamespace(),
        stage_discipline=SimpleNamespace(
            materialized_paths=lambda: ["/workspace/src/a.py", "/workspace/src/b.py"]
        ),
    )
    assert built.materialized_paths() == ["/workspace/src/a.py", "/workspace/src/b.py"]


def test_materialized_paths_accessor_is_total() -> None:
    assert StageAgentBuild(agent=SimpleNamespace(), stage_discipline=None).materialized_paths() == []

    broken = StageAgentBuild(
        agent=SimpleNamespace(),
        stage_discipline=SimpleNamespace(materialized_paths=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    assert broken.materialized_paths() == []


def test_factory_build_declares_the_discipline(tmp_path: Path) -> None:
    """build_stage_agent's declared result carries the discipline — the
    materialized-paths query lives in the return type, not on an undeclared
    agent attribute."""

    from agents.runtime.factory import build_stage_agent
    from tests.helpers.faux import FauxChatModel

    built = build_stage_agent(
        name="probe",
        stage="interface_design",
        model=FauxChatModel(),
        system_prompt="probe",
        response_format=None,
        workspace_root=str(tmp_path),
        writable_roots=[str(tmp_path)],
        skills=[],
        memory=[],
        tools=[],
        node_id="REQ-SESSION-3",
    )
    assert isinstance(built, StageAgentBuild)
    assert built.agent is not None
    assert built.stage_discipline is not None
    assert built.stage_discipline._stage == "interface_design"
    assert built.materialized_paths() == []
    assert not hasattr(built.agent, "arc_stage_discipline")

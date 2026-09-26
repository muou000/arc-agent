"""Faux-model e2e pins for the read-only probe stall nudge (issue #217).

The arc-output-serial-4 REQ-1 storm: 85 greps plus a dozen 10-line window
reads of one Integration log with zero writes, uninterrupted for 35 minutes -
the only guardrail was the 300-step recursion ceiling. The nudge rides the
probe's own tool result once a single target has been probed too often inside
a zero-write window.

These tests drive a real ``build_stage_agent`` deep agent with a scripted
``FauxChatModel``, so what is asserted is exactly the tool-result text the
model receives - middleware chain included, no patches.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call

PROBE_LOG_VIRTUAL = "/workspace/.arc/tdd_runs/REQ-1/Integration-008.log"
PROBE_LOG_HOST = ".arc/tdd_runs/REQ-1/Integration-008.log"


def _seed_probe_log(tmp_project_dir: Path) -> None:
    """Materialize the log file the scripted storm greps."""

    path = tmp_project_dir / PROBE_LOG_HOST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"line {index}: Unable to find a label with the text of `/密码 Password`" for index in range(40)),
        encoding="utf-8",
    )


def _build(tmp_project_dir: Path, responses: list) -> tuple[FauxChatModel, object]:
    model = FauxChatModel(responses=responses)
    built = build_stage_agent(
        name="probe_stall_probe",
        stage="implementation",
        model=model,
        system_prompt="You are a test agent.",
        response_format=None,
        workspace_root=str(tmp_project_dir),
        writable_roots=[str(tmp_project_dir)],
        skills=[],
        memory=[],
        tools=[],
        checkpointer=None,
    )
    return model, built


def _invoke(built: object, tmp_project_dir: Path, thread_id: str) -> None:
    asyncio.run(
        ainvoke_stage_agent(
            built.agent,
            message="investigate the failing integration tests",
            context=AgentRuntimeContext(
                node_id="REQ-PROBE-STALL",
                phase="IMPLEMENT",
                app_type="web",
                workspace_root=str(tmp_project_dir),
                requirement_path="",
            ),
            thread_id=thread_id,
            label="ProbeStallProbe",
        )
    )


def _tool_results(model: FauxChatModel) -> list[str]:
    """Tool-result texts the model received, one entry per tool round-trip.

    The deep-agents history accumulates, so every tool result reappears in
    each later model call's input; the final call carries every result of the
    session exactly once.
    """

    return [
        str(message.content)
        for message in model.calls[-1]
        if getattr(message, "type", "") == "tool"
    ]


def test_grep_storm_on_one_log_injects_the_convergence_nudge_once(
    tmp_project_dir: Path,
) -> None:
    """20 greps of the same log: the 20th result carries the nudge, once.

    Red first: before the middleware counts probes, 20 identical greps return
    identical results and no convergence guidance ever reaches the model.
    """

    _seed_probe_log(tmp_project_dir)
    responses = [
        faux_tool_call(
            "grep",
            {"pattern": "Unable to find a label", "path": PROBE_LOG_VIRTUAL, "output_mode": "content"},
            call_id=f"storm-{index}",
        )
        for index in range(20)
    ] + [faux_text("done")]
    model, built = _build(tmp_project_dir, responses)
    _invoke(built, tmp_project_dir, "REQ-PROBE-STALL:storm")

    results = _tool_results(model)
    assert len(results) == 20
    stalls = [result for result in results if "PROBE STALL" in result]
    assert len(stalls) == 1
    assert stalls[0] is results[-1]
    # Names the concrete shape: the probed file and the count.
    assert ".arc/tdd_runs/REQ-1/Integration-008.log" in stalls[0]
    assert "20 read-only lookups" in stalls[0]
    # Demands a repair action or an explicit surrender.
    assert "write_file/edit_file" in stalls[0]
    assert "abandon" in stalls[0].lower()
    # The probe's own evidence is preserved ahead of the nudge.
    assert stalls[0].startswith("/workspace")


def test_normal_read_write_rhythm_never_trips_the_nudge(tmp_project_dir: Path) -> None:
    """Greps interleaved with successful writes never accumulate in a window."""

    _seed_probe_log(tmp_project_dir)
    responses: list = []
    for cycle in range(4):
        responses.append(
            faux_tool_call(
                "grep",
                {"pattern": f"cycle-{cycle}", "path": PROBE_LOG_VIRTUAL, "output_mode": "content"},
                call_id=f"g-{cycle}",
            )
        )
        responses.append(
            faux_tool_call(
                "write_file",
                {
                    "file_path": f"/workspace/src/module-{cycle}.js",
                    "content": f"export const fix = {cycle};\n",
                },
                call_id=f"w-{cycle}",
            )
        )
    responses.append(faux_text("done"))
    model, built = _build(tmp_project_dir, responses)

    _invoke(built, tmp_project_dir, "REQ-PROBE-STALL:rhythm")

    assert all("PROBE STALL" not in result for result in _tool_results(model))


def test_identical_grep_replays_cached_result_through_the_real_chain(
    tmp_project_dir: Path,
) -> None:
    """The third identical grep is answered from cache with the directive.

    End-to-end through ``build_stage_agent``'s middleware chain: the first
    two greps execute the real search, the third returns the model's own
    earlier evidence plus the ARC REPEATED PROBE directive - the session
    stays alive, only the wasted re-execution disappears.
    """

    _seed_probe_log(tmp_project_dir)
    responses = [
        faux_tool_call(
            "grep",
            {"pattern": "line 3:", "path": PROBE_LOG_VIRTUAL, "output_mode": "content"},
            call_id=f"same-{index}",
        )
        for index in range(3)
    ] + [faux_text("done")]
    model, built = _build(tmp_project_dir, responses)

    _invoke(built, tmp_project_dir, "REQ-PROBE-STALL:mirror")

    results = _tool_results(model)
    assert len(results) == 3
    assert "ARC REPEATED PROBE" not in results[0]
    assert "ARC REPEATED PROBE" not in results[1]
    assert results[2].startswith("/workspace")
    assert "ARC REPEATED PROBE" in results[2]
    assert "never produces new information" in results[2]

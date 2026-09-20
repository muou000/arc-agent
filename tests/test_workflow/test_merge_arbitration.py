"""Merge-layer LLM arbitration (issue #81): gate, pruning, budget, audit.

These tests lock the arbitration contract without touching real git:

- the env gate defaults to off and a closed gate produces no hooks at all;
- the arbitration prompt carries only the conflict files' three-way contents
  and both sides' contract cards — no other repository content;
- a proposal touching anything outside the conflict file set is rejected;
- the budget is exactly one model call per node;
- every arbitration leaves a ``merge_arbitration`` audit record.

Real-git end-to-end arbitration (semantic conflict resolution, boot-failure
repair) lives in ``test_worktree_manager.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from core.merge_arbitration import (
    ARBITRATION_ENV,
    ArbitrationInput,
    MergeArbiter,
    TRIGGER_CONFLICT,
    TRIGGER_HEALTH_GATE,
    arbitration_enabled,
    collect_contract_cards,
    read_conflict_stages,
)
from tests.helpers.faux import FauxChatModel, faux_text


# ----------------------------------------------------------------------
# env gate
# ----------------------------------------------------------------------


def test_arbitration_env_gate_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ARBITRATION_ENV, raising=False)
    assert arbitration_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On "])
def test_arbitration_env_gate_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ARBITRATION_ENV, value)
    assert arbitration_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_arbitration_env_gate_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ARBITRATION_ENV, value)
    assert arbitration_enabled() is False


def test_workflow_builds_no_hooks_when_gate_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed gate must produce no arbitration hooks at all, so integrate()
    takes the pre-arbitration code path byte for byte."""

    from core.workflow import ARCWorkflowManager

    monkeypatch.delenv(ARBITRATION_ENV, raising=False)
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path / "ws"),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *args, **kwargs: None,
    )
    ctx = SimpleNamespace(handle=SimpleNamespace(path="x", node_id="REQ-1"))
    assert manager._build_merge_arbitration_hooks(ctx, "REQ-1", "DESIGN") is None


def test_workflow_budget_lives_in_the_node_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-arbitration budget is durable in the node session: a spent
    budget makes the collect hook decline (no model call), and the run hook
    marks the budget spent before the model call."""

    from core import sessions
    from core.config import set_workspace_root
    from core.workflow import ARCWorkflowManager

    workspace = tmp_path / "ws"
    workspace.mkdir()
    set_workspace_root(str(workspace))
    monkeypatch.setenv(ARBITRATION_ENV, "1")
    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(
        traceability=_NoCardsTraceability(),
        paths=SimpleNamespace(runner_events_path=workspace / ".arc" / "runner-events.jsonl"),
    )
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "ok;\n"}))])
    monkeypatch.setattr(manager, "_build_arbitration_model", lambda: model)
    ctx = SimpleNamespace(handle=SimpleNamespace(path="wt", node_id="REQ-3"))

    hooks = manager._build_merge_arbitration_hooks(ctx, "REQ-3", "IMPLEMENT")
    assert hooks is not None

    # Fresh budget: collect succeeds, run spends the budget, one model call.
    arbitration_input = hooks.collect_input(["backend/src.js"], TRIGGER_CONFLICT)
    assert arbitration_input is not None
    assert hooks.run(arbitration_input, ["backend/src.js"], TRIGGER_CONFLICT) is None
    assert model.call_count == 1
    assert sessions.load_node_session("REQ-3").get("merge_arbitration_used") is True

    # Second trigger: the collect hook declines and run refuses outright.
    assert hooks.collect_input(["backend/src.js"], TRIGGER_HEALTH_GATE, gate_failure="x") is None
    assert "budget is already spent" in hooks.run(
        arbitration_input, ["backend/src.js"], TRIGGER_HEALTH_GATE
    )
    assert model.call_count == 1, "no second model call"


def test_workflow_budget_marks_spent_even_when_the_model_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget is marked before the model call, so a crashed arbitration
    cannot buy a second attempt (run7-style loop prevention)."""

    from core.config import set_workspace_root
    from core.workflow import ARCWorkflowManager

    workspace = tmp_path / "ws"
    workspace.mkdir()
    set_workspace_root(str(workspace))
    monkeypatch.setenv(ARBITRATION_ENV, "1")
    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *args, **kwargs: None,
    )
    manager.runtime = SimpleNamespace(
        traceability=_NoCardsTraceability(),
        paths=SimpleNamespace(runner_events_path=workspace / ".arc" / "runner-events.jsonl"),
    )
    model = FauxChatModel(responses=[])  # exhausted script -> RuntimeError
    monkeypatch.setattr(manager, "_build_arbitration_model", lambda: model)
    ctx = SimpleNamespace(handle=SimpleNamespace(path="wt", node_id="REQ-3"))

    hooks = manager._build_merge_arbitration_hooks(ctx, "REQ-3", "IMPLEMENT")
    arbitration_input = hooks.collect_input(["backend/src.js"], TRIGGER_CONFLICT)
    assert arbitration_input is not None
    failure = hooks.run(arbitration_input, ["backend/src.js"], TRIGGER_CONFLICT)

    assert failure is not None and "crashed" in failure
    assert hooks.collect_input(["backend/src.js"], TRIGGER_CONFLICT) is None, (
        "the crashed attempt still spent the budget"
    )


def test_workflow_run_hook_emits_audit_runner_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every workflow-level arbitration lands in .arc/runner-events.jsonl as a
    merge_arbitration record (the audit trail)."""

    import json as jsonlib

    from core.config import set_workspace_root
    from core.workflow import ARCWorkflowManager

    workspace = tmp_path / "ws"
    workspace.mkdir()
    set_workspace_root(str(workspace))
    monkeypatch.setenv(ARBITRATION_ENV, "1")
    manager = ARCWorkflowManager(
        workspace_path=str(workspace),
        requirement_path="",
        web_port=4000,
        log_cb=lambda *args, **kwargs: None,
    )
    events_path = workspace / ".arc" / "runner-events.jsonl"
    manager.runtime = SimpleNamespace(
        traceability=_NoCardsTraceability(),
        paths=SimpleNamespace(runner_events_path=events_path),
    )
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "ok;\n"}))])
    monkeypatch.setattr(manager, "_build_arbitration_model", lambda: model)
    (workspace / "backend").mkdir()
    (workspace / "backend" / "src.js").write_text("conflict;\n", encoding="utf-8")
    ctx = SimpleNamespace(handle=SimpleNamespace(path="wt", node_id="REQ-3"))

    hooks = manager._build_merge_arbitration_hooks(ctx, "REQ-3", "IMPLEMENT")
    arbitration_input = hooks.collect_input(["backend/src.js"], TRIGGER_CONFLICT)
    assert hooks.run(arbitration_input, ["backend/src.js"], TRIGGER_CONFLICT) is None

    records = [
        jsonlib.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records, "the audit record must be persisted"
    record = records[-1]
    assert record["type"] == "merge_arbitration"
    assert record["node_id"] == "REQ-3"
    assert record["phase"] == "IMPLEMENT"
    assert record["trigger"] == TRIGGER_CONFLICT
    assert record["outcome"] == "applied"


class _NoCardsTraceability:
    """Traceability stub whose nodes carry no contract cards."""

    def list_interfaces(self, req_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_node_contract(self, req_id: str) -> dict[str, Any] | None:
        return None

    def list_node_contracts(self) -> list[dict[str, Any]]:
        return []


# ----------------------------------------------------------------------
# input pruning (the only cost gate)
# ----------------------------------------------------------------------


def _sample_input() -> ArbitrationInput:
    return ArbitrationInput(
        trigger=TRIGGER_CONFLICT,
        ours_label="REQ-2",
        theirs_label="REQ-3",
        files={
            "backend/src.js": {"base": "b\n", "ours": "b ours\n", "theirs": "b theirs\n"},
        },
        contract_cards={
            "REQ-2": {"interfaces": [{"interface_id": "IF-1", "type": "API", "content": "GET /a"}]},
            "REQ-3": {"interfaces": [{"interface_id": "IF-2", "type": "API", "content": "GET /b"}]},
        },
    )


def test_prompt_contains_conflict_files_and_contract_cards_only() -> None:
    prompt = _sample_input().build_prompt()

    assert "backend/src.js" in prompt
    assert "b ours" in prompt and "b theirs" in prompt and "b\n" in prompt
    assert "REQ-2" in prompt and "REQ-3" in prompt
    assert "GET /a" in prompt and "GET /b" in prompt


def test_prompt_never_mentions_files_outside_the_conflict_set() -> None:
    """The pruning nail: no repository content beyond the conflict files and
    the two contract cards may reach the model prompt."""

    arbitration_input = _sample_input()
    arbitration_input.files["backend/app.js"] = {
        "base": "app base\n",
        "ours": "app ours\n",
        "theirs": "app theirs\n",
    }
    prompt = arbitration_input.build_prompt()

    # Every section header in the prompt names a conflict file; nothing else.
    import re

    sections = re.findall(r"^### (.+)$", prompt, flags=re.MULTILINE)
    assert set(sections) == {"backend/src.js", "backend/app.js", "REQ-2", "REQ-3"}


def test_health_gate_trigger_prompt_carries_the_failure_and_resolved_content() -> None:
    arbitration_input = ArbitrationInput(
        trigger=TRIGGER_HEALTH_GATE,
        ours_label="REQ-2",
        theirs_label="REQ-3",
        files={
            "backend/src.js": {
                # After the mechanical resolution staged the file the index
                # holds no conflict stages; the arbiter sees the resolved
                # working-tree content it must repair.
                "base": None,
                "ours": None,
                "theirs": None,
                "resolved": "both routes mounted twice;\n",
            }
        },
        gate_failure="backend runtime failed to start",
    )
    prompt = arbitration_input.build_prompt()

    assert "health gate" in prompt
    assert "backend runtime failed to start" in prompt
    assert "both routes mounted twice;" in prompt
    assert "CURRENT RESOLVED CONTENT" in prompt
    # Empty stages must not render as the literal string "None".
    assert "None" not in prompt


def test_collect_contract_cards_prunes_to_declared_interfaces() -> None:
    traceability = SimpleNamespace(
        list_interfaces=lambda req_id: (
            [
                {
                    "interface_id": f"IF-{req_id}",
                    "type": "API",
                    "content": "GET /x",
                    "file_path": "backend/src.js",
                    "req_ids": [req_id],
                }
            ]
            if req_id == "REQ-1"
            else []
        ),
        get_node_contract=lambda req_id: (
            {"req_id": req_id, "content": {"summary": "contract card"}} if req_id == "REQ-2" else None
        ),
    )

    cards = collect_contract_cards(traceability, ["REQ-1", "REQ-2", "REQ-3"])

    assert set(cards) == {"REQ-1", "REQ-2"}, "nodes with no cards are absent, not empty"
    assert cards["REQ-1"]["interfaces"][0]["interface_id"] == "IF-REQ-1"
    assert cards["REQ-2"]["node_contract"] == {"summary": "contract card"}


def test_read_conflict_stages_reports_missing_base_as_none() -> None:
    """add/add conflicts have no stage-1 base; the stage read must surface
    None instead of failing the whole arbitration input."""

    def git_runner(args: list[str]) -> Any:
        assert args[0] == "show"
        if args[1].startswith(":1:"):
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout="content\n")

    stages = read_conflict_stages(git_runner, ["backend/new.js"])

    assert stages["backend/new.js"] == {"base": None, "ours": "content\n", "theirs": "content\n"}


# ----------------------------------------------------------------------
# narrow edit rights
# ----------------------------------------------------------------------


def _arbiter_with_model(model: Any, tmp_path: Path) -> tuple[MergeArbiter, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    return (
        MergeArbiter(
            model=model,
            workspace_path=str(tmp_path),
            emit_event=events.append,
        ),
        events,
    )


def test_arbitration_rejects_edits_outside_the_conflict_set(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "src.js").write_text("conflicted;\n", encoding="utf-8")
    (tmp_path / "backend" / "owned-by-sibling.js").write_text("sibling;\n", encoding="utf-8")
    model = FauxChatModel(
        responses=[
            faux_text(
                json.dumps(
                    {
                        "backend/src.js": "resolved;\n",
                        "backend/owned-by-sibling.js": "hijacked;\n",
                    }
                )
            )
        ]
    )
    arbiter, events = _arbiter_with_model(model, tmp_path)
    arbitration_input = ArbitrationInput(
        trigger=TRIGGER_CONFLICT,
        ours_label="REQ-2",
        theirs_label="REQ-3",
        files={"backend/src.js": {"base": "b\n", "ours": "o\n", "theirs": "t\n"}},
    )

    result = asyncio_run(arbiter.arbitrate("wt", "main", arbitration_input))

    assert result.accepted is False
    assert "outside the conflict file set" in result.detail
    assert "owned-by-sibling" in result.detail
    # Not one byte outside the set was touched: the in-set rewrite is rolled
    # back too (nothing was applied at all).
    assert (tmp_path / "backend" / "src.js").read_text(encoding="utf-8") == "conflicted;\n"
    assert (tmp_path / "backend" / "owned-by-sibling.js").read_text(encoding="utf-8") == "sibling;\n"
    assert events[-1]["outcome"] == "rejected"


def test_arbitration_rejects_partial_resolution(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/a.js": "a;\n"}))])
    arbiter, _events = _arbiter_with_model(model, tmp_path)
    arbitration_input = ArbitrationInput(
        trigger=TRIGGER_CONFLICT,
        ours_label="REQ-2",
        theirs_label="REQ-3",
        files={
            "backend/a.js": {"base": "b\n", "ours": "o\n", "theirs": "t\n"},
            "backend/b.js": {"base": "b\n", "ours": "o\n", "theirs": "t\n"},
        },
    )

    result = asyncio_run(arbiter.arbitrate("wt", "main", arbitration_input))

    assert result.accepted is False
    assert "missing backend/b.js" in result.detail


def test_arbitration_applies_in_set_rewrites_and_emits_audit(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    model = FauxChatModel(
        responses=[faux_text(json.dumps({"backend/src.js": "merged both sides;\n"}))]
    )
    arbiter, events = _arbiter_with_model(model, tmp_path)
    arbitration_input = _sample_input()

    result = asyncio_run(arbiter.arbitrate("wt-path", "main", arbitration_input, node_id="REQ-3"))

    assert result.accepted is True
    assert (tmp_path / "backend" / "src.js").read_text(encoding="utf-8") == "merged both sides;\n"
    assert result.applied == {"backend/src.js": "merged both sides;\n"}
    record = events[-1]
    assert record["type"] == "merge_arbitration"
    assert record["outcome"] == "applied"
    assert record["node_id"] == "REQ-3"
    assert record["trigger"] == TRIGGER_CONFLICT
    assert record["conflict_files"] == ["backend/src.js"]


def test_arbitration_model_failure_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    model = FauxChatModel(responses=[])  # exhausted script -> RuntimeError
    arbiter, events = _arbiter_with_model(model, tmp_path)

    result = asyncio_run(arbiter.arbitrate("wt", "main", _sample_input()))

    assert result.accepted is False
    assert "crashed" in result.detail
    assert events[-1]["outcome"] == "crashed"


def test_arbitration_parses_fenced_json_responses(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    model = FauxChatModel(
        responses=[
            faux_text(
                "Here is the resolution:\n```json\n"
                + json.dumps({"backend/src.js": "fenced;\n"})
                + "\n```"
            )
        ]
    )
    arbiter, _events = _arbiter_with_model(model, tmp_path)

    result = asyncio_run(arbiter.arbitrate("wt", "main", _sample_input()))

    assert result.accepted is True
    assert (tmp_path / "backend" / "src.js").read_text(encoding="utf-8") == "fenced;\n"


def test_arbitration_prompt_is_delivered_as_single_user_message(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    model = FauxChatModel(responses=[faux_text(json.dumps({"backend/src.js": "ok;\n"}))])
    arbiter, _events = _arbiter_with_model(model, tmp_path)

    asyncio_run(arbiter.arbitrate("wt", "main", _sample_input()))

    assert model.call_count == 1
    messages = model.calls[0]
    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    assert "backend/src.js" in str(messages[0].content)


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)

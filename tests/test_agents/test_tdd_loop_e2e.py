"""Faux-model e2e tests for the real TDD loop.

Drives ``WorkflowPhaseRunner._run_tdd_for_node`` (``core/phases.py``) and the
``TestDrivenDeveloper`` adapter (``agents/test_driven_developer.py``) with a
scripted ``FauxChatModel`` plus a ``FakeAppHandler`` that returns canned
``run_test_group`` outputs. The deep-agents loop, the ``run_tests`` tool
executor, the per-layer budgets, the baseline RED verification, the per-file
micro-loop tracking, the stall governance, the traceability bookkeeping and
the node session updates all run for real — no tokens, no npm.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from agents.test_driven_developer import TestDrivenDeveloper
from core import sessions
from core.phases import TDD_RUN_TESTS_BUDGET, TDD_STALL_THRESHOLD, WorkflowPhaseRunner
from agents.tools.build import build_install_dependencies_tool
from app_type_handler.test_results import parse_test_run
from tests.helpers.faux import (
    FakeAppHandler,
    FauxChatModel,
    failing_test_output,
    faux_text,
    faux_tool_call,
    passing_test_output,
)

UNIT_TEST_FILE = "tests/unit/test_calc.py"
INTEGRATION_TEST_FILE = "tests/integration/test_flow.py"
E2E_TEST_FILE = "test-e2e/register.e2e.spec.js"


def seed_node(runtime, node_id: str, tests: list[dict]) -> None:
    """Register a leaf requirement and its tests in the traceability store."""

    runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )
    for test in tests:
        runtime.traceability.upsert_test(
            test_id=test["test_id"],
            req_id=node_id,
            interface_ids=[],
            type=test["type"],
            file_path=test["file_path"],
            first_line="",
            passed=None,
        )


def make_tdd(tmp_project_dir: Path, model: FauxChatModel, fake: FakeAppHandler) -> TestDrivenDeveloper:
    return TestDrivenDeveloper(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
        app_handler=fake,
    )


def make_runner(tmp_project_dir: Path, tdd: TestDrivenDeveloper, fake: FakeAppHandler) -> WorkflowPhaseRunner:
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
        interface_designer=None,
        test_generator=None,
        test_driven_developer=tdd,
    )
    # Swap the real web app handler (npm) for the scripted one.
    runner.app_handler = fake
    runner.test_driven_developer.app_handler = fake
    return runner


def write_test_file(tmp_project_dir: Path) -> None:
    path = tmp_project_dir / UNIT_TEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_add():\n    assert add(1, 1) == 2\n", encoding="utf-8")


def track_tdd_sessions(tdd: TestDrivenDeveloper) -> list[str]:
    """Record the ``test_type`` of every agent session the scheduler opens."""

    session_types: list[str] = []
    original_run = tdd.run

    async def recording_run(**kwargs: Any) -> str:
        session_types.append(str(kwargs["test_type"]))
        return await original_run(**kwargs)

    tdd.run = recording_run
    return session_types


def track_tdd_handoffs(tdd: TestDrivenDeveloper) -> list[str]:
    """Record the ``previous_failure_summary`` of every agent session."""

    handoffs: list[str] = []
    original_run = tdd.run

    async def recording_run(**kwargs: Any) -> str:
        handoffs.append(str(kwargs.get("previous_failure_summary") or ""))
        return await original_run(**kwargs)

    tdd.run = recording_run
    return handoffs


def tool_results_text(model: FauxChatModel) -> str:
    return "\n".join(
        str(m.content) for call in model.calls for m in call if getattr(m, "type", "") == "tool"
    )


# ---------------------------------------------------------------------------
# Happy path: write -> run_tests (fail) -> fix -> run_tests (pass) -> IMPLEMENTED
# ---------------------------------------------------------------------------


def test_tdd_loop_fail_then_fix_then_pass(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-TDD-1"
    seed_node(arc_runtime, node_id, [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}])
    write_test_file(tmp_project_dir)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a - b\n"},
                call_id="c1",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="c2"),
            # The failed run unlocked the written path, so the fix may rewrite it.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a + b\n"},
                call_id="c3",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="c4"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Baseline RED (failing), agent failing run, agent passing run.
    fake = FakeAppHandler([failing_test_output(), failing_test_output(), passing_test_output()])
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(
        runner._run_tdd_for_node(
            node_id=node_id,
            tests=[{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}],
        )
    )

    assert final_ok is True
    assert model.call_count == 5
    assert model.get_pending_response_count() == 0
    # Baseline RED (single file), then the two agent runs.
    assert fake.calls == [
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [UNIT_TEST_FILE]),
    ]

    # The agent's fix really landed in the workspace.
    assert "return a + b" in (tmp_project_dir / "src" / "calc.py").read_text(encoding="utf-8")
    assert "Exit Code: 0" in (tdd.get_last_run_tests_result() or "")

    # Baseline RED evidence reaches the first agent session.
    first_call_messages = "\n".join(str(m.content) for m in model.calls[0])
    assert "Baseline RED Evidence" in first_call_messages
    assert UNIT_TEST_FILE in first_call_messages

    # Per-file status is reported on every run_tests result.
    assert "ARC_TEST_FILE_STATUS" in tool_results_text(model)

    # Traceability + node session bookkeeping.
    assert arc_runtime.traceability.get_test("T1")["passed"] is True
    node_session = sessions.load_node_session(node_id)
    assert node_session["recent_failure_summary"] == ""
    assert node_session["tdd_handoff"]["last_test_type"] == "Unit"


# ---------------------------------------------------------------------------
# Layer ordering: Unit passes -> Integration layer (cross-layer run rejected)
# ---------------------------------------------------------------------------


def test_tdd_layer_order_and_cross_layer_rejection(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-TDD-2"
    seed_node(
        arc_runtime,
        node_id,
        [
            {"test_id": "T-U", "type": "Unit", "file_path": UNIT_TEST_FILE},
            {"test_id": "T-I", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
        ],
    )

    model = FauxChatModel(
        responses=[
            # Unit layer session.
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="u1"),
            faux_text("unit layer passed"),
            # Integration layer session: first an out-of-layer call (rejected),
            # then the integration run, then the final answer.
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="i0"),
            faux_tool_call("run_tests", {"test_type": "Integration"}, call_id="i1"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Unit baseline (failing), Unit agent run (passing), Integration baseline
    # (failing), Integration agent run (passing).
    fake = FakeAppHandler(
        [failing_test_output(), passing_test_output(), failing_test_output(), passing_test_output()]
    )
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(
        runner._run_tdd_for_node(
            node_id=node_id,
            tests=[
                {"test_id": "T-U", "type": "Unit", "file_path": UNIT_TEST_FILE},
                {"test_id": "T-I", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
            ],
        )
    )

    assert final_ok is True
    assert fake.calls == [
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [UNIT_TEST_FILE]),
        ("Integration", [INTEGRATION_TEST_FILE]),
        ("Integration", [INTEGRATION_TEST_FILE]),
    ]
    # The out-of-layer run_tests call was rejected with an explicit gate message.
    all_tool_results = tool_results_text(model)
    assert "The active TDD layer is `Integration`" in all_tool_results
    assert arc_runtime.traceability.get_test("T-U")["passed"] is True
    assert arc_runtime.traceability.get_test("T-I")["passed"] is True


# ---------------------------------------------------------------------------
# Budget: run_tests exhausts its layer budget and the node fails
# ---------------------------------------------------------------------------


def test_tdd_budget_exhaustion_fails_node(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-TDD-3"
    seed_node(arc_runtime, node_id, [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}])

    script = [faux_tool_call("run_tests", {}, call_id=f"c{i}") for i in range(TDD_RUN_TESTS_BUDGET)]
    script.append(faux_text("STILL FAILING"))
    model = FauxChatModel(responses=script)
    # Baseline + 10 budgeted runs all fail with distinct fingerprints
    # (assertion details vary) so stall governance does not close the loop early.
    outputs = [failing_test_output(detail=f"failure {i}") for i in range(TDD_RUN_TESTS_BUDGET + 1)]
    fake = FakeAppHandler(outputs)
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(
        runner._run_tdd_for_node(
            node_id=node_id,
            tests=[{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}],
        )
    )

    assert final_ok is False
    assert len(fake.calls) == TDD_RUN_TESTS_BUDGET + 1
    assert model.call_count == TDD_RUN_TESTS_BUDGET + 1
    assert arc_runtime.traceability.get_test("T1")["passed"] is False
    node_session = sessions.load_node_session(node_id)
    assert "Unit:" in node_session["recent_failure_summary"]
    assert node_session["tdd_handoff"]["last_test_type"] == "Unit"


# ---------------------------------------------------------------------------
# A session that never calls run_tests fails the layer
# ---------------------------------------------------------------------------


def test_tdd_session_without_run_tests_fails_node(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-TDD-4"
    seed_node(arc_runtime, node_id, [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}])
    model = FauxChatModel(responses=[faux_text("GIVING UP")])
    fake = FakeAppHandler([failing_test_output()])
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(
        runner._run_tdd_for_node(
            node_id=node_id,
            tests=[{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}],
        )
    )

    assert final_ok is False
    # Only the baseline RED run happened; the agent session ran no tests.
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])]
    node_session = sessions.load_node_session(node_id)
    assert "GIVING UP" in node_session["recent_failure_summary"]


# ---------------------------------------------------------------------------
# run_implement_phase top-level: implements + marks interfaces and node state
# ---------------------------------------------------------------------------


def test_run_implement_phase_marks_node_completed(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-TDD-5"
    seed_node(arc_runtime, node_id, [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}])
    arc_runtime.traceability.upsert_interface(
        interface_id="IF-CALC",
        req_ids=[node_id],
        type="FUNC",
        content='{"interface_id": "IF-CALC", "name": "add"}',
        file_path="src/calc.py",
        first_line="def add(a, b):",
        implemented=False,
        callers=[],
        callees=[],
    )
    write_test_file(tmp_project_dir)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a + b\n"},
                call_id="c1",
            ),
            faux_tool_call("run_tests", {}, call_id="c2"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Baseline RED (failing) then the agent's passing run.
    fake = FakeAppHandler([failing_test_output(), passing_test_output()])
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    ok = asyncio.run(runner.run_implement_phase(node_id, {"children_ids": []}))

    assert ok is True
    assert arc_runtime.traceability.get_interface("IF-CALC")["implemented"] is True
    node_session = sessions.load_node_session(node_id)
    assert node_session["phase_status"]["implement"] == "completed"
    # The IMPLEMENT phase must release the session-scoped E2E runtime
    # (web handler keeps one backend server across run_tests calls).
    assert fake.shutdown_calls == 1


# ---------------------------------------------------------------------------
# Adapter-level guard: IMPLEMENTED without a passing run_tests is rejected
# ---------------------------------------------------------------------------


def test_tdd_run_rejects_implemented_without_passing_run_tests(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-TDD-6"
    seed_node(arc_runtime, node_id, [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}])
    model = FauxChatModel(responses=[faux_text("IMPLEMENTED")])
    fake = FakeAppHandler()
    tdd = make_tdd(tmp_project_dir, model, fake)

    executor_calls: list[tuple] = []

    async def executor(test_type, test_files):  # pragma: no cover - must not run
        executor_calls.append((test_type, test_files))
        return parse_test_run(failing_test_output())

    final_text = asyncio.run(
        tdd.run(
            node_id=node_id,
            test_files=[UNIT_TEST_FILE],
            test_type="Unit",
            node_tests=[{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}],
            previous_failure_summary="",
            run_tests_executor=executor,
        )
    )

    assert executor_calls == []
    assert final_text.startswith("Error: latest run_tests result did not pass")


# ---------------------------------------------------------------------------
# Environment failures short-circuit the loop; assertion failures do not
# ---------------------------------------------------------------------------

MISSING_DEP_OUTPUT = "Error: Cannot find module '@testing-library/dom'"


def test_environment_failure_stops_the_tdd_loop_immediately(tmp_project_dir: Path, arc_runtime) -> None:
    """A broken workspace must not burn the budget on every layer.

    Missing-package failures are installable via the ``install_dependencies``
    tool, so they get one extra cycle before the layer closes (see the
    install-cycle tests below). This test scripts an agent that never repairs
    anything: every grant is wasted, and the layer still closes for good long
    before the budget is spent.
    """

    node_id = "REQ-TDD-ENV"
    tests = [
        {"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T2", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
    ]
    seed_node(arc_runtime, node_id, tests)

    # The script *allows* the full budget; the short-circuit must stop sooner.
    script = [faux_tool_call("run_tests", {}, call_id=f"c{i}") for i in range(TDD_RUN_TESTS_BUDGET)]
    script.append(faux_text("BLOCKED"))
    model = FauxChatModel(responses=script)
    # Baseline env failure, agent env failure, unrepaired re-validation, and
    # the still-environmental run after the wasted install cycle.
    fake = FakeAppHandler([failing_test_output(detail=MISSING_DEP_OUTPUT) for _ in range(4)])
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is False
    # Baseline + one failing attempt + one unrepaired re-validation + the
    # post-install-cycle failure, then the layer closes - Integration is never
    # reached and no further doomed command runs.
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])] * 4
    # NOTE: the agent *session* still runs to the end of its script. Ending the
    # LangGraph loop early needs a runtime hook that does not exist yet, so the
    # model keeps polling `run_tests` and getting "budget exhausted". Those
    # turns are cheap (~seconds) next to the test executions they no longer
    # trigger (~a minute each), which is what this guard is about.
    assert model.call_count <= TDD_RUN_TESTS_BUDGET + 1
    # A broken workspace must not advance the layer either: every later layer
    # would fail the same way, and the environment-failure gate stops the loop.
    all_tool_results = tool_results_text(model)
    assert "has advanced the active layer" not in all_tool_results
    # The baseline RED evidence tells the first session about the environment
    # failure and its repair contract.
    first_call_messages = "\n".join(str(m.content) for m in model.calls[0])
    assert "environmental reason" in first_call_messages
    assert "repair" in first_call_messages
    # The first failing run offers the repair-and-revalidate contract; the
    # second (still environmental) run closes the layer for good. The two
    # status headers must be unambiguous about which state the layer is in.
    assert "This is your one repair-and-revalidate attempt" in all_tool_results
    assert "This layer is closed" in all_tool_results
    node_session = sessions.load_node_session(node_id)
    assert "environment failure" in node_session["recent_failure_summary"]
    assert "missing dependency" in node_session["recent_failure_summary"]


def test_environment_failure_repair_revalidates_and_passes(tmp_project_dir: Path, arc_runtime) -> None:
    """An environment failure the agent can repair must be re-validated.

    Observed on the 2026-09-14 ticket-booking run: the agent diagnosed and
    repaired the reported environment failure, but the old executor had
    already burned the layer budget, so the repair was never validated and
    the node failed. The re-validation attempt must run the repaired
    workspace and let the node succeed.
    """

    node_id = "REQ-TDD-ENV-REPAIR"
    tests = [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)

    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="e1"),
            # The failing run unlocks writes; the agent repairs the reported
            # missing local module.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/testing-library-dom.js", "content": "module.exports = {};\n"},
                call_id="w1",
            ),
            # Re-validation of the repaired workspace.
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="e2"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Baseline env failure, agent env failure, repaired re-validation pass.
    fake = FakeAppHandler([failing_test_output(detail=MISSING_DEP_OUTPUT), failing_test_output(detail=MISSING_DEP_OUTPUT), passing_test_output()])
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])] * 3
    assert model.call_count == 4
    all_tool_results = tool_results_text(model)
    assert "This is your one repair-and-revalidate attempt" in all_tool_results
    # The repair really landed in the workspace and the node recovered.
    assert (tmp_project_dir / "src" / "testing-library-dom.js").exists()
    assert arc_runtime.traceability.get_test("T1")["passed"] is True
    node_session = sessions.load_node_session(node_id)
    assert node_session["recent_failure_summary"] == ""


def test_assertion_failure_still_consumes_the_full_budget(tmp_project_dir: Path, arc_runtime) -> None:
    """Control: the short-circuit must not fire on ordinary test failures.

    An assertion failure is something the agent can fix by editing the
    implementation, so it keeps its full retry budget. Distinct failure
    details keep the fingerprints distinct so stall governance stays out of
    the way; its dedicated tests cover the repeated-fingerprint contract.
    """

    node_id = "REQ-TDD-ASSERT"
    tests = [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)

    script = [faux_tool_call("run_tests", {}, call_id=f"c{i}") for i in range(TDD_RUN_TESTS_BUDGET)]
    script.append(faux_text("STILL FAILING"))
    model = FauxChatModel(responses=script)
    outputs = [
        failing_test_output(detail=f"AssertionError: expected '{i}' to equal '{i + 1}'")
        for i in range(TDD_RUN_TESTS_BUDGET + 1)
    ]
    fake = FakeAppHandler(outputs)
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is False
    assert len(fake.calls) == TDD_RUN_TESTS_BUDGET + 1
    node_session = sessions.load_node_session(node_id)
    assert "environment failure" not in node_session["recent_failure_summary"]


# ---------------------------------------------------------------------------
# Hard layer state machine: a passing layer advances immediately and can no
# longer be re-run; the next layer runs in the same session.
# ---------------------------------------------------------------------------


def test_tdd_pass_advances_active_layer_immediately(tmp_project_dir: Path, arc_runtime) -> None:
    """A passed layer must be closed at the moment the pass is reported.

    Before the in-session advance, a model that kept polling `run_tests`
    re-ran the passing batch until its budget was gone and then deadlocked on
    rejected next-layer requests (observed on the 12306 benchmark: attempts
    6-10 of an already-passing Unit batch, then minutes of stuck turns).
    """

    node_id = "REQ-TDD-ADV"
    tests = [
        {"test_id": "T-U", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T-I", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
    ]
    seed_node(arc_runtime, node_id, tests)

    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="u1"),
            # Re-running the closed layer must be rejected without a test run.
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="u2"),
            # The next layer is immediately runnable in the same session.
            faux_tool_call("run_tests", {"test_type": "Integration"}, call_id="i1"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Unit baseline (failing), Unit agent run (passing), in-session
    # Integration run (passing; closes the layer so no outer baseline).
    fake = FakeAppHandler(
        [failing_test_output(), passing_test_output(), passing_test_output()]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    session_types = track_tdd_sessions(tdd)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    # Unit baseline + Unit agent run; the in-session Integration run passed
    # the full layer, so the outer scheduler must not baseline or re-run it.
    assert fake.calls == [
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [UNIT_TEST_FILE]),
        ("Integration", [INTEGRATION_TEST_FILE]),
    ]
    # Integration passed inside the Unit session, so the outer scheduler must
    # not open a follow-up Integration session for it.
    assert session_types == ["Unit"]
    assert model.call_count == 4
    all_tool_results = tool_results_text(model)
    assert "has advanced the active layer to `Integration`" in all_tool_results
    assert "The active TDD layer is `Integration`" in all_tool_results
    assert arc_runtime.traceability.get_test("T-U")["passed"] is True
    assert arc_runtime.traceability.get_test("T-I")["passed"] is True


def test_tdd_budget_exhaustion_advances_to_next_layer(tmp_project_dir: Path, arc_runtime) -> None:
    """An exhausted layer must hand the active layer to its successor.

    The prompt promises "the system moves to later layers even if an earlier
    layer fails or exhausts its budget"; the executor used to keep the active
    layer pinned to the exhausted one, so in-session requests for the next
    layer were rejected and the session deadlocked. The in-session advance
    must also leave the outer scheduler's per-layer contract intact: the
    closed Unit layer never gets a second session, while Integration still
    gets its own session to spend the rest of its budget.
    """

    node_id = "REQ-TDD-EXH"
    tests = [
        {"test_id": "T-U", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T-I", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
    ]
    seed_node(arc_runtime, node_id, tests)

    script = [faux_tool_call("run_tests", {}, call_id=f"u{i}") for i in range(TDD_RUN_TESTS_BUDGET)]
    script += [
        # Hits the budget gate: returns the closed message and advances the
        # active layer without running any test.
        faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="closed"),
        # The successor layer is immediately runnable in the same session.
        faux_tool_call("run_tests", {"test_type": "Integration"}, call_id="i1"),
        faux_text("unit layer closed; integration still failing"),
        # The outer loop opens the Integration session; second attempt passes.
        faux_tool_call("run_tests", {"test_type": "Integration"}, call_id="i2"),
        faux_text("IMPLEMENTED"),
    ]
    model = FauxChatModel(responses=script)
    # Unit baseline + 10 unit agent runs (all failing) + in-session
    # Integration run (failing) + follow-up session Integration run (passing).
    outputs = [failing_test_output(detail=f"unit baseline {i}") for i in range(1)]
    outputs += [failing_test_output(detail=f"unit failure {i}") for i in range(TDD_RUN_TESTS_BUDGET)]
    outputs += [
        failing_test_output(detail="AssertionError: integration mismatch"),
        passing_test_output(),
    ]
    fake = FakeAppHandler(outputs)
    tdd = make_tdd(tmp_project_dir, model, fake)
    session_types = track_tdd_sessions(tdd)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    # Unit is failed and stays failed; Integration passed on its own budget.
    assert final_ok is False
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])] * (TDD_RUN_TESTS_BUDGET + 1) + [
        ("Integration", [INTEGRATION_TEST_FILE]),
        ("Integration", [INTEGRATION_TEST_FILE]),
    ]
    # One Unit session only: after the in-session advance the outer scheduler
    # must not reopen the closed Unit layer, and Integration keeps its own
    # session with its remaining budget (per-layer budget semantics intact).
    assert session_types == ["Unit", "Integration"]
    all_tool_results = tool_results_text(model)
    assert "The Unit layer is closed" in all_tool_results
    assert "has advanced the active layer to `Integration`" in all_tool_results
    assert arc_runtime.traceability.get_test("T-U")["passed"] is False
    assert arc_runtime.traceability.get_test("T-I")["passed"] is True
    node_session = sessions.load_node_session(node_id)
    assert "Unit:" in node_session["recent_failure_summary"]


# ---------------------------------------------------------------------------
# Baseline RED verification: tautology fast path skips the agent session
# ---------------------------------------------------------------------------


def test_baseline_all_green_closes_layer_without_agent_session(tmp_project_dir: Path, arc_runtime) -> None:
    """A layer whose every file already passes must not open an agent session.

    The baseline RED check runs each file once; when everything is green the
    system runs one full-layer regression itself and closes the layer (the
    tautology fast path). An agent session here would only re-run passing
    tests and risk "fixing" them.
    """

    node_id = "REQ-TDD-FAST"
    tests = [
        {"test_id": "T-U", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T-I", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
    ]
    seed_node(arc_runtime, node_id, tests)

    model = FauxChatModel(responses=[])  # no session may be opened
    # Two per-file baselines + one full-layer regression, per layer.
    fake = FakeAppHandler([passing_test_output()] * 6)
    tdd = make_tdd(tmp_project_dir, model, fake)
    session_types = track_tdd_sessions(tdd)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert session_types == []
    assert fake.calls == [
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [UNIT_TEST_FILE]),  # full-layer regression
        ("Integration", [INTEGRATION_TEST_FILE]),
        ("Integration", [INTEGRATION_TEST_FILE]),  # full-layer regression
    ]
    assert arc_runtime.traceability.get_test("T-U")["passed"] is True
    assert arc_runtime.traceability.get_test("T-I")["passed"] is True


# ---------------------------------------------------------------------------
# Micro-loop: a passing subset run marks files green but does not close the layer
# ---------------------------------------------------------------------------


def test_subset_pass_does_not_close_layer_until_full_run(tmp_project_dir: Path, arc_runtime) -> None:
    """The layer closes only on a passing run that covers every file.

    The micro-loop contract: the agent may repair file-by-file with
    ``run_tests(test_files=[...])``; each passing subset run turns those files
    green, but the layer stays open until one passing full-layer run.
    """

    node_id = "REQ-TDD-MICRO"
    other_unit_file = "tests/unit/test_extra.py"
    tests = [
        {"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T2", "type": "Unit", "file_path": other_unit_file},
    ]
    seed_node(arc_runtime, node_id, tests)

    model = FauxChatModel(
        responses=[
            # Initial implementation pass.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a + b\n"},
                call_id="w1",
            ),
            # Repair file 1 alone; it passes (subset).
            faux_tool_call("run_tests", {"test_files": [UNIT_TEST_FILE]}, call_id="r1"),
            # Repair file 2 alone; it passes (subset).
            faux_tool_call("run_tests", {"test_files": [other_unit_file]}, call_id="r2"),
            # Full-layer run closes the layer.
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="r3"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Baselines: file1 red, file2 red; subset runs: file1 green, file2 green;
    # full run: green.
    fake = FakeAppHandler(
        [
            failing_test_output(detail="AssertionError: file1 missing"),
            failing_test_output(detail="AssertionError: file2 missing"),
            passing_test_output(),
            passing_test_output(),
            passing_test_output(),
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert fake.calls == [
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [other_unit_file]),
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [other_unit_file]),
        ("Unit", [UNIT_TEST_FILE, other_unit_file]),
    ]
    all_tool_results = tool_results_text(model)
    # The subset pass reports the still-red file and the open layer.
    assert "still red" in all_tool_results
    assert "ARC_TEST_FILE_STATUS" in all_tool_results
    # The full-layer run reports the layer as passed.
    assert "passed (full layer)" in all_tool_results
    assert arc_runtime.traceability.get_test("T1")["passed"] is True
    assert arc_runtime.traceability.get_test("T2")["passed"] is True


def test_subset_pass_reports_not_yet_run_files_separately(tmp_project_dir: Path, arc_runtime) -> None:
    """Never-run files must not be reported as "still red" (PR review).

    A passing subset run on file 1 leaves file 2 unverified (None state).
    Reporting file 2 as red would send the agent repairing a file with no
    failure evidence; it is pending work, not a repair target.
    """

    node_id = "REQ-TDD-MICRO-PENDING"
    other_unit_file = "tests/unit/test_extra.py"
    tests = [
        {"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T2", "type": "Unit", "file_path": other_unit_file},
    ]
    seed_node(arc_runtime, node_id, tests)

    # Session: baseline both red -> repair file 1 (subset pass) -> full run.
    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {"test_files": [UNIT_TEST_FILE]}, call_id="r1"),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="r2"),
            faux_text("IMPLEMENTED"),
        ]
    )
    fake = FakeAppHandler(
        [
            # Baselines: file1 red, file2 red.
            failing_test_output(detail="AssertionError: file1 missing"),
            failing_test_output(detail="AssertionError: file2 missing"),
            # Subset run on file1: pass (file2 stays red from its baseline).
            passing_test_output(),
            # Full-layer run: pass.
            passing_test_output(),
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    all_tool_results = tool_results_text(model)
    # File 2 was baseline-verified red, so the subset pass reports it as a
    # repair target - but nothing may be labeled "not been run yet" here.
    assert "still red" in all_tool_results
    assert "not been run yet" not in all_tool_results


def test_subset_pass_after_insession_advance_reports_pending_files(tmp_project_dir: Path, arc_runtime) -> None:
    """Files of an in-session advanced layer start as None, not red.

    When Unit passes and the layer advances mid-session, the Integration
    files have never been baseline-verified. A passing subset run on one of
    them must report the others as "not been run yet" - never as "still red",
    which would imply verified failures (PR review finding).
    """

    node_id = "REQ-TDD-MICRO-ADVANCE"
    integration_extra_file = "tests/integration/test_extra_flow.py"
    tests = [
        {"test_id": "T-U", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T-I1", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
        {"test_id": "T-I2", "type": "Integration", "file_path": integration_extra_file},
    ]
    seed_node(arc_runtime, node_id, tests)

    model = FauxChatModel(
        responses=[
            # Unit passes in-session -> advance to Integration (files: None).
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="u1"),
            # Subset run on the first Integration file: passes; the second
            # stays None (never verified in this flow).
            faux_tool_call("run_tests", {"test_type": "Integration", "test_files": [INTEGRATION_TEST_FILE]}, call_id="i1"),
            # Full-layer Integration run closes the layer.
            faux_tool_call("run_tests", {"test_type": "Integration"}, call_id="i2"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Unit baseline (red), Unit agent run (pass), Integration subset (pass),
    # Integration full run (pass).
    fake = FakeAppHandler(
        [
            failing_test_output(),
            passing_test_output(),
            passing_test_output(),
            passing_test_output(),
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    all_tool_results = tool_results_text(model)
    # The subset pass on file 1 of the advanced layer reports file 2 as
    # pending work, not as a verified failure.
    assert "not been run yet: tests/integration/test_extra_flow.py" in all_tool_results
    # And it must NOT be reported as "still red" anywhere in that result.
    assert "still red: tests/integration/test_extra_flow.py" not in all_tool_results
    assert arc_runtime.traceability.get_test("T-I1")["passed"] is True
    assert arc_runtime.traceability.get_test("T-I2")["passed"] is True


# ---------------------------------------------------------------------------
# Stall governance: repeated identical fingerprints force hypothesis rotation
# ---------------------------------------------------------------------------


def test_stall_detection_forces_hypothesis_rotation(tmp_project_dir: Path, arc_runtime) -> None:
    """Three identical consecutive fingerprints must trigger STALL DETECTED.

    The run_tests result tells the agent to stop patching neighbors and
    rotate its hypothesis; a follow-up session carries the same governance
    context so the rotation survives session boundaries.
    """

    node_id = "REQ-TDD-STALL"
    tests = [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)

    # Session 1: three failed runs with the SAME fingerprint (budget 3/10),
    # so the session ends without the layer passing. Session 2 (opened with
    # the stall handoff) rotates and passes.
    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {}, call_id="s1"),
            faux_tool_call("run_tests", {}, call_id="s2"),
            faux_tool_call("run_tests", {}, call_id="s3"),
            faux_text("session one ends, still failing"),
            faux_tool_call("run_tests", {}, call_id="s4"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # Baseline + three same-fingerprint failures + one pass.
    same_failure = failing_test_output(detail="AssertionError: expected 'Login' to equal 'Log in'")
    fake = FakeAppHandler([same_failure, same_failure, same_failure, same_failure, passing_test_output()])
    tdd = make_tdd(tmp_project_dir, model, fake)
    session_types = track_tdd_sessions(tdd)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    # Two sessions: the first ends failing, the second carries the stall handoff.
    assert session_types == ["Unit", "Unit"]
    all_tool_results = tool_results_text(model)
    assert "STALL DETECTED" in all_tool_results
    assert "rotate your hypothesis" in all_tool_results
    # The follow-up session's task message carries the stall governance handoff.
    second_session_messages = "\n".join(str(m.content) for m in model.calls[4])
    assert "Stall Governance Handoff" in second_session_messages
    assert arc_runtime.traceability.get_test("T1")["passed"] is True


# ---------------------------------------------------------------------------
# Baseline RED: the first session starts from system-verified failures
# ---------------------------------------------------------------------------


def test_baseline_red_evidence_reaches_first_session(tmp_project_dir: Path, arc_runtime) -> None:
    """The first agent session's task must include the baseline RED evidence.

    The RED phase is verified by the system, not assumed: each file was run
    once before the session and its failure output is quoted back to the
    agent as the repair queue.
    """

    node_id = "REQ-TDD-RED"
    tests = [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)

    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {}, call_id="r1"),
            faux_text("IMPLEMENTED"),
        ]
    )
    detail = "AssertionError: add(1, 1) returned 0"
    fake = FakeAppHandler([failing_test_output(detail=detail), passing_test_output()])
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    first_call_messages = "\n".join(str(m.content) for m in model.calls[0])
    assert "Baseline RED Evidence" in first_call_messages
    assert "verifiably fail RIGHT NOW" in first_call_messages
    assert detail in first_call_messages


# ---------------------------------------------------------------------------
# All-green layer without a closing full run: system regression closes it
# ---------------------------------------------------------------------------


def test_all_green_without_full_run_closes_layer_via_system_regression(tmp_project_dir: Path, arc_runtime) -> None:
    """A layer whose files all turned green individually must not open a new
    agent session just to run the closing full-layer pass.

    The agent may verify each file with subset runs and end its turn; the
    scheduler then runs the full-layer regression itself and closes the layer.
    (PR review: without this, the follow-up session was pure overhead - and a
    session that ended before re-running tests could fail the layer even
    though every file was green.)
    """

    node_id = "REQ-TDD-SEAL"
    other_unit_file = "tests/unit/test_extra.py"
    tests = [
        {"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T2", "type": "Unit", "file_path": other_unit_file},
    ]
    seed_node(arc_runtime, node_id, tests)

    # Session 1: the agent repairs each file with subset runs (both pass) and
    # ends its turn WITHOUT a closing full-layer run.
    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {"test_files": [UNIT_TEST_FILE]}, call_id="r1"),
            faux_tool_call("run_tests", {"test_files": [other_unit_file]}, call_id="r2"),
            faux_text("both files repaired, ending turn"),
        ]
    )
    # Baselines (red, red), subset runs (pass, pass), system regression (pass).
    fake = FakeAppHandler(
        [
            failing_test_output(detail="AssertionError: file1 missing"),
            failing_test_output(detail="AssertionError: file2 missing"),
            passing_test_output(),
            passing_test_output(),
            passing_test_output(),
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    session_types = track_tdd_sessions(tdd)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    # Exactly one agent session; the closing full-layer run is system-side.
    assert session_types == ["Unit"]
    assert fake.calls == [
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [other_unit_file]),
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [other_unit_file]),
        ("Unit", [UNIT_TEST_FILE, other_unit_file]),
    ]
    assert arc_runtime.traceability.get_test("T1")["passed"] is True
    assert arc_runtime.traceability.get_test("T2")["passed"] is True
    node_session = sessions.load_node_session(node_id)
    assert node_session["recent_failure_summary"] == ""


# ---------------------------------------------------------------------------
# Failure digest: raw-output persistence, digest in tool results, diff hint
# ---------------------------------------------------------------------------


E2E_FAILURE_OUTPUT = """Exit Code: 1

Running 2 tests using 1 worker

  ✘  1 test-e2e\\register.e2e.spec.js:20:3 › register › rejects invalid input (5.1s)

  1) test-e2e\\register.e2e.spec.js:20:3 › register › rejects invalid input ─────

    Error: expect(locator).toBeVisible() failed

    Locator: getByLabel('用户名')
    Expected: visible
    Timeout: 5000ms
    Error: element(s) not found

Exit Code: 1
"""


# The web handler's E2E result preamble (see app_type_handler.web) prepends
# the frontend build, database prepare and backend instance fingerprint
# sections ahead of the Playwright output. On Windows the launcher-PID note
# prints in EVERY E2E run and contains the keyword substring "expected"
# ("This is expected when `npm` ..."), so a keyword-scan fingerprint keyed on
# it instead of the real Playwright error (2026-09-19 test1 run, REQ-2 E2E
# attempts 3/4/6 all shared one fingerprint).
E2E_FAILURE_OUTPUT_WITH_NOTE_PREAMBLE = """Runner: Playwright
Batch Test Type: E2E
Web Port: 3302

=== Frontend Build ===
Exit Code: 0

=== E2E Runtime Env ===
DB Path: D:\\ws\\.arc-test-db\\register-410b239d.sqlite
DB Label: register

=== Database Prepare ===
Exit Code: 0

=== Backend Runtime ===
Command: npm run start
Port: 3302
Startup Cleanup: Pre-start port cleanup: port 3302 is released.

=== Backend Instance Fingerprint ===
Platform: win32
Launcher PID: 49792
Port Owner PID(s): 55492
Note: launcher PID does not own the port directly. This is expected when `npm` or a shell spawns the actual backend child process.
- PID 49792
  Name: cmd.exe
  Command: C:\\WINDOWS\\system32\\cmd.exe /c "npm run start"

Exit Code: 1
STDOUT:

Running 2 tests using 1 worker

  ✘  1 test-e2e\\register.e2e.spec.js:20:3 › register › rejects invalid input (5.1s)

  1) test-e2e\\register.e2e.spec.js:20:3 › register › rejects invalid input ─────

    Error: expect(locator).toBeVisible() failed

    Locator: getByLabel('用户名')
    Expected: visible
    Timeout: 5000ms
    Error: element(s) not found

Exit Code: 1
"""


def test_run_tests_result_carries_digest_and_log_pointer(tmp_project_dir: Path, arc_runtime) -> None:
    """A failed run must give the agent the structured digest and a log path.

    This is the in-session half of the failure-visibility contract: the model
    sees each failed test's location and expected/received up front, plus a
    pointer to the persisted raw output so re-reading the full output is one
    read_file call instead of a re-run.
    """

    node_id = "REQ-TDD-DIGEST"
    tests = [{"test_id": "T1", "type": "E2E", "file_path": E2E_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)
    (tmp_project_dir / E2E_TEST_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_project_dir / E2E_TEST_FILE).write_text("// e2e spec\n", encoding="utf-8")

    # Session 1 runs once (fail) then a continuation note; session 2 makes no
    # run_tests call, which ends the layer ("ended without calling run_tests").
    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {}, call_id="c1"),
            faux_text("continuation needed"),
            faux_text("no more attempts"),
            faux_text("script pad for an extra scheduler turn"),
        ]
    )
    # Baseline fails, agent run fails.
    fake = FakeAppHandler([E2E_FAILURE_OUTPUT, E2E_FAILURE_OUTPUT])
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is False
    results = tool_results_text(model)
    # The agent-facing run_tests result carries the digest block.
    assert "Structured Failure Digest" in results
    assert "register › rejects invalid input" in results
    assert "getByLabel('用户名')" in results
    # ...and the persisted raw-output pointer.
    assert "ARC_RUN_OUTPUT_LOG" in results
    assert ".arc/tdd_runs/REQ-TDD-DIGEST/E2E-" in results
    # The log file itself exists under the runtime-ignored .arc tree and holds
    # the raw output of the agent's run (the per-file baseline runs go through
    # the system path, not run_requested_tests, so they are not persisted).
    log_files = sorted((tmp_project_dir / ".arc" / "tdd_runs" / node_id).glob("*.log"))
    assert len(log_files) == 1
    assert "getByLabel('用户名')" in log_files[0].read_text(encoding="utf-8")


def test_note_preamble_never_becomes_the_failure_headline(tmp_project_dir: Path, arc_runtime) -> None:
    """The informational note preamble must not hijack failure fingerprints.

    The digest's ``fingerprint:`` line and the verifier report's fingerprint
    both feed the cross-session handoff and the stall governor. Before the fix
    both keyed on the launcher-PID note (it contains "expected"), so every
    failed E2E run shared one fingerprint and the handoff led the next session
    on a five-minute dist-build goose chase instead of the real Playwright
    error. Adapter-level test: drives the real adapter with a canned executor
    so both fingerprint exits are exercised.
    """

    node_id = "REQ-TDD-NOTE"
    seed_node(arc_runtime, node_id, [{"test_id": "T1", "type": "E2E", "file_path": E2E_TEST_FILE}])
    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {"test_type": "E2E"}, call_id="c1"),
            faux_text("giving up this session"),
        ]
    )

    async def executor(test_type, test_files):
        return parse_test_run(E2E_FAILURE_OUTPUT_WITH_NOTE_PREAMBLE)

    tdd = make_tdd(tmp_project_dir, model, FakeAppHandler())
    asyncio.run(
        tdd.run(
            node_id=node_id,
            test_files=[E2E_TEST_FILE],
            test_type="E2E",
            node_tests=[{"test_id": "T1", "type": "E2E", "file_path": E2E_TEST_FILE}],
            run_tests_executor=executor,
        )
    )

    digest_text = tdd.get_last_failure_digest()
    verifier_text = tdd.get_last_verifier_report()
    # The fingerprint line is the headline of both exits; neither may key on
    # the note. (The verifier report's raw tail excerpt legitimately still
    # contains the note as part of the output itself.)
    assert "fingerprint: 1|Note:" not in digest_text
    assert "fingerprint: 1|Note:" not in verifier_text
    assert "fingerprint: 1|Error: expect(locator).toBeVisible() failed" in digest_text
    assert "fingerprint: 1|Error: expect(locator).toBeVisible() failed" in verifier_text
    # The digest still lists the real failed test with its per-test detail.
    assert "rejects invalid input" in digest_text
    assert "getByLabel('用户名')" in digest_text


# ---------------------------------------------------------------------------
# Retry-round case filter (#115): an E2E red round's successors re-run only
# the digest's failed cases; the first round and the layer-closing round
# stay full, and unparseable/environmental failures fall back to full.
# ---------------------------------------------------------------------------


def _seed_e2e_node_with_file(arc_runtime, tmp_project_dir: Path, node_id: str) -> list[dict]:
    tests = [{"test_id": "T1", "type": "E2E", "file_path": E2E_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)
    (tmp_project_dir / E2E_TEST_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_project_dir / E2E_TEST_FILE).write_text("// e2e spec\n", encoding="utf-8")
    return tests


def test_e2e_retry_rounds_filter_to_digest_failed_cases(tmp_project_dir: Path, arc_runtime) -> None:
    """Red rounds re-run only the parsed failed cases; the closing round is full.

    Round map: baseline (system, full) -> r1 agent round 1 (full, fails) ->
    r2 filtered retry (fails again) -> r3 filtered retry (passes, must not
    close the layer) -> r4, the agent's immediate full re-run in the same
    session, closes it.
    """

    node_id = "REQ-TDD-GREP"
    tests = _seed_e2e_node_with_file(arc_runtime, tmp_project_dir, node_id)

    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {}, call_id="r1"),
            faux_text("repairing, next round"),
            faux_tool_call("run_tests", {}, call_id="r2"),
            faux_text("still red, editing again"),
            faux_tool_call("run_tests", {}, call_id="r3"),
            # The agent heeds the filter note and re-runs the full layer in
            # the same session; that full green run closes it.
            faux_tool_call("run_tests", {}, call_id="r4"),
            faux_text("IMPLEMENTED"),
            faux_text("script pad for an extra scheduler turn"),
        ]
    )
    fake = FakeAppHandler(
        [
            E2E_FAILURE_OUTPUT,      # baseline RED
            E2E_FAILURE_OUTPUT,      # r1: full run, fails
            E2E_FAILURE_OUTPUT,      # r2: filtered retry, fails again
            passing_test_output(),   # r3: filtered retry, passes
            passing_test_output(),   # r4: full run closes the layer
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert fake.calls == [("E2E", [E2E_TEST_FILE])] * 5
    # baseline + r1 run unfiltered; r2/r3 carry the digest's failed case;
    # the layer-closing r4 is full again.
    assert fake.case_filters == [
        None,
        None,
        ["register › rejects invalid input"],
        ["register › rejects invalid input"],
        None,
    ]
    all_tool_results = tool_results_text(model)
    # The filtered rounds say so, and the filtered pass demands one full run.
    assert "ARC_RETRY_FILTER_NOTE" in all_tool_results
    assert "the layer is not closed yet" in all_tool_results
    assert "passed (full layer)" in all_tool_results


def test_retry_falls_back_to_full_run_without_parseable_digest(tmp_project_dir: Path, arc_runtime) -> None:
    """A failure the digest cannot structure must not produce an empty filter."""

    node_id = "REQ-TDD-GREP-FALLBACK"
    tests = _seed_e2e_node_with_file(arc_runtime, tmp_project_dir, node_id)

    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {}, call_id="r1"),
            faux_text("repairing, next round"),
            faux_tool_call("run_tests", {}, call_id="r2"),
            faux_text("IMPLEMENTED"),
        ]
    )
    fake = FakeAppHandler(
        [
            failing_test_output(),   # baseline RED (no per-test structure)
            failing_test_output(),   # r1: full run, digest unparseable
            passing_test_output(),   # r2: full retry passes and closes the layer
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert fake.case_filters == [None, None, None]
    assert "passed (full layer)" in tool_results_text(model)


def test_environment_failure_clears_the_retry_filter(tmp_project_dir: Path, arc_runtime) -> None:
    """An environmental failure must not narrow the next round to stale cases.

    The round failed on a broken workspace, not just on the reported case, so
    the repair contract revalidates the whole layer.
    """

    node_id = "REQ-TDD-GREP-ENV"
    tests = _seed_e2e_node_with_file(arc_runtime, tmp_project_dir, node_id)
    env_failure_output = E2E_FAILURE_OUTPUT + "\nError: Cannot find module 'db-helper'\n"

    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {}, call_id="r1"),
            # The repair-and-revalidate attempt happens in the same session
            # (the env-failure contract ends the layer when the session ends).
            faux_tool_call("run_tests", {}, call_id="r2"),
            faux_text("IMPLEMENTED"),
        ]
    )
    fake = FakeAppHandler(
        [
            E2E_FAILURE_OUTPUT,      # baseline RED
            env_failure_output,      # r1: parseable case failure + broken workspace
            passing_test_output(),   # r2: full revalidation passes and closes
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert fake.case_filters == [None, None, None]


def test_filtered_pass_and_agent_stop_closes_via_system_regression(tmp_project_dir: Path, arc_runtime) -> None:
    """If the agent stops after a filtered pass, the system closes the layer.

    The safety net: all files green but no full green run -> the system-run
    full-layer regression runs unfiltered and closes (or reopens) the layer
    without a new agent session.
    """

    node_id = "REQ-TDD-GREP-REGRESSION"
    tests = _seed_e2e_node_with_file(arc_runtime, tmp_project_dir, node_id)

    model = FauxChatModel(
        responses=[
            faux_tool_call("run_tests", {}, call_id="r1"),
            faux_text("repairing, next round"),
            faux_tool_call("run_tests", {}, call_id="r2"),
            faux_text("cases green, ending my turn"),
        ]
    )
    fake = FakeAppHandler(
        [
            E2E_FAILURE_OUTPUT,      # baseline RED
            E2E_FAILURE_OUTPUT,      # r1: full run, fails
            passing_test_output(),   # r2: filtered retry, passes
            passing_test_output(),   # system regression: full, closes the layer
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert fake.case_filters == [None, None, ["register › rejects invalid input"], None]
    # The layer closed via the system-run regression, not an agent round: no
    # tool result carries the full-layer status, and the agent only saw the
    # two run_tests rounds (fail, then the filtered pass with its note).
    all_tool_results = tool_results_text(model)
    assert "ARC_RETRY_FILTER_NOTE" in all_tool_results
    assert "the layer is not closed yet" in all_tool_results
    assert "passed (full layer)" not in all_tool_results


def test_tdd_handoff_records_modified_files(tmp_project_dir: Path, arc_runtime) -> None:
    """The node session handoff must tell the next TDD round what was edited.

    ``tdd_handoff.modified_files`` used to be a permanently empty list; it now
    carries the stage-discipline write paths of the last session so a later
    round (post-run TDD retry, --retry-failed) does not re-derive them.
    """

    node_id = "REQ-TDD-HANDOFF"
    tests = [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)
    write_test_file(tmp_project_dir)

    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a - b\n"},
                call_id="c1",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="c2"),
            faux_text("continuation needed"),
            faux_text("no more attempts"),
            faux_text("script pad for an extra scheduler turn"),
        ]
    )
    # Baseline fails, agent run fails with a DIFFERENT detail so the
    # fingerprints differ (exercises the "fingerprint moved" branch).
    fake = FakeAppHandler(
        [
            failing_test_output(detail="AssertionError: expected 2 got 1"),
            failing_test_output(detail="AssertionError: expected 2 got 3"),
        ]
    )
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is False
    node_session = sessions.load_node_session(node_id)
    # modified_files is the round-level union of the sessions' writes, from
    # the stage discipline rather than the model's self-report — the last
    # session here wrote nothing, so only the union survives it.
    assert node_session["tdd_handoff"]["modified_files"] == ["src/calc.py"]


def test_followup_session_receives_digest_and_diff_hint(tmp_project_dir: Path, arc_runtime) -> None:
    """A second session on the same layer starts from the three-part handoff.

    Session 1 fails; session 2's prompt must carry the structured digest (not
    just a raw tail), the files session 1 edited, and the fingerprint movement.
    """

    node_id = "REQ-TDD-HANDOFF-2"
    tests = [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}]
    seed_node(arc_runtime, node_id, tests)
    write_test_file(tmp_project_dir)

    model = FauxChatModel(
        responses=[
            # Session 1: write, run (fail), run (same failure), end turn.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a - b\n"},
                call_id="s1c1",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="s1c2"),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="s1c3"),
            faux_text("continuation needed"),
            # Session 2: no run_tests call, ends the layer.
            faux_text("no more attempts"),
        ]
    )
    # Baseline plus the two session-1 runs all fail with the SAME detail: the
    # diff hint must then report that the fingerprint DID NOT change.
    fake = FakeAppHandler([failing_test_output()] * 3)
    tdd = make_tdd(tmp_project_dir, model, fake)
    handoffs = track_tdd_handoffs(tdd)
    runner = make_runner(tmp_project_dir, tdd, fake)

    asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert len(handoffs) == 2
    session2_handoff = handoffs[1]
    assert "Files edited by the previous session" in session2_handoff
    assert "`src/calc.py`" in session2_handoff
    assert "DID NOT change" in session2_handoff
    # The digest from the adapter's last failed run is the evidence part.
    assert "Structured Failure Digest" in session2_handoff
    # And the persisted raw output pointer reached the next session.
    assert ".arc/tdd_runs/" in session2_handoff


# ---------------------------------------------------------------------------
# Missing-package environment failures get one install_dependencies cycle
# ---------------------------------------------------------------------------


def test_missing_package_failure_grants_install_cycle(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A missing npm package must not close the layer on the second failure.

    Observed on the 2026-09-19 test1 run: the agent required 'cookie-parser'
    (not in the template), the second environmental failure burned the whole
    Integration budget via the short-circuit, and the hand-written replacement
    was never validated. The ``install_dependencies`` tool now exists, so a
    ``missing dependency: <pkg>`` failure gets one extra repair-and-revalidate
    cycle before the layer closes.
    """

    node_id = "REQ-TDD-INSTALL"
    tests = [
        {"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T2", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
    ]
    seed_node(arc_runtime, node_id, tests)

    model = FauxChatModel(
        responses=[
            # Baseline env failure is reported to the first session.
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="u1"),
            # Agent run fails environmentally (missing dependency).
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="u2"),
            # Second env failure: the layer now offers the install path
            # instead of closing. The agent installs and re-validates.
            faux_tool_call(
                "install_dependencies",
                {"package": "cookie-parser", "target": "backend"},
                call_id="i1",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="u3"),
            faux_text("IMPLEMENTED"),
        ]
    )
    fake = FakeAppHandler(
        [
            # Unit baseline (red, environmental), agent failure, still-env
            # failure that grants the install cycle, post-install pass.
            failing_test_output(detail=MISSING_DEP_OUTPUT),
            failing_test_output(detail=MISSING_DEP_OUTPUT),
            failing_test_output(detail=MISSING_DEP_OUTPUT),
            passing_test_output(),
            # Integration baseline is green, and the tautology fast path then
            # runs one full-layer regression (system-run, no agent budget).
            passing_test_output(),
            passing_test_output(),
        ]
    )
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is True
    assert fake.install_calls == [("cookie-parser", "backend")]
    # Four Unit executions: baseline, agent failure, still-env failure (the
    # one that used to burn the budget), post-install re-validation pass. The
    # green Integration baseline closes the node without another agent run.
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])] * 4 + [
        ("Integration", [INTEGRATION_TEST_FILE]),
        ("Integration", [INTEGRATION_TEST_FILE]),
    ]
    all_tool_results = tool_results_text(model)
    # The install result must name the package/target and tell the agent to
    # re-run run_tests to validate the repair.
    assert "cookie-parser" in all_tool_results
    assert "backend/node_modules" in all_tool_results
    assert "Re-run run_tests" in all_tool_results


def test_missing_package_install_fails_closes_layer(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """After one failed install cycle the layer closes like any env failure.

    The extra cycle is granted once per layer; a still-environmental failure
    after it falls back to the original short-circuit (burn the remaining
    budget, stop the loop) so a doomed command cannot spin forever.
    """

    node_id = "REQ-TDD-INSTALL-FAIL"
    tests = [
        {"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE},
        {"test_id": "T2", "type": "Integration", "file_path": INTEGRATION_TEST_FILE},
    ]
    seed_node(arc_runtime, node_id, tests)

    script = [faux_tool_call("run_tests", {}, call_id=f"c{i}") for i in range(TDD_RUN_TESTS_BUDGET)]
    script.append(faux_text("BLOCKED"))
    model = FauxChatModel(responses=script)
    # Baseline, failure, still-env failure (grants install), failure after the
    # used-up install cycle (closes the layer for good).
    fake = FakeAppHandler([failing_test_output(detail=MISSING_DEP_OUTPUT) for _ in range(4)])
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is False
    # The layer closed after the second still-environmental failure; the
    # Integration layer is never reached.
    unit_calls = [call for call in fake.calls if call[0] == "Unit"]
    assert len(unit_calls) == 4
    integration_calls = [call for call in fake.calls if call[0] == "Integration"]
    assert integration_calls == []
    node_session = sessions.load_node_session(node_id)
    assert "missing dependency" in node_session["recent_failure_summary"]


def test_install_tool_reaches_agent_without_shell() -> None:
    """The install tool is a plain function tool over the app handler."""

    fake = FakeAppHandler()
    tool = build_install_dependencies_tool(app_handler=fake, node_id="REQ-X")
    result = asyncio.run(tool(package="cookie-parser", target="backend"))
    assert "Exit Code: 0" in result
    assert fake.install_calls == [("cookie-parser", "backend")]

    missing = FakeAppHandler()
    missing.run_build = None  # type: ignore[method-assign]
    broken = build_install_dependencies_tool(app_handler=object(), node_id="REQ-X")
    result = asyncio.run(broken(package="cookie-parser"))
    assert "Exit Code: 1" in result
    assert "not configured" in result


def test_install_tool_converts_handler_crash_into_failed_install() -> None:
    """A handler exception must not escape into the agent graph.

    Observed on the 2026-09-20 test1 run: ``install_dependencies`` raised
    ``FileNotFoundError: [WinError 2]`` (bare ``npm`` is unspawnable through
    ``create_subprocess_exec`` on Windows), the exception killed the whole
    IMPLEMENT task, and the stream-fallback replay crashed on it again. The
    tool layer now converts any handler crash into an ordinary failed install
    so the agent can fall back to a standard-library implementation.
    """

    class ExplodingHandler:
        async def install_package(self, package: str, target: str = "backend") -> str:
            raise FileNotFoundError(2, "系统找不到指定的文件。", "npm")

    tool = build_install_dependencies_tool(app_handler=ExplodingHandler(), node_id="REQ-X")
    result = asyncio.run(tool(package="bcrypt", target="backend"))
    assert result.startswith("Exit Code: 1")
    assert "FileNotFoundError" in result
    assert "fall back" in result.lower()


def test_install_command_uses_argv_list() -> None:
    """The install command must bypass the shell (review round-1 hardening).

    ``_run_npm_command`` now accepts argv lists and uses
    ``create_subprocess_exec`` for them; ``install_package`` passes the
    validated package name as a single argv element so no shell quoting or
    injection surface remains even if the name validation is ever relaxed.
    """

    import inspect

    from app_type_handler import web as web_mod

    # The public helper must route list commands through exec, not shell.
    source = inspect.getsource(web_mod._run_npm_command)
    assert "create_subprocess_exec" in source
    assert "isinstance(command, list)" in source
    # And install_package builds a list command containing the bare name.
    install_source = inspect.getsource(web_mod.WebAppType.install_package)
    assert '"npm",' in install_source
    assert 'name,' in install_source


def test_exec_argv_resolves_program_through_pathext() -> None:
    """``create_subprocess_exec`` cannot spawn bare ``npm`` on Windows.

    ``CreateProcess`` does not apply ``PATHEXT`` resolution, so the argv-list
    npm install crashed with ``FileNotFoundError: [WinError 2]`` on Windows
    (the real file is ``npm.cmd``). The list path must resolve the program
    with ``shutil.which`` first - on Windows that finds ``npm.cmd``, on POSIX
    it returns the same absolute path, so both platforms spawn it directly.
    """

    import inspect

    from app_type_handler import web as web_mod

    source = inspect.getsource(web_mod._run_npm_command)
    assert "_resolve_executable" in source, "the exec path must resolve the program first"

    # Resolution mirrors a shell: whatever which() finds, it stays on PATH.
    assert web_mod._resolve_executable("definitely-not-a-real-program-xyz") == (
        "definitely-not-a-real-program-xyz"
    ), "an unresolvable name is passed through for the OS error to surface"
    resolved = web_mod._resolve_executable("npm")
    if sys.platform == "win32":
        assert resolved.lower().endswith((".cmd", ".exe", ".bat", ".ps1")) or resolved == "npm"
    else:
        assert resolved == "npm" or Path(resolved).is_absolute()


def test_install_package_converts_spawn_failure_into_failed_install(
    tmp_path, monkeypatch
) -> None:
    """An unspawnable npm must fail the install, not crash the IMPLEMENT task.

    Belt to the tool-layer suspenders: even if resolution misses (a PATH-less
    sandbox, a renamed binary), ``install_package`` returns an ``Exit Code: 1``
    body the agent can recover from instead of raising.
    """

    from app_type_handler import web as web_mod
    from app_type_handler.web import WebAppType

    async def exploding_run(command, target_dir, timeout=None):
        raise FileNotFoundError(2, "系统找不到指定的文件。", "npm")

    async def noop_log(*args, **kwargs):
        return None

    monkeypatch.setattr(web_mod, "_run_npm_command", exploding_run)

    handler = WebAppType.__new__(WebAppType)
    handler.workspace_path = str(tmp_path)
    handler.log_cb = noop_log
    (tmp_path / "backend").mkdir()

    result = asyncio.run(handler.install_package("bcrypt", "backend"))

    assert result.startswith("Exit Code: 1")
    assert "could not run" in result
    assert "FileNotFoundError" in result

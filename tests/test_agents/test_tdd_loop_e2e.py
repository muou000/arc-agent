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
from pathlib import Path
from typing import Any

from agents.test_driven_developer import TestDrivenDeveloper
from core import sessions
from core.phases import TDD_RUN_TESTS_BUDGET, TDD_STALL_THRESHOLD, WorkflowPhaseRunner
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
        return failing_test_output()

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

    The agent cannot install a missing dependency mid-compile, so retrying the
    same doomed command is pure waste. The executor grants exactly one
    repair-and-revalidate attempt: an unrepaired retry executes once more,
    fails environmentally again, and then the layer is closed for good.
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
    # Baseline env failure, agent env failure, unrepaired re-validation.
    fake = FakeAppHandler([failing_test_output(detail=MISSING_DEP_OUTPUT) for _ in range(3)])
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(runner._run_tdd_for_node(node_id=node_id, tests=tests))

    assert final_ok is False
    # Baseline + one failing attempt + one unrepaired re-validation, then the
    # layer closes - Integration is never reached and no further doomed
    # command runs.
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])] * 3
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

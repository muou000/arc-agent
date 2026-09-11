"""Faux-model e2e tests for the real TDD loop.

Drives ``WorkflowPhaseRunner._run_tdd_for_node`` (``core/phases.py``) and the
``TestDrivenDeveloper`` adapter (``agents/test_driven_developer.py``) with a
scripted ``FauxChatModel`` plus a ``FakeAppHandler`` that returns canned
``run_test_group`` outputs. The deep-agents loop, the ``run_tests`` tool
executor, the per-layer budgets, the traceability bookkeeping and the node
session updates all run for real — no tokens, no npm.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.test_driven_developer import TestDrivenDeveloper
from core import sessions
from core.phases import TDD_RUN_TESTS_BUDGET, WorkflowPhaseRunner
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
                id="c1",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, id="c2"),
            # The failed run unlocked the written path, so the fix may rewrite it.
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a + b\n"},
                id="c3",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, id="c4"),
            faux_text("IMPLEMENTED"),
        ]
    )
    fake = FakeAppHandler([failing_test_output(), passing_test_output()])
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
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])] * 2  # fail run + passing run

    # The agent's fix really landed in the workspace.
    assert "return a + b" in (tmp_project_dir / "src" / "calc.py").read_text(encoding="utf-8")
    assert "Exit Code: 0" in (tdd.get_last_run_tests_result() or "")

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
            faux_tool_call("run_tests", {"test_type": "Unit"}, id="u1"),
            faux_text("unit layer passed"),
            # Integration layer session: first an out-of-layer call (rejected),
            # then the integration run, then the final answer.
            faux_tool_call("run_tests", {"test_type": "Unit"}, id="i0"),
            faux_tool_call("run_tests", {"test_type": "Integration"}, id="i1"),
            faux_text("IMPLEMENTED"),
        ]
    )
    fake = FakeAppHandler([passing_test_output(), passing_test_output()])
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
        ("Integration", [INTEGRATION_TEST_FILE]),
    ]
    # The out-of-layer run_tests call was rejected with an explicit gate message.
    all_tool_results = "\n".join(
        str(m.content) for call in model.calls for m in call if getattr(m, "type", "") == "tool"
    )
    assert "The active TDD layer is `Integration`" in all_tool_results
    assert arc_runtime.traceability.get_test("T-U")["passed"] is True
    assert arc_runtime.traceability.get_test("T-I")["passed"] is True


# ---------------------------------------------------------------------------
# Budget: run_tests exhausts its layer budget and the node fails
# ---------------------------------------------------------------------------


def test_tdd_budget_exhaustion_fails_node(tmp_project_dir: Path, arc_runtime) -> None:
    node_id = "REQ-TDD-3"
    seed_node(arc_runtime, node_id, [{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}])

    script = [faux_tool_call("run_tests", {}, id=f"c{i}") for i in range(TDD_RUN_TESTS_BUDGET)]
    script.append(faux_text("STILL FAILING"))
    model = FauxChatModel(responses=script)
    fake = FakeAppHandler([failing_test_output(detail=f"failure {i}") for i in range(TDD_RUN_TESTS_BUDGET)])
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(
        runner._run_tdd_for_node(
            node_id=node_id,
            tests=[{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}],
        )
    )

    assert final_ok is False
    assert len(fake.calls) == TDD_RUN_TESTS_BUDGET
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
    fake = FakeAppHandler()
    runner = make_runner(tmp_project_dir, make_tdd(tmp_project_dir, model, fake), fake)

    final_ok = asyncio.run(
        runner._run_tdd_for_node(
            node_id=node_id,
            tests=[{"test_id": "T1", "type": "Unit", "file_path": UNIT_TEST_FILE}],
        )
    )

    assert final_ok is False
    assert fake.calls == []
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
                id="c1",
            ),
            faux_tool_call("run_tests", {}, id="c2"),
            faux_text("IMPLEMENTED"),
        ]
    )
    fake = FakeAppHandler([passing_test_output()])
    tdd = make_tdd(tmp_project_dir, model, fake)
    runner = make_runner(tmp_project_dir, tdd, fake)

    ok = asyncio.run(runner.run_implement_phase(node_id, {"children_ids": []}))

    assert ok is True
    assert arc_runtime.traceability.get_interface("IF-CALC")["implemented"] is True
    node_session = sessions.load_node_session(node_id)
    assert node_session["phase_status"]["implement"] == "completed"


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

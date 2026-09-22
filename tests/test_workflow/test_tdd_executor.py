"""Executor-interface tests for the TDD test executor (issue #104).

Drives ``core.test_executor.TddTestExecutor`` directly — no agents, no
faux model — so the contracts that used to live inside the
``run_requested_tests`` closure are asserted on the explicit interface:
per-layer budget consumption and exhaustion, unknown-file rejection,
in-session layer advancement, stall governance copy and the shared
red/green/unverified file-state predicate behind the DESIGN baseline gate.
Every scripted run crosses the seam as a :class:`TestRunResult`, exactly as
the real app handler produces it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app_type_handler.test_results import TestRunResult, parse_test_run
from core.test_executor import (
    TDD_RUN_TESTS_BUDGET,
    TddTestExecutor,
    classify_file_state,
)
from tests.helpers.faux import failing_test_output, passing_test_output

UNIT_TEST_FILE = "tests/unit/test_calc.py"
OTHER_UNIT_FILE = "tests/unit/test_extra.py"
INTEGRATION_TEST_FILE = "tests/integration/test_flow.py"
MISSING_DEP_OUTPUT = "Error: Cannot find module '@testing-library/dom'"


def run_of(output: str, *, exit_code: int | None = None) -> TestRunResult:
    """Build the structured run result the way the real handler does."""

    return parse_test_run(output, exit_code=exit_code)


class ScriptedRunner:
    """Stand-in for the app handler's ``run_test_group`` with queued results."""

    def __init__(self, results: list[TestRunResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, list[str]]] = []
        # Parallel to ``calls``: the failed-case filter each call carried
        # (None = unfiltered full run) - the executor-side seam for #115.
        self.case_filters: list[list[str] | None] = []

    async def __call__(
        self,
        test_type: str,
        file_paths: list[str],
        failed_case_names: list[str] | None = None,
    ) -> TestRunResult:
        self.calls.append((test_type, list(file_paths)))
        self.case_filters.append(list(failed_case_names) if failed_case_names else None)
        if not self._results:
            raise RuntimeError(
                f"ScriptedRunner ran out of scripted results after {len(self.calls)} call(s)."
            )
        return self._results.pop(0)


def make_executor(
    tmp_path: Path,
    runner: ScriptedRunner,
    tests: list[dict[str, Any]],
) -> TddTestExecutor:
    executor = TddTestExecutor(
        node_id="REQ-EXEC",
        workspace_path=str(tmp_path),
        run_group=runner,
    )
    executor.register_tests(tests)
    return executor


def manifest(*items: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {"test_id": f"T-{index}", "type": test_type, "file_path": file_path}
        for index, (test_type, file_path) in enumerate(items)
    ]


def passing_run() -> TestRunResult:
    return run_of(passing_test_output())


def failing_run(detail: str = "AssertionError: expected 2 got 1") -> TestRunResult:
    return run_of(failing_test_output(detail=detail))


# ---------------------------------------------------------------------------
# Budget: exhaustion closes the layer, advances the active one, stops running
# ---------------------------------------------------------------------------


def test_budget_exhaustion_closes_layer_and_advances(tmp_path: Path) -> None:
    runner = ScriptedRunner([failing_run(f"failure {i}") for i in range(TDD_RUN_TESTS_BUDGET)])
    executor = make_executor(
        tmp_path,
        runner,
        manifest(("Unit", UNIT_TEST_FILE), ("Integration", INTEGRATION_TEST_FILE)),
    )
    executor.pin_active_layer("Unit")

    for _ in range(TDD_RUN_TESTS_BUDGET):
        result = asyncio.run(executor.run_requested())
        assert result.exit_code == 1
    assert executor.usage("Unit") == TDD_RUN_TESTS_BUDGET
    assert executor.budget_exhausted("Unit")
    assert len(runner.calls) == TDD_RUN_TESTS_BUDGET

    # The next request is refused WITHOUT a test run, and the active layer
    # hands over to the successor.
    result = asyncio.run(executor.run_requested())
    assert "The Unit layer is closed: run_tests budget exhausted" in result.output
    assert "has advanced the active layer to `Integration`" in result.output
    assert executor.active_layer == "Integration"
    assert executor.usage("Unit") == TDD_RUN_TESTS_BUDGET
    assert len(runner.calls) == TDD_RUN_TESTS_BUDGET

    # The successor layer runs on its own independent budget.
    runner._results.append(failing_run("integration failure"))
    result = asyncio.run(executor.run_requested("Integration"))
    assert result.exit_code == 1
    assert runner.calls[-1] == ("Integration", [INTEGRATION_TEST_FILE])
    assert executor.usage("Integration") == 1


def test_budget_exhaustion_on_last_layer_reports_no_successor(tmp_path: Path) -> None:
    runner = ScriptedRunner([failing_run() for _ in range(TDD_RUN_TESTS_BUDGET)])
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))
    executor.pin_active_layer("Unit")

    for _ in range(TDD_RUN_TESTS_BUDGET):
        asyncio.run(executor.run_requested())
    result = asyncio.run(executor.run_requested())

    assert "This was the last scheduled layer." in result.output
    assert executor.active_layer == "Unit"
    assert len(runner.calls) == TDD_RUN_TESTS_BUDGET


# ---------------------------------------------------------------------------
# Unknown-file rejection: only registered manifest files may run
# ---------------------------------------------------------------------------


def test_unknown_file_rejection_refuses_run_and_budget(tmp_path: Path) -> None:
    runner = ScriptedRunner([])
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))
    executor.pin_active_layer("Unit")

    result = asyncio.run(executor.run_requested(requested_files=["tests/unit/test_probe.py"]))

    assert "may only execute registered Unit tests for the current node." in result.output
    assert "Unknown files: tests/unit/test_probe.py" in result.output
    # Refusal must be free: no test run, no budget consumed.
    assert runner.calls == []
    assert executor.usage("Unit") == 0


def test_registered_subset_runs_and_consumes_budget(tmp_path: Path) -> None:
    runner = ScriptedRunner([failing_run()])
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))
    executor.pin_active_layer("Unit")

    result = asyncio.run(executor.run_requested(requested_files=[UNIT_TEST_FILE]))

    assert result.exit_code == 1
    assert runner.calls == [("Unit", [UNIT_TEST_FILE])]
    assert executor.usage("Unit") == 1


# ---------------------------------------------------------------------------
# Layer advancement: a passing full-layer run closes and advances in-session
# ---------------------------------------------------------------------------


def test_full_layer_pass_advances_active_layer(tmp_path: Path) -> None:
    runner = ScriptedRunner([passing_run()])
    executor = make_executor(
        tmp_path,
        runner,
        manifest(("Unit", UNIT_TEST_FILE), ("Integration", INTEGRATION_TEST_FILE)),
    )
    executor.pin_active_layer("Unit")

    result = asyncio.run(executor.run_requested())

    assert "passed (full layer)." in result.output
    assert "has advanced the active layer to `Integration`" in result.output
    assert executor.layer_passed("Unit") is True
    assert executor.active_layer == "Integration"

    # The closed layer can no longer be re-run: the request is rejected
    # against the NEW active layer without consuming any budget.
    result = asyncio.run(executor.run_requested("Unit"))
    assert "The active TDD layer is `Integration`, but run_tests requested `Unit`." in result.output
    assert runner.calls == [("Unit", [UNIT_TEST_FILE])]
    assert executor.usage("Integration") == 0


def test_subset_pass_keeps_layer_open_and_reports_remaining_work(tmp_path: Path) -> None:
    runner = ScriptedRunner([passing_run(), passing_run(), passing_run()])
    executor = make_executor(
        tmp_path,
        runner,
        manifest(("Unit", UNIT_TEST_FILE), ("Unit", OTHER_UNIT_FILE)),
    )
    executor.pin_active_layer("Unit")
    # Baseline: both files verified red by system runs.
    executor.record_file_state("Unit", UNIT_TEST_FILE, "red")
    executor.record_file_state("Unit", OTHER_UNIT_FILE, "red")

    result = asyncio.run(executor.run_requested(requested_files=[UNIT_TEST_FILE]))

    assert executor.layer_passed("Unit") is False
    assert "still red: tests/unit/test_extra.py" in result.output
    assert "not been run yet" not in result.output
    # The second subset run turns the last red file green, but the layer
    # only closes on a passing FULL-layer run.
    result = asyncio.run(executor.run_requested(requested_files=[OTHER_UNIT_FILE]))
    assert "still red" not in result.output
    assert "not been run yet" not in result.output
    assert executor.layer_passed("Unit") is False


def test_no_active_layer_guard(tmp_path: Path) -> None:
    runner = ScriptedRunner([])
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))

    result = asyncio.run(executor.run_requested())

    assert result.exit_code == 1
    assert result.output == (
        "Exit Code: 1\n"
        "STDERR:\n"
        "No active TDD test layer is currently scheduled.\n"
    )
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Stall governance: identical fingerprints inject the rotation directive
# ---------------------------------------------------------------------------


def test_stall_copy_after_identical_fingerprints(tmp_path: Path) -> None:
    same_failure = failing_run("AssertionError: expected 'Login' to equal 'Log in'")
    runner = ScriptedRunner([same_failure, same_failure, same_failure])
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))
    executor.pin_active_layer("Unit")

    asyncio.run(executor.run_requested())
    assert executor.is_stalled("Unit") is False
    asyncio.run(executor.run_requested())
    assert executor.is_stalled("Unit") is False
    third = asyncio.run(executor.run_requested())

    assert executor.is_stalled("Unit") is True
    assert "STALL DETECTED" in third.output
    assert "rotate your hypothesis" in third.output
    assert len(executor.fingerprints("Unit")) == 3
    assert len(set(executor.fingerprints("Unit"))) == 1


# ---------------------------------------------------------------------------
# Shared red/green/unverified predicate (the DESIGN gate's classification)
# ---------------------------------------------------------------------------


def test_baseline_file_states_use_the_shared_predicate(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            passing_run(),
            failing_run("AssertionError: add(1, 1) returned 0"),
            run_of(failing_test_output(detail=MISSING_DEP_OUTPUT)),
        ]
    )
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))

    green = asyncio.run(executor.run_baseline_file("Unit", UNIT_TEST_FILE))
    assert (green.state, green.exit_code) == ("green", 0)
    red = asyncio.run(executor.run_baseline_file("Unit", "tests/unit/test_broken.py"))
    assert (red.state, red.exit_code) == ("red", 1)
    unverified = asyncio.run(executor.run_baseline_file("Unit", "tests/unit/test_env.py"))
    # An environmental failure stays UNVERIFIED (None), never "red".
    assert unverified.state is None
    assert "missing dependency: @testing-library/dom" in unverified.environment_failure

    assert executor.file_states("Unit") == {
        UNIT_TEST_FILE: "green",
        "tests/unit/test_broken.py": "red",
        "tests/unit/test_env.py": None,
    }
    assert classify_file_state(passing_run()) == "green"
    assert classify_file_state(run_of(failing_test_output(detail=MISSING_DEP_OUTPUT))) is None


# ---------------------------------------------------------------------------
# Seeding: DESIGN baseline states pre-verify files so the loop skips them
# ---------------------------------------------------------------------------


def test_seed_file_states_reuses_design_baseline(tmp_path: Path) -> None:
    runner = ScriptedRunner([])
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))
    executor.seed_file_states({UNIT_TEST_FILE: "red"})

    assert executor.file_states("Unit") == {UNIT_TEST_FILE: "red"}


# ---------------------------------------------------------------------------
# Retry-round case filter (#115): red rounds re-run only the digest's failed
# cases through the handler seam; the closing round stays full.
# ---------------------------------------------------------------------------


E2E_DIGEST_FAILURE = (
    "Exit Code: 1\n"
    "\n"
    "Running 2 tests using 1 worker\n"
    "\n"
    "  1) test-e2e\\register.e2e.spec.js:20:3 › register › rejects invalid input ─────\n"
    "\n"
    "    Error: expect(locator).toBeVisible() failed\n"
    "\n"
    "    Locator: getByLabel('用户名')\n"
    "    Expected: visible\n"
    "\n"
    "Exit Code: 1\n"
)


def test_retry_round_filters_to_digest_failed_cases(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            run_of(E2E_DIGEST_FAILURE),
            run_of(E2E_DIGEST_FAILURE),
            passing_run(),
            passing_run(),
        ]
    )
    executor = make_executor(tmp_path, runner, manifest(("E2E", "test-e2e/register.e2e.spec.js")))
    executor.pin_active_layer("E2E")

    first = asyncio.run(executor.run_requested())
    assert first.exit_code == 1
    # The first round is a full (unfiltered) run; the next one filters to the
    # digest's parsed failed case.
    assert runner.case_filters == [None]
    second = asyncio.run(executor.run_requested())
    assert second.exit_code == 1
    assert runner.case_filters == [None, ["register › rejects invalid input"]]

    # A filtered green round cannot close the layer, and the round after it
    # runs unfiltered again - only that full green run closes the layer.
    third = asyncio.run(executor.run_requested())
    assert third.exit_code == 0
    assert not executor.layer_passed("E2E")
    assert "ARC_RETRY_FILTER_NOTE" in third.output
    # Round 3 was itself filtered (round 2 failed again, so the parsed names
    # carried over); its green run resets the filter for the closing round.
    assert runner.case_filters == [
        None,
        ["register › rejects invalid input"],
        ["register › rejects invalid input"],
    ]
    fourth = asyncio.run(executor.run_requested())
    assert fourth.exit_code == 0
    assert executor.layer_passed("E2E")
    assert runner.case_filters[-1] is None


def test_retry_filter_falls_back_to_full_run(tmp_path: Path) -> None:
    runner = ScriptedRunner([failing_run(), passing_run()])
    executor = make_executor(tmp_path, runner, manifest(("E2E", "test-e2e/register.e2e.spec.js")))
    executor.pin_active_layer("E2E")

    first = asyncio.run(executor.run_requested())
    assert first.exit_code == 1
    # The digest parsed nothing (unstructured failure): the retry stays full.
    second = asyncio.run(executor.run_requested())
    assert second.exit_code == 0
    assert runner.case_filters == [None, None]
    assert executor.layer_passed("E2E")


def test_environment_failure_clears_the_retry_filter(tmp_path: Path) -> None:
    env_digest_failure = E2E_DIGEST_FAILURE + "\nError: Cannot find module 'db-helper'\n"
    runner = ScriptedRunner([run_of(env_digest_failure), passing_run()])
    executor = make_executor(tmp_path, runner, manifest(("E2E", "test-e2e/register.e2e.spec.js")))
    executor.pin_active_layer("E2E")

    first = asyncio.run(executor.run_requested())
    assert first.exit_code == 1
    assert first.environment_failure
    # The workspace-repair contract must revalidate broadly: no filter.
    second = asyncio.run(executor.run_requested())
    assert second.exit_code == 0
    assert runner.case_filters == [None, None]


def test_non_e2e_layers_stay_full_even_when_the_digest_parses(tmp_path: Path) -> None:
    vitest_digest_failure = (
        "Exit Code: 1\n"
        "\n=== Backend Vitest Batch ===\n"
        " FAIL tests/unit/test_calc.py > Calc > adds two numbers\n"
        "AssertionError: expected 2 got 1\n"
        "Exit Code: 1\n"
    )
    runner = ScriptedRunner([run_of(vitest_digest_failure), passing_run()])
    executor = make_executor(tmp_path, runner, manifest(("Unit", UNIT_TEST_FILE)))
    executor.pin_active_layer("Unit")

    first = asyncio.run(executor.run_requested())
    assert first.exit_code == 1
    # Per-case filtering is an E2E-runner capability: the unit retry runs
    # full, and its green run closes the layer directly.
    second = asyncio.run(executor.run_requested())
    assert second.exit_code == 0
    assert runner.case_filters == [None, None]
    assert executor.layer_passed("Unit")

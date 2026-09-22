"""The TDD test executor: an explicit interface over the test-execution part
of the TDD loop.

Owns the contracts that used to live inside a closure capturing a dozen
mutable variables (``WorkflowPhaseRunner._run_tdd_for_node``'s
``run_requested_tests``): the per-layer run budget, unknown-file rejection,
in-session layer advancement, stall governance and the environment-failure
repair rules. The orchestrator drives agent sessions on top of this
interface; the DESIGN baseline gate (``_enforce_design_baseline_red``) is an
adapter over the same module — its baseline runs and red/green/unverified
classification flow through :func:`classify_file_state` and
``TddTestExecutor.run_baseline_file`` instead of a second private copy of
the same strategy.

Every run crosses the handler seam as a :class:`~app_type_handler.test_results.TestRunResult`
(the handler fills the structural fields while it still knows the facts);
the executor only appends the model-facing extras (persisted-log pointer,
failure digest) and never re-parses the transcription.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agents.tools.test_failure_digest import (
    build_failure_digest,
    digest_failed_test_names,
    format_failure_digest,
    persist_run_output,
)
from app_type_handler.test_results import TestRunResult
from core.path_compat import normalize_workspace_relative_path
from core.test_types import CANONICAL_TEST_TYPES, canonical_test_type

#: Total ``run_tests`` calls one layer may spend per TDD pass.
TDD_RUN_TESTS_BUDGET = 10
#: Consecutive identical failure fingerprints before stall governance fires.
TDD_STALL_THRESHOLD = 3
#: Ordered test layers the executor schedules (Unit -> Integration -> E2E).
TDD_BATCH_ORDER = CANONICAL_TEST_TYPES

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
#: The handler seam: layer, files, and the retry round's failed-case filter
#: (``None`` = unfiltered full run; only case-filterable runners consume it).
RunGroup = Callable[[str, list[str], "list[str] | None"], Awaitable[TestRunResult]]

#: Per-file verification state: ``"green"`` (passed), ``"red"`` (verifiably
#: failing), or ``None`` (no verified state: never run, or the run stopped at
#: an environment failure).
FileState = str | None


def _rejection(message: str) -> TestRunResult:
    """A refused run_tests request: exit 1 with the gate copy as the output."""

    return TestRunResult(exit_code=1, output=message)


def classify_file_state(result: TestRunResult) -> FileState:
    """Shared red/green/unverified semantics over one structured run result.

    ``green`` = the run passed (exit code 0); ``None`` = the run failed for
    an environmental reason (broken workspace, missing runner — the file is
    UNVERIFIED, not red); ``red`` = a verified failing run. Both the TDD loop
    and the DESIGN baseline gate derive file states through this predicate,
    so "red" means the same thing on both sides.
    """

    if result.exit_code == 0:
        return "green"
    return None if result.environment_failure else "red"


def collect_test_files(tests: list[dict[str, Any]]) -> list[str]:
    """Unique non-empty ``file_path`` values of a manifest, in order."""

    seen: list[str] = []
    for test in tests:
        file_path = str(test.get("file_path", "")).strip()
        if file_path and file_path not in seen:
            seen.append(file_path)
    return seen


@dataclass
class BaselineRun:
    """One system-run batch: transcription plus the shared classification."""

    output: str
    exit_code: int
    state: FileState
    #: Environmental verdict ("" when the failure is the implementation's own).
    environment_failure: str = ""


def _installable_environment_failure(reason: str | None) -> str:
    """Return the missing package name when the environmental failure is installable.

    Environment-failure reasons that name a concrete npm package are
    recoverable through the TDD-stage ``install_dependencies`` tool, so the
    executor grants one extra repair-and-revalidate cycle instead of closing
    the layer. Everything else (empty node_modules, missing runner, browser
    install) has no in-run repair.
    """

    prefix = "missing dependency: "
    text = (reason or "").strip()
    if text.startswith(prefix):
        package = text[len(prefix) :].strip()
        if package:
            return package
    return ""


class TddTestExecutor:
    """System-side test executor behind the TDD loop's ``run_tests`` contract.

    The interface makes the loop's contracts explicit instead of leaving
    them in closure state:

    - **Budget** — ``usage`` / ``budget_exhausted`` per layer; a request
      beyond :data:`TDD_RUN_TESTS_BUDGET` is refused with the closed-layer
      copy and the active layer advances.
    - **Unknown-file rejection** — ``run_requested`` only executes files the
      node's manifest registers for the requested layer.
    - **Layer advancement** — ``active_layer`` / ``layer_passed``; a passing
      full-layer run closes the layer and hands the active role to its
      successor immediately.
    - **Stall copy** — ``fingerprints`` / ``is_stalled``; repeated identical
      failure fingerprints inject the hypothesis-rotation directive into the
      run_tests result.

    The executor never opens agent sessions; the orchestrator decides when a
    layer gets a session and reads the contract surface between them.
    """

    def __init__(
        self,
        *,
        node_id: str,
        workspace_path: str,
        run_group: RunGroup,
        log_cb: LogCallback | None = None,
        agent_name: str = "TestDrivenDeveloper",
    ) -> None:
        self._node_id = node_id
        self._workspace_path = str(workspace_path)
        self._run_group = run_group
        self._log_cb = log_cb
        self._agent_name = agent_name
        # Manifest groups keyed by lowercased raw type; only canonical types
        # are scheduled (non-canonical groups surface via unsupported_layers).
        self._groups: dict[str, list[dict[str, Any]]] = {}
        self._ordered: list[str] = []
        self._usage: dict[str, int] = {}
        self._file_states: dict[str, dict[str, FileState]] = {}
        self._layer_passed: dict[str, bool] = {}
        self._fingerprints: dict[str, list[str]] = {}
        self._install_attempts: dict[str, int] = {}
        self._retry_case_names: dict[str, list[str]] = {}
        self._results: dict[str, TestRunResult] = {}
        self._active_layer: str | None = None
        self._env_failure: str | None = None

    # -- registration and seeding -------------------------------------------

    def register_tests(self, tests: list[dict[str, Any]]) -> list[str]:
        """Group the node manifest into ordered layers; returns the layer order."""

        self._groups = {}
        for test in tests:
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                continue
            self._groups.setdefault(test_type.lower(), []).append(test)
        self._ordered = [t for t in TDD_BATCH_ORDER if self._groups.get(t.lower())]
        self._usage = {t: 0 for t in self._ordered}
        self._file_states = {
            t: {path: None for path in self.layer_files(t)} for t in self._ordered
        }
        self._layer_passed = {t: False for t in self._ordered}
        self._fingerprints = {t: [] for t in self._ordered}
        self._install_attempts = {t: 0 for t in self._ordered}
        # Failed-case names parsed from a layer's most recent failed run
        # digest (#115). A retry round re-runs only these cases through the
        # handler's per-case filter (the web E2E executor's Playwright
        # --grep); empty means run the full layer. Cleared whenever a round
        # passes (the closing round must be a full run) and when the failure
        # was environmental (the repair contract must revalidate broadly) -
        # and unreachable across TDD passes, whose first round always runs
        # the full layer.
        self._retry_case_names = {t: [] for t in self._ordered}
        self._results = {}
        self._active_layer = None
        self._env_failure = None
        return list(self._ordered)

    def seed_file_states(self, baseline: dict[str, Any]) -> None:
        """Seed per-file states from a DESIGN baseline (``{path: state}``).

        Files the DESIGN baseline never saw keep ``None`` and are baseline-
        run by the orchestrator before the layer's first agent session.
        """

        baseline = baseline if isinstance(baseline, dict) else {}
        for layer in self._ordered:
            states = self._file_states[layer]
            for path in states:
                states[path] = baseline.get(path)

    # -- contract surface (queries) ------------------------------------------

    @property
    def ordered_layers(self) -> list[str]:
        return list(self._ordered)

    @property
    def active_layer(self) -> str | None:
        return self._active_layer

    @property
    def environment_failure(self) -> str | None:
        return self._env_failure

    def layer_items(self, test_type: str) -> list[dict[str, Any]]:
        return self._groups.get(str(test_type).lower(), [])

    def layer_files(self, test_type: str) -> list[str]:
        return collect_test_files(self.layer_items(test_type))

    def registered_files(self, test_type: str) -> set[str]:
        return {
            str(item.get("file_path", "") or "").strip()
            for item in self.layer_items(test_type)
            if str(item.get("file_path", "") or "").strip()
        }

    def usage(self, test_type: str) -> int:
        return self._usage.get(test_type, 0)

    def budget_exhausted(self, test_type: str) -> bool:
        return self.usage(test_type) >= TDD_RUN_TESTS_BUDGET

    def layer_passed(self, test_type: str) -> bool:
        return self._layer_passed.get(test_type, False)

    def file_states(self, test_type: str) -> dict[str, FileState]:
        return dict(self._file_states.get(test_type, {}))

    def layer_result(self, test_type: str) -> TestRunResult | None:
        """The layer's latest run result (agent run or system regression)."""

        return self._results.get(test_type)

    def fingerprints(self, test_type: str) -> list[str]:
        return list(self._fingerprints.get(test_type, []))

    def is_stalled(self, test_type: str) -> bool:
        """Whether the layer's last ``TDD_STALL_THRESHOLD`` fingerprints are identical."""

        history = self._fingerprints.get(test_type, [])
        recent = history[-TDD_STALL_THRESHOLD :]
        return len(history) >= TDD_STALL_THRESHOLD and len(set(recent)) == 1

    def unsupported_layers(self) -> list[str]:
        """Non-canonical manifest groups that will never be scheduled."""

        canonical = {t.lower() for t in TDD_BATCH_ORDER}
        return sorted(set(self._groups) - canonical)

    # -- contract surface (orchestrator-driven mutations) ---------------------

    def pin_active_layer(self, test_type: str) -> None:
        """Open a layer for in-session ``run_requested`` calls."""

        self._active_layer = test_type

    def unpin_active_layer(self) -> None:
        self._active_layer = None

    def record_file_state(self, test_type: str, path: str, state: FileState) -> None:
        self._file_states.setdefault(test_type, {})[path] = state

    def mark_layer_passed(self, test_type: str) -> None:
        self._layer_passed[test_type] = True

    def set_environment_failure(self, reason: str | None) -> None:
        self._env_failure = reason

    # -- execution -------------------------------------------------------------

    async def run_requested(
        self,
        requested_type: str | None = None,
        requested_files: list[str] | None = None,
    ) -> TestRunResult:
        """Run the agent's requested tests; returns the structured run result.

        This is the contract the TDD agent experiences through the
        ``run_tests`` tool: budget consumption and exhaustion copy,
        unknown-file rejection, cross-layer/closed-layer rejection, in-session
        layer advancement, stall governance and the environment-failure
        repair-and-revalidate rules.
        """

        requested = str(requested_type or "").strip()
        if self._active_layer is None:
            return _rejection(
                "Exit Code: 1\n"
                "STDERR:\n"
                "No active TDD test layer is currently scheduled.\n"
            )
        if requested.lower() in {"", "all", "current", "next"}:
            selected_type = self._active_layer
        else:
            selected_type = canonical_test_type(requested)
            if selected_type is None or selected_type not in self._ordered:
                return _rejection(
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"Unsupported current-node test_type={requested!r}. "
                    f"Available ordered layers: {', '.join(self._ordered)}.\n"
                )
            if selected_type != self._active_layer:
                return _rejection(
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"The active TDD layer is `{self._active_layer}`, but run_tests requested `{selected_type}`. "
                    "The system attempts layers in Unit -> Integration -> E2E order with independent budgets.\n"
                )

        selected_files = [
            path
            for value in (requested_files or self.layer_files(selected_type))
            if (path := normalize_workspace_relative_path(value, self._workspace_path))
        ]
        registered = self.registered_files(selected_type)
        unknown = [path for path in selected_files if path not in registered]
        if unknown:
            return _rejection(
                "Exit Code: 1\n"
                "STDERR:\n"
                f"run_tests({selected_type}) may only execute registered {selected_type} tests for the current node. "
                f"Unknown files: {', '.join(unknown)}\n"
            )
        used = self.usage(selected_type)
        if used >= TDD_RUN_TESTS_BUDGET:
            await self._log(
                f"`run_tests` {selected_type} budget exhausted at {used}/{TDD_RUN_TESTS_BUDGET}.",
                status="error",
            )
            if self._env_failure is not None:
                return _rejection(
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"run_tests budget exhausted for {selected_type}: {used}/{TDD_RUN_TESTS_BUDGET}.\n"
                    "The workspace failed for an environmental reason. Do not call run_tests again; "
                    "return your report now.\n"
                )
            closed_next_index = self._ordered.index(selected_type) + 1
            closed_next_type = (
                self._ordered[closed_next_index] if closed_next_index < len(self._ordered) else None
            )
            if closed_next_type is not None:
                # Advance the active layer the moment its budget is gone. The
                # prompt promises "the system moves to later layers even if an
                # earlier layer fails or exhausts its budget"; without this
                # in-session advance the model stays locked to the closed
                # layer (next-layer requests are rejected) and deadloops on
                # budget-exhausted responses (observed on the 12306
                # benchmark: ~5 minutes of pure model turns per node). The
                # advance cannot make the outer scheduler reopen this layer:
                # it visits each layer once and its session loop breaks on
                # the budget check, so a closed layer never runs again.
                self._active_layer = closed_next_type
                return _rejection(
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"The {selected_type} layer is closed: run_tests budget exhausted at "
                    f"{used}/{TDD_RUN_TESTS_BUDGET}.\n"
                    f"The system has advanced the active layer to `{closed_next_type}`; "
                    f"`run_tests(test_type='{closed_next_type}')` now targets it.\n"
                    f"Apply a concrete repair before spending the {closed_next_type} budget, or end your turn "
                    f"with a concise summary of the failing {selected_type} tests and the next edit target.\n"
                )
            return _rejection(
                "Exit Code: 1\n"
                "STDERR:\n"
                f"The {selected_type} layer is closed: run_tests budget exhausted at "
                f"{used}/{TDD_RUN_TESTS_BUDGET}. This was the last scheduled layer.\n"
                "End your turn now with a concise summary of the failing tests and the next edit target. "
                "Do not call run_tests again.\n"
            )
        self._usage[selected_type] = used + 1
        await self._log(
            f"`run_tests` {selected_type} usage {self._usage[selected_type]}/{TDD_RUN_TESTS_BUDGET}."
        )
        # Retry round (#115): the previous round's digest parsed failing case
        # names, so re-run only those. First round, cross-pass rounds and any
        # round after an unparseable/environmental failure carry no filter and
        # run the full layer. Per-case filtering is an E2E-runner capability
        # today, so other layers always run full - and run_was_case_filtered
        # (which gates the layer-closing verdict below) must only fire when
        # the runner really filtered, never on an ignored filter.
        case_filter_applies = selected_type.lower() == "e2e"
        prior_failed_names = self._retry_case_names.get(selected_type, []) if case_filter_applies else []
        run_was_case_filtered = bool(prior_failed_names)
        if run_was_case_filtered:
            await self._log(
                (
                    f"`run_tests` {selected_type} retry filters to the {len(prior_failed_names)} "
                    "failing case(s) parsed from the previous round's digest."
                )
            )
        result = await self._run_group(
            selected_type,
            selected_files,
            list(prior_failed_names) if prior_failed_names else None,
        )
        passed = result.passed_run
        # Persist every run's raw output under .arc/tdd_runs (ignored by
        # Git checkpoints/merges) and expose it to the agent: in-session
        # via a pointer line, cross-session via the structured digest in
        # the failure handoff. This is what lets a follow-up session
        # re-localize a failure by reading one file instead of spending
        # budget re-running tests to see output it has already seen.
        run_log_path = ""
        try:
            run_log_path = persist_run_output(
                self._workspace_path,
                self._node_id,
                selected_type,
                used + 1,
                result.output,
            )
        except OSError as exc:
            await self._log(
                f"Failed to persist run output log: {exc}",
                status="warning",
            )
        if run_log_path:
            result.run_log_path = run_log_path
            result.output += (
                f"\n\nARC_RUN_OUTPUT_LOG: the complete raw output of this run is saved at "
                f"`{run_log_path}`. Read that file for the full output of this attempt "
                "instead of re-running the tests.\n"
            )
        if not passed:
            # Structured per-test digest appended to the tool result: the
            # model sees each failed test's location and expected/received
            # up front instead of mining the long raw output for them.
            failure_digest = build_failure_digest(result.output)
            # Remember the parsed names for the next round's case filter.
            # An environmental failure clears them: the workspace-repair
            # contract must revalidate the whole layer, not just the cases
            # that happened to report before the environment broke. An
            # unparseable digest also degrades to the full run.
            if case_filter_applies:
                self._retry_case_names[selected_type] = (
                    [] if result.environment_failure else digest_failed_test_names(failure_digest)
                )
            result.output += (
                "\n\n"
                + format_failure_digest(
                    failure_digest,
                    test_type=selected_type,
                    raw_output_path=run_log_path or None,
                    fingerprint=result.fingerprint,
                    environment_failure=result.environment_failure,
                    build=result.build_note,
                    served=result.served_verdict,
                )
                + "\n"
            )
        else:
            # A passing round resets the filter so the next round
            # revalidates the full layer; a case-filtered green round only
            # proves the previously failing cases now pass.
            self._retry_case_names[selected_type] = []
        await self._log(
            (
                "run_tests raw output\n"
                f"test_type={selected_type}\n"
                f"attempt={self._usage[selected_type]}/{TDD_RUN_TESTS_BUDGET}\n"
                f"test_files={json.dumps(selected_files, ensure_ascii=False)}\n"
                "----- BEGIN RAW TEST OUTPUT -----\n"
                f"{result.output.rstrip()}\n"
                "----- END RAW TEST OUTPUT -----"
            ),
            status="debug",
        )
        await self._log(
            (
                f"`run_tests` {selected_type} {'passed' if passed else 'failed'} "
                f"with Exit Code: {result.exit_code} "
                f"on attempt {self._usage[selected_type]}/{TDD_RUN_TESTS_BUDGET}: "
                f"{', '.join(selected_files)}"
            ),
            status="ok" if passed else "error",
        )
        self._results[selected_type] = result
        if passed:
            for path in selected_files:
                self._file_states[selected_type][path] = "green"
            # A layer passes only through a passing run that covered every
            # registered file; a passing subset run keeps the layer open.
            # A case-filtered run (#115) is such a subset at case level:
            # only the previously failing cases were re-verified, so the
            # layer-closing verdict stays with a full (unfiltered) run.
            if not run_was_case_filtered and registered and set(selected_files) >= registered:
                self._layer_passed[selected_type] = True
            if self._env_failure is not None:
                # The reported environment failure was repaired (e.g. the
                # agent created a missing local module or fixed an import);
                # the normal layer flow resumes.
                await self._log(
                    (
                        f"`run_tests` {selected_type} passed after the reported environment "
                        f"failure ({self._env_failure}) was repaired; resuming normal TDD flow."
                    )
                )
                self._env_failure = None
        else:
            for path in selected_files:
                if self._file_states[selected_type].get(path) != "green":
                    self._file_states[selected_type][path] = "red"
            self._fingerprints[selected_type].append(result.fingerprint)
            failure_now = result.environment_failure or None
            if self._env_failure is None:
                if failure_now:
                    self._env_failure = failure_now
                    await self._log(
                        (
                            f"`run_tests` {selected_type} failed for an environmental reason "
                            f"({self._env_failure}); the workspace is broken, not the "
                            "implementation. Allowing one repair-and-revalidate attempt."
                        ),
                        status="error",
                    )
            elif failure_now:
                # Still environmental after the one re-validation attempt.
                # A missing-package failure is now recoverable through the
                # install_dependencies tool, so it gets one extra
                # repair-and-revalidate cycle instead of burning the layer;
                # every other environmental failure (empty node_modules,
                # missing runner, broken browser install) has no in-run
                # repair, so spend the rest of this layer's budget up
                # front and let the outer loop break instead of re-running
                # a doomed command.
                installable = _installable_environment_failure(failure_now)
                if self._install_attempts.get(selected_type, 0) < 1 and installable:
                    self._install_attempts[selected_type] = (
                        self._install_attempts.get(selected_type, 0) + 1
                    )
                    await self._log(
                        (
                            f"`run_tests` {selected_type} still reports {failure_now}; the missing "
                            "package can be installed with the `install_dependencies` tool. "
                            "Allowing one install-and-revalidate attempt."
                        ),
                        status="error",
                    )
                    self._env_failure = failure_now
                else:
                    await self._log(
                        (
                            f"`run_tests` {selected_type} still fails for an environmental reason "
                            f"({failure_now}); stopping the TDD loop instead of retrying."
                        ),
                        status="error",
                    )
                    self._usage[selected_type] = TDD_RUN_TESTS_BUDGET
            else:
                # The run now fails on assertions, not the environment: the
                # earlier environment failure was repaired, so the normal
                # retry loop resumes.
                self._env_failure = None
        next_index = self._ordered.index(selected_type) + 1
        next_type = self._ordered[next_index] if next_index < len(self._ordered) else None
        # Micro-loop status: per-file red/green state the agent sees on
        # every run_tests result, so each file's red -> green transition is
        # an explicit, verifiable step rather than batch soup.
        layer_file_states = self._file_states[selected_type]
        # Only verified failures are "still red" - never-run files (None)
        # are pending work, not repair targets; reporting them as red
        # would send the agent after files with no failure evidence yet.
        still_red = sorted(path for path, state in layer_file_states.items() if state == "red")
        not_yet_run = sorted(path for path, state in layer_file_states.items() if state is None)
        result.output += (
            "\n\nARC_TEST_FILE_STATUS:\n"
            f"- Layer `{selected_type}` per-file state:\n"
            + "\n".join(
                f"  - {path}: {state or 'not yet run'}"
                for path, state in layer_file_states.items()
            )
            + "\n"
        )
        # Stall governance: three consecutive identical failure fingerprints
        # mean the last repairs did not change the failure - the agent is
        # stuck on one hypothesis. Force an explicit rotation.
        if not passed and self.is_stalled(selected_type):
            recent = self._fingerprints[selected_type][-TDD_STALL_THRESHOLD :]
            await self._log(
                (
                    f"`run_tests` {selected_type} failure fingerprint stalled for "
                    f"{len(recent)} consecutive attempts; requiring hypothesis rotation."
                ),
                status="error",
            )
            result.output += (
                "- STALL DETECTED: the same failure fingerprint has repeated "
                f"{len(recent)} times in a row. Your recent edits are not changing the failure. "
                "Before the next run_tests call, you MUST rotate your hypothesis: "
                "(1) re-classify the failure (implementation logic, boundary wiring, selector/render "
                "state, persistence/test database, framework/config, or test content); "
                "(2) list the hypotheses you have already tried; "
                "(3) pick a DIFFERENT layer of the UI/API/FUNC/DB chain to edit, or a different "
                "fix approach within the same layer; "
                "(4) only then make the repair and re-run.\n"
            )
        if passed and (still_red or not_yet_run):
            # A passing run on a subset of files: verified-red files stay
            # repair targets, never-run files are simply the next work.
            if still_red:
                result.output += (
                    f"- {len(still_red)} file(s) in this layer are still red: {', '.join(still_red)}. "
                    "The layer passes only when every file is green and a final full-layer run passes.\n"
                )
            if not_yet_run:
                result.output += (
                    f"- {len(not_yet_run)} file(s) in this layer have not been run yet: "
                    f"{', '.join(not_yet_run)}.\n"
                )
        if run_was_case_filtered:
            # The agent must know this round was partial at case level: on
            # a pass it must spend one more round on the unfiltered layer
            # (if it stops here, the orchestrator's system-run regression
            # closes the layer instead); on a failure the digest above only
            # covers the re-run cases.
            if passed:
                result.output += (
                    "\nARC_RETRY_FILTER_NOTE:\n"
                    f"- This round re-ran only the previously failing {selected_type} case(s); "
                    "the layer is not closed yet. Call run_tests once more for the full "
                    "layer - the next round runs unfiltered, and a passing full run closes "
                    "the layer.\n"
                )
            else:
                result.output += (
                    "\nARC_RETRY_FILTER_NOTE:\n"
                    f"- This round re-ran only the previously failing {selected_type} case(s); "
                    "cases that passed in earlier rounds were not re-verified here. The digest "
                    "above lists the still-failing case(s) the next round will re-run.\n"
                )
        if self._layer_passed[selected_type] and next_type:
            # Advance immediately instead of waiting for the session to
            # end. Otherwise a model that keeps polling `run_tests` after a
            # pass re-runs the passing layer until its budget is gone and
            # then deadloops on rejected next-layer requests (observed on
            # the 12306 benchmark: attempts 6-10 re-ran an already-passing
            # batch, then the session burned minutes on "the system says it
            # will advance but never does").
            self._active_layer = next_type
            # The next layer's files were never baseline-verified in this
            # session; the outer scheduler will baseline them before their
            # first agent session (in-session advances pin the active
            # layer, and the baseline loop only runs per layer).
            result.output += (
                "\nARC_TEST_LAYER_STATUS:\n"
                f"- {selected_type} passed (full layer).\n"
                f"- The system has advanced the active layer to `{next_type}`; "
                f"`run_tests(test_type='{next_type}')` now targets it.\n"
                f"- The {selected_type} layer is closed: further `{selected_type}` calls are rejected "
                "without consuming budget.\n"
                "- Do not return IMPLEMENTED until all scheduled layers have been attempted and passed.\n"
            )
        elif self._layer_passed[selected_type]:
            result.output += (
                "\nARC_TEST_LAYER_STATUS:\n"
                f"- {selected_type} passed (full layer).\n"
                "- This is the last scheduled test layer. You may return IMPLEMENTED only if all earlier scheduled layers also passed.\n"
            )
        elif self._env_failure and self._usage[selected_type] >= TDD_RUN_TESTS_BUDGET:
            result.output += (
                "\nARC_TEST_LAYER_STATUS:\n"
                f"- {selected_type} is still failing for an environmental reason "
                f"({self._env_failure}).\n"
                "- This layer is closed: the one repair-and-revalidate attempt has been used.\n"
                "- Do not call run_tests again; return a short report naming the missing "
                "dependency instead.\n"
            )
        elif self._env_failure:
            result.output += (
                "\nARC_TEST_LAYER_STATUS:\n"
                f"- {selected_type} could not run: {self._env_failure}.\n"
                "- This is your one repair-and-revalidate attempt for this environment failure.\n"
                "- First decide the root cause. If it is fixable with a file edit (create a "
                "missing local module, correct a wrong relative import, add a missing npm "
                "script), make that edit and call run_tests once more to re-validate.\n"
                "- If instead it names a package that must be installed, you cannot fix it "
                "mid-run: end your turn with a short report naming the missing dependency "
                "and do not call run_tests again.\n"
            )
        return result

    async def run_baseline_file(self, test_type: str, file_path: str) -> BaselineRun:
        """System-run one file's baseline and classify its state (no budget).

        The shared primitive behind both the TDD loop's per-layer baseline
        verification and the DESIGN baseline gate's per-file runs. The state
        follows :func:`classify_file_state` (environmental failures stay
        unverified); callers that need a different recording policy override
        it explicitly via :meth:`record_file_state`.
        """

        result = await self._run_group(test_type, [file_path], None)
        state = classify_file_state(result)
        self._file_states.setdefault(test_type, {})[file_path] = state
        return BaselineRun(
            output=result.output,
            exit_code=result.exit_code,
            state=state,
            environment_failure=result.environment_failure,
        )

    async def run_full_layer(self, test_type: str) -> BaselineRun:
        """System-run the whole layer once (closing regression).

        Records the layer result and closes the layer on a passing run. A
        failing combined run demotes every non-green file to ``red`` (a
        combined failure may implicate any file that is not already verified
        green); verified-green files keep their state so already-passing
        files are not relabeled as repair targets.
        """

        files = self.layer_files(test_type)
        result = await self._run_group(test_type, files, None)
        self._results[test_type] = result
        state = classify_file_state(result)
        if state == "green":
            self._layer_passed[test_type] = True
        else:
            layer_states = self._file_states.setdefault(test_type, {})
            for path in files:
                if layer_states.get(path) != "green":
                    layer_states[path] = "red"
        return BaselineRun(
            output=result.output,
            exit_code=result.exit_code,
            state=state,
            environment_failure=result.environment_failure,
        )

    async def _log(self, message: str, status: str | None = None) -> None:
        if self._log_cb is None:
            return
        result = self._log_cb(self._agent_name, message, status, self._node_id)
        if hasattr(result, "__await__"):
            await result


__all__ = [
    "TDD_BATCH_ORDER",
    "TDD_RUN_TESTS_BUDGET",
    "TDD_STALL_THRESHOLD",
    "BaselineRun",
    "FileState",
    "TddTestExecutor",
    "classify_file_state",
    "collect_test_files",
]

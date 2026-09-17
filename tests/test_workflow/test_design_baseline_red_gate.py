"""DESIGN-phase baseline RED gate over the freshly generated test manifest.

Drives ``WorkflowPhaseRunner.run_design_phase`` with stub stage adapters and a
``FakeAppHandler`` whose ``run_test_group`` outputs are scripted per call. The
gate runs every manifest file once while the workspace only contains DESIGN
skeletons; a file that already passes is rejected back to the TestGenerator
(delete-or-rework, bounded rounds), and surviving green files fail the DESIGN
phase instead of being waved through by the IMPLEMENT tautology fast path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core import sessions
from core.phases import DESIGN_BASELINE_MAX_REJECTIONS, WorkflowPhaseRunner
from tests.helpers.faux import (
    FakeAppHandler,
    failing_test_output,
    passing_test_output,
)

# Reuse the process-wide runtime fixture so WorkflowPhaseRunner.traceability,
# core.sessions and context_pipeline all resolve inside tmp_project_dir.
from tests.test_agents.conftest import arc_runtime  # noqa: F401


UNIT_TEST_FILE = "tests/unit/test_calc.py"
UNIT_TEST_FILE_2 = "tests/unit/test_extra.py"


class _StubDesigner:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def run(self, node_id: str, requirement_data: dict) -> dict:
        return self.payload


class _StubGenerator:
    """Scripted TestGenerator: returns queued manifests and records rejections."""

    def __init__(self, manifests: list[list[dict[str, Any]]]) -> None:
        self.app_handler = None
        self._manifests = list(manifests)
        self.rejection_calls: list[list[dict[str, Any]]] = []

    async def run(self, node_id: str, requirement_data: dict, **kwargs: Any) -> tuple:
        return (self._manifests.pop(0), "{}")

    async def repair_green_baseline(
        self,
        node_id: str,
        requirement_data: dict,
        *,
        green_evidence: list[dict[str, Any]],
        previous_manifest: list[dict[str, Any]],
    ) -> tuple:
        self.rejection_calls.append([dict(item) for item in green_evidence])
        if self._manifests:
            return (self._manifests.pop(0), "{}")
        return ([], "{}")


class _StubTDD:
    def __init__(self) -> None:
        self.app_handler = None


def _manifest_item(test_id: str, file_path: str, test_type: str = "Unit") -> dict[str, Any]:
    return {
        "test_id": test_id,
        "req_id": "REQ-BASE-1",
        "interface_ids": [],
        "type": test_type,
        "file_path": file_path,
        "first_line": "def test_x():",
    }


def _make_runner(
    tmp_project_dir: Path,
    generator: _StubGenerator,
    fake: FakeAppHandler,
) -> tuple[WorkflowPhaseRunner, list[tuple]]:
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    requirements_dir = tmp_project_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    runner = WorkflowPhaseRunner(
        workspace_path=str(tmp_project_dir),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=_StubDesigner(
            {
                "summary": "Calculator contract.",
                "interfaces": [
                    {
                        "interface_id": "IF-CALC",
                        "type": "FUNC",
                        "name": "add",
                        "responsibility": "Add two integers",
                        "file_path": "src/calc.py",
                        "first_line": "def add(a, b):",
                    }
                ],
                "files_written": [],
            }
        ),
        test_generator=generator,
        test_driven_developer=_StubTDD(),
        log_cb=log_cb,
    )
    runner.app_handler = fake
    return runner, logs


def _seed_leaf_requirement(runtime, node_id: str) -> None:
    runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Calculator", "description": "Add two numbers"}
    )


def _run_design(runner: WorkflowPhaseRunner, node_id: str) -> bool:
    return asyncio.run(
        runner.run_design_phase(node_id, {"name": "Calculator", "description": "Add two numbers"})
    )


# ---------------------------------------------------------------------------
# Intended state: all generated files are RED before implementation.
# ---------------------------------------------------------------------------


def test_design_baseline_all_red_passes_without_rejection(tmp_project_dir, arc_runtime) -> None:
    node_id = "REQ-BASE-RED"
    _seed_leaf_requirement(arc_runtime, node_id)
    generator = _StubGenerator([[_manifest_item("T1", UNIT_TEST_FILE)]])
    fake = FakeAppHandler([failing_test_output()])
    runner, logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is True
    assert generator.rejection_calls == []
    # The gate ran exactly one baseline per manifest file; the agent never saw it.
    assert fake.calls == [("Unit", [UNIT_TEST_FILE])]
    assert any("Baseline RED verification complete" in entry[1] for entry in logs)
    # The per-file states are persisted for the IMPLEMENT baseline seeding.
    baseline = sessions.load_node_session(node_id).get("design_baseline")
    assert baseline == {UNIT_TEST_FILE: "red"}


def test_design_baseline_green_file_is_rejected_and_repaired(tmp_project_dir, arc_runtime) -> None:
    """One green file: the gate names it, the repair reworks it red."""
    node_id = "REQ-BASE-GREEN-REPAIR"
    _seed_leaf_requirement(arc_runtime, node_id)
    generator = _StubGenerator(
        [
            [_manifest_item("T1", UNIT_TEST_FILE), _manifest_item("T2", UNIT_TEST_FILE_2)],
            # Repair round: reworked file 1 (same id/path), file 2 untouched.
            [_manifest_item("T1", UNIT_TEST_FILE), _manifest_item("T2", UNIT_TEST_FILE_2)],
        ]
    )
    # file1 green at the first baseline, file2 red; after the repair the
    # reworked file1 is re-run and is red now.
    fake = FakeAppHandler([passing_test_output(), failing_test_output(), failing_test_output()])
    runner, logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is True
    # The rejection carried the exact green file list.
    assert len(generator.rejection_calls) == 1
    assert [item["file_path"] for item in generator.rejection_calls[0]] == [UNIT_TEST_FILE]
    assert any("Green baseline rejection round" in entry[1] for entry in logs)
    # Baseline ran file1 then file2 (manifest order); the repair re-ran only
    # the still-registered rejected file, not file2.
    assert fake.calls == [
        ("Unit", [UNIT_TEST_FILE]),
        ("Unit", [UNIT_TEST_FILE_2]),
        ("Unit", [UNIT_TEST_FILE]),
    ]
    baseline = sessions.load_node_session(node_id).get("design_baseline")
    assert baseline[UNIT_TEST_FILE] == "red"
    assert baseline[UNIT_TEST_FILE_2] == "red"


def test_design_baseline_green_file_deleted_by_repair_passes(tmp_project_dir, arc_runtime) -> None:
    """The repair may legitimately drop the tautological file from the manifest."""
    node_id = "REQ-BASE-GREEN-DELETE"
    _seed_leaf_requirement(arc_runtime, node_id)
    generator = _StubGenerator(
        [
            [_manifest_item("T1", UNIT_TEST_FILE), _manifest_item("T2", UNIT_TEST_FILE_2)],
            # Repair round: file1 deleted from the manifest entirely.
            [_manifest_item("T2", UNIT_TEST_FILE_2)],
        ]
    )
    fake = FakeAppHandler([passing_test_output(), failing_test_output()])
    runner, _logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is True
    assert len(generator.rejection_calls) == 1
    # Baseline ran file1 (green) then file2 (red); the deleted file1 was
    # never re-run because it left the manifest.
    assert fake.calls == [("Unit", [UNIT_TEST_FILE]), ("Unit", [UNIT_TEST_FILE_2])]
    # The deleted file is gone from both the stored manifest and the baseline.
    stored = arc_runtime.traceability.list_tests(req_id=node_id)
    assert [item["file_path"] for item in stored] == [UNIT_TEST_FILE_2]
    baseline = sessions.load_node_session(node_id).get("design_baseline")
    assert UNIT_TEST_FILE not in baseline


def test_design_baseline_exhausts_rejections_and_fails_design(tmp_project_dir, arc_runtime) -> None:
    node_id = "REQ-BASE-EXHAUST"
    _seed_leaf_requirement(arc_runtime, node_id)
    manifests = [[_manifest_item("T1", UNIT_TEST_FILE)]] * (DESIGN_BASELINE_MAX_REJECTIONS + 1)
    generator = _StubGenerator(manifests)
    # Every baseline run of the file passes; the repair never turns it red.
    fake = FakeAppHandler([passing_test_output()] * (DESIGN_BASELINE_MAX_REJECTIONS + 1))
    runner, logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is False
    assert len(generator.rejection_calls) == DESIGN_BASELINE_MAX_REJECTIONS
    errors = [entry for entry in logs if entry[2] == "error"]
    assert any("DESIGN failed" in entry[1] for entry in errors)
    assert UNIT_TEST_FILE in next(entry[1] for entry in errors if "DESIGN failed" in entry[1])
    # A failed gate must not leave the node in a designed state.
    assert sessions.load_node_session(node_id).get("phase_status", {}).get("design") != "completed"


def test_design_baseline_prior_implementation_allows_green(tmp_project_dir, arc_runtime) -> None:
    """A retry over the node's own landed implementation may legitimately be green.

    The DESIGN phase of a full retry sees the workspace with the previous
    run's implementation committed (an ``<node> (implement): ...`` git
    checkpoint); rejecting green tests there would demand impossible work.
    """
    node_id = "REQ-BASE-PRIOR-IMPL"
    _seed_leaf_requirement(arc_runtime, node_id)
    # Real git: initialize a repo in the workspace and commit an implement
    # checkpoint for this node, the way the compiler does between phases.
    import subprocess

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(tmp_project_dir), check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.name", "arc-test")
    _git("config", "user.email", "arc-test@example.com")
    (tmp_project_dir / "src").mkdir(parents=True, exist_ok=True)
    (tmp_project_dir / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git("add", ".")
    _git("commit", "-q", "-m", f"{node_id} (implement): Calculator")

    generator = _StubGenerator([[_manifest_item("T1", UNIT_TEST_FILE)]])
    fake = FakeAppHandler([passing_test_output()])
    runner, logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is True
    assert generator.rejection_calls == []
    assert any("implement checkpoint" in entry[1] for entry in logs)
    baseline = sessions.load_node_session(node_id).get("design_baseline")
    assert baseline == {UNIT_TEST_FILE: "green"}


def test_design_baseline_empty_manifest_skips_gate(tmp_project_dir, arc_runtime) -> None:
    node_id = "REQ-BASE-EMPTY"
    _seed_leaf_requirement(arc_runtime, node_id)
    generator = _StubGenerator([[]])
    fake = FakeAppHandler()
    runner, _logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is True
    assert fake.calls == []


def test_design_baseline_repair_returning_invalid_items_fails_design(tmp_project_dir, arc_runtime) -> None:
    """A repair whose items all fail manifest validation must not masquerade
    as a legitimate empty manifest (which would silently strip coverage)."""
    node_id = "REQ-BASE-REPAIR-JUNK"
    _seed_leaf_requirement(arc_runtime, node_id)

    class _JunkGenerator(_StubGenerator):
        async def repair_green_baseline(self, *args: Any, **kwargs: Any) -> tuple:
            # Record the call, then return items that _prepare_tests drops
            # (no test_id/file_path -> filtered to an empty manifest).
            await super().repair_green_baseline(*args, **kwargs)
            return ([{"type": "Unit"}], "{}")

    generator = _JunkGenerator([[_manifest_item("T1", UNIT_TEST_FILE)]])
    fake = FakeAppHandler([passing_test_output()])
    runner, logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is False
    errors = [entry for entry in logs if entry[2] == "error"]
    assert any("only invalid manifest item(s)" in entry[1] for entry in errors)


def test_design_baseline_repair_explicitly_empty_manifest_passes(tmp_project_dir, arc_runtime) -> None:
    """An explicitly empty repair manifest is a valid full delete."""
    node_id = "REQ-BASE-REPAIR-EMPTY"
    _seed_leaf_requirement(arc_runtime, node_id)

    class _EmptyDeleteGenerator(_StubGenerator):
        async def repair_green_baseline(self, *args: Any, **kwargs: Any) -> tuple:
            await super().repair_green_baseline(*args, **kwargs)
            return ([], "{}")

    generator = _EmptyDeleteGenerator([[_manifest_item("T1", UNIT_TEST_FILE)]])
    fake = FakeAppHandler([passing_test_output()])
    runner, _logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    assert ok is True
    stored = arc_runtime.traceability.list_tests(req_id=node_id)
    assert stored == []
    assert sessions.load_node_session(node_id).get("design_baseline") == {}


def test_design_baseline_prior_implementation_requires_id_anchor(tmp_project_dir, arc_runtime) -> None:
    """A sibling node whose id is a superstring must not flip the anchor.

    REQ-1's gate must stay rejecting when only REQ-10's implement checkpoint
    exists (the pre-fix substring check ``node_id in stdout`` matched both).
    """
    import subprocess

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(tmp_project_dir), check=True, capture_output=True)

    node_id = "REQ-BASE-SUB"
    sibling_id = "REQ-BASE-SUB-2"
    for target in (node_id, sibling_id):
        _seed_leaf_requirement(arc_runtime, target)
    _git("init", "-q")
    _git("config", "user.name", "arc-test")
    _git("config", "user.email", "arc-test@example.com")
    (tmp_project_dir / "src").mkdir(parents=True, exist_ok=True)
    (tmp_project_dir / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git("add", ".")
    # Only the SIBLING's implement checkpoint exists.
    _git("commit", "-q", "-m", f"{sibling_id} (implement): Sibling feature")

    class _SteadyGenerator(_StubGenerator):
        """Every repair round keeps returning the same (still-green) manifest."""

        async def repair_green_baseline(self, *args: Any, **kwargs: Any) -> tuple:
            await super().repair_green_baseline(*args, **kwargs)
            self._manifests.append([_manifest_item("T1", UNIT_TEST_FILE)])
            return ([_manifest_item("T1", UNIT_TEST_FILE)], "{}")

    generator = _SteadyGenerator([[_manifest_item("T1", UNIT_TEST_FILE)]])
    fake = FakeAppHandler([passing_test_output()] * 3)
    runner, logs = _make_runner(tmp_project_dir, generator, fake)

    ok = _run_design(runner, node_id)

    # The sibling's checkpoint must NOT legitimize this node's green file:
    # the gate still rejects (2 bounded rounds, then DESIGN fails).
    assert ok is False
    assert len(generator.rejection_calls) == DESIGN_BASELINE_MAX_REJECTIONS
    assert not any("implement checkpoint" in entry[1] for entry in logs)


# ---------------------------------------------------------------------------
# IMPLEMENT seeding: the DESIGN baseline states must be reused, not re-run.
# ---------------------------------------------------------------------------


def test_implement_baseline_reuses_design_states(tmp_project_dir, arc_runtime) -> None:
    from agents.test_driven_developer import TestDrivenDeveloper
    from tests.helpers.faux import FauxChatModel, faux_text, faux_tool_call

    node_id = "REQ-BASE-REUSE"
    _seed_leaf_requirement(arc_runtime, node_id)
    generator = _StubGenerator([[_manifest_item("T1", UNIT_TEST_FILE)]])
    # DESIGN baseline: red. Then the IMPLEMENT layer: agent failing run,
    # agent passing full-layer run.
    fake = FakeAppHandler([failing_test_output(), failing_test_output(), passing_test_output()])
    runner, _logs = _make_runner(tmp_project_dir, generator, fake)

    assert _run_design(runner, node_id) is True

    # Drive IMPLEMENT through the real TDD scheduler with a faux model.
    model = FauxChatModel(
        responses=[
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a - b\n"},
                call_id="c1",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="c2"),
            faux_tool_call(
                "write_file",
                {"file_path": "/workspace/src/calc.py", "content": "def add(a, b):\n    return a + b\n"},
                call_id="c3",
            ),
            faux_tool_call("run_tests", {"test_type": "Unit"}, call_id="c4"),
            faux_text("IMPLEMENTED"),
        ]
    )
    # DESIGN baseline: red. Then the IMPLEMENT layer: agent failing run,
    # agent passing full-layer run.
    fake.queue(failing_test_output(), passing_test_output())
    tdd = TestDrivenDeveloper(
        model=model,
        workspace_root=str(tmp_project_dir),
        requirement_path=str(tmp_project_dir / "requirements" / "req.md"),
        app_type="web",
        app_handler=fake,
    )
    runner.test_driven_developer = tdd
    tdd.app_handler = fake

    ok = asyncio.run(
        runner.run_implement_phase(node_id, {"name": "Calculator", "description": "Add two numbers"})
    )

    assert ok is True
    # Total run_test_group calls: 1 DESIGN baseline + 2 IMPLEMENT runs. The
    # IMPLEMENT scheduler must NOT re-run the baseline it already knows is red.
    assert len(fake.calls) == 3

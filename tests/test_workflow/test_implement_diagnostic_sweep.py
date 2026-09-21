"""The IMPLEMENT checkpoint must not deliver undeclared diagnostic test files.

Issue #89 (simple-ticketing arc-output1): a TDD agent wrote render-probe
diagnostics under ``frontend/tests/`` (``diag.test.tsx``,
``RegisterPage.diag.test.tsx``) and a framework-behavior probe
(``backend/test-express-wildcard.js``), could not delete them (the blanket
IMPLEMENT delete ban), and the ``git add -A`` checkpoint shipped them into
the delivery commit a48d92e. The stage discipline now owns the delete
channel (see ``tests/test_agents/test_stage_discipline.py``); these tests
lock the mechanical backstop: ``run_implement_phase`` sweeps undeclared,
untracked test-shaped files under ``.arc/diagnostics/`` (gitignored) before
the caller commits the phase checkpoint, in both serial and worktree mode.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from core.phases import WorkflowPhaseRunner, collect_undeclared_test_files
from tests.helpers.faux import FakeAppHandler, failing_test_output, passing_test_output

# Reuse the process-wide runtime fixture so WorkflowPhaseRunner.traceability,
# core.sessions and context_pipeline all resolve inside tmp_project_dir.
from tests.test_agents.conftest import arc_runtime  # noqa: F401


UNIT_TEST_FILE = "backend/tests/unit/auth_service.test.js"


class _StubAdapter:
    """Minimal stage-adapter stand-in: no DESIGN/TDD work happens here."""

    def __init__(self) -> None:
        self.app_handler = None


class _CrashingTDD(_StubAdapter):
    """A TDD adapter whose session crashes (the phase must fail, not sweep)."""

    async def run(self, **kwargs: Any) -> str:
        raise RuntimeError("TDD session crashed")


def _manifest_item(file_path: str, test_type: str = "Unit", req_id: str = "REQ-SWEEP-1") -> dict[str, Any]:
    return {
        "test_id": f"T-{Path(file_path).stem}",
        "req_id": req_id,
        "interface_ids": ["IF-AUTH"],
        "coverage_scope": "owned",
        "type": test_type,
        "file_path": file_path,
        "first_line": "test('x', () => {});",
    }


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _init_git_workspace(project: Path) -> None:
    """A real git repo whose tracked tree mirrors a mid-compile workspace."""

    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        completed = _git(args, project)
        assert completed.returncode == 0, completed.stderr
    # The runtime's managed gitignore (the real production one), so the
    # sweep's git-untracked candidate set matches what a compile workspace
    # actually produces.
    from arcbench_agent_runtime.context import RuntimePaths
    from arcbench_agent_runtime.gitops import GitClient

    paths = RuntimePaths(
        project_dir=project,
        runner_events_path=project / ".arc" / "runner-events.jsonl",
        traceability_dir=project / ".arc" / "traceability",
    )
    GitClient(paths, events=None).ensure_arc_gitignore()
    # A registered manifest test that is already tracked (committed by the
    # DESIGN checkpoint) plus a product file.
    (project / "backend" / "tests" / "unit").mkdir(parents=True)
    (project / UNIT_TEST_FILE).write_text("test('auth', () => {});\n", encoding="utf-8")
    (project / "backend" / "src").mkdir(parents=True)
    (project / "backend" / "src" / "auth.js").write_text("module.exports = {};\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-q", "-m", "design checkpoint"]):
        completed = _git(args, project)
        assert completed.returncode == 0, completed.stderr


def _write_agent_diagnostics(project: Path) -> None:
    """The exact undeclared artifacts the arc-output1 agent left behind."""

    frontend_tests = project / "frontend" / "tests"
    frontend_tests.mkdir(parents=True, exist_ok=True)
    (frontend_tests / "diag.test.tsx").write_text("// render probe\n", encoding="utf-8")
    (frontend_tests / "RegisterPage.diag.test.tsx").write_text("// render probe\n", encoding="utf-8")
    (project / "backend").mkdir(exist_ok=True)
    (project / "backend" / "test-express-wildcard.js").write_text("// probe\n", encoding="utf-8")


def _make_runner(project: Path, scripted_results: list[str] | None = None) -> tuple[WorkflowPhaseRunner, list[tuple]]:
    logs: list[tuple] = []

    def log_cb(agent, message, status=None, node_id=None):
        logs.append((agent, message, status, node_id))

    requirements_dir = project / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    runner = WorkflowPhaseRunner(
        workspace_path=str(project),
        requirement_path=str(requirements_dir / "req.md"),
        app_type="web",
        interface_designer=_StubAdapter(),
        test_generator=_StubAdapter(),
        test_driven_developer=_StubAdapter(),
        log_cb=log_cb,
    )
    runner.app_handler = FakeAppHandler(scripted_results or [passing_test_output()])
    return runner, logs


def _seed_tests(runtime, node_id: str, file_paths: list[str]) -> None:
    runtime.traceability.store_requirement_tree(
        {"id": node_id, "name": "Auth", "description": "Registration and login"}
    )
    for file_path in file_paths:
        item = _manifest_item(file_path, req_id=node_id)
        runtime.traceability.upsert_test(
            test_id=item["test_id"],
            req_id=item["req_id"],
            interface_ids=item["interface_ids"],
            type=item["type"],
            file_path=item["file_path"],
            first_line=item["first_line"],
            passed=None,
        )


def _run_implement(runner: WorkflowPhaseRunner, node_id: str) -> bool:
    return asyncio.run(
        runner.run_implement_phase(node_id, {"name": "Auth", "description": "Registration and login"})
    )


def _commit_all(project: Path, message: str) -> None:
    _git(["add", "-A"], project)
    completed = _git(["commit", "-q", "-m", message], project)
    assert completed.returncode == 0, completed.stderr


def _tracked_files(project: Path) -> list[str]:
    completed = _git(["ls-files"], project)
    assert completed.returncode == 0, completed.stderr
    return sorted(line for line in completed.stdout.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# collect_undeclared_test_files: the pure predicate
# ---------------------------------------------------------------------------


def test_collect_undeclared_reports_untracked_test_files_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_git_workspace(project)
    _write_agent_diagnostics(project)
    # An undeclared *product* file must not match even though it is new.
    (project / "backend" / "src" / "newRoute.js").write_text("r;\n", encoding="utf-8")
    # A new test helper in an ignored directory must never match.
    (project / ".arc" / "tdd_runs" / "REQ-1").mkdir(parents=True)
    (project / ".arc" / "tdd_runs" / "REQ-1" / "unit-001.log").write_text("log\n", encoding="utf-8")

    found = collect_undeclared_test_files(
        str(project), declared_paths=[UNIT_TEST_FILE]
    )

    assert found == [
        "backend/test-express-wildcard.js",
        "frontend/tests/RegisterPage.diag.test.tsx",
        "frontend/tests/diag.test.tsx",
    ]


def test_collect_undeclared_ignores_tracked_and_declared_tests(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_git_workspace(project)
    # A pre-existing sibling test from an earlier node: tracked in git, not
    # in this node's manifest — must stay untouched.
    sibling = "backend/tests/unit/earlierNode.test.js"
    (project / sibling).write_text("test();\n", encoding="utf-8")
    _commit_all(project, "sibling node checkpoint")
    _write_agent_diagnostics(project)

    found = collect_undeclared_test_files(str(project), declared_paths=[UNIT_TEST_FILE])

    assert found == [
        "backend/test-express-wildcard.js",
        "frontend/tests/RegisterPage.diag.test.tsx",
        "frontend/tests/diag.test.tsx",
    ]
    # A modified (tracked, declared) manifest test never matches.
    (project / UNIT_TEST_FILE).write_text("test('auth v2', () => {});\n", encoding="utf-8")
    assert UNIT_TEST_FILE not in collect_undeclared_test_files(
        str(project), declared_paths=[UNIT_TEST_FILE]
    )


def test_collect_undeclared_outside_git_repo_returns_empty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_agent_diagnostics(project)

    assert collect_undeclared_test_files(str(project), declared_paths=[]) == []


# ---------------------------------------------------------------------------
# run_implement_phase: the sweep before the checkpoint (issue #89 regression)
# ---------------------------------------------------------------------------


def test_implement_checkpoint_excludes_undeclared_diagnostic_files(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-SWEEP-DELIVER"
    _init_git_workspace(tmp_project_dir)
    _seed_tests(arc_runtime, node_id, [UNIT_TEST_FILE])
    # One manifest file drives two system runs (per-file baseline, then the
    # tautology full-layer regression) before the agent session is skipped.
    runner, logs = _make_runner(
        tmp_project_dir, [passing_test_output(), passing_test_output()]
    )
    # The agent leaves diagnostics behind and passes its layer.
    _write_agent_diagnostics(tmp_project_dir)

    ok = _run_implement(runner, node_id)

    assert ok is True
    # The diagnostics moved into the gitignored .arc tree, not the commit.
    assert not (tmp_project_dir / "frontend" / "tests" / "diag.test.tsx").exists()
    assert not (tmp_project_dir / "frontend" / "tests" / "RegisterPage.diag.test.tsx").exists()
    assert not (tmp_project_dir / "backend" / "test-express-wildcard.js").exists()
    preserved = sorted(
        str(path.relative_to(tmp_project_dir)).replace("\\", "/")
        for path in (tmp_project_dir / ".arc" / "diagnostics" / node_id).rglob("*")
        if path.is_file()
    )
    assert preserved == [
        ".arc/diagnostics/REQ-SWEEP-DELIVER/backend/test-express-wildcard.js",
        ".arc/diagnostics/REQ-SWEEP-DELIVER/frontend/tests/RegisterPage.diag.test.tsx",
        ".arc/diagnostics/REQ-SWEEP-DELIVER/frontend/tests/diag.test.tsx",
    ]
    assert any("Moved 3 undeclared test file(s)" in entry[1] for entry in logs)
    # The sweep is recorded in the node session for auditability.
    from core import sessions

    assert sessions.load_node_session(node_id).get("swept_diagnostic_files") == [
        "backend/test-express-wildcard.js",
        "frontend/tests/RegisterPage.diag.test.tsx",
        "frontend/tests/diag.test.tsx",
    ]

    # The delivery commit (what git add -A sees afterwards) stays clean.
    _commit_all(tmp_project_dir, "implement checkpoint")
    tracked = _tracked_files(tmp_project_dir)
    assert UNIT_TEST_FILE in tracked
    assert "backend/src/auth.js" in tracked
    assert not any("diag" in path for path in tracked)
    assert not any("test-express-wildcard" in path for path in tracked)


def test_implement_sweep_runs_in_worktree_workspace(tmp_project_dir: Path, arc_runtime) -> None:
    """Parallel mode: the sweep must act on the task's worktree (the runner's
    ``workspace_path``), not the shared integration workspace."""

    node_id = "REQ-SWEEP-WORKTREE"
    _init_git_workspace(tmp_project_dir)
    _seed_tests(arc_runtime, node_id, [UNIT_TEST_FILE])

    # Simulate the task worktree: a second checkout of the same repo.
    worktree_dir = tmp_project_dir.parent / "task-worktree"
    completed = _git(["worktree", "add", "-q", str(worktree_dir)], tmp_project_dir)
    assert completed.returncode == 0, completed.stderr
    _write_agent_diagnostics(worktree_dir)

    runner, logs = _make_runner(
        worktree_dir, [passing_test_output(), passing_test_output()]
    )

    ok = _run_implement(runner, node_id)

    assert ok is True
    assert not (worktree_dir / "frontend" / "tests" / "diag.test.tsx").exists()
    assert (worktree_dir / ".arc" / "diagnostics" / node_id / "frontend" / "tests" / "diag.test.tsx").exists()
    assert any("Moved" in entry[1] for entry in logs)


def test_implement_failure_skips_the_sweep(tmp_project_dir: Path, arc_runtime) -> None:
    """A failed IMPLEMENT phase keeps every workspace file where the agent
    left it — the failed worktree is preserved for inspection/retry, and a
    sweep would hide the diagnostics that explain the failure."""

    node_id = "REQ-SWEEP-FAIL"
    _init_git_workspace(tmp_project_dir)
    _seed_tests(arc_runtime, node_id, [UNIT_TEST_FILE])
    # Baseline fails on an assertion (not environmental): the layer stays
    # red, an agent session opens to repair it, and the crashing TDD adapter
    # fails the phase (the workflow's crash path marks the node failed).
    runner, logs = _make_runner(tmp_project_dir, [failing_test_output()])
    runner.test_driven_developer = _CrashingTDD()
    _write_agent_diagnostics(tmp_project_dir)

    try:
        ok = _run_implement(runner, node_id)
    except RuntimeError:
        ok = False
    assert ok is False
    assert (tmp_project_dir / "frontend" / "tests" / "diag.test.tsx").exists()
    assert not any("Moved" in entry[1] for entry in logs)

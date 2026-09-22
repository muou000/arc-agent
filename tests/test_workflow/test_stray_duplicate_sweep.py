"""The IMPLEMENT wrap-up must sweep stray duplicate files out of the delivery tree.

Issue #159 (easy-ticketbooking arc-output-serial): the DESIGN stage wrote
``src/api/auth.ts`` at the workspace root by mistake and the correct
``frontend/src/api/auth.ts`` fourteen seconds later; the wrong copy was never
cleaned up, rode the DESIGN commit, and polluted every later checkpoint, merge
surface and file inventory. These tests lock the mechanical backstop: when an
IMPLEMENT phase succeeds, a file is deleted from the delivery tree only when
*BOTH* hold —

1. its content duplicates another **committed** file (sha256 over
   newline-normalized content, so a CRLF copy counts as the same text), and
2. its path lies outside every skeleton root declared in the active
   template's ``template.yaml`` ``agent_guidance``.

Anything else — a duplicate that lives inside the skeleton, a unique file
outside it, two identical files that are *both* outside it — stays untouched:
this sweep is deliberately narrow, and generic "unreferenced file" detection
is explicitly out of scope.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from core.phases import (
    WorkflowPhaseRunner,
    collect_stray_duplicate_files,
    load_template_skeleton_roots,
)
from tests.helpers.faux import FakeAppHandler, passing_test_output

# Reuse the process-wide runtime fixture so WorkflowPhaseRunner.traceability,
# core.sessions and context_pipeline all resolve inside tmp_project_dir.
from tests.test_agents.conftest import arc_runtime  # noqa: F401


TWIN_FILE = "frontend/src/api/auth.ts"
STRAY_FILE = "src/api/auth.ts"
UNIQUE_STRAY = "src/unique.ts"
UNIT_TEST_FILE = "backend/tests/unit/auth_service.test.js"
AUTH_CONTENT = "export class AuthService {\n  login() {}\n}\n"
#: The template the wiring tests point the (faked) handler at: roots mirror
#: the real web template, including a file-valued root.
SKELETON_ROOTS = ["frontend/src", "backend/src", "backend/test-e2e"]


class _StubAdapter:
    """Minimal stage-adapter stand-in: no DESIGN/TDD work happens here."""

    def __init__(self) -> None:
        self.app_handler = None


def _manifest_item(file_path: str, test_type: str = "Unit", req_id: str = "REQ-STRAY-1") -> dict[str, Any]:
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
    from arcbench_agent_runtime.context import RuntimePaths
    from arcbench_agent_runtime.gitops import GitClient

    paths = RuntimePaths(
        project_dir=project,
        runner_events_path=project / ".arc" / "runner-events.jsonl",
        traceability_dir=project / ".arc" / "traceability",
    )
    GitClient(paths, events=None).ensure_arc_gitignore()
    (project / "backend" / "tests" / "unit").mkdir(parents=True)
    (project / UNIT_TEST_FILE).write_text("test('auth', () => {});\n", encoding="utf-8")
    (project / "backend" / "src").mkdir(parents=True)
    (project / "backend" / "src" / "auth.js").write_text("module.exports = {};\n", encoding="utf-8")


def _commit_all(project: Path, message: str) -> None:
    _git(["add", "-A"], project)
    completed = _git(["commit", "-q", "-m", message], project)
    assert completed.returncode == 0, completed.stderr


def _tracked_files(project: Path) -> list[str]:
    completed = _git(["ls-files"], project)
    assert completed.returncode == 0, completed.stderr
    return sorted(line for line in completed.stdout.splitlines() if line.strip())


def _write_incident_files(project: Path) -> None:
    """The issue #159 shape: a committed in-skeleton twin plus a stray copy of
    it outside the skeleton (CRLF variant, to pin newline normalization), and
    a unique outside file the sweep must never touch. Written as bytes so the
    Windows text-mode newline translation cannot alter the fixtures."""

    twin = project / TWIN_FILE
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_bytes(AUTH_CONTENT.encode("utf-8"))
    stray = project / STRAY_FILE
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(AUTH_CONTENT.replace("\n", "\r\n").encode("utf-8"))
    unique = project / UNIQUE_STRAY
    unique.parent.mkdir(parents=True, exist_ok=True)
    unique.write_bytes(b"export const onlyHere = 1;\n")


def _make_runner(project: Path, template_dir: Path | None = None) -> tuple[WorkflowPhaseRunner, list[tuple]]:
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
    handler = FakeAppHandler([passing_test_output(), passing_test_output()])
    if template_dir is not None:
        handler.template_dir = lambda: str(template_dir)  # type: ignore[method-assign]
    runner.app_handler = handler
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


def _runner_events(project: Path) -> list[dict[str, Any]]:
    events_path = project / ".arc" / "runner-events.jsonl"
    if not events_path.exists():
        return []
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# collect_stray_duplicate_files: the pure predicate
# ---------------------------------------------------------------------------


def test_collect_stray_reports_skeleton_outside_duplicate_of_committed_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_git_workspace(project)
    _write_incident_files(project)
    _commit_all(project, "design checkpoint")

    found = collect_stray_duplicate_files(str(project), skeleton_roots=SKELETON_ROOTS)

    assert found == [{"path": STRAY_FILE, "twin": TWIN_FILE}]


def test_collect_stray_matches_untracked_stray_against_tracked_twin(tmp_path: Path) -> None:
    """The stray need not be committed yet: an untracked wrong-location copy
    duplicates committed content and would ride the next ``git add -A`` just
    the same."""

    project = tmp_path / "project"
    project.mkdir()
    _init_git_workspace(project)
    (project / TWIN_FILE).parent.mkdir(parents=True, exist_ok=True)
    (project / TWIN_FILE).write_text(AUTH_CONTENT, encoding="utf-8")
    _commit_all(project, "design checkpoint")
    (project / STRAY_FILE).parent.mkdir(parents=True, exist_ok=True)
    (project / STRAY_FILE).write_text(AUTH_CONTENT, encoding="utf-8")

    found = collect_stray_duplicate_files(str(project), skeleton_roots=SKELETON_ROOTS)

    assert found == [{"path": STRAY_FILE, "twin": TWIN_FILE}]


def test_collect_stray_spares_single_condition_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_git_workspace(project)
    _write_incident_files(project)
    # A duplicate that lives *inside* the skeleton is the delivery content
    # itself, not a stray.
    sibling = project / "frontend/src/api/authCopy.ts"
    sibling.write_bytes(AUTH_CONTENT.encode("utf-8"))
    # Two identical files that are *both* outside the skeleton: with no
    # committed in-skeleton twin, deleting either could lose live content.
    (project / "lib").mkdir()
    (project / "lib/mirror.ts").write_bytes(b"export const onlyHere = 1;\n")
    _commit_all(project, "design checkpoint")

    found = collect_stray_duplicate_files(str(project), skeleton_roots=SKELETON_ROOTS)

    assert [item["path"] for item in found] == [STRAY_FILE]


def test_collect_stray_ignores_untracked_twin_pairs(tmp_path: Path) -> None:
    """An untracked in-skeleton copy is not durable state: it never anchors a
    deletion (the sweep acts on committed content only)."""

    project = tmp_path / "project"
    project.mkdir()
    _init_git_workspace(project)
    _write_incident_files(project)
    _commit_all(project, "design checkpoint")
    # Both copies of a second text exist only as untracked files: an
    # in-skeleton one and an outside one. Not committed -> no anchor.
    twin = project / "frontend/src/api/session.ts"
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_bytes(b"export class Session {}\n")
    (project / "src/api/session.ts").write_bytes(b"export class Session {}\n")

    found = collect_stray_duplicate_files(str(project), skeleton_roots=SKELETON_ROOTS)

    assert found == [{"path": STRAY_FILE, "twin": TWIN_FILE}]


def test_collect_stray_without_git_repo_returns_empty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_incident_files(project)

    assert collect_stray_duplicate_files(str(project), skeleton_roots=SKELETON_ROOTS) == []


def test_collect_stray_without_skeleton_roots_returns_empty(tmp_path: Path) -> None:
    """No known skeleton means no whitelist to be outside of: fail open to a
    no-op rather than guess."""

    project = tmp_path / "project"
    project.mkdir()
    _init_git_workspace(project)
    _write_incident_files(project)
    _commit_all(project, "design checkpoint")

    assert collect_stray_duplicate_files(str(project), skeleton_roots=[]) == []


# ---------------------------------------------------------------------------
# load_template_skeleton_roots: the template.yaml reader
# ---------------------------------------------------------------------------


def _write_template_dir(tmp_path: Path, guidance: dict[str, str] | None) -> Path:
    template_dir = tmp_path / "templates" / "web-react-express"
    template_dir.mkdir(parents=True)
    manifest: dict[str, Any] = {"schema_version": "1.0", "id": "web-react-express"}
    if guidance is not None:
        manifest["agent_guidance"] = guidance
    (template_dir / "template.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )
    return template_dir


def test_load_skeleton_roots_reads_agent_guidance(tmp_path: Path) -> None:
    template_dir = _write_template_dir(
        tmp_path,
        {
            "ui_root": "frontend/src",
            "api_root": "backend/src/app.js",
            "function_root": "./backend/src/",
        },
    )

    assert load_template_skeleton_roots(str(template_dir)) == [
        "backend/src",
        "backend/src/app.js",
        "frontend/src",
    ]


def test_load_skeleton_roots_fail_open(tmp_path: Path) -> None:
    missing = tmp_path / "templates" / "nowhere"
    assert load_template_skeleton_roots(str(missing)) == []
    empty_guidance = _write_template_dir(tmp_path, None)
    assert load_template_skeleton_roots(str(empty_guidance)) == []


# ---------------------------------------------------------------------------
# run_implement_phase: the wrap-up sweep (issue #159 regression)
# ---------------------------------------------------------------------------


def test_implement_sweep_deletes_stray_duplicate_and_records_event(
    tmp_project_dir: Path, arc_runtime
) -> None:
    node_id = "REQ-STRAY-DELIVER"
    _init_git_workspace(tmp_project_dir)
    _seed_tests(arc_runtime, node_id, [UNIT_TEST_FILE])
    _write_incident_files(tmp_project_dir)
    _commit_all(tmp_project_dir, "design checkpoint")
    template_dir = _write_template_dir(
        tmp_project_dir.parent, {root: root for root in SKELETON_ROOTS}
    )
    runner, logs = _make_runner(tmp_project_dir, template_dir=template_dir)

    ok = _run_implement(runner, node_id)

    assert ok is True
    # The stray is gone from the delivery tree; the twin and the unique file
    # stay exactly where they were.
    assert not (tmp_project_dir / STRAY_FILE).exists()
    assert (tmp_project_dir / TWIN_FILE).read_text(encoding="utf-8") == AUTH_CONTENT
    assert (tmp_project_dir / UNIQUE_STRAY).exists()
    assert any(STRAY_FILE in entry[1] for entry in logs)
    # The sweep is recorded in the node session for auditability.
    from core import sessions

    assert sessions.load_node_session(node_id).get("swept_stray_files") == [STRAY_FILE]
    # A typed runner event carries the deletion.
    stray_events = [
        event for event in _runner_events(tmp_project_dir) if event.get("type") == "stray_sweep"
    ]
    assert len(stray_events) == 1
    assert stray_events[0]["node_id"] == node_id
    assert stray_events[0]["files"] == [STRAY_FILE]
    assert TWIN_FILE in stray_events[0]["message"]
    # The next checkpoint delivers the deletion instead of the stray.
    _commit_all(tmp_project_dir, "implement checkpoint")
    tracked = _tracked_files(tmp_project_dir)
    assert TWIN_FILE in tracked
    assert not any(path == STRAY_FILE for path in tracked)


def test_implement_sweep_without_template_roots_is_a_noop(
    tmp_project_dir: Path, arc_runtime
) -> None:
    """A handler that cannot produce skeleton roots (unknown template, faked
    handler) disables the sweep: fail open to keeping every file."""

    node_id = "REQ-STRAY-NOOP"
    _init_git_workspace(tmp_project_dir)
    _seed_tests(arc_runtime, node_id, [UNIT_TEST_FILE])
    _write_incident_files(tmp_project_dir)
    _commit_all(tmp_project_dir, "design checkpoint")
    runner, logs = _make_runner(tmp_project_dir, template_dir=None)

    ok = _run_implement(runner, node_id)

    assert ok is True
    assert (tmp_project_dir / STRAY_FILE).exists()
    assert (tmp_project_dir / TWIN_FILE).exists()
    assert not any("stray" in entry[1].lower() for entry in logs)
    assert not [event for event in _runner_events(tmp_project_dir) if event.get("type") == "stray_sweep"]
